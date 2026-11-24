"""Learning rate sweep on the UCI handwritten digits dataset.

Uses sklearn's digits dataset: 1797 samples of 8x8 scanned digit images
(flattened to 64 features), 10 classes. An MLP needs several epochs to converge
and different LRs produce visibly different trajectories.

Each run is `with pyrometer.init(...) as run:`: autolog records the step, loss, learning rate (the cosine
schedule shows up as it is), gradient norm, samples/sec and per-parameter gradient health. The loop only logs what
autolog cannot know (accuracy, validation, an image, samples, events) and profiles the first batch of each epoch.

Three LR arms x three seeds (nine runs, EPOCHS shrunk to keep runtime similar). In the compare view,
use "Group by hyperparams" (ignoring `seed`) to get one mean +/- std band per LR. Arms: too high (unstable early, higher final loss), baseline (clean
convergence), too low (still descending at the last epoch).

Usage:
    pyrometer serve               # terminal 1
    python examples/lr_sweep.py  # terminal 2
"""

import itertools
import os
import sys
from contextlib import nullcontext

import numpy as np

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

try:
    import torch
    import torch.nn as nn
except ImportError:
    print("PyTorch not found — install torch to run this example")
    sys.exit(1)

try:
    from sklearn.datasets import load_digits
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
except ImportError:
    print("scikit-learn not found — pip install scikit-learn")
    sys.exit(1)

import pyrometer

# ── Dataset ──────────────────────────────────────────────────────────────────
N_FEATURES = 64
N_CLASSES = 10

# ── Training ──────────────────────────────────────────────────────────────────
EPOCHS = 14
SEEDS = [0, 1, 2]
BATCH_SIZE = 32
LOG_EVERY = 4  # divides the 44 steps per epoch, so epoch-end validation lands on a logged step

LR_CONFIGS = [
    {"lr": 1e-2, "name": "lr-sweep-1e-2", "tags": ["sweep", "lr-sweep", "high-lr"]},
    {"lr": 5e-4, "name": "lr-sweep-5e-4", "tags": ["sweep", "lr-sweep", "baseline"]},
    {"lr": 1e-5, "name": "lr-sweep-1e-5", "tags": ["sweep", "lr-sweep", "low-lr"]},
]


def load_data() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    digits = load_digits()
    X, y = digits.data.astype(np.float32), digits.target.astype(np.int64)
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train).astype(np.float32)
    X_val = scaler.transform(X_val).astype(np.float32)
    return X_train, y_train, X_val, y_val


def build_model() -> nn.Module:
    return nn.Sequential(
        nn.Linear(N_FEATURES, 256),
        nn.ReLU(),
        nn.Dropout(0.2),
        nn.Linear(256, 128),
        nn.ReLU(),
        nn.Linear(128, N_CLASSES),
    )


