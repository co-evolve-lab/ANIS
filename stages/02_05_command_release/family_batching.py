"""Plan command batches around runtime object families."""

from __future__ import annotations

import re


CONTRACT_FIELDS = {
    "schema_version", "runtime_type", "inheritance", "locator",
    "shared_resolver", "relationships", "persistence_rules",
}


def object_family(operation):
    """Return the static runtime object that owns one implementation item."""
    discovery = operation.get("discovery") or {}
    assigned = operation.get("assigned") or {}
    record = assigned.get("record") or {}
    if not isinstance(record, dict):
        record = {}
    family = (
        operation.get("object_family")
        or discovery.get("runtime_object")
        or record.get("object")
    )
    return str(family or f"command:{operation.get('command', 'unknown')}")


def _contract_name(family):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", family).strip("-.")
    return f"contracts/{slug or 'runtime-object'}.json"


def plan_family_lanes(operations, batch_size):
    """Pack small families together and split large families sequentially.

    A lane is executed in order. Different lanes may run concurrently. This
    lets a large family reuse the contract and shared resolver produced by its
    previous shard without forcing unrelated families to wait.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    families = {}
    for operation in operations:
        family = object_family(operation)
        families.setdefault(family, []).append(operation)

    raw_lanes = []
    packed = []
    packed_families = []

    def flush_packed():
        nonlocal packed, packed_families
        if packed:
            raw_lanes.append([{
                "items": packed,
                "families": packed_families,
                "family_part": None,
                "family_parts": None,
                "family_contract": None,
            }])
        packed = []
        packed_families = []

    for family, items in families.items():
        if len(items) > batch_size:
            flush_packed()
            parts = [
                items[start:start + batch_size]
                for start in range(0, len(items), batch_size)
            ]
            contract = _contract_name(family)
            raw_lanes.append([{
                "items": part,
                "families": [family],
                "family_part": index,
                "family_parts": len(parts),
                "family_contract": contract,
            } for index, part in enumerate(parts, 1)])
            continue

        if packed and len(packed) + len(items) > batch_size:
            flush_packed()
        packed.extend(items)
        packed_families.append(family)
    flush_packed()

    counter = 0
    for lane in raw_lanes:
        for batch in lane:
            counter += 1
            batch["number"] = counter
    return raw_lanes


def contract_error(payload):
    """Return a short structural error for a generated family contract."""
    if not isinstance(payload, dict):
        return "object-family contract must be a JSON object"
    missing = sorted(CONTRACT_FIELDS - set(payload))
    if missing:
        return f"object-family contract missing fields: {', '.join(missing)}"
    return ""
