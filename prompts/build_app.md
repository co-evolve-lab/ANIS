# Application adapter implementation

The census for {app} is available at `apps/{app}/census/raw_ops.json`.
Create these application-owned files:

1. `apps/{app}/engine.py`: application execution adapter.
2. `apps/{app}/atlas_gen.py`: probe that writes initial command definitions under
   `apps/{app}/atlas/`.
3. `apps/{app}/anis-app.json`: build, verification, and release contract.

Do not run filtering, batch implementation, documentation, or release. The Python
controller schedules those stages after validating these outputs.

## Read first

- `anis/proptemplate.py` for property binding and normalization.
- `anis/build.py` and `docs/ATLAS.md` for command definition and rendering contracts.
- `anis/app_contract.py` for application contract structure and path boundaries.
- `apps/{app}/census/raw_ops.json` for actual available symbols.

## Architecture

```text
atlas/*.json -> build.py -> commands/<command>/{spec.json,impl.py}
                        -> verify.py -> build/report.json -> freeze.py
```

Generated implementations contain bindings and invoke shared templates. Property
writers call `proptemplate.run`; getters call `proptemplate.get`. Application
execution belongs in the engine. Declare any additional runtime files explicitly
in `release.runtime_files`.

## Application contract

```json
{
  "schema_version": 1,
  "adapter": null,
  "build": {"file_arg_help": "Artifact file path", "selector_args": {}},
  "verification": {"max_parallel_jobs": 1},
  "documentation": {"example_file": "demo.out"},
  "release": {
    "runtime_files": ["engine.py"],
    "anis_modules": [
      "__init__.py", "kernel.py", "cli.py", "proptemplate.py",
      "actiontemplate.py", "transformtemplate.py"
    ]
  }
}
```

Declare selectors by binding kind in `build.selector_args`; it may be empty when
there are no property bindings. Command-specific arguments belong in
`binding.cli_args`. Include only files required to execute this application.
Set `adapter` only when general verification or release hooks are insufficient;
the adapter must remain under the application directory.

## Engine contract

- `get_raw(args, binding)` returns the raw scalar or list value. Raise
  `AnisError("TARGET_NOT_FOUND", ...)` when selectors do not resolve a target.
- `set_raw(args, binding, raw)` writes the raw value and saves the file.
- `describe_target(args, binding)` returns a human-readable target description.
- `make_demo_doc(path, text=None, table_cells=None)` creates verifier demo files.
- `observe(path)` returns JSON-compatible document state.

Import `AnisError` from `anis.kernel`. Error codes must appear in the command
specification. Standard errors include `TARGET_NOT_FOUND`, `INPUT_NOT_FOUND`,
`ENGINE_UNAVAILABLE`, and `OPEN_FAILED`; declare others in `errors_extra`.

Return raw engine values; shared templates handle normalization. `demo.expect`
must match normalized values: JSON booleans, uppercase hexadecimal colors,
declared enum names, and numeric vector lists. Prefer exactly representable
floating-point demo values.

Define selector semantics consistently for each binding kind. Handle structured
runtime properties inside the engine rather than embedding application logic in
shared code.

## Command definitions

See `docs/ATLAS.md`. Every definition must reference a real census symbol and have
a deterministic demo constructible by `make_demo_doc`.

- Boolean parameters use `--name true` or `--name false`. Only `--help`,
  `--schema`, and `--recipe` are valueless interface controls. File overwrite is
  `--force true`; use a distinct name for unrelated domain concepts.
- Validate `census_ref.channel` and `census_ref.symbol` against actual census data.
- Use action bindings for actions instead of forcing them into property templates.
- Confirm readable and writable behavior with a real minimal probe before adding
  property commands. Use observation bindings for read-only commands.
- Produce 3 to 12 initial definitions to validate adapter integration. Later
  filtering and implementation stages determine broader command coverage.

## Acceptance and constraints

The application contract must load, the engine and probe must exist, the declared
runner must execute the probe, and the probe must produce at least one valid
census-backed command definition.

Write only under `apps/{app}/`. Do not modify shared code or other applications.
Report unsupported template requirements as blockers. Do not hide verification
failures, special-case demo inputs, or return predetermined results. Do not access
the network. Use English for generated documentation and diagnostics.
