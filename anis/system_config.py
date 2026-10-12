"""Load and validate the repository-wide ANIS orchestration configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path


ANIS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ANIS_ROOT / "anis-config.json"
CONFIG_ENV = "ANIS_CONFIG"


class ConfigError(RuntimeError):
    pass


def _positive_int(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{path} must be at least 1 (integer)")


def _nonnegative_int(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{path} must be a nonnegative integer")


def validate(config):
    if config.get("schema_version") != 1:
        raise ConfigError("anis-config.json.schema_version must be 1")
    aliases = config.get("app_aliases", {})
    if not isinstance(aliases, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in aliases.items()):
        raise ConfigError("app_aliases must map nonempty strings to nonempty strings")
    agent = config.get("coding_agent")
    workflow = config.get("workflow")
    if not isinstance(agent, dict) or not isinstance(workflow, dict):
        raise ConfigError("anis-config.json must contain coding_agent and workflow")
    command = agent.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ConfigError("coding_agent.command must be a nonempty string")
    model = agent.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError("coding_agent.model must be null or a nonempty string")
    _positive_int(agent.get("max_parallel_processes"),
                  "coding_agent.max_parallel_processes")

    onboarding = workflow.get("onboarding")
    discovery = workflow.get("capability_discovery")
    review = workflow.get("capability_review")
    implementation = workflow.get("command_implementation")
    documentation = workflow.get("command_documentation")
    verification = workflow.get("verification")
    for name, row in (
            ("onboarding", onboarding),
            ("capability_discovery", discovery),
            ("capability_review", review),
            ("command_implementation", implementation),
            ("command_documentation", documentation),
            ("verification", verification)):
        if not isinstance(row, dict):
            raise ConfigError(f"workflow.{name} must be an object")

    for path, value in (
            ("workflow.onboarding.agent_timeout_seconds",
             onboarding.get("agent_timeout_seconds")),
            ("workflow.onboarding.script_timeout_seconds",
             onboarding.get("script_timeout_seconds")),
            ("workflow.onboarding.stage_attempt_limit",
             onboarding.get("stage_attempt_limit")),
            ("workflow.onboarding.redo_limit", onboarding.get("redo_limit")),
            ("workflow.capability_discovery.batch_size",
             discovery.get("batch_size")),
            ("workflow.capability_discovery.max_parallel_batches",
             discovery.get("max_parallel_batches")),
            ("workflow.capability_discovery.item_attempt_limit",
             discovery.get("item_attempt_limit")),
            ("workflow.capability_discovery.max_runtime_attempts_per_item",
             discovery.get("max_runtime_attempts_per_item")),
            ("workflow.capability_discovery.timeout_seconds",
             discovery.get("timeout_seconds")),
            ("workflow.capability_review.max_parallel_batches",
             review.get("max_parallel_batches")),
            ("workflow.capability_review.timeout_seconds",
             review.get("timeout_seconds")),
            ("workflow.command_implementation.batch_size",
             implementation.get("batch_size")),
            ("workflow.command_implementation.max_parallel_batches",
             implementation.get("max_parallel_batches")),
            ("workflow.command_implementation.timeout_seconds",
             implementation.get("timeout_seconds")),
            ("workflow.command_implementation.verify_timeout_seconds",
             implementation.get("verify_timeout_seconds")),
            ("workflow.command_documentation.timeout_seconds",
             documentation.get("timeout_seconds")),
            ("workflow.verification.jobs", verification.get("jobs"))):
        _positive_int(value, path)
    _nonnegative_int(implementation.get("max_retries"),
                     "workflow.command_implementation.max_retries")
    if not isinstance(documentation.get("enabled"), bool):
        raise ConfigError("workflow.command_documentation.enabled must be a boolean")
    pass_rate = onboarding.get("minimum_pass_rate")
    if (isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float))
            or not 0 < pass_rate <= 1):
        raise ConfigError(
            "workflow.onboarding.minimum_pass_rate must be between 0 and 1 inclusive")
    allowed_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
    for name, value in (
            ("capability_discovery", discovery.get("sandbox")),
            ("capability_review", review.get("sandbox"))):
        if value not in allowed_sandboxes:
            raise ConfigError(
                f"workflow.{name}.sandbox must be {sorted(allowed_sandboxes)} .")
    return config


def load(path=None):
    selected = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    if not selected.is_absolute():
        selected = (ANIS_ROOT / selected).resolve()
    try:
        with selected.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Cannot find ANIS configuration file: {selected}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ANIS Configuration is not valid JSON: {selected}: {exc}") from exc
    validate(config)
    config["_path"] = str(selected)
    return config
