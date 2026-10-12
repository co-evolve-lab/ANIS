"""Generate the compact Agent command guide after command implementation."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
ANIS = HERE.parents[1]
PROMPT = (HERE / "document_commands.md").read_text(encoding="utf-8")
OUTPUT_FIELDS = {"command", "purpose", "operates_on", "produces"}


def _accepted_names(app_dir: Path) -> list[str]:
    """Return verified commands in their existing command-organization order."""
    report_path = app_dir / "build" / "report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        accepted = {
            name for name, result in report.items()
            if isinstance(result, dict) and result.get("pass") is True
        }
    else:
        accepted = None
    command_root = app_dir / "commands"
    available = [
        path.name for path in command_root.iterdir()
        if path.is_dir() and (path / "spec.json").is_file()
    ]
    return ([name for name in available if name in accepted]
            if accepted is not None else available)


def prepare_input(app: str, app_dir: Path,
                  command_names: list[str] | None = None) -> dict:
    """Prepare only metadata needed by the documentation Agent."""
    names = command_names if command_names is not None else _accepted_names(app_dir)
    rows = []
    for name in names:
        spec_path = app_dir / "commands" / name / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        binding = spec.get("binding") or {}
        rows.append({
            "command": name,
            "current_summary": spec.get("summary", ""),
            "object_hint": spec.get("object"),
            "action_hint": spec.get("action"),
            "capability_class": spec.get("capability_class"),
            "runtime_type": binding.get("runtime_type"),
            "binding_kind": binding.get("kind"),
            "generation_route": binding.get("generation_route"),
            "relationship_metadata": {
                key: binding[key] for key in (
                    "relationships", "parent_runtime_types", "reference_types"
                ) if key in binding
            },
        })

    # Progressive disclosure follows the organization already attached to the
    # command specs.  Object groups keep first-seen order, and commands inside
    # a group keep generation order.  No alphabetical re-sorting occurs here.
    grouped = {}
    for row in rows:
        group = str(row.get("object_hint") or "other")
        grouped.setdefault(group, []).append(row)
    organized_rows = [row for group in grouped.values() for row in group]
    sections = [
        {"object": name,
         "commands": [row["command"] for row in group_rows]}
        for name, group_rows in grouped.items()
    ]
    return {"schema_version": 1, "app": app,
            "sections": sections, "commands": organized_rows}


def validate_output(payload: object, source: dict) -> str:
    """Require a complete one-to-one four-field guide."""
    if not isinstance(payload, dict):
        return "guide output must be a JSON object"
    if payload.get("schema_version") != 1:
        return "guide schema_version must be 1"
    if payload.get("app") != source.get("app"):
        return "guide app does not match input"
    rows = payload.get("commands")
    if not isinstance(rows, list):
        return "guide commands must be a list"
    expected = [row["command"] for row in source["commands"]]
    actual = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"guide command {index} must be an object"
        if set(row) != OUTPUT_FIELDS:
            return f"guide command {index} must contain exactly {sorted(OUTPUT_FIELDS)}"
        command = row.get("command")
        actual.append(command)
        for field in ("command", "purpose", "operates_on"):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip() or "\n" in value:
                return f"{command or index}: {field} must be one non-empty line"
        if not row["purpose"].isascii():
            return f"{command}: purpose must be an English ASCII sentence"
        produced = row.get("produces")
        if produced is not None and (
                not isinstance(produced, str) or not produced.strip()
                or "\n" in produced):
            return f"{command}: produces must be null or one non-empty line"
    if actual != expected:
        return "guide commands must exactly match input names and order"
    return ""


def render_markdown(payload: dict) -> str:
    """Render the validated four-field JSON as one compact complete document."""
    def cell(value):
        return str(value if value is not None else "—").replace("|", "\\|")

    lines = [
        f"# {payload['app']} Agent command guide",
        "",
        "This complete compact guide is generated after command implementation.",
        "It intentionally omits parameters and examples; inspect one command's",
        "schema only after selecting it.",
        "",
    ]
    by_name = {row["command"]: row for row in payload["commands"]}
    sections = payload.get("sections") or [{
        "object": "Commands", "commands": list(by_name),
    }]
    for section in sections:
        lines.extend([
            f"## {section['object']}",
            "",
            "| Command | Purpose | Operates on | Produces |",
            "|---|---|---|---|",
        ])
        for command in section["commands"]:
            row = by_name[command]
            lines.append("| " + " | ".join(cell(row[key]) for key in (
                "command", "purpose", "operates_on", "produces")) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_skill(app: str) -> str:
    """Render the small, application-neutral Agent usage skill."""
    tool = f"{app}-anis"
    return f"""---
name: use-{app}-anis
description: Use the released {app} ANIS command package to discover, compose, and execute application commands without bypassing its public interface.
---

