# Stage 1 agent rules

This directory is an evidence-producing capability-discovery workspace.

- Work only on item IDs present in the assigned `batch.json`.
- Do not edit files under the repository's `apps/`, `anis/`, or `dist/`
  directories, or existing capability ledgers.
- Put temporary probes and evidence only inside the current batch directory.
- Do not read a whole large build report or log. Use the current `symbol`,
  command names, and `item_id` with `rg` or `jq` to extract only relevant
  records.
- Do not mark an item unsupported after one failed call. Inspect its runtime
  type, parameter types, required parent/child objects, and retry with a
  mechanically valid minimal context.
- A type name or menu label alone is not an executable capability.
- Never claim success without a real runtime call and an observable result.
- Return every assigned item exactly once in the required JSON schema.
- If evidence is incomplete, return `blocked`; never silently omit the item.
