"""Architecture and regularization ablation on the UCI Adult (census income) dataset.

Dataset: UCI Adult / Census Income (fetch_openml 'adult')
Task: Binary classification — predict whether annual income exceeds $50K
Size: ~48,000 samples, 14 features (mix of continuous and one-hot categorical)
Source: Kohavi & Becker, 1996 — real US Census Bureau data

Five runs ablate different axes from a shared baseline so the hyperparam diff
table highlights many columns at once. Each run changes 2-4 hyperparameters
relative to baseline to produce genuinely different loss/accuracy trajectories:

  baseline      — AdamW, cosine schedule, moderate regularization, 2-layer MLP
  no-reg        — same optimizer but zero dropout and weight_decay → overfits
  deep-model    — 5-layer MLP, more capacity, stronger dropout to compensate
  sgd-step      — SGD+momentum with step-decay LR, needs different LR range
  large-batch   — 8× larger batch with linearly scaled LR, fewer but noisier steps

Usage:
    pyrometer serve                    # terminal 1
    python examples/ablation_study.py  # terminal 2
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
    from sklearn.datasets import fetch_openml
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    import pandas as pd
except ImportError:
    print("scikit-learn / pandas not found — pip install scikit-learn pandas")
    sys.exit(1)

import pyrometer

# ── Dataset ───────────────────────────────────────────────────────────────────
DATASET_NAME = "adult"
N_CLASSES = 2  # binary: <=50K / >50K

# ── Run configs ───────────────────────────────────────────────────────────────
EPOCHS = 20

CONFIGS = [
    {
        "name": "ablation-baseline",
        "tags": ["ablation", "baseline"],
        "notes": "Baseline: AdamW with cosine LR, moderate regularization, 2-layer MLP.",
        "hp": {
            "optimizer": "AdamW",
            "lr": 3e-4,
            "weight_decay": 1e-3,
            "lr_schedule": "cosine",
            "dropout": 0.3,
            "hidden_dims": [256, 128],
            "batch_size": 256,
            "epochs": EPOCHS,
            "architecture": "mlp-2layer",
            "dataset": "adult-census",
        },
    },
    {
        "name": "ablation-no-reg",
        "tags": ["ablation", "no-regularization"],
        "notes": "Ablate regularization: zero dropout and weight_decay. Expect overfitting.",
        "hp": {
            "optimizer": "AdamW",
            "lr": 3e-4,
            "weight_decay": 0.0,
            "lr_schedule": "cosine",
            "dropout": 0.0,
            "hidden_dims": [256, 128],
            "batch_size": 256,
            "epochs": EPOCHS,
            "architecture": "mlp-2layer",
            "dataset": "adult-census",
        },
    },
    {
        "name": "ablation-deep-model",
        "tags": ["ablation", "deep-architecture"],
        "notes": "Deeper 5-layer MLP with stronger dropout to handle extra capacity.",
        "hp": {
            "optimizer": "AdamW",
            "lr": 2e-4,
            "weight_decay": 1e-3,
            "lr_schedule": "cosine",
            "dropout": 0.4,
            "hidden_dims": [512, 256, 128, 64, 32],
            "batch_size": 256,
            "epochs": EPOCHS,
            "architecture": "mlp-5layer",
            "dataset": "adult-census",
        },
    },
    {
        "name": "ablation-sgd-step",
        "tags": ["ablation", "sgd", "step-schedule"],
        "notes": "SGD+Nesterov with step-decay LR instead of AdamW+cosine.",
        "hp": {
            "optimizer": "SGD",
            "lr": 1e-1,
            "weight_decay": 1e-4,
            "lr_schedule": "step",
            "dropout": 0.3,
            "hidden_dims": [256, 128],
            "batch_size": 256,
            "epochs": EPOCHS,
            "architecture": "mlp-2layer",
            "dataset": "adult-census",
        },
    },
    {
        "name": "ablation-large-batch",
        "tags": ["ablation", "large-batch"],
        "notes": "8× larger batch with linearly scaled LR per the linear scaling rule.",
        "hp": {
            "optimizer": "AdamW",
            "lr": 2.4e-3,  # 3e-4 × 8
            "weight_decay": 1e-3,
            "lr_schedule": "cosine",
            "dropout": 0.3,
            "hidden_dims": [256, 128],
            "batch_size": 2048,
            "epochs": EPOCHS,
            "architecture": "mlp-2layer",
            "dataset": "adult-census",
        },
    },
]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Download and preprocess the UCI Adult dataset.

    Continuous features are standardized. Categorical features are one-hot
    encoded. Target is binarized: >50K → 1, <=50K → 0.
    """
    print("  Fetching UCI Adult dataset from OpenML (cached after first run)...")
    data = fetch_openml(DATASET_NAME, version=2, as_frame=True, parser="auto")
    df: pd.DataFrame = data.frame

    # Target
    target_col = data.target.name
    y = (data.target.str.strip().str.rstrip(".") == ">50K").astype(np.int64).values

    # Separate continuous vs. categorical columns
    feature_cols = [c for c in df.columns if c != target_col]
    continuous = ["age", "fnlwgt", "education-num", "capital-gain", "capital-loss", "hours-per-week"]
    categorical = [c for c in feature_cols if c not in continuous]

    cont_df = df[continuous].apply(pd.to_numeric, errors="coerce").fillna(0).astype(np.float32)
    cat_df = pd.get_dummies(df[categorical], drop_first=False).astype(np.float32)

    X = np.hstack([cont_df.values, cat_df.values])

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.15, random_state=42, stratify=y
    )

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train).astype(np.float32)
    X_val = scaler.transform(X_val).astype(np.float32)

    return X_train, y_train.astype(np.int64), X_val, y_val.astype(np.int64)


