import inspect
import json
import os
import shlex
import sys

from anis import kernel

# Only interface controls are valueless. All command parameters, including
# booleans such as force/overwrite, use the single form ``--name true|false``.
_FLAG_NAMES = ("schema", "recipe", "help")


_BUILTIN_SPECS = {
    "observe": {
        "summary": "Inspect document state (built-in): title, layers/objects and their states",
        "args": {"file": {"type": "path", "required": True,
                          "help": "Output file path"}},
    },
    "make-demo": {
        "summary": "Create a demo document for recipes and probes; "
                   "existing files require --force true to overwrite.",
        "args": {"file": {"type": "path", "required": True,
                          "help": "Demo document path"},
                 "text": {"type": "string",
                          "help": "Demo content; use literal \\n or newline separators"},
                 "table": {"type": "string",
                           "help": "Rows separated by | and cells by , ; for example 'A,B|C,D'"},
                 "force": {"type": "bool", "default": False,
                           "help": "Allow overwriting existing files (disabled by default)"}},
    },
    "dir": {
        "summary": "Discover commands (built-in): dir lists categories; dir --search searches keywords; dir <command> shows full documentation",
        "args": {
            "command": {"type": "string",
                        "help": "Command to inspect (optional positional argument)"},
            "search": {"type": "string",
                       "help": "Search command names and descriptions; all terms must match"},
        },
    },
    "guide": {
        "summary": "Read the generated command guide",
        "args": {},
    },
}


def _tool_name(app_dir):
    """Derive the launcher name from an application package directory."""
    base = os.path.basename(os.path.normpath(app_dir))
    return base if base.endswith("-anis") else base + "-anis"


