"""Synthetic training loop — demonstrates the pyrometer SDK end-to-end.

Uses either real PyTorch (random tensors, no torchvision needed) or a pure-Python
simulation if torch isn't installed.

Usage:
    # Start the server first
    pyrometer serve

    # In another terminal
    python examples/synthetic_train.py
"""

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


def main() -> None:
    run = pyrometer.init(
        name="synthetic-mlp-baseline",
        hyperparams={
            "lr": 3e-4,
            "batch_size": 64,
            "hidden_dim": 256,
            "epochs": 5,
        },
        tags=["synthetic", "mlp", "baseline"],
    )
    print(f"Run ID: {run.run_id}")

    if HAS_TORCH:
        _train_torch(run)
    else:
        _train_simulated(run)

    run.finish()
    print("Done. Check: http://localhost:8321/runs")


def _train_torch(run: pyrometer.Run) -> None:
    import tempfile

    import torch
    import torch.nn as nn

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

    # Monitor gradient and weight statistics every 20 backward passes
    run.watch(model, log_freq=20)

    best_val_loss = float("inf")
    global_step = 0
    for epoch in range(5):
        for step_in_epoch in range(100):
            x = torch.randn(64, 784, device=device)
            y = torch.randint(0, 10, (64,), device=device)

            # Profile one step per epoch so the profiler view has data to show.
            if step_in_epoch == 0:
                with run.profile(step=global_step):
                    optimizer.zero_grad()
                    logits = model(x)
                    loss = criterion(logits, y)
                    loss.backward()
                    optimizer.step()
            else:
                optimizer.zero_grad()
                logits = model(x)
                loss = criterion(logits, y)
                loss.backward()
                optimizer.step()

            acc = (logits.argmax(1) == y).float().mean().item()
            grad_norm = (
                sum(p.grad.norm().item() ** 2 for p in model.parameters() if p.grad is not None) ** 0.5
            )
            run.log({"train/loss": loss.item(), "train/accuracy": acc, "train/grad_norm": grad_norm, "lr": 3e-4}, step=global_step)
            global_step += 1

        with torch.no_grad():
            x_val = torch.randn(256, 784, device=device)
            y_val = torch.randint(0, 10, (256,), device=device)
            val_logits = model(x_val)
            val_loss = criterion(val_logits, y_val).item()
            val_acc = (val_logits.argmax(1) == y_val).float().mean().item()

        run.log({"val/loss": val_loss, "val/accuracy": val_acc}, step=global_step)
        print(f"Epoch {epoch + 1}/5 — train_loss={loss.item():.4f}  val_loss={val_loss:.4f}")

        # Log a synthetic prediction confidence image (H=10 class probs, W=8 samples)
        with torch.no_grad():
            sample_x = torch.randn(8, 784, device=device)
            probs = torch.softmax(model(sample_x), dim=1).cpu()  # (8, 10)
        # Reshape to (1, 10, 8) so it's a 1-channel "image" of class probabilities
        conf_img = probs.T.unsqueeze(0)  # (1, 10, 8)
        run.log_image("class_confidence", conf_img, step=global_step)

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
    """Pure-Python fallback generating a plausible loss curve."""
    global_step = 0
    base_loss = 2.302  # ln(10) — random baseline for 10 classes

    for epoch in range(5):
        for _ in range(100):
            decay = math.exp(-global_step / 300)
            loss = base_loss * decay + 0.35 + random.gauss(0, 0.04)
            acc = min(1.0, max(0.0, 1 - loss / base_loss * 0.9 + random.gauss(0, 0.02)))
            run.log({"train/loss": loss, "train/accuracy": acc, "lr": 3e-4}, step=global_step)
            global_step += 1
            time.sleep(0.01)

        val_loss = base_loss * math.exp(-global_step / 280) + 0.45 + random.gauss(0, 0.03)
        val_acc = min(1.0, max(0.0, 1 - val_loss / base_loss * 0.9))
        run.log({"val/loss": val_loss, "val/accuracy": val_acc}, step=global_step)
        print(f"Epoch {epoch + 1}/5 — val_loss={val_loss:.4f}")


if __name__ == "__main__":
    main()
