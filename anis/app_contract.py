"""Load one application's ANIS contract without teaching the core its name."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_NAME = "anis-app.json"
GENERIC_ANIS_MODULES = [
    "__init__.py", "kernel.py", "cli.py", "proptemplate.py",
    "actiontemplate.py", "transformtemplate.py",
]


class AppContractError(RuntimeError):
    pass


def app_dir(app: str) -> Path:
    return ROOT / "apps" / app


def load(app: str) -> dict:
    """Read the declarative build, verification, and release boundary."""
    path = app_dir(app) / CONTRACT_NAME
    if not path.is_file():
        raise AppContractError(
            f"missing {path}; Stage One must create the application contract")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AppContractError(f"invalid application contract {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise AppContractError(f"{path}: schema_version must be 1")
    for section in ("build", "verification", "release"):
        if not isinstance(payload.get(section), dict):
            raise AppContractError(f"{path}: {section} must be an object")
    release = payload["release"]
    for key in ("runtime_files", "anis_modules"):
        values = release.get(key)
        if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values):
            raise AppContractError(f"{path}: release.{key} must be a string list")
    return payload


def load_adapter(app: str, contract: dict | None = None):
    """Load an optional app-owned hook module from apps/<app>/ only."""
    contract = contract or load(app)
    relative = contract.get("adapter")
    if not relative:
        return None
    path = (app_dir(app) / relative).resolve()
    root = app_dir(app).resolve()
    if root not in path.parents or not path.is_file():
        raise AppContractError(
            f"application adapter must be an existing file below {root}: {path}")
    spec = importlib.util.spec_from_file_location(
        f"anis_app_adapter_{app.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hook(adapter, name, default, *args, **kwargs):
    function = getattr(adapter, name, None) if adapter is not None else None
    return function(*args, **kwargs) if callable(function) else default

