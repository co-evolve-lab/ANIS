# Command definition contract

Each `apps/<app>/atlas/*.json` file defines one command. `anis/build.py` renders
its public specification and a thin implementation into
`commands/<command>/{spec.json,impl.py}`. Execution is delegated to shared
binding templates and the application engine.

A property definition has this shape:

```json
{
  "command": "object-set-property",
  "summary": "Set a property on the selected object",
  "binding": {
    "kind": "named_prop",
    "path": "<runtime-property>",
    "value": {"type": "bool", "true": 1, "false": 0}
  },
  "census_ref": {"channel": "<census-channel>", "symbol": "<runtime-symbol>"},
  "demo": {"text": "Demo text", "args": {"name": "example", "value": true},
           "expect": true},
  "errors_extra": {}
}
```

Replace illustrative values with runtime-verified ones. Property binding kinds
must have selector arguments declared in the application contract. Supported
value conversions are defined in `anis/proptemplate.py`; they include boolean,
integer, float, string, enum, color, and vector forms. Boolean values may declare
thresholds or explicit raw true/false values. Enums declare `values` and optional
`names`. Numeric values may declare `min` and `max`.

Action and observation bindings use `verb`, `transform`, `processing`, or
`observation` with explicit `binding.cli_args`. Their engine calls are defined by
`anis/actiontemplate.py`, `anis/transformtemplate.py`, and `anis/build.py`.
Processing definitions may additionally record algorithm, output parameter,
output kind, inputs, and parameter metadata required by the application engine.

`census_ref` establishes provenance. `demo` provides deterministic arguments and
normalized expected results for external verification. `errors_extra` optionally
declares additional public error codes. Read `render_spec()` and `render_impl()`
in `anis/build.py` for the exact fields consumed by the current builder.
