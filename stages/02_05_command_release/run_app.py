#!/usr/bin/env python3
"""Implement isolated command batches, merge them, and verify the result."""

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


HERE = Path(__file__).resolve().parent
ANIS = HERE.parents[1]
sys.path.insert(0, str(ANIS))
from anis import system_config  # noqa: E402
from agent_guide import generate as generate_agent_guide  # noqa: E402
from family_batching import contract_error, plan_family_lanes  # noqa: E402

PROMPT = (HERE / "implement_batch.md").read_text(encoding="utf-8")
MERGE_PROMPT = (HERE / "merge_batch.md").read_text(encoding="utf-8")
IMPLEMENT_ASSIGNMENT = (HERE / "implement_assignment.md").read_text(
    encoding="utf-8")
REPAIR_CONTEXT = (HERE / "repair_context.md").read_text(encoding="utf-8")
MERGE_ASSIGNMENT = (HERE / "merge_assignment.md").read_text(encoding="utf-8")

# Increment only when the implementation/verification contract changes in a
# way that makes old terminal failures eligible for one fresh bounded run.
IMPLEMENTATION_CONTRACT_VERSION = 3


def render_text(template, values):
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def agent_command(executable, model, cwd, last_message):
    """Build one invocation so implementation and merge use one agent setup."""
    command = [
        executable, "exec", "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "danger-full-access", "--json",
        "--output-last-message", str(last_message), "-C", str(cwd), "-",
    ]
    if model:
        command[2:2] = ["--model", model]
    return command


def create_isolated_workspace(batch_dir, attempt_number, app_dir):
    """Snapshot the current app twice: immutable merge base and worker copy."""
    attempt_dir = batch_dir / "workspaces" / f"attempt-{attempt_number:02d}"
    if attempt_dir.exists():
        shutil.rmtree(attempt_dir)
    base_root = attempt_dir / "base"
    candidate_root = attempt_dir / "candidate"
    source = ANIS / "apps" / app_dir
    if not source.is_dir():
        raise FileNotFoundError(f"app source directory does not exist: {source}")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(source, base_root / "apps" / app_dir, ignore=ignore)
    shutil.copytree(source, candidate_root / "apps" / app_dir, ignore=ignore)
    return base_root, candidate_root


def merge_candidate(executable, model, app_dir, batch_id, round_items,
                    base_root, candidate_root, batch_dir, attempt_number,
                    timeout):
    """Ask the shared coding agent to three-way merge one isolated result."""
    merge_last = batch_dir / f"merge_last_message.attempt-{attempt_number:02d}.txt"
    payload = {
        "batch_id": batch_id,
        "app_dir": app_dir,
        "commands": [item["command"] for item in round_items],
        "baseline_app": str(base_root / "apps" / app_dir),
        "candidate_app": str(candidate_root / "apps" / app_dir),
        "destination_app": str(ANIS / "apps" / app_dir),
    }
    prompt = MERGE_PROMPT + render_text(MERGE_ASSIGNMENT, {
        "ASSIGNMENT_JSON": json.dumps(
            payload, indent=2, ensure_ascii=False),
    })
    command = agent_command(executable, model, ANIS, merge_last)
    try:
        result = subprocess.run(
            command, input=prompt, text=True, capture_output=True,
            timeout=timeout)
        (batch_dir / f"merge_events.attempt-{attempt_number:02d}.jsonl").write_text(
            result.stdout, encoding="utf-8")
        (batch_dir / f"merge_stderr.attempt-{attempt_number:02d}.log").write_text(
            result.stderr, encoding="utf-8")
        if result.returncode:
            return (f"merge agent exit={result.returncode}: "
                    f"{result.stderr[-2000:]}")
        return ""
    except subprocess.TimeoutExpired as exc:
        return f"merge attempt timed out after {exc.timeout} seconds"
    except Exception as exc:
        return f"merge controller error: {exc}"


def restore_rejected_commands(base_root, app_dir, rejected_commands):
    """Keep failed commands out of later full builds using the frozen baseline."""
    baseline_app = base_root / "apps" / app_dir
    destination_app = ANIS / "apps" / app_dir
    for command in rejected_commands:
        for relative in (
                Path("atlas") / f"{command}.json",
                Path("commands") / command):
            baseline = baseline_app / relative
            destination = destination_app / relative
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            if baseline.is_dir():
                shutil.copytree(baseline, destination)
            elif baseline.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(baseline, destination)


