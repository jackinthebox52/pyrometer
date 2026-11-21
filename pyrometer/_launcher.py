"""``pyrometer run``: run an unmodified training script inside a Pyrometer run (zero code changes).

    pyrometer run [--name N] [--project P] [--group G] [--preset X] [--tag T ...] [--no-autolog]
                  (script.py | -m module) [script arguments ...]

The launcher opens the run (autolog hooks live, the run is the process's current run), executes the script in
this very process with :mod:`runpy` so global PyTorch hooks see it, and closes the run with the status the
script's outcome implies, exactly like ``with pyrometer.init(...) as run:``. Under ``torchrun`` use
``torchrun ... -m pyrometer run train.py`` (rank 0 logs, other ranks just run the script).
"""

from __future__ import annotations

import math
import os
import re
import runpy
import sys
from typing import Any

USAGE = """usage: pyrometer run [--name N] [--project P] [--group G] [--preset X] [--tag T ...] [--no-autolog]
                     (script.py | -m module) [script arguments ...]

Run a training script in-process inside a Pyrometer run. Options before the script belong to the launcher,
everything after it goes to the script (and becomes the run's hyperparams, best effort).

  --name N        run name (default: the script / module name)
  --project P     project to file the run under
  --group G       experiment / sweep group
  --preset X      preset: llm-pretrain, finetune, rlhf, rl, benchmark (see `pyrometer presets`)
  --tag T         tag (repeatable)
  --no-autolog    only open and close the run; no automatic collection
  -m MODULE       run a module (like python -m) instead of a script file
"""

_VALUE_OPTS = {"--name": "name", "--project": "project", "--group": "group", "--preset": "preset", "--tag": "tag"}


def split_args(argv: list[str]) -> tuple[dict[str, Any], str, str, list[str]]:
    """Split launcher options from the target and the script's own arguments.

    Returns ``(options, kind, target, script_args)`` with ``kind`` ``"script"`` or ``"module"``. Raises
    ``ValueError`` with a message on malformed input.
    """
    opts: dict[str, Any] = {"tags": [], "autolog": True}
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a in ("-h", "--help"):
            raise SystemExit(USAGE.rstrip())
        if a == "-m":
            if i + 1 >= n:
                raise ValueError("-m needs a module name")
            return opts, "module", argv[i + 1], argv[i + 2:]
        if a == "--no-autolog":
            opts["autolog"] = False
            i += 1
            continue
        if a == "--":
            i += 1
            continue
        key, eq, val = a.partition("=")
        if key in _VALUE_OPTS:
            if not eq:
                if i + 1 >= n:
                    raise ValueError(f"{key} needs a value")
                val = argv[i + 1]
                i += 1
            if key == "--tag":
                opts["tags"].append(val)
            else:
                opts[_VALUE_OPTS[key]] = val
            i += 1
            continue
        if a.startswith("-") and a != "-":
            raise ValueError(f"unknown launcher option {a!r} (options for the script go after the script name)")
        return opts, "script", a, argv[i + 1:]
    raise ValueError("missing script (or -m module)")


_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _is_number(s: str) -> bool:
    return bool(_NUM.match(s))


def _coerce(v: str) -> Any:
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    if _is_number(v) and not re.match(r"^[+-]?0\d", v):
        try:
            if re.fullmatch(r"[+-]?\d+", v):
                return int(v)
            f = float(v)
            if math.isfinite(f):
                return f
        except ValueError:
            pass
    return v


def parse_argv(args: list[str]) -> dict[str, Any]:
    """Best-effort ``--key value`` / ``--key=value`` / ``--flag`` / ``-k value`` parsing into a dict.

    Values are coerced to int, float, bool (``true``/``false``) or ``None`` (``none``/``null``) when they look like
    one, else kept as strings. Dashes in keys become underscores. A flag with no value is ``True``; a repeated key
    becomes a list; arguments that belong to no option are collected under ``positional``. The raw list is always
    kept under ``argv`` (and the target under ``script`` unless the script defines an option of that name).
    """
    out: dict[str, Any] = {}
    positional: list[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1 and not _is_number(a):
            key, eq, val = a.lstrip("-").partition("=")
            if not key:
                i += 1
                continue
            key = key.replace("-", "_")
            if eq:
                value: Any = _coerce(val)
            elif i + 1 < n and (not args[i + 1].startswith("-") or _is_number(args[i + 1])):
                value = _coerce(args[i + 1])
                i += 1
            else:
                value = True
            if key in out:
                prev = out[key]
                out[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                out[key] = value
        else:
            positional.append(a)
        i += 1
    if positional:
        out.setdefault("positional", positional)
    out.setdefault("argv", list(args))
    return out


def main(argv: list[str]) -> None:
    """Entry point of ``pyrometer run``; does not return normally (the script's exit status propagates)."""
    try:
        opts, kind, target, script_args = split_args(argv)
    except ValueError as e:
        sys.exit(f"pyrometer run: {e}\n\n{USAGE}")

    from pyrometer._presets import get_preset

    try:
        get_preset(opts.get("preset"))
    except ValueError as e:
        sys.exit(f"pyrometer run: {e}")
    if kind == "script" and not os.path.isfile(target):
        sys.exit(f"pyrometer run: script not found: {target}")

    import pyrometer

    hyperparams = parse_argv(script_args)
    hyperparams.setdefault("script", target)
    default_name = os.path.splitext(os.path.basename(target))[0] if kind == "script" else target
    run = pyrometer.init(
        name=opts.get("name") or default_name,
        hyperparams=hyperparams,
        tags=opts["tags"],
        project=opts.get("project"),
        group=opts.get("group"),
        preset=opts.get("preset"),
        autolog=opts["autolog"],
    )
    # What init() may still change when the script calls it itself (see pyrometer._attach_launcher_run).
    run._launcher_explicit = {k for k in ("name", "group", "preset") if opts.get(k)}  # type: ignore[attr-defined]
    run._launcher_hp = dict(hyperparams)  # type: ignore[attr-defined]
    run._launcher_tags = list(opts["tags"])  # type: ignore[attr-defined]
    run._launcher_project = opts.get("project")  # type: ignore[attr-defined]
    run_is_real = run.run_id is not None
    if run_is_real:
        pyrometer._set_launcher_run(run)

    if kind == "script":
        sys.argv = [target, *script_args]
        sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
    else:
        sys.argv = [target, *script_args]
        sys.path.insert(0, os.getcwd())
    # The block ends the run like ``with pyrometer.init(...)``: completed, failed (exception / SystemExit(nonzero)),
    # killed (Ctrl-C). The exception is never swallowed, so the process exits as the script would have.
    with run:
        try:
            if kind == "script":
                runpy.run_path(target, run_name="__main__")
            else:
                runpy.run_module(target, run_name="__main__", alter_sys=True)
        finally:
            pyrometer._set_launcher_run(None)
