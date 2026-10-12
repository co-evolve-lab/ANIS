"""Discover an installed application's entrypoint before ANIS onboarding."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from pathlib import Path

from anis import system_config


ROOT = Path(__file__).resolve().parents[1]
INTENT_PROMPT_PATH = ROOT / "prompts" / "bootstrap_intent.md"
INTENT_SCHEMA_PATH = ROOT / "prompts" / "bootstrap_intent.schema.json"
ENTRY_PROMPT_PATH = ROOT / "prompts" / "bootstrap_runtime.md"
ENTRY_SCHEMA_PATH = ROOT / "prompts" / "bootstrap_runtime.schema.json"
RUNS_ROOT = ROOT / "bootstrap" / "runs"
APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BootstrapError(RuntimeError):
    pass


def validate_intent(payload: object) -> dict:
    """Validate the model's interpretation without reimplementing semantics."""
    if not isinstance(payload, dict):
        raise BootstrapError("intent output is not a JSON object")
    status = payload.get("status")
    if status not in {"ready", "needs_clarification", "unsupported"}:
        raise BootstrapError(f"invalid intent status: {status!r}")
    if status == "ready":
        app = payload.get("app")
        if payload.get("action") != "anisize":
            raise BootstrapError("ready intent is not an ANIS onboarding action")
        if not isinstance(app, str) or not APP_NAME.fullmatch(app):
            raise BootstrapError("model returned an unsafe application slug")
        if not str(payload.get("display_name", "")).strip():
            raise BootstrapError("ready intent has no application display name")
    elif status == "needs_clarification":
        if not str(payload.get("clarification", "")).strip():
            raise BootstrapError("ambiguous intent has no clarification question")
    return payload


def validate_result(payload: object, app: str) -> dict:
    """Apply small deterministic checks after schema-constrained Agent output."""
    if not isinstance(payload, dict):
        raise BootstrapError("entry-discovery output is not a JSON object")
    if payload.get("app", "").casefold() != app.casefold():
        raise BootstrapError("entry-discovery output names a different application")
    status = payload.get("status")
    if status not in {"found", "not_found", "blocked"}:
        raise BootstrapError(f"invalid discovery status: {status!r}")
    launch = payload.get("launch_command")
    evidence = payload.get("evidence")
    if not isinstance(launch, str) or not isinstance(evidence, list):
        raise BootstrapError("launch_command and evidence have invalid types")
    if status == "found":
        if not launch.strip():
            raise BootstrapError("found result has no launch command")
        if not evidence or not all(isinstance(row, str) and row.strip()
                                   for row in evidence):
            raise BootstrapError("found result has no concrete local evidence")
        try:
            tokens = shlex.split(launch)
        except ValueError as error:
            raise BootstrapError(f"launch command cannot be parsed: {error}") from error
        if not tokens:
            raise BootstrapError("launch command is empty")
        executable = Path(tokens[0]).expanduser()
        if not ((executable.is_absolute() and executable.is_file())
                or shutil.which(tokens[0])):
            raise BootstrapError(
                f"chosen launcher is not currently executable: {tokens[0]}")
    return payload


def agent_command(config: dict, output: Path, work_dir: Path,
                  schema: Path, sandbox: str) -> list[str]:
    agent = config["coding_agent"]
    executable = shutil.which(agent["command"]) or agent["command"]
    command = [
        executable, "exec", "--skip-git-repo-check", "--ephemeral",
        "--sandbox", sandbox, "--json",
        "--output-schema", str(schema),
        "--output-last-message", str(output),
        "-C", str(work_dir), "-",
    ]
    if agent.get("model"):
        command[2:2] = ["--model", agent["model"]]
    return command


