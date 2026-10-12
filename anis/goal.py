"""Runtime for goal-level commands: verified compositions of released atomic commands."""

import json
from pathlib import Path

from anis import kernel


def _rows(value, name):
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise kernel.AnisError("INVALID_ARGS", f"--{name} is not valid JSON: {exc}",
                               "Add --schema to see arguments")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise kernel.AnisError("INVALID_ARGS", f"--{name} must be a non-empty JSON array of objects",
                               "Add --schema to see arguments")
    return rows


def expand(args, contract):
    """Return the atomic (command, arguments) calls in their original order."""
    shared = {name: args[name] for name in contract["shared"] if args.get(name) is not None}
    calls = []
    for step in contract["steps"]:
        if step["mode"] == "batch":
            calls.extend((step["command"], {**shared, **row})
                         for row in _rows(args.get(step["items"]), step["items"]))
        else:
            calls.append((step["command"], {**shared, **{
                child: args[parent] for parent, child in step["args"].items()
                if args.get(parent) is not None}}))
    return calls


def run(args, impl_file, contract):
    root = Path(impl_file).resolve().parents[2]
    calls = expand(args, contract)
    for index, (command, values) in enumerate(calls, 1):
        cmd_dir = str(root / "commands" / command)
        try:
            report = kernel.load_impl(cmd_dir).run(kernel.validate(kernel.load_spec(cmd_dir), values))
        except kernel.AnisError as error:
            raise kernel.AnisError(error.code, f"step {index} ({command}) failed: {error}",
                                   error.fix) from error
        if isinstance(report, dict) and report.get("status", "ok") != "ok":
            raise kernel.AnisError(report.get("code", "STEP_FAILED"),
                                   f"step {index} ({command}) failed: {report.get('message', '')}",
                                   report.get("fix", "Run the atomic commands individually"))
    return {
        "status": "ok",
        "steps_completed": [command for command, _ in calls],
        "task_complete": False,
        "message": ("The listed steps are complete; do not repeat them. "
                    "Continue only with requirements this command did not cover."),
    }
