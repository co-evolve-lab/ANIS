# Capability census adapter

Implement `apps/{app}/census.py` to enumerate capabilities reported by the
installed application and write `apps/{app}/census/raw_ops.json`. The collector
must run without a model after implementation. Its values must come from actual
runtime responses or official installation registries.

Launcher hint: {launch}

## Read first

- `anis/census.py` and `apps/{app}/runtime.json` for collector execution.
- `anis/conformance.py` for deterministic validation requirements.
- The channel and record contracts below.

## Verify the environment

Before implementing the collector, verify noninteractive query and execution
entrypoints. Identify the actual source for each channel. Test the smallest
query for every proposed source before combining them into the collector.
Multiple entrypoints and external interpreters are allowed.

## Collection channels

- **Dispatch:** enumerate the registries used by the engine to resolve operations.
  Include every available operation registry.
- **Engine roster:** enumerate creatable types, services, plugins, and extensions
  from runtime queries or official installation records.
- **Document roster:** enumerate object types that a document or project can hold.
- **Menu:** collect operations advertised by menu configuration or registrations.
  Try a virtual display when needed; record actual errors if unavailable.
- **Members:** enumerate both properties and methods recursively. Record each
  member individually with its complete signature:

  ```json
  {
    "object": "<runtime object type>",
    "member": "<member name>",
    "kind": "method",
    "params": [{"name": "<parameter>", "type": "<runtime type>"}],
    "returns": {"type": "<runtime type>", "is_object": true},
    "writes": false,
    "universal": false,
    "reached_from": "<path from the root object>"
  }
  ```

  `kind` is `property` or `method`. Type names must come from the application;
  empty or `unknown` types discard information needed for implementation.
  For object-returning members, obtain an actual instance and enumerate its
  members. Deduplicate by object type, not by instance. Expand beyond the root
  level: nested collections and child objects may expose additional operations.
  Record each unexpanded declared object type in `channels.members.boundary`
  with its member and the specific reason it could not be obtained. Account for
  declared return types using expanded types and boundary entries; do not silently
  discard inaccessible types.
- **Type spot-check:** use locally documented type names to check three or four
  existing types and one invented nonexistent type. Verify rejection of the
  invented type. Report whether the full type tree can be enumerated, with evidence.

List unavailable channels and specific reasons in `unavailable`. Do not fabricate
counts or substitute documentation claims for runtime evidence.

## Reconciliation

Compare at least two independent channels, preferably menu and dispatch:

- Report intersection counts and coverage.
- List both set differences in full, distinguishing structural placeholders from
  executable candidates.
- Resolve every dispatch entry again and record failed lookups.

Report actual coverage without optimizing for a preferred number.

## Output contract

The following illustrates the required top-level and member-channel structure:

```json
{
  "app": "{app}",
  "engine_version": "<runtime version>",
  "channels": {
    "dispatch": {},
    "engine_roster": {},
    "document_roster": {},
    "menu": {},
    "members": {
      "records": [],
      "boundary": [
        {"object": "<type>", "member": "<member>",
         "declared_type": "<return type>", "reason": "<observed limitation>"}
      ],
      "expanded_types": 0,
      "declared_object_types": 0,
      "max_depth": 0,
      "count": 0
    },
    "type_spotcheck": {}
  },
  "reconciliation": {},
  "unavailable": []
}
```

Populate the illustrated empty collections with observed data. Member fields are
fixed because validation and downstream processing consume them directly. Other
channels may use application-specific internal structures. `count` equals the
number of member records; `max_depth` reflects actual recursive traversal and must
reach at least two levels for member conformance. Compute type accounting from
actual records and boundaries.

Write the JSON file and print per-channel counts and reconciliation results.

## Acceptance

1. `python3 anis/census.py --app {app}` exits successfully.
2. The output is valid JSON, with every channel present or explicitly unavailable.
3. Counts and set accounting agree with the underlying records.
4. Repeated runs are deterministic: sort output and exclude timestamps, temporary
   paths, memory addresses, and unstable iteration order.
5. `python3 anis/conformance.py --app {app}` passes. Repair the reported records
   instead of weakening validation.

## Constraints

- Collection uses no model calls and no network access.
- Never present sampling as a complete inventory. Declare unavoidable sampling.
- Preserve raw vocabulary; semantic filtering happens later.
- Write only under `apps/{app}/`; do not modify shared code or other applications.
- Write generated documentation and diagnostics in English.
