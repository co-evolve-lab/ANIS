import argparse
from collections import Counter
import hashlib
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else default


def _record_symbol(value):
    """Return a readable label without interpreting whether a record is useful."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "cmd", "normalized_action", "action", "service",
                    "type", "member", "id", "path"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float, bool)):
                return str(candidate)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def census_records(census):
    """Losslessly normalize list records under ``channels``.

    This is deliberately software-independent. Every list member is retained
    with its complete JSON value. Only equivalent complete JSON values are
    deduplicated; all original locations remain attached as provenance. The
    function never decides whether a record should become a public command.
    """
    channels = census.get("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("census.channels must be a JSON object")
    grouped = {}
    occurrence_count = 0

    def walk(prefix, node):
        nonlocal occurrence_count
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else key, value)
            return
        if not isinstance(node, list):
            return
        for index, value in enumerate(node):
            occurrence_count += 1
            canonical = json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
            row = grouped.setdefault(canonical, {
                "channel": prefix,
                "channels": [],
                "symbol": _record_symbol(value),
                "record": value,
                "record_sha256": hashlib.sha256(
                    canonical.encode("utf-8")).hexdigest(),
                "locations": [],
                "occurrences": 0,
            })
            if prefix not in row["channels"]:
                row["channels"].append(prefix)
            row["locations"].append(f"channels.{prefix}[{index}]")
            row["occurrences"] += 1

    walk("", channels)
    records = sorted(
        grouped.values(),
        key=lambda row: (row["channel"], row["symbol"], row["locations"][0]))
    if sum(row["occurrences"] for row in records) != occurrence_count:
        raise RuntimeError("normalized census record count does not balance")
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument(
        "--output",
        help="write to this path instead of apps/<app>/capabilities/ledger.json")
    args = ap.parse_args()
    app_dir = os.path.join(_ROOT, "apps", args.app)
    census = load(os.path.join(app_dir, "census", "raw_ops.json"), {})
    records = census_records(census)
    entries = [{**record, "status": "candidate"} for record in records]

    counts = Counter(e["status"] for e in entries)
    payload = {
        "app": args.app,
        "source": "census/raw_ops.json channels",
        "total": len(entries),
        "normalization": {
            "input_occurrences": sum(
                entry["occurrences"] for entry in entries),
            "unique_records": len(entries),
            "exact_duplicates_removed": sum(
                entry["occurrences"] for entry in entries) - len(entries),
            "policy": "preserve every channels list record; exact JSON dedup only",
        },
        "counts": dict(sorted(counts.items())),
        "entries": entries,
    }
    out = (os.path.abspath(args.output) if args.output else
           os.path.join(app_dir, "capabilities", "ledger.json"))
    out_dir = os.path.dirname(out)
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"[ledger] {args.app}: {dict(counts)} -> {out}")


if __name__ == "__main__":
    main()
