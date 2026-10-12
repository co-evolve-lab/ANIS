# ANIS

ANIS (Agent-Native Interface Synthesizer) is a standalone source distribution
that converts an installed desktop application into a command-line interface
for agents. Each released command is an atomic read, write, or operation on an
application object, implemented against the running application and validated
before release.



## Workflow

The workflow has four main stages:

1. **Capability discovery:** identify programmatic entrypoints, collect runtime
   capabilities, and create the application adapter.
2. **Semantic filtering:** decide which capabilities support useful user commands.
3. **Command implementation:** implement object-family batches in isolated working
   copies, then build and validate commands through Python controllers.
4. **Documentation and release:** generate command documentation and an agent
   skill, then package passing commands under `dist/<app>-anis/`.

Coding agents perform scoped tasks. Python controllers determine progression,
validation results, and release contents.

## Goal-level commands

After release, ANIS can extend an interface with goal-level commands that
package recurring compositions of atomic commands
(`stages/06_goal_commands/`). Set `ANIS_CALL_LOG` to a per-request file while
an agent serves a request; every released command then appends its invocation.
Describe each request with one JSONL line:

```json
{"id": "r1", "request": "task text", "workdir": "inputs/r1", "call_log": "logs/r1.jsonl"}
```

`workdir` is a clean copy of the request's starting files; relative paths are
resolved against the JSONL file. Then run:

```bash
./bin/anis-release goals --app <app> --requests requests.jsonl --release
```

Contiguous mutation sequences that recur in at least three requests become
candidates. The coding agent judges whether each candidate realizes a shared
artifact-level subgoal and names it `<object>-<action>`. A named command is
released only if, rerun on clean copies of its originating requests, it saves
the same state as the atomic commands it replaces. Decisions are written to
`stages/06_goal_commands/runs/`.

## Requirements

- Linux and Python 3.10 or later; Python code uses the standard library.
- A command-line coding agent; the default executable is `codex`.
- An installed target application that the current user can launch.
- Permission to create test files and enough disk space for results and logs.

## Quick start

Run from this directory:

```bash
chmod +x bin/anis-release bin/anis-bootstrap
./bin/anis-release doctor
./bin/anis-bootstrap "Convert my XXX[installed application] into agent commands"
```

## Application directory

```text
apps/<app>/
├── runtime.json
├── census.py
├── census/raw_ops.json
├── engine.py
├── atlas_gen.py
├── anis-app.json
├── atlas/
├── commands/
├── build/report.json
├── guide/
└── skills/
```

## Benchmark

The DeskIF-Bench tasks used in our study are available on Hugging Face:
[SimAlan/DeskIF-Bench](https://huggingface.co/datasets/SimAlan/DeskIF-Bench).

## License

ANIS is released under the [MIT License](LICENSE).
