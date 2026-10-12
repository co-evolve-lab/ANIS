#!/usr/bin/env python3
"""Deterministic controller for ANIS goal-level command synthesis.

Released atomic commands record their invocations per request ($ANIS_CALL_LOG).
The controller counts contiguous mutation sequences that recur across requests,
asks the coding agent whether each recurring sequence realizes a shared
artifact-level subgoal and how to name it, reruns each named command on a clean
copy of an originating request, and releases only commands whose saved state
matches that of the atomic sequence they replace.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


STAGE_DIR = Path(__file__).resolve().parent
ANIS_ROOT = STAGE_DIR.parents[1]
sys.path.insert(0, str(ANIS_ROOT))
from anis import system_config  # noqa: E402

PROMPT = STAGE_DIR / "prompts" / "review_goal.md"
SCHEMA = STAGE_DIR / "schemas" / "goal_review.schema.json"
RUNS_DIR = STAGE_DIR / "runs"
NAME_PART = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
ARG_FIELDS = {"type", "required", "default", "min", "max", "elem", "size", "values", "help"}
READ_LAYERS = {"observation", "goal"}


def load_requests(path: Path) -> list[dict]:
    """Read request records: {"id", "request", "workdir"?, "calls" | "call_log"}."""
    requests = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        calls = row.get("calls")
        if calls is None:
            log = (path.parent / row["call_log"]).read_text(encoding="utf-8")
            calls = [json.loads(entry) for entry in log.splitlines() if entry.strip()]
        workdir = row.get("workdir")
        requests.append({
            "id": str(row["id"]), "request": str(row.get("request", "")),
            "workdir": str((path.parent / workdir).resolve()) if workdir else None,
            "calls": [{**call, "index": index} for index, call in enumerate(calls)],
        })
    return requests


def load_specs(package: Path) -> dict[str, dict]:
    return {path.parent.name: json.loads(path.read_text(encoding="utf-8"))
            for path in (package / "commands").glob("*/spec.json")}


def segments(calls: list[dict], specs: dict) -> list[list[dict]]:
    """Split at failed, read-only, goal-level, unknown, or file-less calls."""
    result, current = [], []
    for call in calls:
        spec = specs.get(call["command"])
        if (not call.get("ok") or spec is None or spec.get("layer") in READ_LAYERS
                or "file" not in call["args"]):
            if current:
                result.append(current)
            current = []
        else:
            current.append(call)
    if current:
        result.append(current)
    return result


def blocks(calls: list[dict]) -> list[list[dict]]:
    """Group adjacent calls of one command on one file into a batch."""
    grouped = []
    for call in calls:
        if (grouped and grouped[-1][0]["command"] == call["command"]
                and grouped[-1][0]["args"]["file"] == call["args"]["file"]):
            grouped[-1].append(call)
        else:
            grouped.append([call])
    return grouped


def hits(pattern: tuple, segment: list[dict]) -> list[list[dict]]:
    grouped = blocks(segment)
    if len(pattern) == 2 and pattern[0] == pattern[1]:
        return [block for block in grouped if block[0]["command"] == pattern[0] and len(block) > 1]
    found = []
    for index in range(len(grouped) - len(pattern) + 1):
        window = grouped[index:index + len(pattern)]
        flat = [call for block in window for call in block]
        if (tuple(block[0]["command"] for block in window) == pattern
                and len({call["args"]["file"] for call in flat}) == 1):
            found.append(flat)
    return found


def patterns(segment: list[dict], max_length: int) -> set[tuple]:
    grouped = blocks(segment)
    names = [block[0]["command"] for block in grouped]
    found = {(block[0]["command"],) * 2 for block in grouped if len(block) > 1}
    for size in range(2, min(max_length, len(grouped)) + 1):
        for index in range(len(grouped) - size + 1):
            if hits(tuple(names[index:index + size]), segment):
                found.add(tuple(names[index:index + size]))
    return found


def shared_arguments(occurrences: list[list[dict]]) -> list[str]:
    """Arguments equal across every call of every occurrence are passed once."""
    common = None
    for calls in occurrences:
        keys = set.intersection(*(set(call["args"]) for call in calls))
        equal = {key for key in keys if len({json.dumps(call["args"][key]) for call in calls}) == 1}
        common = equal if common is None else common & equal
    return sorted(common or {"file"})


def mine(requests: list[dict], specs: dict, min_support: int, max_length: int) -> list[dict]:
    by_request = {row["id"]: segments(row["calls"], specs) for row in requests}
    support = defaultdict(set)
    for request_id, parts in by_request.items():
        for part in parts:
            for pattern in patterns(part, max_length):
                support[pattern].add(request_id)
    frequent = {pattern: ids for pattern, ids in support.items() if len(ids) >= min_support}

    def subsumed(pattern):
        return any(len(other) > len(pattern) and frequent[other] == frequent[pattern]
                   and any(other[i:i + len(pattern)] == pattern
                           for i in range(len(other) - len(pattern) + 1))
                   for other in frequent)

    candidates = []
    for pattern in sorted(p for p in frequent if not subsumed(p)):
        occurrences = [{"request": request_id, "calls": calls}
                       for request_id, parts in by_request.items()
                       for part in parts for calls in hits(pattern, part)]
        candidates.append({
            "sequence": list(pattern), "requests": sorted(frequent[pattern]),
            "occurrences": occurrences,
            "shared": shared_arguments([row["calls"] for row in occurrences]),
        })
    return candidates


def render(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def review_prompt(app, candidate, requests, specs, max_requests, feedback=""):
    texts = [requests[request_id] for request_id in candidate["requests"]
             if requests.get(request_id)][:max_requests]
    example = max((row["calls"] for row in candidate["occurrences"]), key=len)
    return render(PROMPT.read_text(encoding="utf-8"), {
        "APP": app, "SUPPORT": len(candidate["requests"]),
        "STEPS": "\n".join(
            f"{index}. {name} (object: {specs[name].get('object', '')}, "
            f"action: {specs[name].get('action', '')}): {specs[name].get('summary', '')}"
            for index, name in enumerate(candidate["sequence"], 1)),
        "EXAMPLE": json.dumps([{"command": call["command"], "args": call["args"]}
                               for call in example], ensure_ascii=False, indent=1),
        "REQUESTS": "\n\n".join(f"[{index}] {text}" for index, text in enumerate(texts, 1)),
        "FEEDBACK": f"\nYour previous answer was rejected: {feedback}\n" if feedback else "",
    })


def check_review(review, taken: set[str]) -> tuple[str | None, str]:
    """Return (command name, problem); a goal judged not shared has neither."""
    if not isinstance(review, dict) or not isinstance(review.get("shared_subgoal"), bool):
        return None, "the answer did not follow the required JSON schema"
    if not review["shared_subgoal"]:
        return None, ""
    name = f"{review.get('object', '')}-{review.get('action', '')}"
    summary = str(review.get("summary", ""))
    if not (NAME_PART.fullmatch(str(review.get("object", "")))
            and NAME_PART.fullmatch(str(review.get("action", "")))):
        return None, "object and action must be lowercase ASCII words joined by hyphens"
    if "then" in name.split("-"):
        return None, f"`{name}` uses the word then; name the shared object and action"
    if name in taken:
        return None, f"`{name}` is already an existing command; choose another name"
    if not summary.strip() or not summary.isascii() or "\n" in summary:
        return None, "summary must be one non-empty English sentence"
    return name, ""


def ask_agent(prompt: str, output: Path, config: dict, timeout: int) -> dict | None:
    agent = config["coding_agent"]
    command = [shutil.which(agent["command"]) or agent["command"], "exec",
               "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
               "--output-schema", str(SCHEMA), "--output-last-message", str(output),
               "-C", str(output.parent), "-"]
    if agent.get("model"):
        command[2:2] = ["--model", agent["model"]]
    try:
        done = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=timeout)
        return json.loads(output.read_text(encoding="utf-8")) if done.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def review(app, candidate, requests, specs, taken, ask, max_requests):
    feedback, answers = "", []
    for _ in range(2):
        answer = ask(review_prompt(app, candidate, requests, specs, max_requests, feedback))
        answers.append(answer)
        name, problem = check_review(answer, taken)
        if not problem:
            return name, {**(answer or {}), "answers": answers}
        feedback = problem
    return None, {"shared_subgoal": None, "problem": feedback, "answers": answers}


def build_command(name: str, candidate: dict, review_record: dict, specs: dict):
    """Return (spec, contract) for a goal-level command."""
    shared = candidate["shared"]
    per_step = [[] for _ in candidate["sequence"]]
    for row in candidate["occurrences"]:
        for index, block in enumerate(blocks(row["calls"])):
            per_step[index].append(block)
    homogeneous = len(candidate["sequence"]) == 2 and len(set(candidate["sequence"])) == 1
    steps_commands = candidate["sequence"][:1] if homogeneous else candidate["sequence"]
    batch = [homogeneous or any(len(block) > 1 for block in per_step[i])
             for i in range(len(steps_commands))]
    single_counts = defaultdict(int)
    for command, is_batch in zip(steps_commands, batch):
        if not is_batch:
            for arg in specs[command].get("args", {}):
                single_counts[arg] += 1
    args, steps = {}, []
    for arg in shared:
        definition = next(spec["args"][arg] for spec in map(specs.get, steps_commands)
                          if arg in spec.get("args", {}))
        args[arg] = {key: value for key, value in definition.items() if key in ARG_FIELDS}
    for index, (command, is_batch) in enumerate(zip(steps_commands, batch), 1):
        if is_batch:
            items = "items" if sum(batch) == 1 else f"step{index}-items"
            args[items] = {"type": "string", "required": True,
                           "help": f"JSON array; one object of {command} arguments per call, "
                                   f"excluding {', '.join(shared)}"}
            steps.append({"command": command, "mode": "batch", "items": items})
            continue
        mapping = {}
        for arg, definition in specs[command].get("args", {}).items():
            if arg in shared:
                continue
            parent = arg if single_counts[arg] == 1 else f"step{index}-{arg}"
            args[parent] = {key: value for key, value in definition.items() if key in ARG_FIELDS}
            mapping[parent] = arg
        steps.append({"command": command, "mode": "single", "args": mapping})
    contract = {"shared": shared, "steps": steps}
    example = max(candidate["occurrences"], key=lambda row: len(row["calls"]))["calls"]
    spec = {
        "command": name, "summary": review_record["summary"], "layer": "goal",
        "capability_class": "goal_level", "object": review_record["object"],
        "action": review_record["action"], "args": args,
        "errors": {"INVALID_ARGS": "Arguments do not match the declared contract"},
        "demo": {"args": {key: value for key, value in invocation(example, contract).items()
                          if key != "file"}},
        "related": {"subgoal": review_record.get("subgoal", ""),
                    "replaces": list(dict.fromkeys(candidate["sequence"]))},
    }
    return spec, contract


def invocation(calls: list[dict], contract: dict) -> dict:
    """Arguments of the goal-level command that reproduce one occurrence."""
    values = {name: calls[0]["args"][name] for name in contract["shared"]}
    for step, block in zip(contract["steps"], blocks(calls)):
        if step["mode"] == "batch":
            values[step["items"]] = json.dumps(
                [{k: v for k, v in call["args"].items() if k not in contract["shared"]}
                 for call in block], ensure_ascii=False)
        else:
            values.update({parent: block[0]["args"][child] for parent, child in step["args"].items()
                           if child in block[0]["args"]})
    return values


def write_command(directory: Path, spec: dict, contract: dict) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    (directory / "impl.py").write_text(
        f'"""{spec["command"]}: ANIS goal-level command."""\n\nfrom anis import goal\n\n'
        f"CONTRACT = {contract!r}\n\n\ndef run(args):\n"
        "    return goal.run(args, __file__, CONTRACT)\n", encoding="utf-8")


def call_cli(package: Path, command: str, values: dict, cwd: Path, old_cwd: str | None, timeout: int):
    tokens = [command]
    for key, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value)
        tokens += [f"--{key}", text.replace(old_cwd, str(cwd)) if old_cwd else text]
    environment = {**os.environ, "PYTHONPATH": str(package)}
    environment.pop("ANIS_CALL_LOG", None)
    try:
        done = subprocess.run([sys.executable, str(package / "anis" / "cli.py"), "--app",
                               str(package), *tokens], cwd=cwd, env=environment,
                              capture_output=True, text=True, timeout=timeout)
        payload = json.loads(done.stdout)
    except (subprocess.TimeoutExpired, ValueError):
        return False, {}
    return done.returncode == 0 and payload.get("status") == "ok", payload


def verify(package: Path, request: dict, calls: list[dict], name: str, contract: dict,
           specs: dict, timeout: int) -> dict:
    """Rerun on a clean copy; the goal command must save the same state as the atomic calls."""
    if not request["workdir"]:
        return {"pass": False, "reason": "request has no workdir to copy"}
    prefix = [call for call in request["calls"][:calls[0]["index"]]
              if call.get("ok") and specs.get(call["command"], {}).get("layer") != "observation"]
    variants = {"atomic": [(call["command"], call["args"]) for call in calls],
                "goal": [(name, invocation(calls, contract))]}
    states = {}
    with tempfile.TemporaryDirectory(prefix="anis-goal-") as tmp:
        for label, steps in variants.items():
            work = Path(tmp) / "work"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(request["workdir"], work)
            for command, values, old in ([(c["command"], c["args"], c.get("cwd")) for c in prefix]
                                         + [(c, v, calls[0].get("cwd")) for c, v in steps]):
                ok, payload = call_cli(package, command, values, work, old, timeout)
                if not ok:
                    return {"pass": False, "reason": f"{label}: {command} failed: "
                                                     f"{payload.get('message', '')}"[:300]}
            ok, payload = call_cli(package, "observe", {"file": calls[0]["args"]["file"]},
                                   work, calls[0].get("cwd"), timeout)
            if not ok:
                return {"pass": False, "reason": f"{label}: observe failed"}
            states[label] = json.dumps(payload.get("state"), sort_keys=True).replace(str(work), "")
    same = states["atomic"] == states["goal"]
    return {"pass": same, "request": request["id"],
            "reason": "" if same else "saved state differs from the atomic sequence"}


def release(app: str, released: list[tuple[str, dict, dict]]) -> None:
    app_dir = ANIS_ROOT / "apps" / app
    report_path = app_dir / "build" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    guide_path = app_dir / "guide" / "commands.json"
    guide = json.loads(guide_path.read_text(encoding="utf-8")) if guide_path.is_file() else None
    for name, spec, contract in released:
        write_command(app_dir / "commands" / name, spec, contract)
        report[name] = {"pass": True, "goal_level": True}
        if guide is not None:
            guide["commands"] = [row for row in guide["commands"] if row.get("command") != name]
            guide["commands"].append({"command": name, "purpose": spec["summary"],
                                      "operates_on": spec["object"], "produces": None})
            section = next((row for row in guide.setdefault("sections", [])
                            if row.get("object") == spec["object"]), None)
            if section is None:
                guide["sections"].append({"object": spec["object"], "commands": [name]})
            elif name not in section["commands"]:
                section["commands"].append(name)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if guide is not None:
        guide_path.write_text(json.dumps(guide, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([sys.executable, str(ANIS_ROOT / "anis" / "freeze.py"), "--app", app], check=True)


def synthesize(app, requests, ask, min_support, max_length, max_requests, verify_timeout):
    package = ANIS_ROOT / "dist" / f"{app}-anis"
    if not (package / "commands").is_dir():
        raise SystemExit(f"[goals] no released package at {package}; release atomic commands first")
    specs = load_specs(package)
    texts = {row["id"]: row["request"] for row in requests}
    by_id = {row["id"]: row for row in requests}
    taken, decisions, released = set(specs), [], []
    with tempfile.TemporaryDirectory(prefix="anis-goal-package-") as tmp:
        staged = Path(tmp) / package.name
        shutil.copytree(package, staged)
        shutil.copy2(ANIS_ROOT / "anis" / "goal.py", staged / "anis" / "goal.py")
        for candidate in mine(requests, specs, min_support, max_length):
            name, record = review(app, candidate, texts, specs, taken, ask, max_requests)
            decision = {"sequence": candidate["sequence"], "requests": candidate["requests"],
                        "review": record, "command": name, "verification": []}
            decisions.append(decision)
            if not name:
                continue
            taken.add(name)
            spec, contract = build_command(name, candidate, record, specs)
            write_command(staged / "commands" / name, spec, contract)
            tried = [row for row in candidate["occurrences"] if by_id[row["request"]]["workdir"]][:3]
            decision["verification"] = [
                verify(staged, by_id[row["request"]], row["calls"], name, contract, specs, verify_timeout)
                for row in tried]
            if tried and all(row["pass"] for row in decision["verification"]):
                released.append((name, spec, contract))
    return decisions, released


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthesize goal-level commands from request logs")
    parser.add_argument("--app", required=True)
    parser.add_argument("--requests", type=Path, required=True,
                        help="JSONL request records; see README, Goal-level commands")
    parser.add_argument("--min-support", type=int, default=3)
    parser.add_argument("--max-length", type=int, default=5)
    parser.add_argument("--max-requests", type=int, default=6,
                        help="requests shown to the agent per candidate")
    parser.add_argument("--release", action="store_true",
                        help="publish verified commands and refreeze dist/<app>-anis")
    parser.add_argument("--config")
    args = parser.parse_args()
    config = system_config.load(args.config)
    run_dir = RUNS_DIR / f"{args.app}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True)
    counter = iter(range(1, 1_000_000))
    timeout = config["workflow"]["capability_review"]["timeout_seconds"]

    def ask(prompt):
        output = run_dir / f"review-{next(counter):03d}.json"
        (output.with_suffix(".prompt.md")).write_text(prompt, encoding="utf-8")
        return ask_agent(prompt, output, config, timeout)

    decisions, released = synthesize(
        args.app, load_requests(args.requests), ask, args.min_support, args.max_length,
        args.max_requests, config["workflow"]["command_implementation"]["verify_timeout_seconds"])
    (run_dir / "decisions.json").write_text(json.dumps(decisions, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    if args.release and released:
        release(args.app, released)
    print(json.dumps({"run": str(run_dir.relative_to(ANIS_ROOT)), "candidates": len(decisions),
                      "verified": [name for name, _, _ in released],
                      "released": bool(args.release and released)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
