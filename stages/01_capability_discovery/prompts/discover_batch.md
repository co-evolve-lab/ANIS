# Runtime capability investigation

Investigate all items assigned in `batch.json`. The controller determines scope;
do not add or omit items.

## Inputs

- Assignment: `{{BATCH_PATH}}`
- ANIS root, for read-only reference: `{{ANIS_ROOT}}`
- Application census: `{{CENSUS_PATH}}`
- Application ledger: `{{LEDGER_PATH}}`
- Runtime family: `{{RUNTIME_FAMILY}}`

Read the assignment and ledger first. Reuse established evidence without
redesigning ANIS or rewriting the collector. Extract relevant records using the
current symbol and command names rather than loading entire reports or logs.

## Investigation

For each item establish its runtime object or operation, properties and methods,
full parameter and return types, required parent/child objects and context,
minimal valid invocation, observable effects, and persistence after reopening.

- Query actual runtime objects; do not infer interfaces from model memory.
- Follow object references, collections, method return objects, and child objects.
- Record parameter names, types, optionality, defaults, enums, and return types.
- Supply machine-readable `value_kind`. Object references also require
  `target_type`; a command-line string does not make a runtime reference a string.
- Use an empty string for absent defaults and an empty list for absent enum ranges.
- Construct minimal valid arguments for parameterized getters before following
  their returned objects.
- After a failed call, check object, selection, mode, file type, and container
  requirements. Record at most `{{MAX_ATTEMPTS}}` distinct context attempts.
- Use `out_of_scope` only with evidence of purely temporary effects.
- Use `equivalent` only when named existing commands produce the same file effect.
- Use `unsupported` only after real calls in multiple valid contexts establish a
  runtime limitation. Insufficient evidence requires `blocked`.
- List all executable operations in `command_candidates`, including existing
  coverage and missing creation, reading, update, or deletion operations.
- Select a valid `implementation_route` for each candidate: direct property
  assignment, validated enum/file, structured region, resolved runtime object,
  constructed runtime struct or typed sequence, runtime factory, or a single
  session for context-dependent operations.
- Include `persistence_check` describing execution, saving, closing, reopening,
  and independent observation where persistence applies.
- Read inherited methods and complete signatures. Construct native references and
  typed values rather than substituting plain JSON. Preserve temporary handles,
  selections, and editing modes within one session when required.

## Evidence and output

Store scripts, inputs, outputs, and logs under this batch's `evidence/`. Evidence
paths in the result must be relative to the batch directory and exist.

Minimum evidence by status:

- `executable`: runtime signature, successful call, and external observation.
- `equivalent`: existing command documentation and a same-effect comparison.
- `out_of_scope`: runtime evidence of temporary-only or nonpersistent effects.
- `unsupported`: signatures, calls in multiple valid contexts, and specific limits.
- `blocked`: attempts and the unresolved blocker.

Return each assigned `item_id` exactly once. Candidate lists must be complete,
with kebab-case names, capability classes, selectors, and parameters. Do not
modify shared census, ledger, atlas, commands, or release files. Use English for
explanations. Output only JSON conforming to the supplied schema.
