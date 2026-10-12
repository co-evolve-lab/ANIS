# User-facing command semantic filtering

Decide whether each candidate can support a meaningful user command after being
wrapped in a suitable command interface. Raw class names, methods, and parameters
are implementation entrypoints; the final command may use clearer names.

Input: `{{BATCH_PATH}}`. It contains complete census records, source channels,
and original locations.

Interpret `record.object` together with `record.member`. Describe the operation a
user could perform, then decide for every `item_id`:

- `expose`: performs a user operation or provides a necessary creation, reading,
  modification, deletion, reference, composition, import, export, or query step.
- `skip`: has no observable user result and serves only runtime bookkeeping,
  debugging, callbacks, caching, traversal, or type registration; or an explicitly
  listed `existing_commands` entry fully covers the same semantics and target.

## Decision rules

1. Constructor, factory, container, node, object-model, or low-level terminology
   alone is not a reason to skip. Expose operations that create or organize
   persistent user content.
2. Reusable definitions such as styles, templates, symbols, patterns, gradients,
   filters, masks, resources, and pages are user content even when not directly
   visible. Expose their creation, editing, referencing, and deletion.
3. An operation need not complete an entire workflow on its own. Expose necessary
   component steps; later implementation determines their composition.
4. Skip for existing coverage only when the input explicitly lists a fully
   equivalent command. Similar names or categories do not establish equivalence.
5. Apply consistent criteria to related objects and operations. Familiarity with
   an object type must not determine whether its creation is exposed.
6. When the record does not establish that an operation is purely internal,
   choose `expose` for later runtime investigation.

Type names, constants, registry descriptions, debug information, cache upkeep,
event callbacks, internal serialization, and generic traversal without a user
result are possible exclusions. These are semantic examples, not keyword rules.

Judge only the candidate name, source category, and complete raw record. Do not
run software, create probes, inspect logs or evidence, investigate parameters,
design commands, or plan persistence checks. Missing runtime evidence at this
stage does not justify skipping a useful capability.

Return exactly one entry per assigned `item_id`, containing `item_id`, `decision`,
and a short English `reason`. Exposure reasons describe the user operation.
Exclusion reasons explain the absence of an observable user result or name the
explicitly listed equivalent command. Generic labels such as "internal" or
"constructor" are insufficient. Do not modify files. Output only JSON conforming
to the supplied schema.
