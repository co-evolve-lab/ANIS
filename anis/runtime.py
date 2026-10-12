import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class RuntimeError_(Exception):
    pass


def declaration_path(app):
    return os.path.join(ROOT, "apps", app, "runtime.json")


def available_apps():
    apps_dir = os.path.join(ROOT, "apps")
    if not os.path.isdir(apps_dir):
        return []
    out = []
    for name in sorted(os.listdir(apps_dir)):
        if name.startswith("_") or name.startswith("."):
            continue
        if os.path.isfile(os.path.join(apps_dir, name, "runtime.json")):
            out.append(name)
    return out


def load(app):
    path = declaration_path(app)
    if not os.path.isfile(path):
        known = available_apps()
        raise RuntimeError_(
            f"Missing {path}.\n"
            "This file declares the application runner and is written after runtime\n"
            "discovery (see prompts/census_adapter.md environment checks).\n"
            f"Applications with declarations: {known or 'none'}")
    with open(path, encoding="utf-8") as handle:
        declaration = json.load(handle)

    runner = declaration.get("runner")
    if not isinstance(runner, list) or not runner:
        raise RuntimeError_(
            f"{path} must declare runner as a nonempty list, for example\n"
            '  {"runner": ["python3", "{script}"]}')
    placeholders = sum(str(token).count("{script}") for token in runner)
    if placeholders != 1:
        raise RuntimeError_(
            f"{path}: runner must contain exactly one {{script}} placeholder; "
            f"found {placeholders}")
    return declaration


def _prefix_of(declaration):
    prefix = declaration.get("prefix")
    return os.path.expanduser(prefix) if prefix else None


def build_command(app, script):
    declaration = load(app)
    prefix = _prefix_of(declaration)
    script_parts = ([script] if isinstance(script, str)
                    else [str(x) for x in script])
    script_path = str(script_parts[0])
    script_args = script_parts[1:]
    template = declaration["runner"]
    parts = []
    for token in template:
        text = str(token)
        if "{prefix}" in text:
            if not prefix:
                raise RuntimeError_(
                    f"{declaration_path(app)}: runner uses {{prefix}}, "
                    "but does not declare prefix field")
            text = text.replace("{prefix}", prefix)
        text = text.replace("{script}", script_path)
        parts.append(text)
    return parts + script_args


def build_env(app):
    declaration = load(app)
    extra = declaration.get("env") or {}
    if not extra:
        return None

    prefix = _prefix_of(declaration)
    env = dict(os.environ)
    for key, value in extra.items():
        text = str(value)
        if prefix:
            text = text.replace("{prefix}", prefix)

        if text.endswith(":"):
            env[key] = text + env.get(key, "")
        elif text.startswith(":"):
            env[key] = env.get(key, "") + text
        else:
            env[key] = text

    for directory in declaration.get("mkdirs") or []:
        expanded = os.path.expanduser(str(directory))
        if prefix:
            expanded = expanded.replace("{prefix}", prefix)
        os.makedirs(expanded, exist_ok=True)
    return env


def document_extension(app):
    declaration = load(app)
    extension = declaration.get("document_extension")
    if not isinstance(extension, str) or not extension.startswith(".") \
            or os.path.basename(extension) != extension:
        raise RuntimeError_(
            f"{declaration_path(app)} is missing a valid document_extension, "
            "for example '.svg' or '.odt'")
    return extension
