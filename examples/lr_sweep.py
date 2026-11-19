"""Learning rate sweep on the UCI handwritten digits dataset.

Uses sklearn's digits dataset: 1797 samples of 8x8 scanned digit images
(flattened to 64 features), 10 classes. An MLP needs several epochs to converge
and different LRs produce visibly different trajectories.

Three runs: too high (unstable early, higher final loss), baseline (clean
convergence), too low (still descending at the last epoch).

Usage:
    pyrometer serve               # terminal 1
    python examples/lr_sweep.py  # terminal 2
"""

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
EPOCHS = 40
BATCH_SIZE = 32

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

    model = build_model().to(device)
    run.watch(model, log_freq=20)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss()

    steps_per_epoch = len(X_train) // BATCH_SIZE
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=EPOCHS * steps_per_epoch
    )

    global_step = 0
    n_train = len(X_train)
    best_val_acc = 0.0

    for epoch in range(EPOCHS):
        model.train()
        perm = torch.randperm(n_train, device=device)
        X_shuf, y_shuf = X_train[perm], y_train[perm]

        ep_loss, ep_correct, n_batches = 0.0, 0, 0

        for i in range(0, n_train - BATCH_SIZE + 1, BATCH_SIZE):
            xb, yb = X_shuf[i : i + BATCH_SIZE], y_shuf[i : i + BATCH_SIZE]

            # Profile the first batch of each epoch
            ctx = run.profile(step=global_step) if i == 0 else nullcontext()
            with ctx:
                optimizer.zero_grad()
                logits = model(xb)
                loss = criterion(logits, yb)
                loss.backward()

                grad_norm = (
                    sum(p.grad.norm().item() ** 2 for p in model.parameters() if p.grad is not None) ** 0.5
                )

                optimizer.step()
                scheduler.step()

            run.log(
                {
                    "train/loss": loss.item(),
                    "train/accuracy": (logits.argmax(1) == yb).float().mean().item(),
                    "train/grad_norm": grad_norm,
                    "lr": scheduler.get_last_lr()[0],
                },
                step=global_step,
            )

            ep_loss += loss.item()
            ep_correct += (logits.argmax(1) == yb).sum().item()
            n_batches += 1
            global_step += 1

        model.eval()
        with torch.no_grad():
            val_logits = model(X_val)
            val_loss = criterion(val_logits, y_val).item()
            val_acc = (val_logits.argmax(1) == y_val).float().mean().item()

        run.log({"val/loss": val_loss, "val/accuracy": val_acc}, step=global_step)

        # Log class-confidence heatmap: rows=classes, cols=16 val samples
        conf_img = torch.softmax(val_logits[:16], dim=1).T.unsqueeze(0).cpu()
        run.log_image("class_confidence", conf_img, step=global_step)

        print(
            f"  epoch {epoch + 1:2d}/{EPOCHS} — "
            f"train_loss={ep_loss/n_batches:.4f}  train_acc={ep_correct/(n_batches*BATCH_SIZE):.3f}  "
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
    print(f"Running LR sweep: {len(LR_CONFIGS)} runs × {EPOCHS} epochs × {steps_per_epoch} steps/epoch\n")

    for config in LR_CONFIGS:
        lr = config["lr"]
        run = pyrometer.init(
            name=config["name"],
            hyperparams={
                "optimizer": "AdamW",
                "lr": lr,
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
            notes=f"LR sweep arm: lr={lr}. Same AdamW+cosine setup, same train/val split.",
        )
        print(f"[{config['name']}] run_id={run.run_id}")

        train_one_run(run, lr, X_train, y_train, X_val, y_val)

        run.finish()
        print(f"[{config['name']}] done\n")

    print("LR sweep complete. Open the dashboard and select all three runs to compare.")


if __name__ == "__main__":
    main()
