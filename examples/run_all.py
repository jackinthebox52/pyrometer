"""Run all example scripts in sequence.

Executes each example using the same Python interpreter that runs this script,
so virtual-environment dependencies (pyrometer, torch, etc.) are always available.

Usage:
    pyrometer serve          # terminal 1
    python examples/run_all.py      # terminal 2

    # Run a subset by passing script names (without path or .py):
    python examples/run_all.py synthetic_train lr_sweep

    # Run every script through the launcher instead (`pyrometer run script.py`): same runs, the first init()
    # of each script attaches to the launcher's run
    python examples/run_all.py --launch
"""

import subprocess
import sys
import time
from pathlib import Path
import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

EXAMPLES_DIR = Path(__file__).parent

# Default run order — change to reorder or comment out entries
DEFAULT_SCRIPTS = [
    "synthetic_train",
    "lr_sweep",
    "optimizer_compare",
    "ablation_study",
]


def run_script(name: str, launch: bool = False) -> bool:
    """Run a single example script. Returns True on success."""
    script = EXAMPLES_DIR / f"{name}.py"
    if not script.exists():
        print(f"  [skip] {name}.py not found")
        return False

    print(f"{'─' * 60}")
    print(f"  Running: {name}.py")
    print(f"{'─' * 60}")

    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, *(["-m", "pyrometer", "run"] if launch else []), str(script)],
        check=False,
    )
    elapsed = time.monotonic() - start

    if result.returncode == 0:
        print(f"\n  ✓ {name}.py completed in {elapsed:.1f}s")
    else:
        print(f"\n  ✗ {name}.py exited with code {result.returncode} after {elapsed:.1f}s")

    return result.returncode == 0


def main() -> None:
    argv = [a for a in sys.argv[1:] if a != "--launch"]
    launch = len(argv) != len(sys.argv) - 1
    scripts = argv or DEFAULT_SCRIPTS

    print(f"\nPyrometer — running {len(scripts)} example script(s)\n")
    print("Make sure the server is running:  pyrometer serve")
    print()

    results: dict[str, bool] = {}
    total_start = time.monotonic()

    for name in scripts:
        ok = run_script(name, launch)
        results[name] = ok
        print()

    total = time.monotonic() - total_start

    print(f"{'═' * 60}")
    print(f"  All done in {total:.1f}s\n")
    for name, ok in results.items():
        icon = "✓" if ok else "✗"
        print(f"  {icon}  {name}")
    print()
    print("  Open the dashboard and compare runs:")
    print("  http://localhost:8321")
    print(f"{'═' * 60}\n")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