def verify_items(items, app_dir, timeout, batch_dir, attempt_number):
    """Build and verify every command independently within a merged batch."""
    failures = {}
    targets = []
    for item in items:
        command = item["command"]
        atlas = ANIS / "apps" / app_dir / "atlas" / f"{command}.json"
        if atlas.is_file():
            targets.append(item)
        else:
            failures[command] = "missing atlas cell"
    build_logs = []
    verification = []
    for item in targets:
        command = item["command"]
        try:
            build = subprocess.run(
                ["python3", "anis/build.py", "--app", app_dir,
                 "--only", command],
                cwd=ANIS, capture_output=True, text=True, timeout=timeout)
            build_log = build.stdout + build.stderr
        except subprocess.TimeoutExpired:
            build = None
            build_log = f"build timed out after {timeout} seconds"
        build_logs.append(f"## {command}\n{build_log}")
        if build is None or build.returncode:
            failures[command] = f"build failed: {build_log[-4000:]}"
            continue
        try:
            check = subprocess.run(
                ["python3", "anis/verify.py", "--app", app_dir,
                 "--only", command],
                cwd=ANIS, capture_output=True, text=True, timeout=timeout)
            log = check.stdout + check.stderr
        except subprocess.TimeoutExpired:
            log = f"verification timed out after {timeout} seconds"
            failures[command] = log
            verification.append(f"## {command}\n{log}")
            continue
        verification.append(f"## {command}\n{log}")
        if check.returncode:
            failures[command] = (
                f"verification failed for {command}:\n{log[-4000:]}")
    (batch_dir / f"build.attempt-{attempt_number:02d}.log").write_text(
        "\n".join(build_logs), encoding="utf-8")
    (batch_dir / f"verify.attempt-{attempt_number:02d}.log").write_text(
        "\n".join(verification), encoding="utf-8")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--config")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--parallel-batches", type=int)
    parser.add_argument("--parallel-agents", type=int)
    parser.add_argument("--model")
    parser.add_argument(
        "--timeout", type=int,
        help="Maximum seconds per implementation attempt; defaults from anis-config.json")
    parser.add_argument(
        "--verify-timeout", type=int,
        help="Maximum seconds per build or verification; timeout fails only the current command")
    parser.add_argument(
        "--max-retries", type=int,
        help="Retries after the first failure; default1; maximum attempts per command:2attempts")
    parser.add_argument("--max-batches", type=int)
    parser.add_argument(
        "--skip-documentation", action="store_true",
        help="Skip command documentation for partial development runs")
    args = parser.parse_args()

    global_config = system_config.load(args.config)
    workflow_config = global_config["workflow"]["command_implementation"]
    agent_config = global_config["coding_agent"]
    batch_size = args.batch_size or workflow_config["batch_size"]
    parallel_batches = (
        args.parallel_batches or workflow_config["max_parallel_batches"])
    parallel_agents = (
        args.parallel_agents or agent_config["max_parallel_processes"])
    max_workers = min(parallel_batches, parallel_agents)
    timeout = args.timeout or workflow_config["timeout_seconds"]
    verify_timeout = (
        args.verify_timeout or workflow_config["verify_timeout_seconds"])
    max_retries = (workflow_config["max_retries"]
                   if args.max_retries is None else args.max_retries)
    model = args.model or agent_config.get("model")
    executable = agent_config["command"]
    documentation_config = global_config["workflow"].get(
        "command_documentation", {})
    documentation_timeout = documentation_config.get(
        "timeout_seconds", timeout)
    documentation_enabled = documentation_config.get("enabled", True)

    run_dir = HERE / "runs" / args.run_id / args.app
    manifest = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8"))
    operations = manifest["operations"]
    state_path = run_dir / "state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8"))
             if state_path.is_file() else {
                 "completed": [], "failed": {}, "failed_commands": {},
                 "command_attempts": {}, "last_errors": {},
                 "implementation_contract_version": IMPLEMENTATION_CONTRACT_VERSION})
    state.setdefault("failed_commands", {})
    state.setdefault("command_attempts", {})
    state.setdefault("last_errors", {})
    # Freeze the first complete command order. A later --only-needed manifest
    # may shrink, but must never renumber UI batches or hide accepted commands.
    universe = list(state.get("command_universe", []))
    known_commands = set(universe)
    for item in operations:
        command = item.get("command", "")
        if command and command not in known_commands:
            universe.append(command)
            known_commands.add(command)
    for command in list(state.get("completed", [])) + list(
            state.get("failed_commands", {})):
        if command and command not in known_commands:
            universe.append(command)
            known_commands.add(command)
    state["command_universe"] = universe
    session_id = f"implementation-{time.time_ns()}"
    state["implementation_session_id"] = session_id
    old_contract = int(state.get("implementation_contract_version", 1))
    if old_contract < IMPLEMENTATION_CONTRACT_VERSION:
        old_failures = dict(state["failed_commands"])
        if old_failures:
            state.setdefault("failure_history", []).append({
                "contract_version": old_contract,
                "reason": "Object-family contract updated; previous failures receive a new bounded retry",
                "failed_commands": old_failures,
            })
            for command, failure in old_failures.items():
                state["command_attempts"][command] = 0
                state["last_errors"][command] = failure.get("reason", "")
            state["failed_commands"] = {}
        state["implementation_contract_version"] = IMPLEMENTATION_CONTRACT_VERSION
    completed = set(state["completed"])
    max_attempts = max_retries + 1
    # A controller interruption may happen after an attempt was durably
    # counted but before its outcome was written.  Never turn that into an
    # accidental third attempt on resume.
    for item in operations:
        command = item["command"]
        attempts = int(state["command_attempts"].get(command, 0))
        if (command not in completed
                and command not in state["failed_commands"]
                and attempts >= max_attempts):
            state["failed_commands"][command] = {
                "batch_id": "interrupted",
                "attempts": attempts,
                "reason": "attempt ended before the controller recorded an outcome",
            }
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    planned_lanes = plan_family_lanes(operations, batch_size)
    remaining_limit = args.max_batches
    lanes = []
    for planned_lane in planned_lanes:
        lane = []
        for planned in planned_lane:
            if remaining_limit is not None and remaining_limit <= 0:
                break
            batch_id = f"{args.app}-{planned['number']:02d}"
            batch = [
                item for item in planned["items"]
                if item["command"] not in completed
                and item["command"] not in state["failed_commands"]]
            if not batch:
                print(f"[skip] {batch_id}")
                continue
            lane.append((batch_id, batch, {
                "object_families": planned["families"],
                "family_part": planned["family_part"],
                "family_parts": planned["family_parts"],
                "family_contract": (
                    f"apps/{manifest['app_dir']}/"
                    f"{planned['family_contract']}"
                    if planned["family_contract"] else None),
            }))
            if remaining_limit is not None:
                remaining_limit -= 1
        if lane:
            lanes.append(lane)
        if remaining_limit is not None and remaining_limit <= 0:
            break

    state_lock = threading.Lock()
    # Candidate workers never touch the shared app. Merging and acceptance are
    # serialized so every merge sees the latest accepted destination and the
    # destination cannot change during its mechanical verification.
    acceptance_lock = threading.Lock()
    # Implementation and merge calls share one global Agent process budget.
    agent_slots = threading.Semaphore(parallel_agents)

    def write_state():
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    def run_batch(batch_id, batch, family_context):
        batch_dir = run_dir / batch_id
        batch_dir.mkdir(exist_ok=True)
        status_path = batch_dir / "status.json"

        def write_batch_status(status, phase, **extra):
            status_path.write_text(json.dumps({
                "session_id": session_id,
                "batch_id": batch_id,
                "status": status,
                "phase": phase,
                "updated_at": time.time(),
                **extra,
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        write_batch_status("running", "Preparing isolated workspace",
                           command_count=len(batch), attempt=0)
        events = batch_dir / "codex_events.jsonl"
        last = batch_dir / "last_message.txt"
        active = {item["command"]: item for item in batch}
        failure_reasons = {
            command: state["last_errors"][command]
            for command in active if command in state["last_errors"]}
        round_number = 0
        while active:
            round_number += 1
            round_items = list(active.values())
            write_batch_status(
                "running", "Isolated implementation Agent is running",
                command_count=len(batch), active=len(round_items),
                attempt=round_number)
            payload = {
                "batch_id": batch_id,
                "app": args.app,
                "app_dir": manifest["app_dir"],
                "reference_anis_root": str(ANIS),
                "operations": round_items,
                **family_context,
            }
            (batch_dir / f"batch.attempt-{round_number:02d}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            # Keep batch.json as a convenient pointer to the latest, shrinking
            # attempt.  It never contains commands already accepted.
            (batch_dir / "batch.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            with state_lock:
                for item in round_items:
                    command = item["command"]
                    state["command_attempts"][command] = (
                        int(state["command_attempts"].get(command, 0)) + 1)
                write_state()
            repair_context = ""
            if failure_reasons:
                repair_context = render_text(REPAIR_CONTEXT, {
                    "FAILURES_JSON": json.dumps(
                        failure_reasons, indent=2, ensure_ascii=False),
                })
            prompt = PROMPT + render_text(IMPLEMENT_ASSIGNMENT, {
                "BATCH_JSON": json.dumps(
                    payload, indent=2, ensure_ascii=False),
                "REPAIR_CONTEXT": repair_context,
            })
            result = None
            codex_failure = ""
            try:
                # Snapshot only while no merge can mutate the source. The lock
                # is released before the Agent starts, so workers still run in
                # parallel after receiving a coherent baseline.
                with acceptance_lock:
                    base_root, candidate_root = create_isolated_workspace(
                        batch_dir, round_number, manifest["app_dir"])
            except Exception as exc:
                base_root = candidate_root = None
                codex_failure = f"could not create isolated workspace: {exc}"
            agent_slots.acquire()
            try:
                if candidate_root is not None:
                    implementation_command = agent_command(
                        executable, model, candidate_root, last)
                    result = subprocess.run(
                        implementation_command,
                        input=prompt, text=True, capture_output=True,
                        timeout=timeout)
                    events.with_name(
                        f"codex_events.attempt-{round_number:02d}.jsonl"
                    ).write_text(result.stdout, encoding="utf-8")
                    (batch_dir / f"stderr.attempt-{round_number:02d}.log").write_text(
                        result.stderr, encoding="utf-8")
                    if result.returncode != 0:
                        codex_failure = (
                            f"Codex exit={result.returncode}: "
                            f"{result.stderr[-2000:]}")
            except subprocess.TimeoutExpired as exc:
                codex_failure = f"attempt timed out after {exc.timeout} seconds"
            except Exception as exc:
                codex_failure = f"controller error: {exc}"
            finally:
                agent_slots.release()

            # Python serializes the Agent merge and the acceptance check.
            # Isolated implementation workers may continue in parallel because
            # they cannot mutate the shared destination app.
            with acceptance_lock:
                merge_failure = ""
                write_batch_status(
                    "running", "Merge Agent is merging implementations",
                    command_count=len(batch), active=len(round_items),
                    attempt=round_number)
                if candidate_root is not None:
                    agent_slots.acquire()
                    try:
                        merge_failure = merge_candidate(
                            executable, model, manifest["app_dir"], batch_id,
                            round_items, base_root, candidate_root, batch_dir,
                            round_number, timeout)
                    finally:
                        agent_slots.release()
                write_batch_status(
                    "running", "Python is building and verifying each command",
                    command_count=len(batch), active=len(round_items),
                    attempt=round_number)
                round_failures = verify_items(
                    round_items, manifest["app_dir"], verify_timeout,
                    batch_dir, round_number)
                contract_path = family_context.get("family_contract")
                if contract_path:
                    try:
                        contract = json.loads(
                            (ANIS / contract_path).read_text(encoding="utf-8"))
                        family_failure = contract_error(contract)
                    except (OSError, json.JSONDecodeError) as exc:
                        family_failure = (
                            f"object-family contract unavailable: {exc}")
                    if family_failure:
                        for item in round_items:
                            round_failures.setdefault(
                                item["command"], family_failure)
                if base_root is not None:
                    restore_rejected_commands(
                        base_root, manifest["app_dir"], round_failures)
            if codex_failure:
                for command in round_failures:
                    round_failures[command] += f"\n{codex_failure}"
            if merge_failure:
                for command in round_failures:
                    round_failures[command] += f"\n{merge_failure}"

            passed = [
                item["command"] for item in round_items
                if item["command"] not in round_failures]
            with state_lock:
                completed.update(passed)
                state["completed"] = sorted(completed)
                for command in passed:
                    state["failed_commands"].pop(command, None)
                    state["last_errors"].pop(command, None)
                    active.pop(command, None)
                failure_reasons = {}
                for command, reason in round_failures.items():
                    attempts = int(state["command_attempts"].get(command, 0))
                    if attempts >= max_attempts:
                        state["failed_commands"][command] = {
                            "batch_id": batch_id,
                            "attempts": attempts,
                            "reason": reason,
                        }
                        state["last_errors"].pop(command, None)
                        active.pop(command, None)
                    else:
                        failure_reasons[command] = reason
                        state["last_errors"][command] = reason
                write_state()
            if failure_reasons:
                write_batch_status(
                    "running", "Preparing bounded retries for failed commands",
                    command_count=len(batch), active=len(failure_reasons),
                    accepted=len(passed), attempt=round_number)
                print(
                    f"[retry] {batch_id}: {len(passed)} accepted, "
                    f"{len(failure_reasons)} retrying")
        batch_passed = [
            item["command"] for item in batch
            if item["command"] in completed]
        batch_unfinished = [
            item["command"] for item in batch
            if item["command"] in state["failed_commands"]]
        if result is not None:
            events.write_text(result.stdout, encoding="utf-8")
            (batch_dir / "stderr.log").write_text(
                result.stderr, encoding="utf-8")
        write_batch_status(
            "completed_with_unfinished" if batch_unfinished else "accepted",
            "Batch completed", command_count=len(batch),
            accepted=len(batch_passed), failed=len(batch_unfinished),
            active=0, attempt=round_number)
        print(
            f"[terminal] {batch_id}: {len(batch_passed)} accepted, "
            f"{len(batch_unfinished)} unfinished")

    def run_lane(lane):
        # Shards of one oversized family are sequential so each new workspace
        # sees the contract and resolver merged by the preceding shard.
        for batch_id, batch, family_context in lane:
            run_batch(batch_id, batch, family_context)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_lane, lane): lane[0][0]
            for lane in lanes
        }
        for future in as_completed(futures):
            future.result()

    all_terminal = all(
        item["command"] in completed
        or item["command"] in state["failed_commands"]
        for item in operations)
    if not all_terminal:
        print(json.dumps({
            "app": args.app,
            "total": len(operations),
            "completed": len(completed),
            "failed_batches": state["failed"],
            "status": "paused",
        }, ensure_ascii=False))
        return

    skip_phase = ("Command documentation disabled in configuration"
                  if not documentation_enabled else
                  "Command documentation skipped by command-line option"
                  if args.skip_documentation else
                  "Partial run did not generate command documentation")
    documentation = {"status": "skipped", "phase": skip_phase}
    if (documentation_enabled and args.max_batches is None
            and not args.skip_documentation):
        implemented_names = [
            item["command"] for item in operations
            if item["command"] in completed
        ]
        documentation = generate_agent_guide(
            args.app, manifest["app_dir"], run_dir, executable, model,
            documentation_timeout, command_names=implemented_names)
        with state_lock:
            state["documentation"] = documentation
            write_state()

    # Building, verification and freezing are separate outer workflow stages.
    # This worker only implements commands and records per-command outcomes;
    # it must not publish early or introduce a second release decision.
    print(json.dumps({
        "app": args.app,
        "total": len(operations),
        "completed": len(completed),
        "failed_batches": state["failed"],
        "documentation": documentation,
        "status": ("complete_with_unfinished"
                   if state["failed"] or state["failed_commands"]
                   else "complete_with_documentation_error"
                   if documentation.get("status") == "failed"
                   else "complete"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
