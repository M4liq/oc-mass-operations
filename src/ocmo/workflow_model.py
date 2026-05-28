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

def load_workflow(path: Path, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        raise OcmoError(f"workflow not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OcmoError(f"invalid workflow yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise OcmoError("workflow must be a mapping")
    return resolve_parameterized_document(data, params or {}, "workflow")


def validate_workflow(workflow: dict[str, Any], workflow_path: Path) -> None:
    if workflow.get("schema") != "ocmo-workflow/v1":
        raise OcmoError("workflow schema must be ocmo-workflow/v1")
    metadata = require_mapping(workflow, "workflow")
    require_string(metadata, "id")
    state = workflow.get("state", {})
    if state is not None and not isinstance(state, dict):
        raise OcmoError("state must be a mapping")
    defaults = workflow.get("defaults", {})
    if defaults is not None and not isinstance(defaults, dict):
        raise OcmoError("defaults must be a mapping")
    validate_workflow_options(defaults or {}, "defaults")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise OcmoError("steps must be a non-empty list")
    seen: set[str] = set()
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise OcmoError(f"steps[{index}] must be a mapping")
        step_id = require_string(step, "id")
        if step_id in seen:
            raise OcmoError(f"duplicate workflow step id: {step_id}")
        seen.add(step_id)
        require_string(step, "manifest")
        validate_workflow_options(step, f"steps[{index}]")
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
        validate_manifest(manifest, manifest_path)


def validate_workflow_options(options: dict[str, Any], field: str) -> None:
    value = options.get("stopOnFailure")
    if value is not None and not isinstance(value, bool):
        raise OcmoError(f"{field}.stopOnFailure must be a boolean")
    value = options.get("operationSelect")
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise OcmoError(f"{field}.operationSelect must be a non-empty string")
    value = options.get("concurrency")
    if value is not None and (not isinstance(value, int) or value < 1):
        raise OcmoError(f"{field}.concurrency must be a positive integer")
    value = options.get("timeoutSeconds")
    if value is not None and (not isinstance(value, int) or value < 1):
        raise OcmoError(f"{field}.timeoutSeconds must be a positive integer")
    value = options.get("allowSharedWorktreeConcurrency")
    if value is not None and not isinstance(value, bool):
        raise OcmoError(f"{field}.allowSharedWorktreeConcurrency must be a boolean")
    value = options.get("params")
    if value is not None:
        if not isinstance(value, dict):
            raise OcmoError(f"{field}.params must be a mapping")
        validate_parameter_mapping(value, f"{field}.params")


def workflow_step_manifest_path(workflow_path: Path, step: dict[str, Any]) -> Path:
    value = str(step["manifest"])
    path = Path(value)
    if path.is_absolute():
        return path
    return (workflow_path.parent / path).resolve()


def workflow_step_value(workflow: dict[str, Any], step: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in step:
        return step.get(key)
    defaults = workflow.get("defaults") if isinstance(workflow.get("defaults"), dict) else {}
    return defaults.get(key, default)


def workflow_step_stop_on_failure(workflow: dict[str, Any], step: dict[str, Any]) -> bool:
    return bool(workflow_step_value(workflow, step, "stopOnFailure", True))


def workflow_step_operation_select(workflow: dict[str, Any], step: dict[str, Any]) -> str | None:
    value = workflow_step_value(workflow, step, "operationSelect")
    return value.strip() if isinstance(value, str) else None


def workflow_step_concurrency(workflow: dict[str, Any], step: dict[str, Any]) -> int | None:
    value = workflow_step_value(workflow, step, "concurrency")
    return value if isinstance(value, int) else None


def workflow_step_timeout_seconds(workflow: dict[str, Any], step: dict[str, Any]) -> int | None:
    value = workflow_step_value(workflow, step, "timeoutSeconds")
    return value if isinstance(value, int) else None


def workflow_step_allow_shared_worktree_concurrency(workflow: dict[str, Any], step: dict[str, Any]) -> bool:
    return bool(workflow_step_value(workflow, step, "allowSharedWorktreeConcurrency", False))


def workflow_step_operation_params(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    params = resolved_params(workflow)
    defaults = workflow.get("defaults") if isinstance(workflow.get("defaults"), dict) else {}
    for source, field in ((defaults, "defaults.params"), (step, f"steps.{step.get('id', '<unknown>')}.params")):
        value = source.get("params") if isinstance(source, dict) else None
        if value is None:
            continue
        if not isinstance(value, dict):
            raise OcmoError(f"{field} must be a mapping")
        params.update(validate_parameter_mapping(value, field))
    return params


def workflow_step_operation_command(workflow: dict[str, Any], workflow_path: Path, step: dict[str, Any], action: str) -> str:
    manifest_path = workflow_step_manifest_path(workflow_path, step)
    command = ["ocmo", "operation", action, str(manifest_path)]
    select = workflow_step_operation_select(workflow, step)
    if select and action != "resume":
        command += ["--select", select]
    concurrency = workflow_step_concurrency(workflow, step)
    if concurrency is not None and action != "resume":
        command += ["--concurrency", str(concurrency)]
    timeout_seconds = workflow_step_timeout_seconds(workflow, step)
    if timeout_seconds is not None and action != "resume":
        command += ["--timeout-seconds", str(timeout_seconds)]
    command += parameter_arguments(workflow_step_operation_params(workflow, step))
    if workflow_step_allow_shared_worktree_concurrency(workflow, step) and action != "resume":
        command.append("--allow-shared-worktree-concurrency")
    return " ".join(quote_arg(part) for part in command)


def select_workflow_steps(workflow: dict[str, Any], state: dict[str, Any], selector: str | None) -> list[dict[str, Any]]:
    steps = workflow["steps"]
    selector = (selector or "uncompleted").strip()
    states = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    if selector == "all":
        return steps
    status_sets = {
        "pending": {"pending"},
        "uncompleted": None,
        "failed": {"failed", "timed_out"},
        "paused": PAUSED_STATUSES,
        "killed": {"killed"},
        "retryable": {"failed", "timed_out", "killed", "paused_unresumable"},
    }
    if selector in status_sets:
        statuses = status_sets[selector]
        selected = []
        for step in steps:
            status = workflow_step_status(step, states)
            if statuses is None:
                if status not in DONE_STATUSES:
                    selected.append(step)
            elif status in statuses:
                selected.append(step)
        return selected
    requested = expand_workflow_selector(selector, steps)
    selected = [step for step in steps if str(step.get("id")) in requested]
    missing = requested - {str(step.get("id")) for step in selected}
    if missing:
        raise OcmoError(f"selection did not match workflow step ids: {', '.join(sorted(missing))}")
    return selected


def workflow_step_status(step: dict[str, Any], states: dict[str, Any]) -> str:
    step_state = states.get(str(step["id"])) if isinstance(states.get(str(step["id"])), dict) else {}
    return str(step_state.get("status") or step.get("status") or "pending")


def expand_workflow_selector(selector: str, steps: list[dict[str, Any]]) -> set[str]:
    by_index = {str(index): str(step["id"]) for index, step in enumerate(steps, start=1)}
    result: set[str] = set()
    for token in [part.strip() for part in selector.split(",") if part.strip()]:
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if end < start:
                raise OcmoError(f"invalid descending range: {token}")
            for value in range(start, end + 1):
                result.add(by_index.get(str(value), str(value)))
        else:
            result.add(by_index.get(token, token))
    return result


def print_workflow_dry_run(workflow: dict[str, Any], workflow_path: Path, selected: list[dict[str, Any]], rerun: bool, resume: bool) -> None:
    action = "resume" if resume else "rerun" if rerun else "run"
    print(f"workflow: {workflow['workflow']['id']}")
    print(f"steps: {len(selected)}")
    for step in selected:
        print(f"- {step['id']}: {workflow_step_operation_command(workflow, workflow_path, step, action)}")


def item_state_matches_statuses(item_state: dict[str, Any], statuses: set[str]) -> bool:
    if str(item_state.get("status", "")) in statuses:
        return True
    runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
    return any(isinstance(run, dict) and str(run.get("status", "")) in statuses for run in runs.values())


def expand_selector(selector: str) -> set[str]:
    result: set[str] = set()
    for token in [part.strip() for part in selector.split(",") if part.strip()]:
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if end < start:
                raise OcmoError(f"invalid descending range: {token}")
            result.update(str(value) for value in range(start, end + 1))
        else:
            result.add(token)
    return result
