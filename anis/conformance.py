import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


EMPTY_TYPES = {"", "unknown", "none", "null", "?", "any", "object", "-"}

MEMBER_FIELDS = ("object", "member", "kind", "params", "returns", "writes",
                 "universal", "reached_from")
MIN_DEPTH = 2


class Check:
    def __init__(self, name):
        self.name = name
        self.passed = True
        self.notes = []
        self.failures = []

    def note(self, text):
        self.notes.append(text)

    def fail(self, text):
        self.passed = False
        self.failures.append(text)


def census_path(app):
    return os.path.join(ROOT, "apps", app, "census", "raw_ops.json")


def load_census(app):
    path = census_path(app)
    if not os.path.isfile(path):
        raise SystemExit(f"Cannot find {path}. First run python3 anis/census.py --app {app}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def c1_file_readable(census):
    check = Check("C1  Census is readable and has required top-level fields")
    for key in ("app", "channels"):
        if key not in census:
            check.fail(f"Missing top-level {key!r} field")
    if "unavailable" not in census:
        check.note("Missing unavailable field. Channels without equivalents must be"
                   " listed here with reasons; use an empty array when none apply")
    channels = census.get("channels", {})
    check.note(f"channels: {', '.join(sorted(channels))}")
    return check


def count_member_lists(node, found=None, depth=0):
    if found is None:
        found = {}
    if depth > 8:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("methods", "properties", "attributes", "members") \
                    and isinstance(value, list):
                found[key] = found.get(key, 0) + len(value)
            else:
                count_member_lists(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:5000]:
            count_member_lists(item, found, depth + 1)
    return found


def c2_members_channel_present(census):
    check = Check("C2  Member census channel exists with individual records")
    channels = census.get("channels", {})
    members = channels.get("members")

    if members is None:
        found = count_member_lists(channels)
        detail = ", ".join(f"{key} {value} items"
                          for key, value in sorted(found.items())) or "no member lists"
        check.fail(
            "The census has no channels.members channel.\n"
            f"      Existing channels contain {detail}.\n"
            "      Only counts or names were recorded, without signatures in individual\n"
            "      records; parameter and return types are unavailable,"
            " preventing command generation.\n"
            "      Add channels.members as specified in prompts/census_adapter.md.")
        return check, []

    if not isinstance(members, dict):
        check.fail("channels.members is not an object")
        return check, []

    records = members.get("records")
    if not isinstance(records, list):
        check.fail("channels.members.records is missing or not a list")
        return check, []
    if not records:
        check.fail("channels.members.records is empty")
        return check, []

    check.note(f"member records {len(records)} records")
    return check, records


def c3_member_fields(records):
    check = Check("C3  Member records have all fields and concrete type names")
    missing_field = []
    empty_type = []
    bad_returns = []
    bad_kind = []

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            missing_field.append((index, "record is not an object"))
            continue

        absent = [key for key in MEMBER_FIELDS if key not in record]
        if absent:
            missing_field.append((index, f"Missing {absent}"))
            continue

        if record["kind"] not in ("property", "method"):
            bad_kind.append((index, record["kind"]))

        params = record["params"]
        if isinstance(params, list):
            for param in params:
                if not isinstance(param, dict):
                    empty_type.append((index, "parameter is not an object"))
                elif str(param.get("type", "")).strip().lower() in EMPTY_TYPES:
                    empty_type.append(
                        (index, f"{record['object']}::{record['member']} parameter "
                                f"{param.get('name')!r} type is "
                                f"{param.get('type')!r}"))
        else:
            missing_field.append((index, "params is not a list"))

        returns = record["returns"]
        if not isinstance(returns, dict) or "type" not in returns \
                or "is_object" not in returns:
            bad_returns.append(
                (index, f"{record['object']}::{record['member']} in returns "
                        "Missing type or is_object"))
        elif str(returns["type"]).strip().lower() in EMPTY_TYPES:
            empty_type.append(
                (index, f"{record['object']}::{record['member']} return type is "
                        f"{returns['type']!r}"))

    def report(label, items, why):
        if not items:
            return
        check.fail(f"{len(items)} records{label}.{why}")
        for index, detail in items[:8]:
            check.failures.append(f"        Item {index} : {detail}")
        if len(items) > 8:
            check.failures.append(f"        ... remaining: {len(items) - 8} records")

    report("records have missing fields", missing_field,
           f"Required fields: {MEMBER_FIELDS}")
    report("records have kind invalid", bad_kind, "must be one of property or method")
    report("records have returns invalid structure", bad_returns,
           "returns must include both type and is_object, is_object determines whether to expand recursively")
    report("records have unspecified type names", empty_type,
           "Type names must come from the application. Missing types prevent reliable"
           " invocation and complete command generation.")

    if check.passed:
        check.note("All records have required fields and nonempty type names")
    return check


def c4_recursion_depth(members, records):
    check = Check(f"C4  Recursive expansion depth ≥ {MIN_DEPTH} levels")

    depths = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        path = str(record.get("reached_from", ""))
        depths[path.count(".")] = depths.get(path.count("."), 0) + 1
    observed = max(depths) if depths else 0
    declared_depth = members.get("max_depth")

    check.note(f"Records by depth: {dict(sorted(depths.items()))}")
    if declared_depth is not None and declared_depth != observed:
        check.fail(
            f"reports max_depth={declared_depth}; calculated from reached_from is "
            f"{observed}. Reported counts must match records")

    if observed < MIN_DEPTH:
        check.fail(
            f"Expanded only {observed} levels. Nested members may require following "
            "document, collection, table, row, and cell objects; shallow expansion misses "
            "members such as setString.\n"
            "      Follow prompts/census_adapter.md in §E-2: returns.is_object "
            "true requires an actual object instance for further enumeration.")

    object_returning = [
        record for record in records
        if isinstance(record, dict)
        and isinstance(record.get("returns"), dict)
        and record["returns"].get("is_object") is True]
    if not object_returning:
        check.fail(
            "No record has returns.is_object set to true. Either the application "
            "has no object-returning members, or is_object is inaccurate; "
            "the latter prevents recursive expansion")
    else:
        check.note(f"object-returning members {len(object_returning)} records")
    return check


def c5_boundary_conserved(members, records):
    check = Check("C5  Unexpanded object types are accounted for")

    boundary = members.get("boundary")
    if boundary is None:
        check.fail(
            "Missing channels.members.boundary. Object-returning members whose instances cannot be obtained "
            "must be listed here with reasons to account for unexpanded types")
        return check
    if not isinstance(boundary, list):
        check.fail("channels.members.boundary is not a list")
        return check

    for index, item in enumerate(boundary[:200]):
        if not isinstance(item, dict) or not str(item.get("reason", "")).strip():
            check.fail(f"Boundary entry {index} is missing reason. Each entry must explain "
                       "why the object could not be obtained")
            break

    expanded_types = {record["object"] for record in records
                      if isinstance(record, dict) and "object" in record}
    declared_types = {record["returns"]["type"] for record in records
                      if isinstance(record, dict)
                      and isinstance(record.get("returns"), dict)
                      and record["returns"].get("is_object") is True}
    unreached = declared_types - expanded_types
    boundary_types = {str(item.get("declared_type", "")) for item in boundary
                      if isinstance(item, dict)}

    check.note(f"expanded object types {len(expanded_types)} ; "
               f"declared object return types {len(declared_types)} ; "
               f"unexpanded types {len(unreached)} ; boundary entries {len(boundary)} records")

    unaccounted = unreached - boundary_types
    if unaccounted:
        check.fail(
            f"There are {len(unaccounted)} object types neither expanded nor recorded as boundaries; "
            f"for example {sorted(unaccounted)[:5]}.\n"
            "      Required equality: expanded types + boundary entries = declared return "
            "object types.\n"
            "      A mismatch indicates missing coverage needed for downstream generation.")

    for key in ("expanded_types", "declared_object_types", "count"):
        if key in members and isinstance(members[key], int):
            actual = {"expanded_types": len(expanded_types),
                      "declared_object_types": len(declared_types),
                      "count": len(records)}[key]
            if members[key] != actual:
                check.fail(f"reports {key}={members[key]}; calculated: {actual}, "
                           "does not match")
    return check


def c6_determinism(app, census):
    check = Check("C6  Deterministic census across two runs on the same machine")
    path = census_path(app)
    with open(path, "rb") as handle:
        before = hashlib.sha256(handle.read()).hexdigest()

    backup = path + ".conformance_backup"
    os.replace(path, backup)
    try:
        completed = subprocess.run(
            [sys.executable, os.path.join(HERE, "census.py"), "--app", app],
            cwd=ROOT, capture_output=True, text=True, timeout=3600)
        if completed.returncode != 0:
            check.fail(f"Census rerun failed with exit code {completed.returncode}\n"
                       f"      {completed.stderr[-800:]}")
            return check
        with open(path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        if before != after:
            check.fail(
                "Census outputs differ. Sort collections before output and "
                "omit timestamps, temporary paths, memory addresses, and unstable ordering")
        else:
            check.note(f"Outputs match; digest {before[:16]}")
    finally:
        if os.path.isfile(backup) and not os.path.isfile(path):
            os.replace(backup, path)
        elif os.path.isfile(backup):
            os.remove(backup)
    return check


def run(app, deep=False):
    census = load_census(app)
    checks = [c1_file_readable(census)]

    c2, records = c2_members_channel_present(census)
    checks.append(c2)
    if records:
        members = census["channels"]["members"]
        checks.append(c3_member_fields(records))
        checks.append(c4_recursion_depth(members, records))
        checks.append(c5_boundary_conserved(members, records))
    if deep:
        checks.append(c6_determinism(app, census))

    print(f"\nCensus adapter validation: {app}")
    print("=" * 72)
    for check in checks:
        mark = "passed" if check.passed else "failed"
        print(f"\n[{mark}] {check.name}")
        for note in check.notes:
            print(f"      {note}")
        for failure in check.failures:
            print(f"      {failure}" if failure.startswith(" ")
                  else f"      × {failure}")

    failed = [check for check in checks if not check.passed]
    print("\n" + "=" * 72)
    if failed:
        print(f"{len(failed)}/{len(checks)} checks failed: ")
        for check in failed:
            print(f"  · {check.name}")
        print("\nUse the diagnostics above as feedback for prompts/census_adapter.md generation, "
              "so the model can repair the adapter. Preserve validation requirements "
              "to ensure complete coverage.")
        return 1
    print(f"All {len(checks)} checks passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Deterministic census adapter validation")
    parser.add_argument("--app", required=True)
    parser.add_argument("--deep", action="store_true",
                        help="Also check determinism by rerunning the census")
    options = parser.parse_args()
    raise SystemExit(run(options.app, deep=options.deep))


if __name__ == "__main__":
    main()
