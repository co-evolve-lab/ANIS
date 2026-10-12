# Programmatic entrypoint discovery

Find at least one programmatic query entrypoint for {app} that works on this
machine. Declare how to run a collector stored in the application directory.
The collector may use any suitable language or official interface.

Launcher hint: {launch}

## Scope

An entrypoint must repeatedly expose information reported by the installed
application or its official installation registries without human interaction.
Possible sources include command registries, batch actions, interactive command
shells, extension manifests, interprocess services, embedded scripts, and official
libraries. Execution inside the application process is not required.

This step discovers entrypoints and the collector runner. A later step writes
`apps/{app}/census.py`, combining entrypoints as needed to cover:

1. Operation dispatch registries.
2. Creatable types, services, or plugins.
3. Document object types.
4. Menu and interface operations.
5. Object properties and methods.
6. Named type existence checks.

One entrypoint need not cover all channels. The collector records unavailable
channels with observed reasons.

## Required local checks

Record commands and results in `notes`:

1. Query at least one real capability or type without interactive input.
2. If execution is supported, run a harmless minimal action and exit normally.
3. Verify that the collector host can execute a temporary script and exit.
4. Identify display, isolated profile, environment, and timeout requirements.

Information must come from the installed software or its official registries.
Commands must execute and results must be reproducible.

## Output

Write `apps/{app}/runtime.json`:

```json
{
  "runner": ["<collector host>", "<argument>", "{script}"],
  "prefix": null,
  "env": {},
  "mkdirs": [],
  "document_extension": ".<editable-document-extension>",
  "entrypoints": [
    {
      "kind": "<entrypoint category>",
      "query": "<verified query command or call>",
      "execute": "<verified execution call, or explanation if unavailable>"
    }
  ],
  "notes": "Verified commands, output summaries, and runtime requirements"
}
```

- `runner` is a command template containing exactly one `{script}` placeholder.
  A common value is `["python3", "{script}"]`, but the language is unrestricted.
- `prefix` optionally declares an environment root referenced as `{prefix}` in
  `runner` and `env`.
- `env` supplies environment variables. Values ending in `:` prepend to existing
  values; values starting with `:` append to existing values.
- `mkdirs` lists directories to create before execution.
- `document_extension` determines the verifier's demo file extension.
- `entrypoints` records verified interfaces available to the collector.

## Validation

```bash
python3 anis/onboard.py --app {app} --only "Discover programmatic entrypoint"
```

This check verifies the declaration and host executable. The later collector run
checks whether the entrypoint actually produces a capability inventory.

Only report `BLOCKED.md` after investigating local command help, operation
registries, interactive interfaces, extension registries, official bridges,
embedded scripts, and installation files without finding a repeatable query
interface. Missing embedded Python, complete reflection, or individual channels
alone does not establish a blocker.

## Constraints

- Write only under `apps/{app}/`.
- Do not access the network.
- Do not treat model memory or online documentation as installed-version evidence.
- Never invent query results; record incomplete coverage in the collector output.
- Write generated documentation, messages, and metadata in English.