def invoke_agent(prompt: str, schema: Path, output: Path, work_dir: Path,
                 config: dict, sandbox: str, label: str) -> dict:
    """Run one schema-constrained worker and preserve its complete record."""
    timeout = config["workflow"]["onboarding"]["agent_timeout_seconds"]
    try:
        with (work_dir / "events.jsonl").open(
                "w", encoding="utf-8") as events, (
                work_dir / "stderr.log").open(
                    "w", encoding="utf-8") as errors:
            completed = subprocess.run(
                agent_command(config, output, work_dir, schema, sandbox),
                input=prompt, text=True, stdout=events, stderr=errors,
                timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise BootstrapError(
            f"{label} Agent timed out after {error.timeout} seconds") from error
    if completed.returncode:
        raise BootstrapError(
            f"{label} Agent exited with code {completed.returncode}; "
            f"see {work_dir / 'stderr.log'}")
    try:
        return json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BootstrapError(f"{label} output is unreadable: {error}") from error


def new_request_run(request: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()[:10]
    run_dir = RUNS_ROOT / f"{timestamp}-request-{digest}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = RUNS_ROOT / f"{timestamp}-request-{digest}-{suffix}"
    run_dir.mkdir(parents=True)
    (run_dir / "request.txt").write_text(request + "\n", encoding="utf-8")
    (run_dir / "bootstrap.json").write_text(
        json.dumps({"pid": os.getpid()}, indent=2) + "\n", encoding="utf-8")
    return run_dir


def report_progress(run_dir: Path, step: str, status: str,
                    message: str) -> None:
    """Persist and print every bootstrap transition for people and the UI."""
    row = {"at": dt.datetime.now().astimezone().isoformat(),
           "step": step, "status": status, "message": message}
    with (run_dir / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[bootstrap] {step}: {status} · {message}", flush=True)


def available_port(preferred: int) -> int:
    """Prefer the familiar port, then choose the next local free port."""
    for port in range(preferred, preferred + 50):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise BootstrapError(
        f"no observatory port available in {preferred}-{preferred + 49}")


def start_observatory(run_dir: Path, preferred_port: int) -> tuple[str, int]:
    """Start an independent read-only server that survives a stopped parent."""
    port = available_port(preferred_port)
    url = f"http://127.0.0.1:{port}"
    log_path = run_dir / "observatory.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "anis" / "observatory.py"),
             "--run-dir", str(run_dir), "--port", str(port)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    record = {"pid": process.pid, "port": port, "url": url,
              "log": str(log_path)}
    (run_dir / "observatory.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    for _ in range(20):
        try:
            with urlopen(url + "/health", timeout=0.2) as response:
                if response.status == 200:
                    return url, process.pid
        except OSError:
            time.sleep(0.1)
    raise BootstrapError(f"observatory did not become ready; see {log_path}")


def interpret_request(request: str, config: dict,
                      run_dir: Path) -> dict:
    work_dir = run_dir / "intent"
    work_dir.mkdir()
    output = work_dir / "result.json"
    prompt = INTENT_PROMPT_PATH.read_text(encoding="utf-8").replace(
        "{{REQUEST_JSON}}", json.dumps(request, ensure_ascii=False))
    payload = invoke_agent(
        prompt, INTENT_SCHEMA_PATH, output, work_dir, config,
        sandbox="read-only", label="intent")
    return validate_intent(payload)


def discover(app: str, config: dict, run_dir: Path | None = None
             ) -> tuple[dict, Path]:
    run_dir = run_dir or new_request_run(app)
    work_dir = run_dir / "entry"
    work_dir.mkdir()
    output = work_dir / "result.json"
    prompt = ENTRY_PROMPT_PATH.read_text(encoding="utf-8").replace(
        "{{APP}}", app)
    payload = invoke_agent(
        prompt, ENTRY_SCHEMA_PATH, output, work_dir, config,
        sandbox="danger-full-access", label="entry-discovery")
    return validate_result(payload, app), run_dir


def downstream_command(app: str, launch: str, config_path: str,
                       fresh: bool) -> list[str]:
    command = [
        str(ROOT / "bin" / "anis-release"), "run",
        "--app", app, "--launch", launch, "--config", config_path,
    ]
    if fresh:
        command.extend(["--", "--no-resume"])
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="anis-bootstrap",
        description="Understand a natural-language request, then start ANIS",
    )
    parser.add_argument(
        "request", nargs="*",
        help="an informal natural-language request; reads stdin when omitted")
    parser.add_argument("--config")
    parser.add_argument(
        "--discover-only", action="store_true",
        help="record the discovered entrypoint without starting ANIS",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="archive prior ANIS progress and start a new end-to-end run",
    )
    parser.add_argument(
        "--monitor-port", type=int, default=8765,
        help="preferred local observatory port; the next free port is used",
    )
    parser.add_argument(
        "--no-monitor", action="store_true",
        help="do not start the read-only Web observatory",
    )
    args = parser.parse_args()
    request = " ".join(args.request).strip()
    if not request and not sys.stdin.isatty():
        request = sys.stdin.read().strip()
    if not request:
        request = input("Describe the application you want ANIS to onboard: ").strip()
    if not request:
        parser.error("the natural-language request cannot be empty")
    run_dir = None
    current_step = "request"
    try:
        config = system_config.load(args.config)
        run_dir = new_request_run(request)
        report_progress(run_dir, "request", "completed", request)
        if not args.no_monitor:
            current_step = "observatory"
            report_progress(run_dir, current_step, "running",
                            "Starting read-only monitoring service")
            url, _ = start_observatory(run_dir, args.monitor_port)
            report_progress(run_dir, current_step, "completed", url)
            print(f"[bootstrap] Monitor: {url}", flush=True)
        current_step = "intent"
        report_progress(run_dir, current_step, "running",
                        "Request interpretation Agent is identifying the application and task")
        intent = interpret_request(request, config, run_dir)
        report_progress(
            run_dir, current_step, "completed",
            f"Identified as {intent.get('display_name') or intent.get('status')}")
        if intent["status"] != "ready":
            print(json.dumps({"intent": intent, "run_dir": str(run_dir)},
                             ensure_ascii=False, indent=2))
            if intent.get("clarification"):
                print(intent["clarification"], file=sys.stderr)
            return 3
        current_step = "entry"
        report_progress(run_dir, current_step, "running",
                        f"Entrypoint discovery Agent is investigating {intent['display_name']}")
        result, run_dir = discover(intent["app"], config, run_dir)
    except (BootstrapError, system_config.ConfigError) as error:
        if run_dir is not None:
            report_progress(run_dir, current_step, "failed", str(error))
        print(f"[bootstrap] ERROR: {error}", file=sys.stderr)
        return 2
    report_progress(
        run_dir, "entry", "completed",
        (f"Entrypoint confirmed: {result['launch_command']}" if result["status"] == "found"
         else f"Entrypoint discovery finished: {result['status']}"))
    print(json.dumps({"intent": intent, "entry": result,
                      "run_dir": str(run_dir)},
                     ensure_ascii=False, indent=2))
    if result["status"] != "found":
        report_progress(run_dir, "handoff", "skipped",
                        "No verified entrypoint; did not start ANIS")
        print(
            f"[bootstrap] did not start ANIS because status={result['status']}; "
            f"evidence is preserved in {run_dir}", file=sys.stderr)
        return 3
    if args.discover_only:
        report_progress(run_dir, "handoff", "skipped",
                        "Discovery-only mode selected")
        return 0
    current_step = "handoff"
    command = downstream_command(
        intent["app"], result["launch_command"],
        config["_path"], args.fresh)
    report_progress(run_dir, current_step, "running",
                    "Python is starting the four-stage ANIS controller")
    process = subprocess.Popen(command, cwd=ROOT)
    handoff = {"pid": process.pid, "status": "running", "command": command,
               "started_at": dt.datetime.now().astimezone().isoformat()}
    (run_dir / "handoff.json").write_text(
        json.dumps(handoff, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    report_progress(run_dir, current_step, "completed",
                    f"main ANIS started; PID {process.pid}")
    return_code = process.wait()
    handoff.update({"status": "completed" if return_code == 0 else "failed",
                    "return_code": return_code,
                    "finished_at": dt.datetime.now().astimezone().isoformat()})
    (run_dir / "handoff.json").write_text(
        json.dumps(handoff, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
