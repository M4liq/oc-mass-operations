from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import io
import os
from importlib import resources
import json
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import yaml

from .common import *

def load_manifest(path: Path, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        raise OcmoError(f"manifest not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise OcmoError("manifest must be a YAML mapping")
    return resolve_parameterized_document(data, params or {}, "manifest")


def load_manifest_text(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise OcmoError("manifest must be a YAML mapping")
    return data


def resolve_parameterized_document(data: dict[str, Any], runtime_params: dict[str, Any], kind: str) -> dict[str, Any]:
    defaults = data.get("params", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise OcmoError(f"{kind}.params must be a mapping")
    params = validate_parameter_mapping(defaults, f"{kind}.params")
    params.update(runtime_params)
    effective_params = resolve_parameter_values(params)
    resolved = resolve_parameter_placeholders(copy.deepcopy(data), effective_params)
    if isinstance(resolved, dict):
        resolved[RESOLVED_PARAMS_KEY] = effective_params
        return resolved
    raise OcmoError(f"{kind} must be a YAML mapping")  # pragma: no cover


def resolve_parameter_values(params: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(params)
    for _ in range(10):
        resolved = resolve_parameter_placeholders(copy.deepcopy(current), current)
        if resolved == current:
            return resolved
        current = resolved
    return current


def resolve_parameter_placeholders(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_parameter_placeholders(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_parameter_placeholders(item, params) for item in value]
    if isinstance(value, str):
        return resolve_parameter_string(value, params)
    return value


def resolve_parameter_string(value: str, params: dict[str, Any]) -> Any:
    exact = PARAM_PLACEHOLDER_EXACT_RE.fullmatch(value)
    if exact:
        return parameter_value(params, exact.group(1))

    def replace(match: re.Match[str]) -> str:
        return format_placeholder_value(parameter_value(params, match.group(1)))

    return PARAM_PLACEHOLDER_RE.sub(replace, value)


def parameter_value(params: dict[str, Any], name: str) -> Any:
    if name not in params:
        value = resolve_brace_placeholder(name, {"params": params})
        if value is MISSING_PLACEHOLDER:
            raise OcmoError(f"unresolved parameter: params.{name}")
        return value
    return params[name]


def resolved_params(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get(RESOLVED_PARAMS_KEY)
    return dict(value) if isinstance(value, dict) else {}


def validate_manifest(manifest: dict[str, Any], manifest_path: Path, allow_shared_worktree_concurrency: bool = False) -> None:
    validate_manifest_schema(manifest, manifest_path, allow_shared_worktree_concurrency)
    workspace = manifest["operation"]["workspace"]
    if not resolve_manifest_path(manifest_path, workspace).exists():
        raise OcmoError(f"operation.workspace does not exist: {workspace}")
    auto_worktrees = auto_worktrees_config(manifest)
    if auto_worktrees["enabled"]:
        ensure_git_repository(resolve_manifest_path(manifest_path, workspace))
    template = manifest["prompt"]["template"]
    template_path = resolve_manifest_path(manifest_path, template)
    if not template_path.exists():
        raise OcmoError(f"prompt template not found: {template_path}")
    for index, work_unit in enumerate(manifest["workUnits"], start=1):
        validate_work_unit_run_paths(work_unit, manifest_path, index)


def validate_manifest_schema(manifest: dict[str, Any], manifest_path: Path, allow_shared_worktree_concurrency: bool = False) -> None:
    if manifest.get("schema") != "ocmo/v1":
        raise OcmoError("manifest schema must be ocmo/v1")
    operation = require_mapping(manifest, "operation")
    require_string(operation, "id")
    require_string(operation, "workspace")
    if "kind" in operation:
        raise OcmoError("operation.kind is no longer supported")
    runner = require_mapping(manifest, "runner")
    require_string(runner, "command")
    validate_build_agent(runner.get("agent"), "runner.agent")
    validate_model_value(runner.get("model"), "runner.model")
    validate_reasoning_effort(runner.get("reasoningEffort"), "runner.reasoningEffort")
    if "mode" in runner:
        raise OcmoError("runner.mode is no longer supported; ocmo always uses opencode run")
    timeout_seconds = runner.get("timeoutSeconds")
    if timeout_seconds is not None and (not isinstance(timeout_seconds, int) or timeout_seconds < 1):
        raise OcmoError("runner.timeoutSeconds must be a positive integer")
    queue = require_mapping(manifest, "queue")
    concurrency = queue.get("concurrency", 1)
    if not isinstance(concurrency, int) or concurrency < 1:
        raise OcmoError("queue.concurrency must be a positive integer")
    validate_operation_hooks(manifest.get("hooks"))
    auto_worktrees = auto_worktrees_config(manifest)
    if auto_worktrees["enabled"]:
        validate_auto_worktrees(auto_worktrees)
    policy = manifest.get("policy", {})
    if isinstance(policy, dict) and policy.get("worktree") == "single" and auto_worktrees["enabled"]:
        raise OcmoError("policy.worktree=single cannot be used with queue.autoWorktrees.enabled=true")
    prompt = require_mapping(manifest, "prompt")
    validate_template_value(require_string(prompt, "template"), "prompt.template")
    normalize_skills(prompt.get("skills"), "prompt.skills")
    work_units = manifest.get("workUnits")
    if not isinstance(work_units, list) or not work_units:
        raise OcmoError("workUnits must be a non-empty list")
    seen = set()
    for index, work_unit in enumerate(work_units, start=1):
        if not isinstance(work_unit, dict):
            raise OcmoError(f"workUnits[{index}] must be a mapping")
        work_unit_id = work_unit.get("id")
        if work_unit_id is None:
            raise OcmoError(f"workUnits[{index}].id is required")
        if "status" in work_unit:
            raise OcmoError(f"workUnits[{index}].status is not supported; status is stored in state.json")
        work_unit_key = str(work_unit_id)
        if work_unit_key in seen:
            raise OcmoError(f"duplicate work unit id: {work_unit_key}")
        seen.add(work_unit_key)
        validate_work_unit_runs(manifest, work_unit, manifest_path, index)


def validate_template_value(value: str, field: str) -> None:
    if "\n" in value or "\r" in value:
        raise OcmoError(f"{field} must be a file path, not inline template text")


def validate_work_unit_runs(manifest: dict[str, Any], work_unit: dict[str, Any], manifest_path: Path, work_unit_index: int) -> None:
    runs = work_unit.get("runs")
    if runs is None:
        return
    if not isinstance(runs, dict):
        raise OcmoError(f"workUnits[{work_unit_index}].runs must be a mapping")
    mode = runs.get("mode", "sequential")
    if mode != "sequential":
        raise OcmoError(f"workUnits[{work_unit_index}].runs.mode must be sequential")
    steps = runs.get("steps")
    if not isinstance(steps, list) or not steps:
        raise OcmoError(f"workUnits[{work_unit_index}].runs.steps must be a non-empty list")
    seen = set()
    produced_by_step: dict[str, set[str]] = {}
    for step_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise OcmoError(f"workUnits[{work_unit_index}].runs.steps[{step_index}] must be a mapping")
        run_id = step.get("id")
        if run_id is None or not str(run_id).strip():
            raise OcmoError(f"workUnits[{work_unit_index}].runs.steps[{step_index}].id is required")
        run_key = str(run_id)
        if run_key in seen:
            raise OcmoError(f"duplicate run id for work unit {work_unit['id']}: {run_key}")
        timeout_seconds = step.get("timeoutSeconds")
        if timeout_seconds is not None and (not isinstance(timeout_seconds, int) or timeout_seconds < 1):
            raise OcmoError(f"workUnits[{work_unit_index}].runs.steps[{step_index}].timeoutSeconds must be a positive integer")
        validate_build_agent(step.get("agent"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].agent")
        validate_model_value(step.get("model"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].model")
        validate_reasoning_effort(step.get("reasoningEffort"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].reasoningEffort")
        prompt = step.get("prompt")
        if prompt is not None:
            if not isinstance(prompt, dict):
                raise OcmoError(f"workUnits[{work_unit_index}].runs.steps[{step_index}].prompt must be a mapping")
            template = prompt.get("template")
            if template is not None:
                if not isinstance(template, str) or not template.strip():
                    raise OcmoError("template must be a non-empty string")
                validate_template_value(template, f"workUnits[{work_unit_index}].runs.steps[{step_index}].prompt.template")
            normalize_skills(prompt.get("skills"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].prompt.skills")
        validate_consumes(step.get("consumes"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].consumes", produced_by_step)
        produced_by_step[run_key] = validate_produces(step.get("produces"), f"workUnits[{work_unit_index}].runs.steps[{step_index}].produces")
        seen.add(run_key)


def shared_worktree_concurrency_warning(manifest: dict[str, Any]) -> str | None:
    policy = manifest.get("policy", {})
    concurrency = manifest.get("queue", {}).get("concurrency", 1)
    if isinstance(policy, dict) and policy.get("worktree") == "single" and isinstance(concurrency, int) and concurrency > 1:
        return SHARED_WORKTREE_CONCURRENCY_WARNING
    return None


def warn_shared_worktree_concurrency(manifest: dict[str, Any]) -> None:
    warning = shared_worktree_concurrency_warning(manifest)
    if warning:
        print(warning, file=sys.stderr)


def validate_build_agent(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise OcmoError(f"{field} must be build")
    if value.strip() != RUN_AGENT:
        raise OcmoError(f"{field} must be build")


def validate_model_value(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise OcmoError(f"{field} must be a non-empty string in the form provider/model")
    text = value.strip()
    if "/" not in text:
        # Allow bare model id (opencode resolves the default provider).
        return
    provider, _, model_id = text.partition("/")
    if not provider or not model_id:
        raise OcmoError(f"{field} must be in the form provider/model")
    if provider not in KNOWN_MODEL_PROVIDERS:
        known = ", ".join(KNOWN_MODEL_PROVIDERS)
        raise OcmoError(f"{field} provider '{provider}' is not supported (known: {known})")


def validate_reasoning_effort(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value not in REASONING_EFFORT_VALUES:
        allowed = ", ".join(REASONING_EFFORT_VALUES)
        raise OcmoError(f"{field} must be one of: {allowed}")


def validate_artifact_name(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or not ARTIFACT_NAME_RE.fullmatch(value.strip()):
        raise OcmoError(f"{field} artifact names must be non-empty and contain only letters, numbers, dots, underscores, or hyphens")


def parse_artifact_reference(value: Any, field: str) -> tuple[str, str]:
    if not isinstance(value, str) or "." not in value:
        raise OcmoError(f"{field} must use step.artifact syntax")
    step_id, artifact_id = value.split(".", 1)
    validate_artifact_name(step_id, field)
    validate_artifact_name(artifact_id, field)
    return step_id, artifact_id


def validate_produces(value: Any, field: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, dict):
        raise OcmoError(f"{field} must be a mapping")
    artifact_ids: set[str] = set()
    for artifact_id, config in value.items():
        artifact_key = str(artifact_id)
        if not ARTIFACT_NAME_RE.fullmatch(artifact_key):
            raise OcmoError(f"{field}.{artifact_key} must be a simple artifact name")
        if config is None:
            artifact_ids.add(artifact_key)
            continue
        if not isinstance(config, dict):
            raise OcmoError(f"{field}.{artifact_key} must be a mapping")
        if "path" in config:
            validate_artifact_path_template(config["path"], f"{field}.{artifact_key}.path")
        if "required" in config and not isinstance(config["required"], bool):
            raise OcmoError(f"{field}.{artifact_key}.required must be a boolean")
        if "description" in config and not isinstance(config["description"], str):
            raise OcmoError(f"{field}.{artifact_key}.description must be a string")
        if "type" in config:
            raise OcmoError(f"{field}.{artifact_key}.type is not supported")
        validate_handoff_gates(config.get("gates"), f"{field}.{artifact_key}.gates")
        artifact_ids.add(artifact_key)
    return artifact_ids


def validate_handoff_gates(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OcmoError(f"{field} must be a mapping")
    decision = value.get("decision")
    if decision is not None and (not isinstance(decision, str) or not decision.strip()):
        raise OcmoError(f"{field}.decision must be a non-empty string")
    min_confidence = value.get("minConfidence")
    if min_confidence is not None and (not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool) or min_confidence < 0 or min_confidence > 1):
        raise OcmoError(f"{field}.minConfidence must be a number between 0 and 1")
    require_conditions = value.get("requireConditionsMet")
    if require_conditions is not None and not isinstance(require_conditions, bool):
        raise OcmoError(f"{field}.requireConditionsMet must be a boolean")


def validate_consumes(value: Any, field: str, produced_by_step: dict[str, set[str]]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise OcmoError(f"{field} must be a list")
    for index, reference in enumerate(value, start=1):
        if not isinstance(reference, str) or not reference.strip():
            raise OcmoError(f"{field}[{index}] must be a non-empty string")
        parts = reference.split(".", 1)
        if len(parts) != 2 or not all(ARTIFACT_NAME_RE.fullmatch(part) for part in parts):
            raise OcmoError(f"{field}[{index}] must use <step-id>.<artifact-id>")
        if parts[0] not in produced_by_step:
            raise OcmoError(f"{field}[{index}] must reference an earlier step")
        if parts[1] not in produced_by_step[parts[0]]:
            raise OcmoError(f"{field}[{index}] references an unknown artifact")


def validate_artifact_path_template(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OcmoError(f"{field} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or path.drive or ".." in path.parts or path == Path(".") or not path.parts or path.parts[0] != ARTIFACT_ROOT:
        raise OcmoError(f"{field} must be relative and stay under {ARTIFACT_ROOT}/")


def produced_artifacts(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    produces = run.get("produces")
    if not isinstance(produces, dict):
        return {}
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact_id, config in produces.items():
        artifacts[str(artifact_id)] = dict(config or {})
    return artifacts


def is_handoff_artifact(artifact_id: str, config: dict[str, Any]) -> bool:
    return artifact_id == "handoff" or "gates" in config


def artifact_reference_map(manifest_path: Path, item: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for run in runs:
        run_id = str(run["id"])
        for artifact_id, config in produced_artifacts(run).items():
            artifacts[f"{run_id}.{artifact_id}"] = default_artifact_path(manifest_path, item, run_id, artifact_id, config)
    return artifacts


def artifact_instructions(manifest_path: Path, item: dict[str, Any], run: dict[str, Any]) -> str:
    artifacts = produced_artifacts(run)
    if not artifacts:
        return ""
    lines = ["## Required Artifacts", "", "Before finishing this run, write these handoff artifact files exactly as specified:"]
    for artifact_id, config in artifacts.items():
        path = default_artifact_path(manifest_path, item, str(run["id"]), artifact_id, config)
        required = config.get("required", True)
        lines.append(f"- {artifact_id}: {path}")
        lines.append(f"  Manifest-relative path: {relative_to_manifest(path, manifest_path)}")
        if is_handoff_artifact(artifact_id, config):
            gates = config.get("gates") if isinstance(config.get("gates"), dict) else {}
            lines.append("  Type: handoff JSON")
            lines.append("  Schema: ocmo-handoff/v1")
            if gates.get("decision") is not None:
                lines.append(f"  Required decision: {gates['decision']}")
            if gates.get("minConfidence") is not None:
                lines.append(f"  Minimum confidence: {gates['minConfidence']}")
            if gates.get("requireConditionsMet"):
                lines.append("  All conditions[].met values must be true")
        if config.get("description"):
            lines.append(f"  Description: {config['description']}")
        lines.append(f"  Required: {'yes' if required else 'no'}")
    lines.append("")
    lines.append("Create parent directories if needed. Required artifacts must be non-empty.")
    if any(is_handoff_artifact(artifact_id, config) for artifact_id, config in artifacts.items()):
        lines.append('For handoff JSON, set decision to "block" when the next run should not proceed. Do not inflate confidence to satisfy a gate.')
    return "\n".join(lines)


def consumed_artifacts(manifest_path: Path, item: dict[str, Any], run: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    consumes = run.get("consumes") or []
    if not consumes:
        return ""
    references = artifact_reference_map(manifest_path, item, runs)
    sections = ["## Chained Inputs", ""]
    for index, reference in enumerate(consumes, start=1):
        step_id, artifact_id = parse_artifact_reference(reference, f"run.consumes[{index}]")
        key = f"{step_id}.{artifact_id}"
        path = references[key]
        sections.append(f"### {key}")
        sections.append(f"Source: {relative_to_manifest(path, manifest_path)}")
        sections.append("")
        if path.exists():
            sections.append(path.read_text(encoding="utf-8", errors="replace"))
        else:
            sections.append("[artifact will be generated by an earlier sequential run]")
        sections.append("")
    return "\n".join(sections).rstrip()


def validate_work_unit_run_paths(work_unit: dict[str, Any], manifest_path: Path, work_unit_index: int) -> None:
    runs = work_unit.get("runs")
    if runs is None:
        return
    for step_index, step in enumerate(runs["steps"], start=1):
        prompt = step.get("prompt") or {}
        template = prompt.get("template")
        if template is not None:
            template_path = resolve_manifest_path(manifest_path, template)
            if not template_path.exists():
                raise OcmoError(f"prompt template not found: {template_path}")


def work_unit_runs(manifest: dict[str, Any], work_unit: dict[str, Any]) -> list[dict[str, Any]]:
    runs = work_unit.get("runs")
    if runs is None:
        runner = manifest.get("runner", {})
        return [{"id": "default", "mode": "sequential", "index": 1, **runner}]
    steps = runs["steps"]
    result = []
    for index, step in enumerate(steps, start=1):
        result.append({"mode": runs.get("mode", "sequential"), "index": index, **step, "id": str(step["id"])})
    return result


def effective_runner(manifest: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    runner = dict(manifest.get("runner", {}))
    for key, value in run.items():
        if key not in {"id", "index", "mode", "prompt", "produces", "consumes"}:
            runner[key] = value
    return runner


def effective_prompt(manifest: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    prompt = dict(manifest.get("prompt", {}))
    prompt.update(run.get("prompt", {}) or {})
    return prompt


def normalize_skills(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OcmoError(f"{field} must be a list of skill names")
    skills = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise OcmoError(f"{field}[{index}] must be a non-empty string")
        skill = item.strip().lstrip("/")
        if not skill or re.search(r"\s", skill):
            raise OcmoError(f"{field}[{index}] must be a skill name without whitespace")
        skills.append(skill)
    return skills


def skill_instructions(skills: list[str]) -> str:
    if not skills:
        return ""
    commands = "\n".join(f"- /{skill}" for skill in skills)
    return f"You must use the following opencode skills before doing this task, in order:\n{commands}"


def require_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise OcmoError(f"{key} must be a mapping")
    return value


def require_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OcmoError(f"{key} must be a non-empty string")
    return value


def auto_worktrees_config(manifest: dict[str, Any]) -> dict[str, Any]:
    config = manifest.get("queue", {}).get("autoWorktrees", {})
    if config is None:
        return {"enabled": False}
    if isinstance(config, bool):
        return {"enabled": config}
    if not isinstance(config, dict):
        raise OcmoError("queue.autoWorktrees must be a mapping")
    return {"enabled": False, **config}


def validate_auto_worktrees(config: dict[str, Any]) -> None:
    if not isinstance(config.get("enabled"), bool):
        raise OcmoError("queue.autoWorktrees.enabled must be a boolean")
    for key in ("root", "baseBranch", "branchPattern", "cleanup"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise OcmoError(f"queue.autoWorktrees.{key} must be a non-empty string")
    cleanup = config.get("cleanup", "never")
    if cleanup not in {"never", "onSuccess", "always"}:
        raise OcmoError("queue.autoWorktrees.cleanup must be never, onSuccess, or always")
    for key in ("setup", "teardown"):
        normalize_scripts(config.get(key), f"queue.autoWorktrees.{key}")
    try:
        str(config.get("branchPattern", "ocmo/{operation_id}/{work_unit_id}")).format(operation_id="operation", work_unit_id="work-unit", work_unit_slug="work-unit")
    except (KeyError, ValueError, IndexError) as exc:
        raise OcmoError(f"invalid queue.autoWorktrees.branchPattern: {exc}") from exc


def validate_operation_hooks(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OcmoError("hooks must be a mapping")
    allowed = ("beforeRun", "afterRun", "onFailure")
    for key in value:
        if key not in allowed:
            raise OcmoError(f"hooks.{key} is not supported")
    for key in allowed:
        normalize_scripts(value.get(key), f"hooks.{key}")


def normalize_scripts(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return value
    raise OcmoError(f"{field} must be a string or list of non-empty strings")


def ensure_git_repository(path: Path) -> None:
    try:
        completed = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(path), capture_output=True, text=True)
    except OSError as exc:
        raise OcmoError(f"could not inspect git repository: {exc}") from exc
    if completed.returncode != 0:
        raise OcmoError(f"operation.workspace must be inside a git repository when auto worktrees are enabled: {path}")


def resolve_manifest_path(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (manifest_path.parent / path).resolve()


def artifact_path(manifest_path: Path, item: dict[str, Any], run_id: str, artifact_id: str, configured_path: str | None = None) -> Path:
    validate_artifact_name(artifact_id, "artifact")
    value = configured_path or f"{ARTIFACT_ROOT}/{slugify(str(item.get('id', 'work-unit')))}/{slugify(run_id)}/{slugify(artifact_id)}.md"
    if configured_path is not None:
        substitutions = {
            "work_unit_id": slugify(str(item.get("id", ""))),
            "run_id": slugify(run_id),
            "artifact_id": slugify(artifact_id),
        }
        value = Template(configured_path).safe_substitute(substitutions)
    validate_artifact_path_template(value, "artifact.path")
    path = resolve_manifest_path(manifest_path, value)
    root = (manifest_path.parent / ARTIFACT_ROOT).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - validate_artifact_path_template rejects normal escape attempts.
        raise OcmoError(f"artifact.path must stay under {ARTIFACT_ROOT}/") from exc
    return path


def default_artifact_path(manifest_path: Path, item: dict[str, Any], run_id: str, artifact_id: str, config: dict[str, Any]) -> Path:
    extension = "json" if is_handoff_artifact(artifact_id, config) else "md"
    configured_path = config.get("path") or f"{ARTIFACT_ROOT}/{slugify(str(item.get('id', 'work-unit')))}/{slugify(run_id)}/{slugify(artifact_id)}.{extension}"
    return artifact_path(manifest_path, item, run_id, artifact_id, configured_path)


def artifact_relative_path(manifest_path: Path, item: dict[str, Any], run_id: str, artifact_id: str, configured_path: str | None = None) -> str:
    return relative_to_manifest(artifact_path(manifest_path, item, run_id, artifact_id, configured_path), manifest_path)


def produced_artifact_relative_path(manifest_path: Path, item: dict[str, Any], run_id: str, artifact_id: str, config: dict[str, Any]) -> str:
    return relative_to_manifest(default_artifact_path(manifest_path, item, run_id, artifact_id, config), manifest_path)


def select_work_units(manifest: dict[str, Any], selector: str | None, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    work_units = manifest["workUnits"]
    selector = selector or manifest.get("selection", {}).get("default") or "uncompleted"
    selector = selector.strip()
    if selector == "all":
        return work_units
    if selector == "pending":
        return [work_unit for work_unit in work_units if work_unit_state_status(work_unit, state) == "pending"]
    if selector == "uncompleted":
        return [work_unit for work_unit in work_units if work_unit_state_status(work_unit, state) not in DONE_STATUSES]

    requested = expand_selector(selector)
    selected = [work_unit for work_unit in work_units if str(work_unit.get("id")) in requested]
    missing = requested - {str(work_unit.get("id")) for work_unit in selected}
    if missing:
        raise OcmoError(f"selection did not match manifest work unit ids: {', '.join(sorted(missing))}")
    return selected


def work_unit_state_status(work_unit: dict[str, Any], state: dict[str, Any] | None) -> str:
    item_states = state.get("workUnits") if isinstance(state, dict) and isinstance(state.get("workUnits"), dict) else {}
    item_state = item_states.get(str(work_unit.get("id"))) if isinstance(item_states.get(str(work_unit.get("id"))), dict) else {}
    return str(item_state.get("status") or "pending").lower()


def select_rerun_work_units(manifest: dict[str, Any], state: dict[str, Any], selector: str | None) -> list[dict[str, Any]]:
    selector = (selector or "retryable").strip()
    if selector == "all":
        return manifest["workUnits"]
    status_sets = {
        "retryable": RERUN_RETRYABLE_STATUSES,
        "unresumable": {"paused_unresumable"},
        "paused_unresumable": {"paused_unresumable"},
        "timed-out": {"timed_out"},
        "timed_out": {"timed_out"},
        "failed": FAILED_STATUSES,
        "killed": {"killed"},
    }
    statuses = status_sets.get(selector)
    if statuses is None:
        return select_work_units(manifest, selector, state)
    item_states = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    selected = []
    for item in manifest["workUnits"]:
        item_id = str(item.get("id"))
        item_state = item_states.get(item_id) if isinstance(item_states.get(item_id), dict) else {}
        if item_state_matches_statuses(item_state, statuses):
            selected.append(item)
    return selected