def _file_example(app_dir):
    """Read the display-only example filename from the app-owned contract."""
    path = os.path.join(app_dir, "anis-app.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                contract = json.load(handle)
            example = (contract.get("documentation") or {}).get(
                "example_file")
            if isinstance(example, str) and example.strip():
                return example.strip()
        except (OSError, ValueError):
            pass
    return "demo.out"


def _example_for(app_dir, spec):
    """Render one copyable example from the command's verified demo values."""
    tool = _tool_name(app_dir)
    parts = [tool, spec["command"], "--file", _file_example(app_dir)]
    demo_args = (spec.get("demo") or {}).get("args", {})
    for k, v in demo_args.items():
        if k not in spec.get("args", {}):
            continue

        cli_key = k.replace("_", "-")
        parts += ([f"--{cli_key}", shlex.quote(str(v))] if isinstance(v, str)
                  else [f"--{cli_key}", json.dumps(v)])
    return " ".join(parts)


def _builtin_example(app_dir, name):
    tool, f = _tool_name(app_dir), _file_example(app_dir)
    if name == "observe":
        return f"{tool} observe --file {f}"
    if name == "make-demo":
        return f"{tool} make-demo --file {f} --text 'Alpha\\nBeta'"
    if name == "guide":
        return f"{tool} guide"
    return (f"{tool} dir ; {tool} dir --search 'keyword'; "
            f"{tool} dir <command>")


def _parse(argv):
    if not argv:
        return None, set(), {}
    command, flags, kv = argv[0], set(), {}
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("--"):
            kernel.emit({"status": "error", "code": "INVALID_ARGS",
                         "message": f"Unexpected argument {tok!r}", "fix": "Argument syntax: --name value"},
                        kernel.EXIT_BAD_ARGS)
        name = tok[2:]
        if name in _FLAG_NAMES:
            flags.add(name)
            i += 1
        else:
            if i + 1 >= len(argv):
                kernel.emit({"status": "error", "code": "INVALID_ARGS",
                             "message": f"--{name} is missing a value", "fix": "Argument syntax: --name value"},
                            kernel.EXIT_BAD_ARGS)
            kv[name] = argv[i + 1]
            i += 2
    return command, flags, kv


def _normalize_cli_keys(kv, spec):
    """Accept conventional --kebab-case for internal snake_case arguments."""
    known = spec.get("args", {})
    normalized = {}
    for key, value in kv.items():
        target = key
        snake = key.replace("-", "_")
        if key not in known and snake in known:
            target = snake
        if target in normalized:
            kernel.emit(
                {"status": "error", "code": "INVALID_ARGS",
                 "message": f"Argument --{key} duplicates another spelling",
                 "fix": f"Use only --{target.replace('_', '-')} one spelling"},
                kernel.EXIT_BAD_ARGS)
        normalized[target] = value
    return normalized


def _iter_specs(app_dir):
    cmd_root = os.path.join(app_dir, "commands")
    if os.path.isdir(cmd_root):
        available = os.listdir(cmd_root)
        ordered = []
        guide_path = os.path.join(app_dir, "guide", "commands.json")
        if os.path.isfile(guide_path):
            with open(guide_path, encoding="utf-8") as handle:
                guide = json.load(handle)
            ordered.extend(
                row.get("command") for row in guide.get("commands", [])
                if isinstance(row, dict) and row.get("command") in available)
        seen = set(ordered)
        ordered.extend(name for name in available if name not in seen)
        for name in ordered:
            spec_path = os.path.join(cmd_root, name, "spec.json")
            if os.path.isfile(spec_path):
                with open(spec_path, encoding="utf-8") as f:
                    yield name, json.load(f)


def _catalog(app_dir):
    builtin_rows = [{"command": n, "summary": _BUILTIN_SPECS[n]["summary"]}
                    for n in ("dir", "guide", "observe", "make-demo")]
    by_obj = {"builtin": builtin_rows}
    for name, spec in _iter_specs(app_dir):
        obj = spec.get("object") or name.split("-")[0]
        by_obj.setdefault(obj, []).append(
            {"command": name, "summary": spec.get("summary", "")})
    return by_obj


def _dir_list(app_dir):
    tool = _tool_name(app_dir)
    by_obj = _catalog(app_dir)
    categories = [
        {"category": obj, "command_count": len(by_obj[obj]),
         "summary": ("ANIS Built-in commands" if obj == "builtin"
                     else f"{obj} Object commands")}
        for obj in by_obj
    ]
    kernel.emit({
        "status": "ok",
        "category_count": len(categories),
        "command_count": sum(x["command_count"] for x in categories),
        "categories": categories,
        "usage": (f"{tool} dir <category> shows category commands; "
                  f"{tool} dir --search 'keyword' searches; "
                  f"{tool} dir <command> shows full documentation"),
    }, kernel.EXIT_OK)


def _dir_category(app_dir, category):
    tool = _tool_name(app_dir)
    commands = _catalog(app_dir).get(category)
    if commands is None:
        kernel.emit({
            "status": "error", "code": "UNKNOWN_CATEGORY",
            "message": f"Category not found {category!r}",
            "fix": f"Use {tool} dir to list all categories",
        }, kernel.EXIT_BAD_ARGS)
    kernel.emit({
        "status": "ok", "category": category,
        "command_count": len(commands), "commands": commands,
        "usage": f"{tool} dir <command> shows full documentation",
    }, kernel.EXIT_OK)


def _dir_search(app_dir, query):
    tool = _tool_name(app_dir)
    terms = [term.casefold() for term in str(query).split() if term.strip()]
    if not terms:
        kernel.emit({
            "status": "error", "code": "INVALID_ARGS",
            "message": "--search At least one search term is required",
            "fix": f"{tool} dir --search 'action or object'",
        }, kernel.EXIT_BAD_ARGS)
    matches = []
    for category, rows in _catalog(app_dir).items():
        for row in rows:
            haystack = " ".join((
                category, row.get("command", ""), row.get("summary", "")
            )).casefold()
            if all(term in haystack for term in terms):
                matches.append({
                    "command": row["command"],
                    "category": category,
                    "summary": row.get("summary", ""),
                })
    kernel.emit({
        "status": "ok",
        "query": query,
        "match_count": len(matches),
        "commands": matches,
        "usage": f"{tool} dir <command> shows full documentation",
    }, kernel.EXIT_OK)


def _dir_show(app_dir, name):
    tool = _tool_name(app_dir)
    if name in _BUILTIN_SPECS:
        b = _BUILTIN_SPECS[name]
        kernel.emit({"status": "ok", "command": name, "summary": b["summary"],
                     "args": b["args"], "example": _builtin_example(app_dir, name)},
                    kernel.EXIT_OK)
    cmd_dir = os.path.join(app_dir, "commands", name)
    if not os.path.isfile(os.path.join(cmd_dir, "spec.json")):
        kernel.emit({"status": "error", "code": "UNKNOWN_COMMAND",
                     "message": f"Command not found {name!r}",
                     "fix": f"Use {tool} dir to list all commands"}, kernel.EXIT_BAD_ARGS)
    spec = kernel.load_spec(cmd_dir)
    kernel.emit({"status": "ok", "command": spec.get("command", name),
                 "summary": spec.get("summary", ""), "args": spec.get("args", {}),
                 "example": _example_for(app_dir, spec),
                 "errors": spec.get("errors", {}),
                 "related": spec.get("related", {})}, kernel.EXIT_OK)


def _builtin(app_dir, command, kv, flags):
    import engine
    try:
        if command == "guide":
            path = os.path.join(app_dir, "guide", "commands.json")
            if not os.path.isfile(path):
                raise kernel.AnisError(
                    "GUIDE_UNAVAILABLE", "This release has no command guide",
                    "Complete documentation and rebuild the release")
            with open(path, encoding="utf-8") as handle:
                guide = json.load(handle)
            kernel.emit({"status": "ok", "guide": guide}, kernel.EXIT_OK)
        if command == "observe":
            if "file" not in kv:
                raise kernel.AnisError("INVALID_ARGS", "Missing --file", "observe --file <document path>")
            kernel.emit({"status": "ok", "state": engine.observe(kv["file"])}, kernel.EXIT_OK)
        if command == "make-demo":
            args = kernel.validate(
                _BUILTIN_SPECS["make-demo"],
                _normalize_cli_keys(kv, _BUILTIN_SPECS["make-demo"]))
            path = args.get("file", "demo.odt")
            force = args["force"]
            if os.path.exists(path) and not force:
                raise kernel.AnisError(
                    "OUTPUT_EXISTS",
                    f"make-demo Refusing to overwrite existing file: {path}",
                    "Change --file target path; to overwrite the file, add --force true and retry")
            table_cells = None
            if "table" in kv:
                table_cells = [row.split(",") for row in kv["table"].split("|")]

            if "force" in inspect.signature(engine.make_demo_doc).parameters:
                engine.make_demo_doc(path, args.get("text"), table_cells, force=force)
            else:
                engine.make_demo_doc(path, args.get("text"), table_cells)
            kernel.emit({"status": "ok", "command": "make-demo", "file": path},
                        kernel.EXIT_OK)
    except kernel.AnisError as e:
        kernel.emit({"status": "error", "code": e.code, "message": str(e), "fix": e.fix},
                    kernel.EXIT_CMD_ERROR)


def main():
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "--app":
        app_dir, argv = os.path.abspath(argv[1]), argv[2:]
    else:
        kernel.emit({"status": "error", "code": "INVALID_ARGS",
                     "message": "Missing --app <Application directory>", "fix": "Invoke through the launcher in bin/ ."},
                    kernel.EXIT_BAD_ARGS)
    sys.path.insert(0, app_dir)


    if argv and argv[0] == "dir":
        rest = argv[1:]
        if not rest:
            _dir_list(app_dir)
        if len(rest) == 2 and rest[0] == "--search":
            _dir_search(app_dir, rest[1])
        if len(rest) == 1 and rest[0] == "--help":
            _dir_show(app_dir, "dir")
        if len(rest) == 1 and not rest[0].startswith("--"):
            target = rest[0]
            if target in _BUILTIN_SPECS or os.path.isfile(os.path.join(
                    app_dir, "commands", target, "spec.json")):
                _dir_show(app_dir, target)
            _dir_category(app_dir, target)
        kernel.emit({"status": "error", "code": "INVALID_ARGS",
                     "message": "dir Usage: dir [category-or-command] or dir --search <keyword>",
                     "fix": (f"{_tool_name(app_dir)} dir or "
                             f"{_tool_name(app_dir)} dir --search 'keyword' or "
                             f"{_tool_name(app_dir)} dir <category-or-command>")},
                    kernel.EXIT_BAD_ARGS)

    command, flags, kv = _parse(argv)
    if command in (None, "--list", "--help"):
        _dir_list(app_dir)
    if command in ("guide", "observe", "make-demo"):
        if "schema" in flags or "help" in flags:
            _dir_show(app_dir, command)
        _builtin(app_dir, command, kv, flags)

    cmd_dir = os.path.join(app_dir, "commands", command)
    if not os.path.isfile(os.path.join(cmd_dir, "spec.json")):
        kernel.emit({"status": "error", "code": "UNKNOWN_COMMAND",
                     "message": f"Command not found {command!r}",
                     "fix": f"Use {_tool_name(app_dir)} dir to list all commands"},
                    kernel.EXIT_BAD_ARGS)
    spec = kernel.load_spec(cmd_dir)
    if "schema" in flags or "help" in flags:
        kernel.emit({"status": "ok", "command": spec.get("command", command),
                     "summary": spec.get("summary", ""), "args": spec.get("args", {}),
                     "example": _example_for(app_dir, spec),
                     "errors": spec.get("errors", {}),
                     "related": spec.get("related", {})}, kernel.EXIT_OK)
    if "recipe" in flags:
        kernel.emit({"status": "ok", "recipe": spec.get("recipe", {})}, kernel.EXIT_OK)
    kv = _normalize_cli_keys(kv, spec)
    kernel.run(cmd_dir, kv)


if __name__ == "__main__":
    main()
