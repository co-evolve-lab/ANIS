#!/usr/bin/env python3
"""Turn Stage-1 semantic approvals into implementation work orders."""

import argparse
import json
import sys
from pathlib import Path

from routing import command_name, route_for


ANIS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANIS))
from anis import system_config  # noqa: E402


RESULT_ROOTS = (ANIS / "stages" / "01_capability_discovery" / "runs",)
APP_DIR = {}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def selection_from_accepted(accepted):
    by_app = {}
    for app, key in accepted:
        by_app.setdefault(app, []).append(key)
    return {"apps": [{"app": app, "remaining_keys": sorted(keys)}
                     for app, keys in sorted(by_app.items())]}


def implementation_discovery(assigned, reviewed):
    """Build an implementation work order without claiming runtime proof."""
    record = assigned.get("record")
    record = record if isinstance(record, dict) else {}
    symbol = str(assigned.get("symbol", "capability"))
    member = record.get("member")
    runtime_object = record.get("object")
    if member and runtime_object:
        object_name = str(runtime_object).rsplit(".", 1)[-1]
        suggested_name = f"{object_name}-{member}"
    else:
        suggested_name = symbol
    raw_parameters = record.get("params", [])
    parameters = [
        {"name": str(value.get("name", "parameter")),
         "type": str(value.get("type", "unknown"))}
        for value in raw_parameters if isinstance(value, dict)
    ]
    return {
        "disposition": "pending_implementation",
        "reason": reviewed["reason"],
        "runtime_object": str(runtime_object or ""),
        "operation": symbol,
        "parameters": parameters,
        "command_candidates": [{
            "name": suggested_name,
            "capability_class": "to_be_determined_during_implementation",
            "selector": "",
            "arguments": [],
            "implementation_route": "single_session_operation",
            "persistence_check": (
                "Determined by the implementation Agent using actual command effects; then verified by Python Verify"),
        }],
        "requires_runtime_investigation": True,
    }


def accepted_items(run_names):
    accepted = {}
    run_dirs = []
    for run_id in run_names:
        for root in RESULT_ROOTS:
            if (root / run_id).is_dir():
                run_dirs.append(root / run_id)
    for run_dir in run_dirs:
        modern_state = run_dir / "state.json"
        if modern_state.is_file():
            state = load(modern_state)
            for batch_state in state.get("batches", []):
                batch_dir = run_dir / batch_state["path"]
                original_batch = load(batch_dir / "batch.json")
                assigned_by_id = {
                    item["item_id"]: item
                    for item in original_batch["items"]}
                for item_id, item_state in batch_state.get(
                        "items", {}).items():
                    if item_state.get("status") != "accepted":
                        continue
                    attempt = run_dir / item_state["accepted_attempt"]
                    review = load(attempt / "review_result.json")
                    reviewed = next(
                        item for item in review["items"]
                        if item["item_id"] == item_id)
                    if reviewed.get("decision") != "expose":
                        continue
                    assigned = assigned_by_id[item_id]
                    # item_id is the census identity.  Symbol names are only
                    # labels and can legitimately repeat for two registry
                    # entries belonging to different runtime types.
                    key = item_id
                    accepted[(original_batch["app"], key)] = {
                        "key": key,
                        "command_symbol": assigned["symbol"],
                        "assigned": assigned,
                        "review": reviewed,
                        "discovery": implementation_discovery(
                            assigned, reviewed),
                        "source": str(attempt.relative_to(ANIS)),
                    }
        for state_path in sorted(run_dir.glob("*/state.json")):
            app = state_path.parent.name
            state = load(state_path)
            for batch_id, status in state.get("batches", {}).items():
                if status.get("status") != "accepted":
                    continue
                attempt = state_path.parent / status["accepted_attempt"]
                batch = load(attempt / "batch.json")
                result = load(attempt / "discovery_result.json")
                by_id = {item["item_id"]: item for item in result["items"]}
                for assigned in batch["items"]:
                    item_id = assigned["item_id"]
                    discovered = by_id[item_id]
                    key = assigned["symbol"]
                    if discovered.get("disposition") != "executable":
                        raise RuntimeError(
                            f"{app}/{key} accepted but is not executable")
                    accepted[(app, key)] = {
                        "key": key,
                        "assigned": assigned,
                        "discovery": discovered,
                        "source": str(attempt.relative_to(ANIS)),
                    }
    return accepted


def supplemental_items(paths):
    items = {}
    for path in paths:
        result = load(path)
        app = result["app"]
        for discovered in result.get("items", []):
            if discovered.get("disposition") != "executable":
                continue
            key = f"supplemental:{discovered['item_id']}"
            try:
                source = str(path.relative_to(ANIS))
            except ValueError:
                source = str(path)
            items[(app, key)] = {
                "key": key,
                "assigned": {
                    "item_id": discovered["item_id"],
                    "symbol": discovered.get(
                        "runtime_object", discovered["item_id"]),
                },
                "discovery": discovered,
                "source": source,
            }
    return items


def command_needed(app_dir, command):
    atlas = ANIS / "apps" / app_dir / "atlas" / f"{command}.json"
    report_path = ANIS / "apps" / app_dir / "build" / "report.json"
    if not atlas.is_file() or not report_path.is_file():
        return True
    report = load(report_path)
    return not report.get(command, {}).get("pass", False)