# Use {app} ANIS

Use this skill when a task must be completed through `{tool}`.

## Discover only as much as needed

1. Start with `{tool} guide`. It is organized by operated object and gives each
   command's purpose, input object, and newly produced reusable object.
2. Use `{tool} dir` to see object categories, then `{tool} dir <category>` to
   narrow the command set. Use `{tool} dir --search \"task words\"` when the
   relevant object or command is not yet known.
3. After selecting a command, use `{tool} dir <command>` or
   `{tool} <command> --schema` for its parameters and verified invocation shape.
   Do not load every detailed schema in advance.

## Compose commands by objects

ANIS commands are small operations centered on runtime objects. A creation
command may produce a reusable object; later commands can operate on that same
object. Build a sequence by matching `Produces` to `Operates on`, not by guessing
that similarly named parameters or commands are compatible.

Prefer the shortest sequence that creates or locates the required object,
changes it, and saves or exports the requested artifact. Use only released ANIS
commands; if the guide and `dir` expose no required operation, report the missing
capability instead of bypassing ANIS with another API or direct file rewriting.
"""


def _agent_command(executable: str, model: str | None, cwd: Path,
                   last_message: Path) -> list[str]:
    command = [
        executable, "exec", "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "workspace-write", "--json",
        "--output-last-message", str(last_message), "-C", str(cwd), "-",
    ]
    if model:
        command[2:2] = ["--model", model]
    return command


def generate(app: str, app_dir_name: str, run_dir: Path, executable: str,
             model: str | None, timeout: int,
             command_names: list[str] | None = None,
             publish: bool = True) -> dict:
    """Run one documentation Agent, validate its output, and publish the guide."""
    doc_dir = run_dir / "documentation"
    doc_dir.mkdir(parents=True, exist_ok=True)
    status_path = doc_dir / "status.json"
    source = prepare_input(app, ANIS / "apps" / app_dir_name, command_names)
    input_path = doc_dir / "input.json"
    output_path = doc_dir / "candidate.json"
    input_path.write_text(
        json.dumps(source, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    if output_path.exists():
        output_path.unlink()

    def status(value, phase, **extra):
        record = {
            "status": value, "phase": phase, "updated_at": time.time(),
            "command_count": len(source["commands"]), **extra,
        }
        status_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return record

    status("running", "Documentation Agent is preparing the command guide")
    prompt = (PROMPT.replace("{{INPUT_PATH}}", str(input_path))
              .replace("{{OUTPUT_PATH}}", str(output_path)))
    last = doc_dir / "last_message.txt"
    try:
        result = subprocess.run(
            _agent_command(executable, model, doc_dir, last), input=prompt,
            text=True, capture_output=True, timeout=timeout)
        (doc_dir / "events.jsonl").write_text(result.stdout, encoding="utf-8")
        (doc_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            return status("failed", "Documentation Agent execution failed",
                          error=f"agent exit={result.returncode}: {result.stderr[-2000:]}")
    except subprocess.TimeoutExpired as exc:
        return status("failed", "Documentation Agent timed out",
                      error=f"timed out after {exc.timeout} seconds")
    except Exception as exc:
        return status("failed", "Documentation controller error", error=str(exc))

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return status("failed", "Documentation output is unreadable", error=str(exc))
    error = validate_output(payload, source)
    if error:
        return status("failed", "Python Documentation completeness check failed", error=error)

    payload["sections"] = source["sections"]
    guide_dir = (ANIS / "apps" / app_dir_name / "guide"
                 if publish else doc_dir / "result")
    guide_dir.mkdir(parents=True, exist_ok=True)
    (guide_dir / "commands.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (guide_dir / "commands.md").write_text(
        render_markdown(payload), encoding="utf-8")
    skill_dir = ((ANIS / "apps" / app_dir_name / "skills"
                  / f"use-{app}-anis") if publish else
                 doc_dir / "result" / "skills" / f"use-{app}-anis")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(render_skill(app), encoding="utf-8")
    return status("completed", "Command guide generated",
                  output_json=str(guide_dir / "commands.json"),
                  output_markdown=str(guide_dir / "commands.md"),
                  output_skill=str(skill_path))


def main():
    parser = argparse.ArgumentParser(
        description="Generate the complete organized command guide and skill")
    parser.add_argument("--app", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    import sys
    if str(ANIS) not in sys.path:
        sys.path.insert(0, str(ANIS))
    from anis import system_config
    config = system_config.load(args.config)
    agent = config["coding_agent"]
    documentation = config["workflow"]["command_documentation"]
    result = generate(
        args.app, args.app,
        HERE / "runs" / args.run_id / args.app,
        agent["command"], agent.get("model"),
        documentation["timeout_seconds"], publish=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
