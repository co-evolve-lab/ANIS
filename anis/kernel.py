import importlib.util
import json
import os
import sys

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_CMD_ERROR = 3
EXIT_UNEXPECTED = 4

_TRUE_VALUES = {"1", "true", "yes"}
_FALSE_VALUES = {"0", "false", "no"}


class AnisError(Exception):
    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code = code
        self.fix = fix


def emit(payload, exit_code):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _error(e, exit_code):
    emit({"status": "error", "code": e.code, "message": str(e), "fix": e.fix}, exit_code)


def _coerce(name, aspec, value):
    t = aspec.get("type", "string")
    try:
        if t == "int":
            value = int(value)
        elif t == "float":
            value = float(value)
        elif t == "bool":
            if isinstance(value, bool):
                pass
            else:
                normalized = str(value).strip().lower()
                if normalized in _TRUE_VALUES:
                    value = True
                elif normalized in _FALSE_VALUES:
                    value = False
                else:
                    raise ValueError
        elif t == "vec":
            parts = (value if isinstance(value, list)
                     else [p for p in str(value).strip().strip("[]").split(",")])
            elem = aspec.get("elem", "float")
            cast = {"float": float, "int": int,
                    "bool": lambda p: str(p).strip().lower() in ("1", "true", "yes")}[elem]
            value = [cast(p) for p in parts]
    except ValueError:
        raise AnisError("INVALID_ARGS", f"Argument --{name} requires {t}; received {value!r}",
                        "Add --schema to see parameter types")
    if t == "vec":
        size = aspec.get("size")
        if size is not None and len(value) != size:
            raise AnisError("INVALID_ARGS",
                            f"--{name} requires {size} components; received {len(value)} items",
                            "comma-separated, for example --value 1,2,3,4")
    if t == "enum" and value not in aspec.get("values", []):
        raise AnisError("INVALID_ARGS", f"--{name} value {value!r} is invalid; choices: {aspec.get('values')}",
                        "Add --schema to see valid values")
    if t in ("int", "float"):
        lo, hi = aspec.get("min"), aspec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise AnisError("INVALID_ARGS", f"--{name}={value} out of range [{lo}, {hi}]",
                            "Add --schema to see the allowed range")
    return value


def cli_value(value):
    """Encode one JSON-compatible value using the public ANIS CLI contract."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def argument_tokens(values, include=None):
    """Encode command parameters; every parameter, including bool, has a value."""
    allowed = set(include) if include is not None else None
    tokens = []
    for name, value in values.items():
        if value is None or (allowed is not None and name not in allowed):
            continue
        tokens.extend([f"--{name.replace('_', '-')}", cli_value(value)])
    return tokens


def validate(spec, raw):
    known = spec.get("args", {})
    for k in raw:
        if k not in known:
            raise AnisError("INVALID_ARGS", f"Unknown argument --{k}", "Add --schema to see arguments")
    args = {}
    for name, aspec in known.items():
        if name in raw:
            args[name] = _coerce(name, aspec, raw[name])
        elif "default" in aspec:
            args[name] = aspec["default"]
        elif aspec.get("required"):
            raise AnisError("INVALID_ARGS", f"Missing required argument --{name}", "Add --schema to see arguments")
        else:
            args[name] = None
    return args


def load_spec(cmd_dir):
    with open(os.path.join(cmd_dir, "spec.json"), encoding="utf-8") as f:
        return json.load(f)


def load_impl(cmd_dir):
    path = os.path.join(cmd_dir, "impl.py")
    mod_spec = importlib.util.spec_from_file_location("anis_impl", path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise RuntimeError("impl.py does not define run(); the kernel refused to register the command")
    return mod


def record_call(cmd_dir, raw_args, ok):
    """Append one invocation to $ANIS_CALL_LOG; goal-level synthesis mines these logs."""
    path = os.environ.get("ANIS_CALL_LOG")
    if not path:
        return
    entry = {"command": os.path.basename(os.path.normpath(cmd_dir)),
             "args": raw_args, "ok": ok, "cwd": os.getcwd()}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(cmd_dir, raw_args):
    spec = load_spec(cmd_dir)
    try:
        args = validate(spec, raw_args)
    except AnisError as e:
        record_call(cmd_dir, raw_args, False)
        _error(e, EXIT_BAD_ARGS)
    try:
        impl = load_impl(cmd_dir)
        report = impl.run(args)
        if not isinstance(report, dict):
            raise TypeError(f"run() must return JSON object; received {type(report).__name__}")
        record_call(cmd_dir, raw_args, report.get("status", "ok") == "ok")
        emit(report, EXIT_OK)
    except AnisError as e:
        record_call(cmd_dir, raw_args, False)
        _error(e, EXIT_CMD_ERROR)
    except Exception as e:
        record_call(cmd_dir, raw_args, False)
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"status": "error", "code": "UNEXPECTED",
              "message": f"{type(e).__name__}: {e}",
              "fix": "Command implementation failed. Output state is unverified; inspect it with observe to see the current state."},
             EXIT_UNEXPECTED)