def expand_item(item):
    """Expand every discovered operation; never collapse an object to one command."""
    discovery = item["discovery"]
    assigned_record = item.get("assigned", {}).get("record") or {}
    if not isinstance(assigned_record, dict):
        assigned_record = {}
    candidates = discovery.get("command_candidates") or [{}]
    expanded = []
    seen = set()
    for candidate in candidates:
        command = command_name(
            item.get("command_symbol", item["key"]), candidate)
        if command in seen:
            continue
        seen.add(command)
        row = dict(item)
        row["command"] = command
        row["candidate"] = candidate
        row["generation_route"] = route_for(discovery, candidate)
        row["object_family"] = str(
            discovery.get("runtime_object")
            or assigned_record.get("object")
            or f"command:{command}")
        row["persistence_check"] = candidate.get(
            "persistence_check",
            "Save, close, and reopen the file after execution; confirm state with an independent read command")
        expanded.append(row)
    return expanded


def main():
    global APP_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--selection", type=Path,
        help="Selection file; ignored with --all-accepted .")
    parser.add_argument(
        "--all-accepted", action="store_true",
        help="Implement all approved discoveries without a selection file")
    parser.add_argument(
        "--apps", default="",
        help="Comma-separated application names; empty means all")
    parser.add_argument(
        "--config", help="ANIS Global configuration; defaults to the root anis-config.json")
    parser.add_argument(
        "--discovery-run", action="append", dest="discovery_runs",
        help="Repeatable discovery run ID")
    parser.add_argument(
        "--discovery-result", action="append", type=Path, default=[],
        help="Additional discovery_result.json; repeatable")
    parser.add_argument(
        "--only-needed", action=argparse.BooleanOptionalAction, default=True,
        help="Generate only atlas missing or unverified commands")
    args = parser.parse_args()
    APP_DIR = system_config.load(args.config).get("app_aliases", {})

    discovery_runs = args.discovery_runs or [args.run_id]
    accepted = accepted_items(discovery_runs)
    supplemental_paths = list(args.discovery_result)
    supplements = supplemental_items(dict.fromkeys(supplemental_paths))
    accepted.update(supplements)

    if args.all_accepted or args.selection is None:
        selection = selection_from_accepted(accepted)
        if not selection["apps"]:
            raise SystemExit(
                "No approved discoveries available for implementation.\n"
                "First run: python3 stages/01_capability_discovery/workflow.py run")
    else:
        selection = load(args.selection)
        expected = {
            (app["app"], key)
            for app in selection["apps"]
            for key in app["remaining_keys"]
        }
        actual = set(accepted)
        if not expected <= actual:
            raise SystemExit(
                f"accepted discoveries do not match selection; "
                f"missing={sorted(expected-actual)}")

    if args.apps:
        wanted = {name.strip() for name in args.apps.split(",") if name.strip()}
        selection = {"apps": [row for row in selection["apps"]
                              if row["app"] in wanted
                              or APP_DIR.get(row["app"], row["app"]) in wanted]}

    out = Path(__file__).parent / "runs" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    totals = {}
    for app_row in selection["apps"]:
        app = app_row["app"]
        app_dir = APP_DIR.get(app, app)
        operations = []
        selected_keys = list(app_row["remaining_keys"])
        selected_keys.extend(
            key for supplement_app, key in supplements
            if supplement_app == app)
        for key in selected_keys:
            operations.extend(expand_item(accepted[(app, key)]))
        # Several runtime registry entries can legitimately lead to the same
        # public command.  Merge their provenance, but never emit/implement the
        # command twice.
        by_command = {}
        for item in operations:
            name = item["command"]
            if name not in by_command:
                item["source_items"] = [item["key"]]
                by_command[name] = item
            else:
                by_command[name]["source_items"].append(item["key"])
                if by_command[name]["generation_route"] != item["generation_route"]:
                    route_priority = {
                        "direct_property": 0, "validated_enum": 1,
                        "validated_file": 2, "structured_region": 3,
                        "construct_runtime_struct": 4,
                        "construct_typed_sequence": 5,
                        "resolve_runtime_object": 6,
                        "create_from_runtime_factory": 7,
                        "single_session_operation": 8,
                    }
                    # A command shared by several registry entries must use the
                    # strongest required route, not the easiest source.
                    if (route_priority[item["generation_route"]]
                            > route_priority[
                                by_command[name]["generation_route"]]):
                        by_command[name]["generation_route"] = item[
                            "generation_route"]
        operations = list(by_command.values())
        if args.only_needed:
            operations = [
                item for item in operations
                if command_needed(app_dir, item["command"])
            ]
        manifest = {
            "schema_version": 1,
            "run_id": args.run_id,
            "app": app,
            "app_dir": app_dir,
            "operation_count": len(operations),
            "operations": operations,
        }
        app_out = out / app
        app_out.mkdir(exist_ok=True)
        (app_out / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        totals[app] = {
            "discovered_items": len(selected_keys),
            "commands": len(operations),
        }

    summary = {
        "run_id": args.run_id,
        "total_discovered_items": sum(
            item["discovered_items"] for item in totals.values()),
        "total_commands": sum(item["commands"] for item in totals.values()),
        "apps": totals,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
