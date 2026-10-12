"""Turn Stage-1 runtime types into deterministic command-generation routes.

The discovery agent still records the exact software type.  This module adds a
small, machine-readable classification so command generation cannot silently
treat every CLI value as a scalar string.
"""

import re


ROUTES = {
    "scalar": "direct_property",
    "enum": "validated_enum",
    "file": "validated_file",
    "region": "structured_region",
    "object_reference": "resolve_runtime_object",
    "struct": "construct_runtime_struct",
    "sequence": "construct_typed_sequence",
    "service": "create_from_runtime_factory",
    "stateful": "single_session_operation",
}


def value_kind(type_text):
    text = str(type_text or "").lower()
    if any(word in text for word in (
            "interface reference", "object reference", "runtime object",
            "pointer", "handle", "locator", "object id")):
        return "object_reference"
    if any(word in text for word in (
            "sequence<", "[]", "collection", "index access", "name access",
            "list[", "array of object", "property value")):
        return "sequence"
    if any(word in text for word in (
            "struct", "point", "size", "rectangle", "matrix", "vector")):
        return "struct"
    if any(word in text for word in (
            "service", "factory", "createinstance", "new object")):
        return "service"
    if any(word in text for word in (
            "selection state", "active object", "edit mode", "session",
            "multi-step", "transaction")):
        return "stateful"
    if any(word in text for word in ("file", "path", "directory", "uri", "url")):
        return "file"
    if any(word in text for word in ("range", "region", "extent", "bbox")):
        return "region"
    if "enum" in text or "one of" in text:
        return "enum"
    return "scalar"


def route_for(discovery, candidate):
    """Return all routes required by one command candidate."""
    explicit = candidate.get("implementation_route")
    if explicit:
        return explicit
    kinds = {value_kind(item.get("type")) for item in discovery["parameters"]}
    capability = candidate.get("capability_class", "").lower()
    operation = " ".join((
        discovery.get("operation", ""), capability,
        candidate.get("selector", ""))).lower()
    if any(word in operation for word in (
            "create", "insert", "factory", "lifecycle")):
        kinds.add("service")
    if any(word in operation for word in (
            "selection", "active object", "mode", "multi-step", "transaction")):
        kinds.add("stateful")
    if not kinds:
        kinds.add("scalar")
    priority = (
        "stateful", "service", "object_reference", "sequence", "struct",
        "region", "file", "enum", "scalar",
    )
    return ROUTES[next(kind for kind in priority if kind in kinds)]


def command_name(symbol, candidate):
    name = candidate.get("name", "").strip()
    if name:
        return name
    return re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
