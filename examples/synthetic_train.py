"""Synthetic training loop — demonstrates the pyrometer SDK end-to-end.

Uses either real PyTorch (random tensors, no torchvision needed) or a pure-Python
simulation if torch isn't installed.

The run is opened with ``with pyrometer.init(...) as run:`` and the ``llm-pretrain`` preset, so the step counter,
loss, learning rate, gradient norm, tokens/sec and the per-parameter gradient health are logged by autolog without
a single ``run.log`` call for them. Only what autolog cannot know is logged by hand: accuracy, validation
metrics, an image, generation samples, a checkpoint artifact and events.

Usage:
    # Start the server first
    pyrometer serve

    # In another terminal
    python examples/synthetic_train.py

    # The same script with zero SDK calls in the loop, through the launcher (init() below attaches to its run)
    pyrometer run --preset llm-pretrain examples/synthetic_train.py

    # Continue an earlier run (same run, steps carry on after its last logged step)
    python examples/synthetic_train.py --resume <run_id>
"""

import argparse
import math
import os
import random

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
import time

try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    print("PyTorch not found — running in simulation mode")

import pyrometer

SEED = 0
TOKENS_PER_STEP = 64 * 128  # batch 64 x sequence length 128; autolog logs the "tokens_seen" x-axis metric
SPIKE_STEP = 300  # a deliberate loss spike so the dashboard shows a real anomaly event


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", metavar="RUN_ID", help="continue this existing run instead of starting a new one")
    args = parser.parse_args()
    with pyrometer.init(
        name="synthetic-mlp-baseline",
        hyperparams={
            "lr": 3e-4,
            "batch_size": 64,
            "hidden_dim": 256,
            "epochs": 5,
            "seed": SEED,
        },
        tags=["synthetic", "mlp", "baseline"],
        group="synthetic-baselines",  # groups related runs; filter by it in the dashboard
        resume=args.resume,
        preset="llm-pretrain",  # grad_norm on, tokens/sec, profile every 500 steps
        # Short run: log every step (the anomaly detector needs ~50 samples), profile every 100 steps,
        # and watch per-parameter gradient/weight statistics every 20 backward passes.
        autolog={"log_every": 1, "profile_every": 100, "grad_health": True, "grad_log_freq": 20},
    ) as run:
        print(f"Run ID: {run.run_id}" + (f" (resumed after step {run.last_step})" if run.resumed else ""))
        if HAS_TORCH:
            _train_torch(run)
        else:
            _train_simulated(run)
    print(f"Done. Check: {run.server}/#/runs/{run.run_id}")


def _train_torch(run: pyrometer.Run) -> None:
    import tempfile

    import torch
    import torch.nn as nn

    torch.manual_seed(SEED)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  device: {device}")

    model = nn.Sequential(
        nn.Linear(784, 256),
        nn.ReLU(),
        nn.Linear(256, 128),
        nn.ReLU(),
        nn.Linear(128, 10),
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=3e-4)
    criterion = nn.CrossEntropyLoss()

    # Hints autolog cannot guess: tokens per batch (-> tokens_seen axis, tokens/sec) and the model to watch.
    run.track(tokens_per_batch=TOKENS_PER_STEP, samples_per_batch=64, model=model)

    best_val_loss = float("inf")
    for epoch in range(5):
        for _ in range(100):
            x = torch.randn(64, 784, device=device)
            y = torch.randint(0, 10, (64,), device=device)

            optimizer.zero_grad()
            logits = model(x)
            loss = criterion(logits, y)
            if run.step == SPIKE_STEP - 1:  # the step about to complete is SPIKE_STEP
                loss = loss * 50  # autolog records this loss; its anomaly detector flags the spike
            loss.backward()  # <- autolog captures the loss here
            optimizer.step()  # <- ... and counts the step, reads the lr, the gradient norm and the throughput

            # Only what autolog cannot know: accuracy (step-less log: lands on the optimizer-step counter).
            run.log({"train/accuracy": (logits.argmax(1) == y).float().mean().item()})

        with torch.no_grad():
            x_val = torch.randn(256, 784, device=device)
            y_val = torch.randint(0, 10, (256,), device=device)
            val_logits = model(x_val)
            val_loss = criterion(val_logits, y_val).item()
            val_acc = (val_logits.argmax(1) == y_val).float().mean().item()

        run.log({"val/loss": val_loss, "val/accuracy": val_acc, "eval/accuracy": val_acc})  # eval/* scores feed the Leaderboard page
        print(f"Epoch {epoch + 1}/5 — step={run.step}  val_loss={val_loss:.4f}")

        # Log a synthetic prediction confidence image (H=10 class probs, W=8 samples)
        with torch.no_grad():
            sample_x = torch.randn(8, 784, device=device)
            probs = torch.softmax(model(sample_x), dim=1).cpu()  # (8, 10)
        # Reshape to (1, 10, 8) so it's a 1-channel "image" of class probabilities
        conf_img = probs.T.unsqueeze(0)  # (1, 10, 8)
        run.log_image("class_confidence", conf_img)

        # Synthetic prompt/completion/reward samples (stand-in for LLM generations), low cadence
        run.log_samples([
            {"prompt": f"What is {a} + {b}?", "completion": f"The answer is {a + b + (i % 2)}.",
             "reward": round(float(max(0.0, 1.0 - val_loss / 3 - 0.3 * (i % 2))), 3), "reference": str(a + b)}
            for i, (a, b) in enumerate([(2, 3), (7, 5), (12, 9), (4, 4)])
        ], name="eval_generations")

        # Track best checkpoint and log an event when val loss improves
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            ckpt_path = os.path.join(tempfile.gettempdir(), f"synthetic_mlp_epoch{epoch + 1}.pt")
            torch.save(model.state_dict(), ckpt_path)
            run.log_artifact(
                ckpt_path,
                artifact_type="checkpoint",
                metadata={"epoch": epoch + 1, "val_loss": round(val_loss, 4)},
            )
            run.event(
                f"best val_loss improved to {val_loss:.4f}",
                event_type="checkpoint_saved",
                metadata={"epoch": epoch + 1, "val_loss": round(val_loss, 4)},
            )


def _train_simulated(run: pyrometer.Run) -> None:
    """Pure-Python fallback generating a plausible loss curve (no torch: no autolog, so steps are explicit)."""
    global_step = run.last_step + 1 if run.resumed and run.last_step is not None else 0
    base_loss = 2.302  # ln(10) — random baseline for 10 classes

    for epoch in range(5):
        for _ in range(100):
            decay = math.exp(-global_step / 300)
            loss = base_loss * decay + 0.35 + random.gauss(0, 0.04)
            if global_step == SPIKE_STEP:
                loss *= 50
            acc = min(1.0, max(0.0, 1 - loss / base_loss * 0.9 + random.gauss(0, 0.02)))
            run.log({"train/loss": loss, "train/accuracy": acc, "lr": 3e-4,
                     "tokens_seen": (global_step + 1) * TOKENS_PER_STEP}, step=global_step)
            global_step += 1
            time.sleep(0.01)

        val_loss = base_loss * math.exp(-global_step / 280) + 0.45 + random.gauss(0, 0.03)
        val_acc = min(1.0, max(0.0, 1 - val_loss / base_loss * 0.9))
        run.log({"val/loss": val_loss, "val/accuracy": val_acc, "eval/accuracy": val_acc,
                 "tokens_seen": global_step * TOKENS_PER_STEP}, step=global_step)
        print(f"Epoch {epoch + 1}/5 — val_loss={val_loss:.4f}")


if __name__ == "__main__":
    main()
