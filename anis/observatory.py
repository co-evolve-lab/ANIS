"""Read-only Web observatory for bootstrap and the four-stage ANIS workflow."""

from __future__ import annotations

import argparse
import datetime as dt
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "monitor" / "static"

PHASES = [
    ("Stage 1", "Capability discovery", [
        "1-Discover programmatic entrypoint", "2-Write census collector", "3-Run census", "4-Validate census",
        "5-Write engine and probe", "6-Run probe", "7-List pending capabilities",
    ]),
    ("Stage 2", "Semantic filtering", ["8-Filter capability batches"]),
    ("Stage 3", "Command implementation", [
        "9-Implement commands", "10-Render commands", "11-Reconcile capability ledger", "12-Verify commands",
    ]),
    ("Stage 4", "Documentation and release", [
        "13-Generate command documentation and Skill", "14-Freeze as CLI",
    ]),
]


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def read_jsonl(path: Path):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return rows


def process_info(pid):
    if not isinstance(pid, int) or pid < 1:
        return {"pid": None, "alive": False, "stopped": False, "command": ""}
    process = Path("/proc") / str(pid)
    if not process.is_dir():
        return {"pid": pid, "alive": False, "stopped": False,
                "command": "process exited"}
    try:
        status = (process / "status").read_text(encoding="utf-8")
        state = next((line.split(":", 1)[1].strip()
                      for line in status.splitlines() if line.startswith("State:")), "")
        command = (process / "cmdline").read_bytes().replace(
            b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        state, command = "", "unreadable process"
    return {"pid": pid, "alive": True, "stopped": state.startswith("T"),
            "state": state, "command": command}


def formal_phases(app: str, formal_process: dict):
    state = read_json(ROOT / "apps" / app / "onboard" / "state.json", {})
    history = {row.get("stage"): row for row in state.get("history", [])}
    done = set(state.get("done", []))
    flat = [name for _, _, names in PHASES for name in names]
    active = next((name for name in flat if name not in done), None)
    phases = []
    for phase_id, title, names in PHASES:
        children = []
        for name in names:
            row = history.get(name, {})
            if name in done or row.get("passed") is True:
                status = "completed"
            elif row.get("passed") is False:
                status = "failed"
            elif name == active and formal_process.get("alive"):
                status = "stopped" if formal_process.get("stopped") else "running"
            else:
                status = "pending"
            children.append({
                "name": name, "status": status,
                "owner": row.get("owner", ""),
                "detail": row.get("detail", ""), "at": row.get("at", ""),
            })
        statuses = {row["status"] for row in children}
        status = ("failed" if "failed" in statuses else
                  "stopped" if "stopped" in statuses else
                  "running" if "running" in statuses else
                  "completed" if statuses == {"completed"} else
                  "running" if "completed" in statuses else "pending")
        phases.append({"id": phase_id, "name": title,
                       "status": status, "children": children})
    return phases, state


def batch_summary(run_id: str, app: str):
    stage1 = read_json(
        ROOT / "stages" / "01_capability_discovery" / "runs"
        / run_id / "state.json", {})
    stage1_batches = stage1.get("batches", [])
    filtered = sum(row.get("status") in {
        "accepted", "completed_with_unfinished"} for row in stage1_batches)
    implementation = read_json(
        ROOT / "stages" / "02_05_command_release" / "runs"
        / run_id / app / "state.json", {})
    return {
        "filtering": {"completed": filtered, "total": len(stage1_batches)},
        "implementation": {
            "completed": len(implementation.get("completed", [])),
            "failed": len(implementation.get("failed_commands", {})),
            "planned": len(implementation.get("command_universe", [])),
        },
    }


def snapshot(run_dir: Path):
    intent = read_json(run_dir / "intent" / "result.json", {})
    entry = read_json(run_dir / "entry" / "result.json", {})
    handoff = read_json(run_dir / "handoff.json", {})
    bootstrap_pid = read_json(run_dir / "bootstrap.json", {}).get("pid")
    bootstrap_process = process_info(bootstrap_pid)
    formal_process = process_info(handoff.get("pid"))
    app = str(intent.get("app", ""))
    phases, state = formal_phases(app, formal_process) if app else ([], {})
    progress = read_jsonl(run_dir / "progress.jsonl")
    run_id = state.get("workflow_run_id", "")
    return {
        "now": dt.datetime.now().astimezone().isoformat(),
        "run_dir": str(run_dir),
        "request": (run_dir / "request.txt").read_text(
            encoding="utf-8").strip() if (run_dir / "request.txt").is_file() else "",
        "intent": intent, "entry": entry, "handoff": handoff,
        "progress": progress, "phases": phases,
        "processes": {"bootstrap": bootstrap_process, "formal": formal_process},
        "batches": batch_summary(run_id, app) if run_id and app else {},
    }


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            self.send_json(snapshot(self.server.run_dir))
            return
        if path == "/health":
            self.send_json({"status": "ok"})
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()


def main():
    parser = argparse.ArgumentParser(description="ANIS read-only observatory")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        parser.error(f"run directory does not exist: {run_dir}")
    server = ThreadingHTTPServer(
        (args.host, args.port), lambda *values, **kwargs: Handler(
            *values, directory=str(STATIC), **kwargs))
    server.run_dir = run_dir
    print(f"ANIS observatory: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
