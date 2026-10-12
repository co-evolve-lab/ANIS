import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import runtime  # noqa: E402
import system_config  # noqa: E402

PROMPTS = os.path.join(ROOT, "prompts")
RETRY_PROMPT = os.path.join(PROMPTS, "retry_feedback.md")
DEFAULT_LLM_TIMEOUT = 7200
DEFAULT_SCRIPT_TIMEOUT = 10800
DEFAULT_MAX_ATTEMPTS = 3
ACTIVE_CONFIG = None

STAGE1 = os.path.join(ROOT, "stages", "01_capability_discovery", "workflow.py")
STAGE2_DIR = os.path.join(ROOT, "stages", "02_05_command_release")


STAGE1_RESULT_ROOTS = (
    os.path.join(ROOT, "stages", "01_capability_discovery", "runs"),
)


BACKLOG_STATUS = ("candidate", "not_generated_with_reason",
                  "partially_generated_with_reason")


class StageFailed(Exception):
    def __init__(self, stage, detail):
        super().__init__(f"[{stage}] {detail}")
        self.stage = stage
        self.detail = detail


def app_dir(app):
    return os.path.join(ROOT, "apps", app)


def work_dir(app):
    path = os.path.join(app_dir(app), "onboard")
    os.makedirs(path, exist_ok=True)
    return path


def state_path(app):
    return os.path.join(work_dir(app), "state.json")


def load_state(app):
    path = state_path(app)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {"app": app, "done": [], "history": []}


