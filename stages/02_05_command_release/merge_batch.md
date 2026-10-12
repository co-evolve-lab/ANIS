# ANIS isolated implementation merge

You are the merge worker for one completed ANIS command-implementation batch.
The Python controller gives you three app directories:

- `baseline_app`: the exact app snapshot from which the implementation worker started;
- `candidate_app`: that worker's isolated result;
- `destination_app`: the current shared app, which may already contain changes from other batches.

Compare all three directories and merge only the candidate's intended changes
into the destination. The implementation worker and this merge worker use the
same configured coding agent and model; this is a separate task, not a separate
agent identity or model setting.

Rules:

1. The listed commands are the complete scope. Do not implement a new command
   and do not change another app or shared files under `anis/`.
2. Treat `baseline_app -> candidate_app` as this batch's proposed change and
   `baseline_app -> destination_app` as changes already accepted from other
   batches.
3. Semantically merge `engine.py`: preserve every accepted destination branch,
   handler, import, helper, and behavior while adding the candidate's work.
   Never replace the destination engine wholesale with the candidate copy.
4. Merge other app-owned source or data files required by the listed commands,
   including their atlas cells, object-family contracts, and justified census
   attestations. Preserve
   unrelated destination entries. Do not edit generated `commands/` files.
5. Inspect the actual differences. Do not assume that every file in the
   candidate changed, and do not reapply changes that are already present.
6. Do not run `anis/build.py`, `anis/verify.py`, or freeze a release. Python owns
   those decisions and will mechanically reject commands that do not work.
7. If two changes genuinely conflict, resolve them so both accepted destination
   behavior and the candidate command remain supported. Report any unresolved
   conflict clearly in the final message; never silently discard either side.

At the end, summarize the destination files changed and any conflict resolved.
Verification remains pending the Python controller.
