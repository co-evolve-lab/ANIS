import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from anis import app_contract  # noqa: E402


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def copy_release_guide(app, app_dir, release_dir, passed):
    """Copy only released commands into the compact guide."""
    source = os.path.join(app_dir, "guide", "commands.json")
    if not os.path.isfile(source):
        return []
    with open(source, encoding="utf-8") as handle:
        payload = json.load(handle)
    source_rows = [
        row for row in payload.get("commands", [])
        if isinstance(row, dict) and isinstance(row.get("command"), str)
    ]
    by_name = {row["command"]: row for row in source_rows}
    passed_set = set(passed)
    missing = [name for name in passed if name not in by_name]
    rows = [row for row in source_rows if row["command"] in passed_set]
    sections = []
    included = {row["command"] for row in rows}
    for section in payload.get("sections", []):
        names = [name for name in section.get("commands", [])
                 if name in included]
        if names:
            sections.append({"object": section.get("object", "other"),
                             "commands": names})
    output = {"schema_version": 1, "app": app,
              "sections": sections, "commands": rows}
    destination = os.path.join(release_dir, "guide")
    os.makedirs(destination)
    with open(os.path.join(destination, "commands.json"), "w",
              encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, ensure_ascii=False)
    with open(os.path.join(destination, "commands.md"), "w",
              encoding="utf-8") as handle:
        handle.write(f"# {app} Agent command guide\n\n")
        section_rows = sections or [{"object": "Commands",
                                     "commands": [r["command"] for r in rows]}]
        for section in section_rows:
            handle.write(f"\n## {section['object']}\n\n")
            handle.write("| Command | Purpose | Operates on | Produces |\n")
            handle.write("|---|---|---|---|\n")
            for name in section["commands"]:
                row = by_name[name]
                values = []
                for key in ("command", "purpose", "operates_on", "produces"):
                    value = row.get(key)
                    values.append(str(value if value is not None else "—")
                                  .replace("|", "\\|"))
                handle.write("| " + " | ".join(values) + " |\n")
    return missing


def latest_implementation_state(app):
    """Read the newest command-run outcome for the release manifest."""
    root = os.path.join(_ROOT, "stages", "02_05_command_release", "runs")
    candidates = []
    if os.path.isdir(root):
        for run_id in os.listdir(root):
            path = os.path.join(root, run_id, app, "state.json")
            if os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        return {}
    path = max(candidates, key=os.path.getmtime)
    return json.load(open(path, encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="S8 Freeze")
    ap.add_argument("--app", required=True)
    args = ap.parse_args()

    app_dir = os.path.join(_ROOT, "apps", args.app)
    contract = app_contract.load(args.app)
    adapter = app_contract.load_adapter(args.app, contract)
    census = json.load(open(os.path.join(app_dir, "census", "raw_ops.json"),
                            encoding="utf-8"))
    warnings = []
    if adapter and hasattr(adapter, "release_warnings"):
        warnings.extend(adapter.release_warnings(census) or [])
    report_path = os.path.join(app_dir, "build", "report.json")
    report = (json.load(open(report_path, encoding="utf-8"))
              if os.path.isfile(report_path) else {})
    if not report:
        warnings.append("No verification report; release will contain no commands")
    passed = sorted(
        n for n, r in report.items()
        if isinstance(r, dict) and r.get("pass")
        and os.path.isfile(os.path.join(app_dir, "commands", n, "spec.json"))
        and os.path.isfile(os.path.join(app_dir, "commands", n, "impl.py")))
    implementation_state = latest_implementation_state(args.app)
    failure_records = implementation_state.get("failed_commands", {})
    failed = sorted((set(report) - set(passed)) | set(failure_records))
    if not passed:
        warnings.append("No commands passed; generating an empty release with a full report")

    dist = os.path.join(_ROOT, "dist", f"{args.app}-anis")
    tmp = dist + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "anis"))
    os.makedirs(os.path.join(tmp, "commands"))
    for relative in dict.fromkeys([*contract["release"]["anis_modules"], "goal.py"]):
        src = (Path(_HERE) / relative).resolve()
        if Path(_HERE).resolve() not in src.parents or not src.is_file():
            raise SystemExit(f"[freeze] invalid declared ANIS module: {relative}")
        destination = Path(tmp) / "anis" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, destination)
    for relative in contract["release"]["runtime_files"]:
        src = (Path(app_dir) / relative).resolve()
        if Path(app_dir).resolve() not in src.parents or not src.is_file():
            raise SystemExit(f"[freeze] invalid declared runtime file: {relative}")
        destination = Path(tmp) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, destination)
    # The shared CLI reads display metadata from the app-owned contract.
    shutil.copy2(Path(app_dir) / app_contract.CONTRACT_NAME,
                 Path(tmp) / app_contract.CONTRACT_NAME)
    undocumented = copy_release_guide(args.app, app_dir, tmp, passed)
    if undocumented:
        warnings.append(
            f"Command guide is missing {len(undocumented)} released commands")
    skills_src = os.path.join(app_dir, "skills")
    if os.path.isdir(skills_src):
        shutil.copytree(skills_src, os.path.join(tmp, "skills"))
    for name in passed:
        shutil.copytree(
            os.path.join(app_dir, "commands", name),
            os.path.join(tmp, "commands", name),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    files = {}
    for root, _, names in os.walk(tmp):
        for n in names:
            p = os.path.join(root, n)
            files[os.path.relpath(p, tmp)] = sha256(p)
    manifest = {"app": args.app, "engine_version": census.get("engine_version"),
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "commands": passed, "rejected": failed,
                "status": "published_with_exclusions" if warnings or failed else "published",
                "warnings": warnings,
                "implementation_outcome": {
                    "planned": len(implementation_state.get(
                        "command_universe", [])),
                    "accepted": len(implementation_state.get("completed", [])),
                    "excluded": len(failure_records),
                    "exclusion_details": failure_records,
                },
                "evidence": "Command verification records: apps/<app>/build/report.json",
                "files": files}
    json.dump(manifest, open(os.path.join(tmp, "MANIFEST.json"), "w"),
              indent=1, ensure_ascii=False)

    shutil.rmtree(dist, ignore_errors=True)
    os.rename(tmp, dist)

    bin_dir = os.path.join(_ROOT, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    launcher = os.path.join(bin_dir, f"{args.app}-anis")
    with open(launcher, "w") as f:
        f.write(
            "#!/bin/sh\n"
            'anis_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd)\n'
            f'app_dir="$anis_root/dist/{args.app}-anis"\n'
            'exec env PYTHONPATH="$app_dir" python3 "$app_dir/anis/cli.py" '
            '--app "$app_dir" "$@"\n'
        )
    os.chmod(launcher, 0o755)

    print(f"[freeze] {args.app}: {len(passed)} commands frozen -> {dist}")
    for warning in warnings:
        print(f"[freeze] Warning: {warning}")
    if failed:
        print(f"[freeze] {len(failed)} commands failed verification and were excluded from dist: {failed}")
    print(f"[freeze] Launcher: {launcher}")


if __name__ == "__main__":
    main()
