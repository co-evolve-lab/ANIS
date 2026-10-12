"""Stable command-line entry point for the standalone ANIS release artifact."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from anis import system_config


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"


def _onboard_command(config: str | None, arguments: list[str]) -> list[str]:
    command = [sys.executable, str(ROOT / "anis" / "onboard.py")]
    if config:
        command.extend(["--config", config])
    command.extend(arguments)
    return command


def doctor(config_path: str | None) -> int:
    """Check everything the controller needs before a long workflow starts."""
    checks = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    try:
        config = system_config.load(config_path)
        record("configuration", True, config["_path"])
    except Exception as error:
        record("configuration", False, str(error))
        config = None

    record(
        "python",
        sys.version_info >= (3, 10),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    if config:
        executable = config["coding_agent"]["command"]
        resolved = shutil.which(executable)
        record(
            "coding_agent",
            resolved is not None,
            resolved or f"executable not found: {executable}",
        )
    required = [
        ROOT / "prompts" / "probe_runtime.md",
        ROOT / "prompts" / "census_adapter.md",
        ROOT / "prompts" / "build_app.md",
        ROOT / "prompts" / "bootstrap_runtime.md",
        ROOT / "prompts" / "bootstrap_runtime.schema.json",
        ROOT / "prompts" / "bootstrap_intent.md",
        ROOT / "prompts" / "bootstrap_intent.schema.json",
        ROOT / "anis" / "observatory.py",
        ROOT / "monitor" / "static" / "index.html",
        ROOT / "monitor" / "static" / "app.js",
        ROOT / "monitor" / "static" / "style.css",
        ROOT / "stages" / "01_capability_discovery" / "workflow.py",
        ROOT / "stages" / "02_05_command_release" / "run_app.py",
        ROOT / "stages" / "06_goal_commands" / "workflow.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    record(
        "artifact_layout",
        not missing,
        "complete" if not missing else "missing: " + ", ".join(missing),
    )
    apps = sorted(
        path.parent.name
        for path in (ROOT / "apps").glob("*/runtime.json")
    )
    record(
        "configured_apps",
        True,
        ", ".join(apps) if apps else "none yet; Stage 1 can onboard a new app",
    )
    payload = {"status": "ok" if all(row["passed"] for row in checks) else "error",
               "checks": checks}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="anis-release",
        description="Configure, inspect, and run the standalone ANIS workflow",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="print the artifact version")
    doctor_parser = subparsers.add_parser(
        "doctor", help="validate configuration and local prerequisites")
    doctor_parser.add_argument("--config")

    plan = subparsers.add_parser("plan", help="print the four-stage workflow")
    plan.add_argument("--app", required=True)
    plan.add_argument("--launch", default="")
    plan.add_argument("--config")

    run = subparsers.add_parser("run", help="run or resume the workflow")
    run.add_argument("--app", required=True)
    run.add_argument("--launch", default="")
    run.add_argument("--config")
    run.add_argument(
        "onboard_args", nargs=argparse.REMAINDER,
        help="extra onboard.py arguments after --, for example -- --no-resume",
    )

    goals = subparsers.add_parser(
        "goals", help="synthesize goal-level commands from recorded requests")
    goals.add_argument("--app", required=True)
    goals.add_argument("--requests", required=True)
    goals.add_argument("--release", action="store_true")
    goals.add_argument("--config")

    args = parser.parse_args()
    if args.command == "goals":
        command = [sys.executable, str(ROOT / "stages" / "06_goal_commands" / "workflow.py"),
                   "--app", args.app, "--requests", str(Path(args.requests).resolve())]
        command += ["--release"] if args.release else []
        command += ["--config", args.config] if args.config else []
        return subprocess.run(command, cwd=ROOT).returncode
    if args.command == "version":
        print(VERSION_FILE.read_text(encoding="utf-8").strip())
        return 0
    if args.command == "doctor":
        return doctor(args.config)
    common = ["--app", args.app, "--launch", args.launch]
    if args.command == "plan":
        return subprocess.run(
            _onboard_command(args.config, common + ["--plan"]), cwd=ROOT
        ).returncode
    extras = list(args.onboard_args)
    if extras[:1] == ["--"]:
        extras = extras[1:]
    return subprocess.run(
        _onboard_command(args.config, common + extras), cwd=ROOT
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