# ── Model ─────────────────────────────────────────────────────────────────────

def build_model(n_features: int, hidden_dims: list[int], dropout: float) -> nn.Module:
    layers: list[nn.Module] = []
    in_dim = n_features
    for h in hidden_dims:
        layers += [nn.Linear(in_dim, h), nn.ReLU()]
        if dropout > 0:
            layers.append(nn.Dropout(dropout))
        in_dim = h
    layers.append(nn.Linear(in_dim, N_CLASSES))
    return nn.Sequential(*layers)


# ── Optimizer / scheduler factory ─────────────────────────────────────────────

def build_optimizer(model: nn.Module, hp: dict) -> torch.optim.Optimizer:
    name = hp["optimizer"]
    lr, wd = hp["lr"], hp["weight_decay"]
    if name == "AdamW":
        return torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=wd)
    elif name == "SGD":
        return torch.optim.SGD(
            model.parameters(), lr=lr, momentum=0.9, nesterov=True, weight_decay=wd
        )
    raise ValueError(f"Unknown optimizer: {name}")


def build_scheduler(
    optimizer: torch.optim.Optimizer, hp: dict, total_steps: int
) -> torch.optim.lr_scheduler.LRScheduler:
    if hp["lr_schedule"] == "cosine":
        return torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps)
    else:
        # Step: decay ×0.1 at 50% and 80% of total steps
        return torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=[int(total_steps * 0.5), int(total_steps * 0.8)],
            gamma=0.1,
        )


# ── Training loop ─────────────────────────────────────────────────────────────

def train_one_run(
    run: pyrometer.Run,
    hp: dict,
    X_train_np: np.ndarray,
    y_train_np: np.ndarray,
    X_val_np: np.ndarray,
    y_val_np: np.ndarray,
) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  device: {device}  optimizer: {hp['optimizer']}  hidden: {hp['hidden_dims']}")

    n_features = X_train_np.shape[1]
    batch_size = hp["batch_size"]
    epochs = hp["epochs"]

    X_train = torch.from_numpy(X_train_np).to(device)
    y_train = torch.from_numpy(y_train_np).to(device)
    X_val = torch.from_numpy(X_val_np).to(device)
    y_val = torch.from_numpy(y_val_np).to(device)

    model = build_model(n_features, hp["hidden_dims"], hp["dropout"]).to(device)
    run.watch(model, log_freq=20)
    optimizer = build_optimizer(model, hp)
    criterion = nn.CrossEntropyLoss()

    steps_per_epoch = max(1, len(X_train) // batch_size)
    scheduler = build_scheduler(optimizer, hp, epochs * steps_per_epoch)

    global_step = 0
    n_train = len(X_train)
    best_val_acc = 0.0

    for epoch in range(epochs):
        model.train()
        perm = torch.randperm(n_train, device=device)
        X_shuf, y_shuf = X_train[perm], y_train[perm]

        ep_loss, ep_correct, n_batches = 0.0, 0, 0

        for i in range(0, n_train - batch_size + 1, batch_size):
            xb = X_shuf[i : i + batch_size]
            yb = y_shuf[i : i + batch_size]

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
            # Chunked val pass to avoid OOM on large-batch configs
            chunk = 4096
            val_preds, val_losses = [], []
            for j in range(0, len(X_val), chunk):
                xv, yv = X_val[j : j + chunk], y_val[j : j + chunk]
                lv = model(xv)
                val_losses.append(criterion(lv, yv).item())
                val_preds.append((lv.argmax(1) == yv).float())
            val_loss = float(np.mean(val_losses))
            val_acc = torch.cat(val_preds).mean().item()

        run.log({"val/loss": val_loss, "val/accuracy": val_acc}, step=global_step)
        print(
            f"  epoch {epoch + 1:2d}/{epochs} — "
            f"train_loss={ep_loss/n_batches:.4f}  train_acc={ep_correct/(n_batches*batch_size):.3f}  "
            f"val_loss={val_loss:.4f}  val_acc={val_acc:.3f}"
        )

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            run.event(
                f"best val_acc improved to {val_acc:.3f}",
                event_type="custom",
                metadata={"epoch": epoch + 1, "val_acc": round(val_acc, 4)},
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading UCI Adult (census income) dataset...")
    X_train, y_train, X_val, y_val = load_data()
    n_features = X_train.shape[1]
    print(
        f"  {len(X_train)} train / {len(X_val)} val — "
        f"{n_features} features (6 continuous + one-hot categorical), binary classification\n"
    )

    print(f"Running ablation study: {len(CONFIGS)} configs × {EPOCHS} epochs\n")

    for config in CONFIGS:
        hp = config["hp"]
        hp["n_features"] = n_features  # log actual feature count after one-hot
        run = pyrometer.init(
            name=config["name"],
            hyperparams=hp,
            tags=config["tags"],
            group="ablation-study",
            notes=config["notes"],
        )
        print(f"[{config['name']}] run_id={run.run_id}")

        train_one_run(run, hp, X_train, y_train, X_val, y_val)

        run.finish()
        print(f"[{config['name']}] done\n")

    print(
        "Ablation study complete. "
        "Select all five runs in the dashboard — the hyperparam diff table "
        "will highlight optimizer, lr, weight_decay, dropout, hidden_dims, batch_size, and lr_schedule."
    )


if __name__ == "__main__":
    main()