def new_workflow_run_id(app):
    """Return a unique run ID for an explicitly fresh onboarding run."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    suffix = time.time_ns() % 1_000_000_000
    return f"onboard-{app}-{stamp}-{suffix:09d}"


def start_fresh_state(app):
    """Archive the current progress record and create independent progress."""
    path = state_path(app)
    if os.path.isfile(path):
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        archive = os.path.join(
            work_dir(app), f"state.before-{stamp}-{time.time_ns()}.json")
        os.replace(path, archive)
        log(f"Archived previous progress: {archive}")
    state = {
        "app": app,
        "done": [],
        "history": [],
        "workflow_run_id": new_workflow_run_id(app),
    }
    save_state(app, state)
    return state


def save_state(app, state):
    tmp = state_path(app) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(app))


def log(message):
    print(f"[onboard] {message}", flush=True)


def run_script(command, env=None, timeout=None, cwd=ROOT):
    config = ACTIVE_CONFIG or system_config.load()
    timeout = timeout or config["workflow"]["onboarding"][
        "script_timeout_seconds"]
    log("Run " + " ".join(str(part) for part in command))
    try:
        completed = subprocess.run(
            [str(part) for part in command], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Timed out ({timeout} seconds)"
    except FileNotFoundError as error:
        return False, f"Executable not found: {error}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output


def invoke_model(prompt, app, tag, config, timeout=None):
    logs = os.path.join(work_dir(app), "logs")
    os.makedirs(logs, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    last_message = os.path.join(logs, f"{tag}-{stamp}.txt")
    events = os.path.join(logs, f"{tag}-{stamp}.jsonl")

    agent = config["coding_agent"]
    timeout = timeout or config["workflow"]["onboarding"][
        "agent_timeout_seconds"]
    command = [
        shutil.which(agent["command"]) or agent["command"], "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "danger-full-access", "--json",
        "--output-last-message", last_message,
        "-C", ROOT, "-",
    ]
    if agent.get("model"):
        command[2:2] = ["--model", agent["model"]]
    log(f"Invoking model: {tag}(prompt: {len(prompt)} characters)")
    with open(events, "w", encoding="utf-8") as event_handle:
        try:
            completed = subprocess.run(
                command, input=prompt, text=True, stdout=event_handle,
                stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"Model timed out ({timeout} seconds); log: {events}"
    if completed.returncode != 0:
        return False, (f"Model failed with exit code {completed.returncode}\n"
                       f"{(completed.stderr or '')[-2000:]}\nLog {events}")
    summary = ""
    if os.path.isfile(last_message):
        with open(last_message, encoding="utf-8") as handle:
            summary = handle.read()
    return True, summary


def read_prompt(name, app, launch=None):
    path = os.path.join(PROMPTS, name)
    if not os.path.isfile(path):
        raise StageFailed("prompt", f"Missing prompt template {path}")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("{app}", app)
    text = text.replace("{launch}", launch or "Not specified; discover locally")
    return text


def check_runtime_declaration(app):
    try:
        command = runtime.build_command(app, os.path.join(
            app_dir(app), "census.py"))
    except runtime.RuntimeError_ as error:
        return False, str(error)
    binary = command[0]
    if not (os.path.isabs(binary) and os.path.isfile(binary)) \
            and not shutil.which(binary):
        return False, (f"The runner {binary!r} declared in runtime.json was not found "
                       "on this machine. Verify that it runs before declaring it.")
    return True, f"Collector runner available: {' '.join(command)}"


def check_file_exists(app, relative, what):
    path = os.path.join(app_dir(app), relative)
    if not os.path.isfile(path):
        return False, f"Missing {path}({what})"
    return True, f"{relative} is ready"


def check_app_adapter(app):
    """Require every application-owned input needed by later generic stages."""
    required = (
        ("engine.py", "engine adapter"),
        ("anis-app.json", "application contract"),
    )
    for relative, what in required:
        passed, detail = check_file_exists(app, relative, what)
        if not passed:
            return passed, detail
    try:
        from anis import app_contract
        app_contract.load(app)
    except Exception as error:
        return False, f"anis-app.json failed structural validation: {error}"
    probe = os.path.join(app_dir(app), "atlas_gen.py")
    atlas_ok, _ = check_atlas_not_empty(app)
    if not os.path.isfile(probe) and not atlas_ok:
        return False, ("Neither atlas_gen.py nor existing command definitions were found; "
                       "the adapter must provide a source of command candidates")
    source = "atlas_gen.py" if os.path.isfile(probe) else "existing atlas command definitions"
    return True, f"engine.py, anis-app.json and {source} are ready and the contract is valid"


def run_atlas_probe(app):
    """Run an app-owned probe, while preserving already-materialized adapters."""
    script = os.path.join(app_dir(app), "atlas_gen.py")
    if not os.path.isfile(script):
        passed, detail = check_atlas_not_empty(app)
        return (passed, "No probe script; using validated existing command definitions: " + detail
                if passed else detail)
    return run_script(
        runtime.build_command(app, script), env=runtime.build_env(app))


def check_conformance(app):
    ok, output = run_script(
        [sys.executable, os.path.join(HERE, "conformance.py"), "--app", app])
    return ok, output


def check_atlas_not_empty(app):
    directory = os.path.join(app_dir(app), "atlas")
    if not os.path.isdir(directory):
        return False, f"Missing {directory} directory; probe produced no command definitions"
    count = len([n for n in os.listdir(directory) if n.endswith(".json")])
    if count == 0:
        return False, "atlas directory is empty; probe produced no command definitions"
    return True, f"command definitions {count} files"


def check_commands_built(app):
    directory = os.path.join(app_dir(app), "commands")
    if not os.path.isdir(directory):
        return False, f"Missing {directory} directory"
    count = len([n for n in os.listdir(directory)
                 if os.path.isfile(os.path.join(directory, n, "spec.json"))])
    if count == 0:
        return False, "commands directory contains no commands with spec.json command"
    return True, f"command {count} records"


def check_verify_report(app, min_pass_rate):
    path = os.path.join(app_dir(app), "build", "report.json")
    if not os.path.isfile(path):
        return False, f"No verification report {path}"
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)


    results = {name: item for name, item in report.items()
               if isinstance(item, dict) and "pass" in item}
    total = len(results)
    if total == 0:
        return False, f"Verification report {path} contains no command records"

    passed = sum(1 for item in results.values() if item.get("pass"))
    rate = passed / total
    detail = f"Verify {total} commands; passed {passed} commands ({rate:.1%})"
    if passed == 0:
        return False, detail + "; no verified commands are available for release"
    if rate >= min_pass_rate:
        return True, detail


    clusters = {}
    for name, item in results.items():
        if item.get("pass"):
            continue
        for gate, info in (item.get("gates") or {}).items():
            if info.get("pass"):
                continue
            text = str(info.get("detail", ""))
            key = (gate, text[:110])
            clusters.setdefault(key, []).append(name)

    lines = []
    for (gate, text), names in sorted(clusters.items(),
                                      key=lambda kv: -len(kv[1]))[:12]:
        lines.append(f"  [{gate}] {len(names)} : {text}")
        lines.append(f"      for example {', '.join(sorted(names)[:4])}")
    return True, (detail + f", below reference rate {min_pass_rate:.0%}; "
                  "Failed commands will be recorded and excluded; passing commands will be released.\n"
                  "Failures grouped by check and error message: \n"
                  + "\n".join(lines))


def stage_name(app):
    """Resolve historical workflow aliases from data, not core conditionals."""
    aliases = (ACTIVE_CONFIG or system_config.load()).get("app_aliases", {})
    reverse = {value: key for key, value in aliases.items()}
    return reverse.get(app, app)


def backlog_symbols(app):
    path = os.path.join(app_dir(app), "capabilities", "ledger.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        ledger = json.load(handle)
    return {entry["symbol"] for entry in ledger.get("entries", [])
            if entry.get("status") in BACKLOG_STATUS}


def investigated_symbols(app):
    """Return symbols that received a semantic expose/skip decision."""
    wanted = stage_name(app)
    done = set()
    app_runs = []
    for results_root in STAGE1_RESULT_ROOTS:
        if not os.path.isdir(results_root):
            continue
        for run in sorted(os.listdir(results_root)):
            candidate = os.path.join(results_root, run, wanted)
            if os.path.isdir(candidate):
                app_runs.append(candidate)
    for app_run in app_runs:
        for root, _, files in os.walk(app_run):
            if "batch.json" not in files or "review_result.json" not in files:
                continue
            try:
                with open(os.path.join(root, "batch.json"),
                          encoding="utf-8") as handle:
                    batch = json.load(handle)
                with open(os.path.join(root, "review_result.json"),
                          encoding="utf-8") as handle:
                    review = json.load(handle)
            except (OSError, ValueError):
                continue
            approved_ids = {
                item["item_id"] for item in review.get("items", [])
                if (item.get("decision") in {"expose", "skip"}
                    or item.get("verdict") == "accept")}
            for item in batch.get("items", []):
                if item.get("item_id") in approved_ids and item.get("symbol"):
                    done.add(item["symbol"])
    return done


def release_approved_symbols(app):
    """Return symbols whose semantic decision is expose."""
    wanted = stage_name(app)
    approved = set()
    app_runs = []
    for results_root in STAGE1_RESULT_ROOTS:
        if not os.path.isdir(results_root):
            continue
        for run in sorted(os.listdir(results_root)):
            candidate = os.path.join(results_root, run, wanted)
            if os.path.isdir(candidate):
                app_runs.append(candidate)
    for app_run in app_runs:
        for root, _, files in os.walk(app_run):
            if "batch.json" not in files or "review_result.json" not in files:
                continue
            try:
                with open(os.path.join(root, "batch.json"),
                          encoding="utf-8") as handle:
                    batch = json.load(handle)
                with open(os.path.join(root, "review_result.json"),
                          encoding="utf-8") as handle:
                    review = json.load(handle)
            except (OSError, ValueError):
                continue
            reviewed_by_id = {
                item.get("item_id"): item for item in review.get("items", [])}
            for assigned in batch.get("items", []):
                item_id = assigned.get("item_id")
                reviewed = reviewed_by_id.get(item_id, {})
                decision = (reviewed.get("decision")
                            or reviewed.get("release_decision"))
                if decision == "expose":
                    if assigned.get("symbol"):
                        approved.add(assigned["symbol"])
    return approved


def atlas_symbols(app):
    directory = os.path.join(app_dir(app), "atlas")
    found = set()
    if not os.path.isdir(directory):
        return found
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                cell = json.load(handle)
        except (OSError, ValueError):
            continue
        symbol = (cell.get("census_ref") or {}).get("symbol")
        if symbol:
            found.add(symbol)
    return found


def check_backlog_listed(app):
    backlog = backlog_symbols(app)
    if backlog is None:
        return False, "No pending ledger; run the ledger script to list census capabilities"
    path = os.path.join(app_dir(app), "capabilities", "ledger.json")
    with open(path, encoding="utf-8") as handle:
        ledger = json.load(handle)
    total = sum(entry.get("status") in BACKLOG_STATUS
                for entry in ledger.get("entries", []))
    return True, f"Candidates {total} listed; next step is semantic filtering"


def check_backlog_investigated(app):
    """Every candidate label must receive an expose/skip decision."""
    backlog = backlog_symbols(app)
    if backlog is None:
        return False, "No pending ledger"
    remaining = backlog - investigated_symbols(app)
    if not remaining:
        return True, f"Candidate names {len(backlog)} all have semantic filtering decisions"
    sample = ", ".join(sorted(remaining)[:6])
    return False, (f"Pending {len(backlog)} items include {len(remaining)} items without "
                   f"filtering decisions; for example: {sample}")


def check_accepted_implemented(app, run_id=None):
    """Require recorded outcomes for the current implementation work order."""
    run_id = run_id or f"onboard-{app}"
    directory = os.path.join(STAGE2_DIR, "runs", run_id, stage_name(app))
    try:
        with open(os.path.join(directory, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as handle:
            state = json.load(handle)
        if manifest.get("run_id") != run_id or manifest.get("app") != stage_name(app):
            return False, "Implementation work order belongs to a different run or application"
        planned = {item["command"] for item in manifest["operations"]}
        # Preparation can omit already verified commands on resume. Preserve the
        # controller's complete order when checking whether work remains.
        planned.update(state["command_universe"])
        completed = set(state["completed"])
        failed = set(state["failed_commands"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        return False, f"Implementation outcomes are not ready for {run_id}: {error}"
    pending = planned - completed - failed
    if pending:
        return False, (f"Implementation pending for {len(pending)}/{len(planned)} commands; "
                       f"for example: {', '.join(sorted(pending)[:6])}")
    return True, (f"Implementation attempted all {len(planned)} planned commands; "
                  f"{len(planned & completed)} accepted, {len(planned & failed)} failed")


def run_discovery(app, config, run_id):
    """Run batched semantic filtering without launching the target app."""
    run_dir = os.path.join(STAGE1_RESULT_ROOTS[0], run_id)
    command = [sys.executable, STAGE1, "run"]
    if os.path.isdir(run_dir):
        log(f"Resuming existing discovery run: {run_dir}")
        command.extend(["--run-dir", run_dir])
    else:
        command.extend([
            "--apps", stage_name(app), "--run-id", run_id])
    command.extend(["--config", config["_path"]])
    return run_script(command)


def run_release(app, config, run_id):
    ok, output = run_script(
        [sys.executable, os.path.join(STAGE2_DIR, "prepare.py"),
         "--all-accepted", "--apps", stage_name(app),
         "--run-id", run_id, "--discovery-run", run_id,
         "--config", config["_path"]],
        cwd=STAGE2_DIR)
    if not ok:
        return False, output
    ok2, output2 = run_script(
        [sys.executable, os.path.join(STAGE2_DIR, "run_app.py"),
         "--app", stage_name(app), "--run-id", run_id,
         "--config", config["_path"], "--skip-documentation"],
        cwd=STAGE2_DIR,
        timeout=config["workflow"]["command_implementation"].get(
            "total_timeout_seconds", 172800))
    return ok2, output + "\n" + output2


def check_dist(app):
    directory = os.path.join(ROOT, "dist", f"{app}-anis", "commands")
    if not os.path.isdir(directory):
        return False, f"Missing {directory}"
    count = len(os.listdir(directory))
    if count == 0:
        return False, "Release contains no verified commands"
    launcher = os.path.join(ROOT, "bin", f"{app}-anis")
    if not os.path.isfile(launcher):
        return False, f"Missing launcher {launcher}"
    return True, f"Freeze {count} commands; launcher {launcher}"


def stages(app, launch, min_pass_rate, config, run_id=None):
    run_id = run_id or f"onboard-{app}"

    return [
        {
            "name": "1-Discover programmatic entrypoint",
            "owner": "Model",
            "prompt": "probe_runtime.md",
            "check": lambda: check_runtime_declaration(app),
            "what": "Find a queryable programmatic entrypoint and declare how to run the generated Collector",
        },
        {
            "name": "2-Write census collector",
            "owner": "Model",
            "prompt": "census_adapter.md",
            "check": lambda: check_file_exists(app, "census.py", "capability census collector"),
            "what": "Enumerate all capabilities reported by the application runtime",
        },
        {
            "name": "3-Run census",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "census.py"),
                 "--app", app]),
            "check": lambda: check_file_exists(
                app, os.path.join("census", "raw_ops.json"), "census output"),
        },
        {
            "name": "4-Validate census",
            "owner": "Script",
            "check": lambda: check_conformance(app),
            "on_fail_redo": "2-Write census collector",
        },
        {
            "name": "5-Write engine and probe",
            "owner": "Model",
            "prompt": "build_app.md",
            "check": lambda: check_app_adapter(app),
            "what": "Application-owned engine.py, atlas_gen.py and anis-app.json",
        },
        {
            "name": "6-Run probe",
            "owner": "Script",
            "run": lambda: run_atlas_probe(app),
            "check": lambda: check_atlas_not_empty(app),
            "on_fail_redo": "5-Write engine and probe",
        },
        {
            "name": "7-List pending capabilities",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "ledger.py"),
                 "--app", app]),
            "check": lambda: check_backlog_listed(app),
            "what": "List census capabilities not yet implemented as model work orders",
        },
        {
            "name": "8-Filter capability batches",
            "owner": "Model",
            "run": lambda: run_discovery(app, config, run_id),
            "basis": "stages/01 prompts with batching and retries",
            "check": lambda: check_backlog_investigated(app),
            "what": "Classify candidates by user value as expose or skip without running the application",
        },
        {
            "name": "9-Implement commands",
            "owner": "Model",
            "run": lambda: run_release(app, config, run_id),
            "basis": "stages/02_05/implement_batch.md",
            "check": lambda: check_accepted_implemented(app, run_id),
            "recheck_on_resume": True,
            "what": "Implement approved capabilities as command definitions and engine code",
        },
        {
            "name": "10-Render commands",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "build.py"),
                 "--app", app]),
            "check": lambda: check_commands_built(app),
        },
        {
            "name": "11-Reconcile capability ledger",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "ledger.py"),
                 "--app", app]),
            "check": lambda: check_file_exists(
                app, os.path.join("capabilities", "ledger.json"), "capability ledger"),
        },
        {
            "name": "12-Verify commands",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "verify.py"),
                 "--app", app, "--jobs",
                 str(config["workflow"]["verification"]["jobs"])]),
            "check": lambda: check_verify_report(app, min_pass_rate),
            "on_fail_redo": "5-Write engine and probe",
        },
        {
            "name": "13-Generate command documentation and Skill",
            "owner": "Model",
            "run": lambda: run_script(
                [sys.executable, os.path.join(
                    STAGE2_DIR, "agent_guide.py"),
                 "--app", app, "--run-id", run_id,
                 "--config", config["_path"]],
                cwd=STAGE2_DIR),
            "basis": "stages/02_05/document_commands.md",
            "check": lambda: check_file_exists(
                app, os.path.join("guide", "commands.json"),
                "command guide"),
        },
        {
            "name": "14-Freeze as CLI",
            "owner": "Script",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "freeze.py"),
                 "--app", app]),
            "check": lambda: check_dist(app),
        },
    ]


def stage1_discovery(steps):
    """Adapter onboarding, runtime census, probing, and deterministic ledger."""
    return steps[:7]


def stage2_filter(steps):
    """Semantic exposure filtering only."""
    return steps[7:8]


def stage3_implementation(steps):
    """Isolated implementation, rendering, ledger reconciliation, verification."""
    return steps[8:12]


def stage4_organization_and_release(steps):
    """Organized documentation, Agent skill, and deterministic release."""
    return steps[12:]


def workflow_stages(app, launch, min_pass_rate, config, run_id=None):
    """Expose the complete workflow as four auditable software-neutral stages."""
    steps = stages(app, launch, min_pass_rate, config, run_id=run_id)
    return [
        {"name": "Stage 1 · Capability discovery", "steps": stage1_discovery(steps)},
        {"name": "Stage 2 · Semantic filtering", "steps": stage2_filter(steps)},
        {"name": "Stage 3 · Command implementation", "steps": stage3_implementation(steps)},
        {"name": "Stage 4 · Documentation and release",
         "steps": stage4_organization_and_release(steps)},
    ]


def run_stage(stage, app, launch, max_attempts, config, force=False):

    name = stage["name"]
    feedback = stage.pop("_feedback", "")

    if not force and not feedback:
        passed, detail = stage["check"]()
        if passed:
            log(f"── {name} Outputs ready; skipping: {detail}")
            return True, detail

    for attempt in range(1, max_attempts + 1):
        log(f"── {name}({stage['owner']}) attempt {attempt}/{max_attempts} attempts")


        if stage.get("prompt"):
            prompt = read_prompt(stage["prompt"], app, launch)
            if feedback:
                with open(RETRY_PROMPT, encoding="utf-8") as handle:
                    retry_prompt = handle.read()
                prompt += retry_prompt.replace(
                    "{{FEEDBACK}}", feedback[-8000:])
            ok, output = invoke_model(prompt, app, tag=name, config=config)
            if not ok:
                feedback = output
                log(f"   Model execution failed: {output[:400]}")
                continue
        elif stage.get("run"):
            ok, output = stage["run"]()
            if not ok:
                feedback = output
                log(f"   Script failed: {output[-1500:]}")
                if not stage.get("on_fail_redo"):
                    continue

        passed, detail = stage["check"]()
        if passed:
            log(f"   Passed: {detail}")
            return True, detail
        feedback = detail
        log(f"   Validation failed: {detail[:1500]}")

        if stage.get("on_fail_redo") and not stage.get("prompt"):
            return False, detail

    return False, feedback


def onboard(app, launch, only=None, max_attempts=DEFAULT_MAX_ATTEMPTS,
            min_pass_rate=1.0, redo_limit=2, force=False,
            config=None, state=None):
    global ACTIVE_CONFIG
    config = config or system_config.load()
    ACTIVE_CONFIG = config
    state = state or load_state(app)
    run_id = state.setdefault("workflow_run_id", f"onboard-{app}")
    phases = workflow_stages(
        app, launch, min_pass_rate, config, run_id=run_id)
    plan = [step for phase in phases for step in phase["steps"]]
    phase_by_step = {
        step["name"]: phase["name"]
        for phase in phases for step in phase["steps"]
    }
    by_name = {stage["name"]: stage for stage in plan}
    redo_used = {}

    log(f"Application {app}; total: {len(plan)} steps"
        + (f"; only: {only}" if only else "")
        + (f"; completed: {len(state['done'])} steps" if state["done"] else ""))

    index = 0
    while index < len(plan):
        stage = plan[index]
        name = stage["name"]

        if only and only not in name:
            index += 1
            continue
        if name in state["done"] and not only:
            if stage.get("recheck_on_resume"):
                valid, reason = stage["check"]()
                if not valid:
                    invalidated = [step["name"] for step in plan[index:]]
                    state["done"] = [step for step in state["done"]
                                     if step not in invalidated]
                    state["rerun_steps"] = list(dict.fromkeys(
                        state.get("rerun_steps", []) + invalidated))
                    state["history"].append({
                        "phase": phase_by_step[name], "stage": name,
                        "owner": stage["owner"], "passed": False,
                        "detail": "Resume revalidation: " + reason,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "invalidated_steps": invalidated})
                    save_state(app, state)
                    log(f"── {name}: saved completion has no valid implementation outcomes; "
                        "resuming this step and refreshing downstream outputs")
            if name in state["done"]:
                log(f"── {name} completed; skipping (restart with --no-resume)")
                index += 1
                continue

        passed, detail = run_stage(stage, app, launch, max_attempts, config,
                                   force=force or name in state.get("rerun_steps", []))
        state["history"].append({
            "phase": phase_by_step[name], "stage": name,
            "owner": stage["owner"], "passed": passed,
            "detail": detail[:4000], "at": time.strftime("%Y-%m-%d %H:%M:%S")})

        if passed:
            if name not in state["done"]:
                state["done"].append(name)
            if name in state.get("rerun_steps", []):
                state["rerun_steps"].remove(name)
            save_state(app, state)
            index += 1
            continue

        save_state(app, state)
        redo_target = stage.get("on_fail_redo")
        if redo_target and redo_used.get(redo_target, 0) < redo_limit:
            redo_used[redo_target] = redo_used.get(redo_target, 0) + 1
            log(f"   Returning to '{redo_target}' for retry "
                f"(attempt {redo_used[redo_target]}/{redo_limit}), "
                "including validation feedback")
            if redo_target in state["done"]:
                state["done"].remove(redo_target)
            target_stage = by_name[redo_target]
            target_stage["_feedback"] = detail
            index = plan.index(target_stage)
            continue

        raise StageFailed(name, detail)

    if only:
        log(f"Selected step completed: {only}. This does not imply that later steps or the CLI are complete.")
    else:
        log(f"Completed. CLI at dist/{app}-anis/; launcher bin/{app}-anis")
    return state


def main():
    parser = argparse.ArgumentParser(
        description="Convert an installed application into a working CLI")
    parser.add_argument("--app", required=True, help="Application name; also the directory name under apps/ .")
    parser.add_argument("--launch", default="",
                        help="Application launch command used as the discovery starting point")
    parser.add_argument("--only", default=None,
                        help="Run only steps whose names contain this text")
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument(
        "--resume", dest="resume", action="store_true", default=True,
        help="Resume existing workflow and stage progress (default)")
    resume.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="Archive previous progress and restart with a new run ID")
    parser.add_argument("--max-attempts", type=int,
                        help="Maximum attempts per step")
    parser.add_argument("--min-pass-rate", type=float,
                        help="Required verification pass rate; default: 100%%")
    parser.add_argument("--plan", action="store_true",
                        help="Print workflow steps and owners without executing")
    parser.add_argument(
        "--config", help="ANIS Global configuration; defaults to the root anis-config.json")
    parser.add_argument("--redo-limit", type=int,
                        help="Maximum retries of an earlier model step after validation fails")
    options = parser.parse_args()
    config = system_config.load(options.config)
    onboarding_config = config["workflow"]["onboarding"]
    max_attempts = options.max_attempts or onboarding_config[
        "stage_attempt_limit"]
    min_pass_rate = (onboarding_config["minimum_pass_rate"]
                     if options.min_pass_rate is None
                     else options.min_pass_rate)
    redo_limit = (onboarding_config["redo_limit"]
                  if options.redo_limit is None else options.redo_limit)

    if options.plan:
        print(f"\nApplication {options.app} workflow\n" + "=" * 68)
        print(f"{'Step':<46}{'Owner':<10}{'Source'}")
        print("-" * 68)
        for phase in workflow_stages(
                options.app, options.launch, min_pass_rate, config):
            print(f"\n{phase['name']}")
            for stage in phase["steps"]:
                basis = (stage.get("prompt") or stage.get("basis")
                         or "Deterministic script")
                print(f"{stage['name']:<46}{stage['owner']:<10}{basis}")
        print("\nScripts validate model outputs. Validation failures are "
              "returned to the model as repair feedback.")
        return 0

    if options.resume:
        state = load_state(options.app)
        log("Mode: resume existing progress")
    else:
        state = start_fresh_state(options.app)
        log(f"Mode: fresh start; run ID {state['workflow_run_id']}")

    try:
        onboard(options.app, options.launch, only=options.only,
                max_attempts=max_attempts,
                min_pass_rate=min_pass_rate, redo_limit=redo_limit,
                force=not options.resume,
                config=config, state=state)
    except StageFailed as error:
        print(f"\nWorkflow stopped at {error.stage}\n\n{error.detail}\n", file=sys.stderr)
        if options.resume:
            print("Progress saved. After repair, rerun the same command to resume this step.",
                  file=sys.stderr)
        else:
            print("Progress saved. After repair, rerun with --resume to resume this step.",
                  file=sys.stderr)
        return 1
    except runtime.RuntimeError_ as error:
        print(f"\n{error}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
