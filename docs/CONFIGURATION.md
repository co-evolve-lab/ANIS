# ANIS configuration

`anis-config.json` is the single repository-wide configuration file. Pass a
different file with `--config`, or set `ANIS_CONFIG` to an absolute path.

## Coding Agent

```json
"coding_agent": {
  "command": "codex",
  "model": null,
  "max_parallel_processes": 3
}
```

- `command`: executable available on `PATH`;
- `model`: `null` uses the Agent's configured default; a string selects one;
- `max_parallel_processes`: hard upper bound shared by concurrent stages.

## Onboarding controller

- `agent_timeout_seconds`: timeout for one direct Agent task;
- `script_timeout_seconds`: timeout for one deterministic script;
- `stage_attempt_limit`: attempts within one internal stage;
- `redo_limit`: how often a failed later gate may return to an earlier stage;
- `minimum_pass_rate`: reference pass rate between `0` and `1`. Failed commands
  are recorded and excluded; successful commands can still be published.

## Capability filtering

- `capability_discovery.batch_size`: records assigned to one batch;
- `capability_discovery.max_parallel_batches`: concurrent batches;
- `capability_review.max_parallel_batches`: concurrent semantic reviews;
- `sandbox`: Agent filesystem permission. Supported values are `read-only`,
  `workspace-write`, and `danger-full-access`.

The semantic review decides only whether a capability should be exposed to a
user. Runtime experiments and parameter implementation belong to Stage 3.

## Command implementation

- `batch_size`: target number of commands in one implementation unit;
- `max_parallel_batches`: independent implementation lanes;
- `max_retries`: retries after the first attempt;
- `timeout_seconds`: one implementation Agent timeout;
- `verify_timeout_seconds`: targeted verification timeout;
- `total_timeout_seconds`: complete Stage 3 timeout.

Object families are kept together when possible. A large family is split into
sequential chunks in one lane, while unrelated families can run in parallel.

## Documentation and verification

- `command_documentation.enabled`: generate the complete guide and Skill;
- `command_documentation.timeout_seconds`: documentation Agent timeout;
- `verification.jobs`: requested verification concurrency. An application may
  lower this with `anis-app.json.verification.max_parallel_jobs`.

All counts are positive integers except `max_retries`, which may be zero.
Invalid values fail before a workflow starts.