def train_one_run(
    run: pyrometer.Run,
    lr: float,
    seed: int,
    X_train_np: np.ndarray,
    y_train_np: np.ndarray,
    X_val_np: np.ndarray,
    y_val_np: np.ndarray,
) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  device: {device}")

    X_train = torch.from_numpy(X_train_np).to(device)
    y_train = torch.from_numpy(y_train_np).to(device)
    X_val = torch.from_numpy(X_val_np).to(device)
    y_val = torch.from_numpy(y_val_np).to(device)

    torch.manual_seed(seed)
    model = build_model().to(device)
    # samples_seen (a chart x-axis) and samples/sec come from this hint; the model is the gradient-health target.
    run.track(samples_per_batch=BATCH_SIZE, model=model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss()

    steps_per_epoch = len(X_train) // BATCH_SIZE
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=EPOCHS * steps_per_epoch
    )

    n_train = len(X_train)
    best_val_acc = 0.0

    for epoch in range(EPOCHS):
        model.train()
        perm = torch.randperm(n_train, device=device)
        X_shuf, y_shuf = X_train[perm], y_train[perm]

        ep_loss = torch.zeros((), device=device)  # accumulate on the device: no per-step sync
        ep_correct = torch.zeros((), device=device)
        n_batches = 0

        for i in range(0, n_train - BATCH_SIZE + 1, BATCH_SIZE):
            xb, yb = X_shuf[i : i + BATCH_SIZE], y_shuf[i : i + BATCH_SIZE]

            # Profile the first batch of each epoch
            ctx = run.profile() if i == 0 else nullcontext()
            with ctx:
                optimizer.zero_grad()
                logits = model(xb)
                loss = criterion(logits, yb)
                loss.backward()  # autolog captures the loss ...
                optimizer.step()  # ... counts the step and logs lr, grad norm and throughput
                scheduler.step()

            batch_correct = (logits.argmax(1) == yb).sum()
            ep_loss += loss.detach()
            ep_correct += batch_correct
            n_batches += 1
            if run.step % LOG_EVERY == 0:  # a custom metric, on the same step as the auto metrics
                run.log({"train/accuracy": batch_correct.item() / BATCH_SIZE})

        model.eval()
        with torch.no_grad():
            val_logits = model(X_val)
            val_loss = criterion(val_logits, y_val).item()
            val_acc = (val_logits.argmax(1) == y_val).float().mean().item()

        run.log({"val/loss": val_loss, "val/accuracy": val_acc, "eval/accuracy": val_acc})  # step-less; eval/* feeds the Leaderboard

        # Log class-confidence heatmap: rows=classes, cols=16 val samples
        conf_img = torch.softmax(val_logits[:16], dim=1).T.unsqueeze(0).cpu()
        run.log_image("class_confidence", conf_img)

        # Synthetic prompt/completion/reward samples (stand-in for LLM generations), low cadence
        if (epoch + 1) % 5 == 0:
            run.log_samples([
                {"prompt": f"What is {a} + {b}?", "completion": f"The answer is {a + b + (i % 2)}.",
                 "reward": round(float(max(0.0, 1.0 - val_loss / 3 - 0.3 * (i % 2))), 3), "reference": str(a + b)}
                for i, (a, b) in enumerate([(2, 3), (7, 5), (12, 9), (4, 4)])
            ], name="eval_generations")

        print(
            f"  epoch {epoch + 1:2d}/{EPOCHS} — "
            f"train_loss={ep_loss.item()/n_batches:.4f}  train_acc={ep_correct.item()/(n_batches*BATCH_SIZE):.3f}  "
            f"val_loss={val_loss:.4f}  val_acc={val_acc:.3f}  lr={scheduler.get_last_lr()[0]:.2e}"
        )

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            run.event(
                f"best val_acc improved to {val_acc:.3f}",
                event_type="custom",
                metadata={"epoch": epoch + 1, "val_acc": round(val_acc, 4), "lr": lr},
            )


def main() -> None:
    print("Loading UCI digits dataset (sklearn)...")
    X_train, y_train, X_val, y_val = load_data()
    print(f"  {len(X_train)} train / {len(X_val)} val — 64 features, 10 classes\n")

    steps_per_epoch = len(X_train) // BATCH_SIZE
    print(
        f"Running LR sweep: {len(LR_CONFIGS)} LRs × {len(SEEDS)} seeds × {EPOCHS} epochs × "
        f"{steps_per_epoch} steps/epoch\n"
    )

    for config, seed in itertools.product(LR_CONFIGS, SEEDS):
        lr = config["lr"]
        run_name = f"{config['name']}-s{seed}"
        with pyrometer.init(
            name=run_name,
            hyperparams={
                "optimizer": "AdamW",
                "lr": lr,
                "seed": seed,
                "weight_decay": 0.01,
                "batch_size": BATCH_SIZE,
                "hidden_dims": [256, 128],
                "epochs": EPOCHS,
                "architecture": "mlp-2layer",
                "dataset": "sklearn-digits",
                "n_train": len(X_train),
                "n_val": len(X_val),
                "lr_schedule": "cosine",
            },
            tags=config["tags"],
            group="lr-sweep",  # one group per sweep: filter by it in the dashboard
            notes=f"LR sweep arm: lr={lr}, seed={seed}. Same AdamW+cosine setup, same train/val split.",
            autolog={"log_every": LOG_EVERY, "grad_norm": True, "grad_health": True, "grad_log_freq": 20},
        ) as run:
            print(f"[{run_name}] run_id={run.run_id}")
            train_one_run(run, lr, seed, X_train, y_train, X_val, y_val)
        print(f"[{run_name}] done\n")

    print("LR sweep complete. Open the dashboard and select all runs and group by hyperparams (ignore seed).")


if __name__ == "__main__":
    main()
