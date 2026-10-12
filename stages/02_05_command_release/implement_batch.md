# ANIS command implementation batch

You are implementing a fixed batch of software capabilities approved for user
exposure. Stage 1 deliberately performed no runtime probe, parameter design,
evidence review, or persistence test. Those responsibilities begin here. Work
only in the isolated ANIS workspace selected as your current directory. It is a
private candidate copy, not the shared source.

The controller will append a JSON batch after this prompt. Implement every
operation in that batch exactly once. Do not implement operations outside it.
`reference_anis_root` names the original ANIS tree. Use it only to read shared
framework code and the structured census records referenced by each operation.
Write every change under the current isolated workspace.

Rules:

1. The command name is fixed by `command`. Do not rename it.
2. Read the complete structured census record in `assigned.record` and the
   current app census. Determine the exact runtime call and only the parameters
   needed by the public command. Perform a minimal real call while implementing;
   verify it against the installed application and keep runtime dependencies
   inside the application package.
3. The command must be general. Never hard-code an evaluation task ID, fixture
   path, task-specific value, row count, object name, or expected answer.
4. Add one atlas JSON file per command under `apps/<app_dir>/atlas/`. Use the
   existing atlas schema and a stable engine binding.
   **Never write to `apps/<app_dir>/census/raw_ops.json`.** That file is owned by
   the census script and is overwritten wholesale on every rerun; symbols added
   there by hand vanish the next time census runs.
   If the capability you implemented is genuinely absent from the census, record
   it in `apps/<app_dir>/census/attested.json` with `symbol`, a `reason` stating
   why automatic enumeration missed it, a `gap_class`, and — when the capability
   is a composition — a `composed_of` list naming the underlying runtime calls.
   Then set the atlas `census_ref.channel` to `"attested"`. The hallucination
   gate accepts that channel but labels the command as human-attested rather
   than runtime-enumerated, so the release can report the two sources separately.
5. Implement execution in the app's existing engine. Prefer a shared dispatcher
   for this capability family over duplicated code. Do not edit generated files
   under `apps/<app_dir>/commands/` by hand.
   Treat commands as members of static runtime object families, not as isolated
   functions. Before editing, inspect the owning classes, inheritance,
   constructor return types, declared relationships and their accepted endpoint
   types, and existing atlas cells for every `object_families` entry in the
   batch. Derive compatibility from those
   static facts, never from surface argument names such as `id`, `name`, or
   `element_id`, and never by trying every pair of commands. Commands for one
   runtime type must reuse one resolver and one stable locator meaning. A create
   command must return the locator consumed by that type's query, update, and
   delete commands. Every statically valid relationship endpoint must be
   addressable by the public interface. Derive its locator from the owning
   software's object identity and lifetime semantics; do not assume that every
   object has a user-assigned id, a name, or any other universal locator. When
   the runtime model has no persistent per-object identifier, expose an
   explicit deterministic context selector justified by that model. Keep the
   concrete locator choice in the app atlas and engine; never promote an
   application-specific exception into this shared workflow rule.

   When `family_contract` is present, this is one shard of an oversized object
   family. Part 1 must create that exact JSON path; later parts must read and
   follow it instead of redesigning the family. Use exactly these top-level
   fields: `schema_version`, `runtime_type`, `inheritance`, `locator`,
   `shared_resolver`, `relationships`, and `persistence_rules`. A relationship
   records only links established by the target software's static object model;
   use an empty list when the runtime type has no relevant links. Keep it short.
   If a resumed older run has no contract yet, create it before editing
   commands. If static source cannot establish a relationship, write `unknown`
   rather than inventing one. The controller runs shards of this family in
   order, while unrelated families remain parallel.
6. Parameters copied from census are raw source information, not a finished
   public interface. Expose only parameters an ANIS user can supply as JSON-compatible CLI values.
   Include selectors needed to locate the target object and output paths needed
   by file-producing commands. Reject invalid targets and unsafe overwrite
   cases with AnisError.
   When `requires_runtime_investigation` is true, the route in the work order is
   only a safe placeholder. Replace it in the atlas with the route established
   by the implementation investigation. The final atlas `generation_route` is
   mandatory, not advisory:
   - `resolve_runtime_object`: resolve a CLI name/identifier to the real runtime
     object and verify its required type before assignment.
   - `construct_runtime_struct` / `construct_typed_sequence`: construct the
     software-native value recursively; never assign a JSON string directly.
   - `create_from_runtime_factory`: create and attach the object through the
     registered service/factory and provide create/read/update/delete commands
     listed by Stage 1.
   - `single_session_operation`: perform every selection/mode/sub-operation in
     one engine invocation so temporary handles are not lost between CLI calls.
   ANIS has one Boolean parameter syntax: every Boolean command parameter is
   written explicitly as `--name true` or `--name false`. Never define a
   presence-only Boolean switch and never put a bare `--force` in a demo or
   recipe. Only the interface controls `--help`, `--schema`, and `--recipe`
   have no value. `force` means the ordinary Boolean overwrite parameter; if
   the target software has a different numeric/domain concept also called
   “force”, expose it under an unambiguous name such as `force-value` or
   `plotter-force`.
7. Read-only operations must not change the file. Editing operations must save
   the file; file-producing operations must report the actual output path.
8. Give every atlas cell a deterministic demo using the existing make-demo
   fixture and an externally checkable expectation. During this app-scoped
   batch, do not edit shared files under `anis/`; if the standard verifier
   cannot express a necessary observation, add a general observation operation
   inside this app's engine and record the remaining verifier limitation in the
   final message. Never special-case a task ID or return a precomputed answer.
9. Do not run `anis/build.py` or `anis/verify.py` from this worker. Other fixed
   implementation batches run in their own isolated copies. After you finish,
   Python starts a merge Agent that compares your candidate with its baseline
   and the latest shared app, merges `engine.py` and other required app files,
   then exclusively runs build and `anis/verify.py --only <command>` for every
   command in this batch. It returns the exact failures for repair. Fix those
   failures on the next attempt.
   Verification must implement the supplied `persistence_check`, including a
   saved/reopened file for persistent editing commands.
   Every direct target-application subprocess used while investigating must have
   a 60-second timeout (for example, `timeout 60s ...`). If it times out, kill
   that process tree and treat that experiment as failed; never leave a GUI,
   Xvfb, or shell process waiting in the background.
10. Preserve all existing commands and behavior. Do not modify another app or
    shared files under `anis/`.
11. Reuse verified application implementations where available. Commands must
    remain discoverable by ordinary English words in their names or summaries.
12. Persistent edits must be saved and reopened, preserving unrelated document
    content. Multi-step operations must resolve their targets and complete
    within one application session when runtime handles are session-local.
13. Do not silently drop a assigned command because its generic demo fixture is
    inconvenient. Create a deterministic app-level fixture, then make the
    command pass the same external verification and repeat-execution checks as
    every other released command.

At the end, print a compact table with each operation, command, and implementation
status. Mark verification as pending controller validation; only the controller
may change it to passed after invoking the command through ANIS CLI.

Write generated documentation, messages, and metadata in English.
