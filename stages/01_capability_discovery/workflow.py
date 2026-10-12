#!/usr/bin/env python3
"""Deterministic controller for ANIS stage-1 capability discovery.

The controller owns scope, batching, Codex invocation, structured-output
validation, evidence validation, semantic filtering, resume state, and the
completion gate. Codex investigates assigned items but cannot decide which
items exist or whether the stage is complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import copy
import resource
from concurrent.futures import ThreadPoolExecutor, as_completed


STAGE_DIR = Path(__file__).resolve().parent
ANIS_ROOT = STAGE_DIR.parents[1]
sys.path.insert(0, str(ANIS_ROOT))
from anis import system_config  # noqa: E402

CONFIG_PATH = STAGE_DIR / "config.json"
DISCOVERY_SCHEMA = STAGE_DIR / "schemas" / "discovery_result.schema.json"
REVIEW_SCHEMA = STAGE_DIR / "schemas" / "review_result.schema.json"
DISCOVERY_PROMPT = STAGE_DIR / "prompts" / "discover_batch.md"
REVIEW_PROMPT = STAGE_DIR / "prompts" / "review_batch.md"
REVIEW_ASSIGNMENT = STAGE_DIR / "prompts" / "review_assignment.md"
RUNS_DIR = STAGE_DIR / "runs"
TERMINAL_DISCOVERED = {
    "executable", "equivalent", "out_of_scope", "unsupported",
}
TERMINAL_ITEM_STATES = {"accepted", "filtered", "unfinished"}


class WorkflowError(RuntimeError):
    pass


def load_config():
    config = copy.deepcopy(load_json(CONFIG_PATH))
    apps_root = ANIS_ROOT / "apps"
    for app_dir in sorted(apps_root.iterdir() if apps_root.is_dir() else []):
        if not app_dir.is_dir() or app_dir.name in config["apps"]:
            continue
        runtime_path = app_dir / "runtime.json"
        census_path = app_dir / "census" / "raw_ops.json"
        ledger_path = app_dir / "capabilities" / "ledger.json"
        if not (runtime_path.is_file() and census_path.is_file()
                and ledger_path.is_file()):
            continue
        runtime_decl = load_json(runtime_path)
        config["apps"][app_dir.name] = {
            "app_dir": app_dir.name,
            "runtime_family": runtime_decl.get(
                "runtime_family", "model_selected_programmatic_entrypoints"),
            "existing_census": str(census_path.relative_to(ANIS_ROOT)),
            "existing_ledger": str(ledger_path.relative_to(ANIS_ROOT)),
        }
    return config


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def write_jsonl(path: Path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_jsonl(path: Path):
    values = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                values.append(json.loads(line))
    return values


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_apps(config, raw):
    if raw == "all":
        return list(config["apps"])
    requested = [value.strip() for value in raw.split(",") if value.strip()]
    unknown = sorted(set(requested) - set(config["apps"]))
    if unknown:
        raise WorkflowError(f"Unknown application: {', '.join(unknown)}")
    return requested


def stable_item_id(app, index, entry):
    record_identity = entry.get("record_sha256")
    if not record_identity:
        record_identity = json.dumps(
            entry.get("record"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
    key = "\0".join((
        app, str(entry.get("channel", "")), str(entry.get("symbol", "")),
        record_identity))
    short = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{app}:{index:05d}:{short}"


def existing_commands_are_materialized(entry, app_root):
    commands = entry.get("commands") or []
    if not commands:
        return False
    return all(
        command.get("command")
        and (app_root / "commands" / command["command"] / "spec.json").is_file()
        and (app_root / "commands" / command["command"] / "impl.py").is_file()
        for command in commands
    )


def classify_entry(entry, config, app_root=None):
    status = str(entry.get("status", ""))
    reason = str(entry.get("reason", ""))
    if status in set(config["revisit_statuses"]):
        return "queue", f"status={status}"
    if status in {"generated", "generated_and_verified"}:
        if app_root is not None and not existing_commands_are_materialized(
                entry, app_root):
            return "queue", "Ledger reports generation but command files or records are incomplete"
        return "reuse", f"Reuse existing terminal result {status}"
    if status == "merged_equivalent":
        if not reason.strip():
            return "queue", "Ledger reports equivalent coverage without a reason"
        return "reuse", "Reuse existing equivalence decision"
    if status == "excluded_with_reason":
        lowered = reason.lower()
        phrases = [value.lower()
                   for value in config["suspicious_exclusion_phrases"]]
        if any(phrase in lowered for phrase in phrases):
            return "queue", "Exclusion cites failure, timeout, or insufficient context; rediscovery required"
        return "reuse", "Reuse existing scope exclusion and reason"
    # Unknown states are never silently accepted.
    return "queue", f"Unknown or nonterminal state status={status!r}"


def make_items(app, ledger, config, app_root=None):
    items = []
    for index, entry in enumerate(ledger.get("entries", []), 1):
        action, why = classify_entry(entry, config, app_root)
        items.append({
            "item_id": stable_item_id(app, index, entry),
            "app": app,
            "ledger_index": index,
            "channel": entry.get("channel", ""),
            "symbol": entry.get("symbol", ""),
            "original_status": entry.get("status", ""),
            "original_reason": entry.get("reason", ""),
            "record": entry.get("record"),
            "source_channels": entry.get(
                "channels", [entry.get("channel", "")]),
            "source_locations": entry.get("locations", []),
            "occurrences": entry.get("occurrences", 1),
            "existing_commands": entry.get("commands") or [],
            "failed_commands": entry.get("failed_commands") or [],
            "stage1_action": action,
            "stage1_reason": why,
        })
    return items


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def new_run_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prepare_run(args, global_config=None):
    config = load_config()
    global_config = global_config or system_config.load(args.config)
    discovery_config = global_config["workflow"]["capability_discovery"]
    apps = selected_apps(config, args.apps)
    run_id = args.run_id or new_run_id()
    run_dir = RUNS_DIR / run_id
    if run_dir.exists():
        raise WorkflowError(
            f"Run directory exists: {run_dir}; use --run-dir to resume instead of overwriting")
    run_dir.mkdir(parents=True)
    inputs = {
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "anis_root": str(ANIS_ROOT),
        "apps": apps,
        "batch_size": args.batch_size or discovery_config["batch_size"],
        "global_config": global_config["_path"],
        "global_config_sha256": sha256(Path(global_config["_path"])),
        "config_sha256": sha256(CONFIG_PATH),
        "controller_files": {
            str(path.relative_to(STAGE_DIR)): sha256(path)
            for path in (
                Path(__file__).resolve(),
                CONFIG_PATH,
                STAGE_DIR / "AGENTS.md",
                REVIEW_PROMPT,
                REVIEW_SCHEMA,
            )
        },
        "files": {},
    }
    batches = []
    counts = {}
    for app in apps:
        app_config = config["apps"][app]
        census = ANIS_ROOT / app_config["existing_census"]
        ledger_path = ANIS_ROOT / app_config["existing_ledger"]
        for path in (census, ledger_path):
            if not path.is_file():
                raise WorkflowError(f"Missing discovery input: {path}")
        ledger = load_json(ledger_path)
        app_root = ANIS_ROOT / "apps" / app_config["app_dir"]
        items = make_items(app, ledger, config, app_root)
        queued = [item for item in items if item["stage1_action"] == "queue"]
        app_run = run_dir / app
        write_jsonl(app_run / "inventory.jsonl", items)
        write_jsonl(app_run / "queue.jsonl", queued)
        batch_size = int(inputs["batch_size"])
        for number, batch_items in enumerate(
                chunks(queued, batch_size), 1):
            batch_id = f"{app}-{number:05d}"
            batch_dir = app_run / "batches" / batch_id
            batch = {
                "schema_version": 1,
                "run_id": run_id,
                "batch_id": batch_id,
                "app": app,
                "runtime_family": app_config["runtime_family"],
                "items": batch_items,
            }
            atomic_json(batch_dir / "batch.json", batch)
            batches.append({
                "batch_id": batch_id, "app": app,
                "path": str(batch_dir.relative_to(run_dir)),
                "status": "pending", "attempts": 0,
                "items": {
                    item["item_id"]: {
                        "status": "pending", "attempts": 0,
                        "accepted_attempt": "", "error": "",
                    }
                    for item in batch_items
                },
            })
        counts[app] = {
            "ledger_entries": len(items),
            "queued": len(queued),
            "reused": len(items) - len(queued),
            "batches": sum(batch["app"] == app for batch in batches),
        }
        inputs["files"][app] = {
            "census": str(census),
            "census_sha256": sha256(census),
            "ledger": str(ledger_path),
            "ledger_sha256": sha256(ledger_path),
        }
    atomic_json(run_dir / "inputs.json", inputs)
    atomic_json(run_dir / "state.json", {
        "run_id": run_id, "status": "prepared", "counts": counts,
        "batches": batches, "updated_at": time.time(),
    })
    return run_dir


def load_run(run_dir):
    run_dir = Path(run_dir).resolve()
    if not (run_dir / "inputs.json").is_file():
        raise WorkflowError(f"Invalid discovery run directory: {run_dir}")
    return run_dir, load_json(run_dir / "inputs.json"), load_json(
        run_dir / "state.json")


def verify_frozen_inputs(inputs):
    problems = []
    global_path = inputs.get("global_config")
    global_digest = inputs.get("global_config_sha256")
    if global_path and global_digest:
        path = Path(global_path)
        if not path.is_file() or sha256(path) != global_digest:
            problems.append(str(path))
    if sha256(CONFIG_PATH) != inputs["config_sha256"]:
        problems.append(str(CONFIG_PATH))
    for relative, expected in inputs.get("controller_files", {}).items():
        path = STAGE_DIR / relative
        if not path.is_file() or sha256(path) != expected:
            problems.append(str(path))
    for app, files in inputs["files"].items():
        for label in ("census", "ledger"):
            path = Path(files[label])
            expected = files[f"{label}_sha256"]
            if not path.is_file() or sha256(path) != expected:
                problems.append(f"{app}:{path}")
    if problems:
        raise WorkflowError(
            "Inputs changed after preparation; cannot combine versions in one run: "
            + ", ".join(problems))


def render_prompt(path, values):
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftovers = [part.split("}}", 1)[0]
                 for part in text.split("{{")[1:] if "}}" in part]
    if leftovers:
        raise WorkflowError(f"Prompt Unresolved template variables: {leftovers}")
    return text


def codex_command(work_dir, schema, output, model, sandbox):
    command = [
        shutil.which("codex") or "codex", "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", sandbox, "--json",
        "--output-schema", str(schema),
        "--output-last-message", str(output),
        "-C", str(work_dir), "-",
    ]
    if model:
        command[2:2] = ["--model", model]
    return command


def configured_codex_command(executable, work_dir, schema, output,
                             model, sandbox):
    command = codex_command(work_dir, schema, output, model, sandbox)
    command[0] = shutil.which(executable) or executable
    return command


def invoke_codex(prompt, work_dir, schema, output, events, stderr,
                 model, timeout, sandbox="workspace-write", executable="codex"):
    command = configured_codex_command(
        executable, work_dir, schema, output, model, sandbox)
    runtime_dir = work_dir / ".runtime"
    runtime_paths = {
        "XDG_CACHE_HOME": runtime_dir / "cache",
        "XDG_CONFIG_HOME": runtime_dir / "config",
        "XDG_RUNTIME_DIR": runtime_dir / "run",
        "TMPDIR": runtime_dir / "tmp",
    }
    for path in runtime_paths.values():
        path.mkdir(parents=True, exist_ok=True)
    runtime_paths["XDG_RUNTIME_DIR"].chmod(0o700)
    environment = dict(os.environ)
    environment.update({
        name: str(path) for name, path in runtime_paths.items()
    })
    with events.open("w", encoding="utf-8") as event_handle, \
            stderr.open("w", encoding="utf-8") as error_handle:
        try:
            result = subprocess.run(
                command, input=prompt, text=True, stdout=event_handle,
                stderr=error_handle, timeout=timeout, env=environment,
                preexec_fn=disable_core_dumps)
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                f"Codex Worker timed out ({timeout}s): {work_dir}") from exc
    if result.returncode != 0:
        tail = stderr.read_text(encoding="utf-8", errors="replace")[-1200:]
        raise WorkflowError(
            f"Codex Worker failed (exit={result.returncode}): {tail}")
    if not output.is_file():
        raise WorkflowError(f"Codex No structured output: {output}")


def run_assigned_review(batch, result_path, attempt_dir, model_override,
                        global_config, batch_path=None):
    """Run one lightweight semantic-filter batch with the configured agent."""
    del result_path
    agent = global_config["coding_agent"]
    review_config = global_config["workflow"]["capability_review"]
    review_prompt = render_prompt(REVIEW_PROMPT, {
        "BATCH_PATH": batch_path or attempt_dir / "batch.json",
    })
    review_prompt += REVIEW_ASSIGNMENT.read_text(encoding="utf-8")
    review_path = attempt_dir / "review_result.json"
    invoke_codex(
        review_prompt, attempt_dir, REVIEW_SCHEMA, review_path,
        attempt_dir / "review_events.jsonl",
        attempt_dir / "review_stderr.log",
        model_override or agent.get("model"),
        review_config["timeout_seconds"],
        sandbox=review_config["sandbox"], executable=agent["command"])
    review = load_json(review_path)
    validate_result_envelope(batch, review, "Review result")
    return review


def disable_core_dumps():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def exact_ids(batch, report):
    expected = [item["item_id"] for item in batch["items"]]
    actual = [item.get("item_id") for item in report.get("items", [])]
    if len(actual) != len(set(actual)):
        raise WorkflowError("Codex Result contains duplicate item_id")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise WorkflowError(
            f"Codex Result does not cover the exact assigned items; missing={missing}, extra={extra}")


def safe_evidence_path(attempt_dir, raw):
    candidate = (attempt_dir / raw).resolve()
    try:
        candidate.relative_to(attempt_dir.resolve())
    except ValueError as exc:
        raise WorkflowError(f"Evidence path escapes batch directory: {raw}") from exc
    if not candidate.is_file():
        raise WorkflowError(f"Evidence file does not exist: {raw}")
    return candidate


def validate_discovery(batch, report, attempt_dir, max_attempts):
    validate_result_envelope(batch, report, "Discovery result")
    problems = discovery_item_errors(
        batch, report, attempt_dir, max_attempts)
    if problems:
        raise WorkflowError("; ".join(
            f"{item_id}: {reason}" for item_id, reason in problems.items()
        )[:8000])


def validate_result_envelope(batch, report, label):
    """Validate only batch-wide identity; item completeness is item-scoped."""
    if report.get("batch_id") != batch["batch_id"]:
        raise WorkflowError(f"{label} batch_id does not match")
    if report.get("app") != batch["app"]:
        raise WorkflowError(f"{label} app does not match")
    if not isinstance(report.get("items"), list):
        raise WorkflowError(f"{label} items is not a list")


def discovery_item_errors(batch, report, attempt_dir, max_attempts):
    """Return independently attributable discovery errors by assigned item."""
    expected = {item["item_id"] for item in batch["items"]}
    grouped = {}
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        grouped.setdefault(item.get("item_id"), []).append(item)
    problems = {}
    extras = sorted(
        str(item_id) for item_id in grouped if item_id not in expected)
    if extras:
        raise WorkflowError(f"Discovery result includes unassigned item_id: {extras}")
    for item_id in expected:
        matches = grouped.get(item_id, [])
        if not matches:
            problems[item_id] = "Discovery result is missing this item"
            continue
        if len(matches) > 1:
            problems[item_id] = "Discovery result repeats this item"
            continue
        item = matches[0]
        item_problems = []
        if not isinstance(item, dict):
            problems[item_id] = "Discovery item is not an object"
            continue
        disposition = item.get("disposition")
        if not isinstance(disposition, str) or not disposition:
            problems[item_id] = "Discovery item is missing disposition"
            continue
        attempts = item.get("attempts", [])
        evidence = item.get("evidence", [])
        if not isinstance(attempts, list):
            problems[item_id] = "attempts is not a list"
            continue
        if not isinstance(evidence, list):
            problems[item_id] = "evidence is not a list"
            continue
        if len(attempts) > max_attempts:
            item_problems.append(
                f"Attempt count {len(attempts)} > {max_attempts}")
        if not attempts:
            item_problems.append("No actual attempts recorded")
        if disposition in TERMINAL_DISCOVERED and not evidence:
            item_problems.append("Terminal state has no evidence")
        for record in evidence:
            if not isinstance(record, dict) or not record.get("path"):
                item_problems.append("Evidence record is missing a path")
                continue
            try:
                safe_evidence_path(attempt_dir, record["path"])
            except WorkflowError as exc:
                item_problems.append(str(exc))
        if disposition == "executable":
            if not any(
                    isinstance(value, dict) and value.get("exit_code") == 0
                    for value in attempts):
                item_problems.append("No successful execution recorded")
            if not item.get("command_candidates"):
                item_problems.append("Missing complete command candidate list")
            for parameter in item.get("parameters", []):
                if not isinstance(parameter, dict):
                    item_problems.append("Parameter record is not an object")
                    continue
                if not parameter.get("value_kind"):
                    item_problems.append(
                        f"Argument {parameter.get('name')} is missing a machine-readable value_kind")
                if (parameter.get("value_kind") == "object_reference"
                        and not parameter.get("target_type")):
                    item_problems.append(
                        f"Object reference parameter {parameter.get('name')} is missing target_type")
            for candidate in item.get("command_candidates", []):
                if not isinstance(candidate, dict):
                    item_problems.append("Command candidate is not an object")
                    continue
                if not candidate.get("implementation_route"):
                    item_problems.append(
                        f"Candidate {candidate.get('name')} is missing implementation_route")
                if not candidate.get("persistence_check"):
                    item_problems.append(
                        f"Candidate {candidate.get('name')} is missing persistence_check")
        if disposition == "equivalent" and not item.get(
                "equivalent_commands"):
            item_problems.append("Missing equivalent command list")
        if disposition == "unsupported" and len(attempts) < 2:
            item_problems.append("One attempt cannot establish runtime non-support")
        if item_problems:
            problems[item_id] = "; ".join(item_problems)
    return problems


def validate_review(batch, review):
    validate_result_envelope(batch, review, "Review result")
    exact_ids(batch, review)


def review_item_errors(batch, review):
    """Return missing or duplicate filter decisions without rejecting peers."""
    expected = {item["item_id"] for item in batch["items"]}
    grouped = {}
    for item in review.get("items", []):
        if not isinstance(item, dict):
            continue
        grouped.setdefault(item.get("item_id"), []).append(item)
    errors = {}
    verdicts = {}
    for item_id in expected:
        matches = grouped.get(item_id, [])
        if not matches:
            errors[item_id] = "Review is missing this item"
        elif len(matches) > 1:
            errors[item_id] = "Review repeats this item"
        elif matches[0].get("decision") not in {"expose", "skip"}:
            errors[item_id] = "Semantic filtering result lacks a valid decision"
        else:
            verdicts[item_id] = matches[0]
    return verdicts, errors


def batch_prompt_values(batch, attempt_dir, config):
    app_config = config["apps"][batch["app"]]
    return {
        "BATCH_PATH": attempt_dir / "batch.json",
        "ANIS_ROOT": ANIS_ROOT,
        "CENSUS_PATH": ANIS_ROOT / app_config["existing_census"],
        "LEDGER_PATH": ANIS_ROOT / app_config["existing_ledger"],
        "RUNTIME_FAMILY": batch["runtime_family"],
        "MAX_ATTEMPTS": config["max_attempts_per_item"],
    }


def item_result(report, item_id):
    return next(
        (item for item in report.get("items", [])
         if item.get("item_id") == item_id), None)


def terminal_batch_status(item_states):
    statuses = {item["status"] for item in item_states.values()}
    if statuses <= {"accepted", "filtered"}:
        return "accepted"
    if statuses <= TERMINAL_ITEM_STATES:
        return "completed_with_unfinished"
    return "needs_retry"


def investigate_batch(run_dir, batch_state, args, config, global_config,
                      review_semaphore):
    """Apply one semantic expose/skip decision to every assigned record.

    This stage deliberately performs no runtime call, parameter analysis,
    evidence collection, or implementation planning. Those belong to the
    isolated command implementation stage.
    """
    source_dir = run_dir / batch_state["path"]
    full_batch = load_json(source_dir / "batch.json")
    item_states = batch_state["items"]
    active = [item for item in full_batch["items"]
              if item_states[item["item_id"]]["status"] == "pending"]
    attempt_number = batch_state["attempts"] + (1 if active else 0)
    if active:
        batch = {**full_batch, "items": active}
        attempt_dir = source_dir / f"attempt-{attempt_number:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(attempt_dir / "batch.json", batch)
        shutil.copy2(STAGE_DIR / "AGENTS.md", attempt_dir / "AGENTS.md")
        try:
            with review_semaphore:
                review = run_assigned_review(
                    batch, None, attempt_dir, args.model, global_config,
                    batch_path=attempt_dir / "batch.json")
            review_by_id, review_errors = review_item_errors(batch, review)
        except (WorkflowError, json.JSONDecodeError, KeyError) as exc:
            review_by_id = {}
            review_errors = {
                item["item_id"]: f"Semantic filtering task failed: {exc}"
                for item in active
            }
            atomic_json(attempt_dir / "controller_error.json", {
                "error": str(exc), "attempt": attempt_number})
        attempt_limit = global_config["workflow"]["capability_discovery"][
            "item_attempt_limit"]
        for assigned in active:
            item_id = assigned["item_id"]
            state = item_states[item_id]
            state["attempts"] += 1
            if item_id in review_errors:
                state.update({
                    "status": ("unfinished" if state["attempts"] >= attempt_limit
                               else "pending"),
                    "error": review_errors[item_id],
                })
                continue
            verdict = review_by_id[item_id]
            attempt_path = str(attempt_dir.relative_to(run_dir))
            if verdict["decision"] == "expose":
                state.update({
                    "status": "accepted",
                    "accepted_attempt": attempt_path,
                    "review_reason": verdict["reason"],
                    "error": "",
                })
            else:
                state.update({
                    "status": "filtered",
                    "filtered_attempt": attempt_path,
                    "review_reason": verdict["reason"],
                    "error": "",
                })
    return {
        "status": terminal_batch_status(item_states),
        "attempts": attempt_number,
        "items": item_states,
        "error": "",
    }


def save_state(run_dir, state):
    state["updated_at"] = time.time()
    if state.get("status") in {
            "complete", "complete_with_unfinished", "gate_failed"}:
        atomic_json(run_dir / "state.json", state)
        return
    statuses = {batch["status"] for batch in state["batches"]}
    if statuses <= {"accepted", "completed_with_unfinished"}:
        state["status"] = "investigation_complete"
    elif "needs_retry" in statuses:
        state["status"] = "needs_retry"
    elif "running" in statuses:
        state["status"] = "running"
    atomic_json(run_dir / "state.json", state)


def run_pending(run_dir, args):
    run_dir, inputs, state = load_run(run_dir)
    verify_frozen_inputs(inputs)
    config = load_config()
    global_config = system_config.load(args.config or inputs.get("global_config"))


    recovered = False
    for batch in state["batches"]:
        if batch.get("status") != "running":
            continue
        source_dir = run_dir / batch["path"]
        attempts = sorted(source_dir.glob("attempt-*"))
        if attempts:
            numbers = []
            latest_error = ""
            for attempt_dir in attempts:
                try:
                    numbers.append(int(attempt_dir.name.rsplit("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
                error_path = attempt_dir / "controller_error.json"
                if error_path.is_file():
                    latest_error = load_json(error_path).get("error", latest_error)
            batch["attempts"] = max(numbers or [batch.get("attempts", 0)])
            if latest_error:
                batch["error"] = latest_error
        batch["status"] = "needs_retry"
        recovered = True
    if recovered:
        save_state(run_dir, state)
    pending = [
        batch for batch in state["batches"]
        if batch["status"] not in {"accepted", "completed_with_unfinished"}
    ]
    if args.max_batches is not None:
        pending = pending[:args.max_batches]
    max_workers = min(
        global_config["workflow"]["capability_discovery"][
            "max_parallel_batches"],
        global_config["coding_agent"]["max_parallel_processes"],
        len(pending) or 1)
    review_config = global_config["workflow"]["capability_review"]
    review_semaphore = threading.Semaphore(
        min(review_config["max_parallel_batches"],
            global_config["coding_agent"]["max_parallel_processes"]))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending_iterator = iter(pending)
        futures = {}

        def submit_next():
            try:
                batch = next(pending_iterator)
            except StopIteration:
                return False
            batch["status"] = "running"
            future = executor.submit(
                investigate_batch, run_dir, batch, args, config,
                global_config, review_semaphore)
            futures[future] = batch
            return True

        for _ in range(max_workers):
            submit_next()
        save_state(run_dir, state)
        while futures:
            future = next(as_completed(tuple(futures)))
            batch = futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                batch["status"] = "needs_retry"
                batch["error"] = str(exc)
            else:
                batch.update(result)
            submit_next()
            # Only the controller thread writes the shared state file.
            verify_frozen_inputs(inputs)
            save_state(run_dir, state)
            print(
                f"[stage1] {batch['batch_id']}: {batch['status']} "
                f"(attempts={batch['attempts']})", flush=True)
    return run_dir


def accepted_results(run_dir, state):
    """Collect completed semantic expose/skip decisions."""
    results = {}
    for batch in state["batches"]:
        if batch.get("items"):
            for item_id, item_state in batch["items"].items():
                if item_state["status"] not in {"accepted", "filtered"}:
                    continue
                attempt_key = (
                    "accepted_attempt" if item_state["status"] == "accepted"
                    else "filtered_attempt")
                attempt = run_dir / item_state[attempt_key]
                report = load_json(attempt / "review_result.json")
                item = item_result(report, item_id)
                if item is None:
                    raise WorkflowError(f"Semantic filtering result is missing items: {item_id}")
                results[item_id] = {
                    **item,
                    "batch_id": batch["batch_id"],
                    "result_root": str(attempt.relative_to(run_dir)),
                }
            continue
    return results


def verify_run(run_dir):
    run_dir, inputs, state = load_run(run_dir)
    verify_frozen_inputs(inputs)
    accepted = accepted_results(run_dir, state)
    item_states = {
        item_id: item_state
        for batch in state["batches"]
        for item_id, item_state in batch.get("items", {}).items()
    }
    app_reports = {}
    all_entries = []
    gate_errors = []
    unfinished_commands = []
    for app in inputs["apps"]:
        inventory = read_jsonl(run_dir / app / "inventory.jsonl")
        summary = {
            "total": len(inventory), "reused": 0, "expose": 0,
            "skip": 0, "unfinished": 0, "missing": 0,
        }
        for item in inventory:
            if item["stage1_action"] == "reuse":
                record = {
                    **item, "stage1_status": "reused",
                    "stage1_result": None,
                }
                summary["reused"] += 1
            else:
                result = accepted.get(item["item_id"])
                if result is None:
                    state_record = item_states.get(item["item_id"], {})
                    if state_record.get("status") == "unfinished":
                        record = {
                            **item, "stage1_status": "unfinished",
                            "stage1_result": None,
                            "unfinished": state_record,
                        }
                        summary["unfinished"] += 1
                        unfinished_commands.append({
                            **item,
                            "attempts": state_record.get("attempts", 0),
                            "reason": state_record.get("error", ""),
                        })
                    else:
                        record = {
                            **item, "stage1_status": "missing",
                            "stage1_result": None,
                        }
                        summary["missing"] += 1
                        gate_errors.append(
                            f"{item['item_id']} Not yet in an accepted or unfinished terminal state")
                else:
                    decision = result["decision"]
                    record = {
                        **item, "stage1_status": decision,
                        "stage1_result": result,
                    }
                    summary[decision] += 1
            all_entries.append(record)
        app_reports[app] = summary
    output = {
        "schema_version": 1,
        "run_id": inputs["run_id"],
        "source_inputs": inputs["files"],
        "apps": app_reports,
        "gate": {
            "passed": not gate_errors,
            "errors": gate_errors,
        },
        "entries": all_entries,
    }
    atomic_json(run_dir / "stage1_discovery.json", output)
    atomic_json(run_dir / "unfinished_commands.json", {
        "schema_version": 1,
        "run_id": inputs["run_id"],
        "count": len(unfinished_commands),
        "items": unfinished_commands,
    })
    lines = [
        "# ANIS Capability discovery report", "",
        f"- Run: `{inputs['run_id']}`",
        f"- Completion check: {'passed' if not gate_errors else 'failed'}", "",
        "| Application | Total items | Reused results | Approved for exposure | Filtered out | Unfinished | Missing |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for app, summary in app_reports.items():
        lines.append(
            f"| {app} | {summary['total']} | {summary['reused']} | "
            f"{summary['expose']} | {summary['skip']} | "
            f"{summary['unfinished']} | "
            f"{summary['missing']} |")
    if unfinished_commands:
        lines.extend([
            "", "## Commands unfinished after two attempts", "",
            "These terminal items do not block other commands and will not be retried automatically.",
            "Detailed machine-readable records: `unfinished_commands.json`.", "",
        ])
        lines.extend(
            f"- {item['item_id']} `{item['symbol']}`: {item['reason']}"
            for item in unfinished_commands[:200])
    if gate_errors:
        lines.extend([
            "",
            f"## Unfinished (total: {len(gate_errors)} items; showing first 200 items)",
            "",
        ])
        lines.extend(f"- {error}" for error in gate_errors[:200])
    (run_dir / "REPORT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    state["status"] = (
        "complete_with_unfinished" if unfinished_commands and not gate_errors
        else "complete" if not gate_errors else "gate_failed")
    save_state(run_dir, state)
    return output


def print_status(run_dir):
    run_dir, inputs, state = load_run(run_dir)
    counts = {}
    for batch in state["batches"]:
        counts[batch["status"]] = counts.get(batch["status"], 0) + 1
    item_counts = {}
    for batch in state["batches"]:
        for item in batch.get("items", {}).values():
            status = item["status"]
            item_counts[status] = item_counts.get(status, 0) + 1
    print(json.dumps({
        "run_id": inputs["run_id"], "status": state["status"],
        "batch_statuses": counts, "item_statuses": item_counts,
        "run_dir": str(run_dir),
    }, indent=2, ensure_ascii=False))


def parser():
    root = argparse.ArgumentParser(
        description="ANIS Deterministic capability discovery workflow")
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Freeze inputs and create the complete task queue")
    prepare.add_argument("--apps", default="all")
    prepare.add_argument("--run-id")
    prepare.add_argument("--batch-size", type=int)
    prepare.add_argument("--config")

    run = sub.add_parser("run", help="Create or resume a run using the local Codex")
    run.add_argument("--apps", default="all")
    run.add_argument("--run-id")
    run.add_argument("--run-dir")
    run.add_argument("--batch-size", type=int)
    run.add_argument("--model")
    run.add_argument("--timeout", type=int)
    run.add_argument("--retries", type=int, default=1)
    run.add_argument("--max-batches", type=int)
    run.add_argument("--fail-fast", action="store_true")
    run.add_argument("--config")

    verify = sub.add_parser("verify", help="Aggregate results and check discovery completion")
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--config")
    status = sub.add_parser("status", help="Show resumable run status")
    status.add_argument("--run-dir", required=True)
    status.add_argument("--config")
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "prepare":
            run_dir = prepare_run(args)
            print(run_dir)
        elif args.command == "run":
            if args.run_dir:
                run_dir = Path(args.run_dir)
            else:
                run_dir = prepare_run(args)
            run_pending(run_dir, args)
            print_status(run_dir)
        elif args.command == "verify":
            output = verify_run(Path(args.run_dir))
            gate = output["gate"]
            print(json.dumps({
                "passed": gate["passed"],
                "error_count": len(gate["errors"]),
                "examples": gate["errors"][:20],
            }, indent=2, ensure_ascii=False))
            if not output["gate"]["passed"]:
                return 2
        elif args.command == "status":
            print_status(Path(args.run_dir))
    except WorkflowError as exc:
        print(f"[stage1] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
