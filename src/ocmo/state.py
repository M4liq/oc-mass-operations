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

def state_path(manifest: dict[str, Any], manifest_path: Path) -> Path:
    configured = manifest.get("state", {}).get("path")
    if configured:
        return resolve_manifest_path(manifest_path, configured)
    operation_id = manifest["operation"]["id"]
    return (manifest_path.parent / ".ocmo" / "state" / f"{operation_id}.json").resolve()


def infer_workflow_path(value: Path | None) -> Path:
    if value is None:
        default_path = Path.cwd() / "workflow.yaml"
        if default_path.exists():
            return default_path
        generated = sorted((Path.cwd() / ".ocmo").glob("*/workflow.yaml"))
        if len(generated) == 1:
            return generated[0]
        if len(generated) > 1:
            raise OcmoError("multiple workflows found under .ocmo/*/workflow.yaml; pass one explicitly")
        raise OcmoError("workflow not found: workflow.yaml, and no generated workflows found under .ocmo/*/workflow.yaml")
    if value.is_dir():
        return value / "workflow.yaml"
    return value


def workflow_state_path(workflow: dict[str, Any], workflow_path: Path) -> Path:
    configured = workflow.get("state", {}).get("path") if isinstance(workflow.get("state"), dict) else None
    if configured:
        path = Path(str(configured))
        if path.is_absolute():
            return path
        return (workflow_path.parent / path).resolve()
    workflow_id = workflow["workflow"]["id"]
    return (workflow_path.parent / ".ocmo" / "state" / f"{workflow_id}.json").resolve()


def workflow_detached_runs_dir(workflow_path: Path) -> Path:
    return workflow_path.parent / ".ocmo" / "runs"


def relative_to_workflow(path: Path, workflow_path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workflow_path.parent.resolve()))
    except ValueError:
        return str(path)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def ensure_operation(self, manifest: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            now = utc_now()
            data.setdefault("schema", "ocmo-state/v1")
            data.setdefault("operationId", manifest["operation"]["id"])
            params = resolved_params(manifest)
            if params:
                data["params"] = params
            data.setdefault("startedAt", now)
            data.setdefault("workUnits", {})
            data.pop("completedAt", None)
            data["control"] = {"status": "running", "updatedAt": now}
            data["updatedAt"] = now
            self._write(data)

    def mark(self, item_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            item_state.update(patch)
            item_state["status"] = status
            data["updatedAt"] = utc_now()
            self._write(data)

    def mark_run(self, item_id: str, run_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            runs = item_state.setdefault("runs", {})
            run_state = runs.setdefault(run_id, {})
            run_state.update(patch)
            run_state["status"] = status
            data["updatedAt"] = utc_now()
            self._write(data)

    def replace_run(self, item_id: str, run_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            runs = item_state.setdefault("runs", {})
            runs[run_id] = {**patch, "status": status}
            data["updatedAt"] = utc_now()
            self._write(data)

    def clear_item_terminal_fields(self, item_id: str) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            for key in ("completedAt", "exitCode", "error", "pausedAt", "killedAt", "resumedAt"):
                item_state.pop(key, None)
            data["updatedAt"] = utc_now()
            self._write(data)

    def patch_run(self, item_id: str, run_id: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            runs = item_state.setdefault("runs", {})
            run_state = runs.setdefault(run_id, {})
            run_state.update(patch)
            data["updatedAt"] = utc_now()
            self._write(data)

    def patch(self, item_id: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("workUnits", {})
            item_state = data["workUnits"].setdefault(item_id, {})
            item_state.update(patch)
            data["updatedAt"] = utc_now()
            self._write(data)

    def finish(self, status: str) -> None:
        with self.lock:
            data = self._read()
            now = utc_now()
            if status in DONE_STATUSES or (status in TERMINAL_STATUSES and status not in PAUSED_STATUSES):
                data["completedAt"] = now
            else:
                data.pop("completedAt", None)
            data.setdefault("control", {}).update({"status": status, "updatedAt": now})
            data["updatedAt"] = now
            self._write(data)

    def data(self) -> dict[str, Any]:
        with self.lock:
            return self._read()

    def item(self, item_id: str) -> dict[str, Any]:
        item = self.data().get("workUnits", {}).get(item_id, {})
        return item if isinstance(item, dict) else {}

    def run(self, item_id: str, run_id: str) -> dict[str, Any]:
        runs = self.item(item_id).get("runs", {})
        run = runs.get(run_id, {}) if isinstance(runs, dict) else {}
        return run if isinstance(run, dict) else {}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class WorkflowStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def ensure_workflow(self, workflow: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            now = utc_now()
            data.setdefault("schema", "ocmo-workflow-state/v1")
            data.setdefault("workflowId", workflow["workflow"]["id"])
            params = resolved_params(workflow)
            if params:
                data["params"] = params
            data.setdefault("startedAt", now)
            data.setdefault("steps", {})
            data["status"] = "running"
            data.pop("completedAt", None)
            data["control"] = {"status": "running", "updatedAt": now}
            data["updatedAt"] = now
            self._write(data)

    def mark_step(self, step_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("steps", {})
            step_state = data["steps"].setdefault(step_id, {})
            if status in {"pending", "running", "completed"}:
                for key in ("completedAt", "exitCode", "error", "pausedAt", "killedAt"):
                    step_state.pop(key, None)
            if status == "pending":
                step_state.pop("startedAt", None)
            step_state.update(patch)
            step_state["status"] = status
            data["updatedAt"] = utc_now()
            self._write(data)

    def prepare_step(self, workflow: dict[str, Any], workflow_path: Path, step: dict[str, Any], rerun: bool) -> None:
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
        self.mark_step(
            str(step["id"]),
            "pending",
            {
                "manifestPath": str(manifest_path.resolve()),
                "statePath": str(state_path(manifest, manifest_path)),
                "operationId": str(manifest["operation"]["id"]),
            },
        )

    def finish(self, status: str) -> None:
        with self.lock:
            data = self._read()
            now = utc_now()
            if status == "completed":
                status = workflow_overall_status(data)
            data["status"] = status
            if status in TERMINAL_STATUSES or status in DONE_STATUSES or status == "completed":
                data["completedAt"] = now
            else:
                data.pop("completedAt", None)
            data.setdefault("control", {}).update({"status": status, "updatedAt": now})
            data["updatedAt"] = now
            self._write(data)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def workflow_overall_status(state: dict[str, Any]) -> str:
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    statuses = [step.get("status") for step in steps.values() if isinstance(step, dict)]
    if not statuses:
        return "pending"
    if any(status == "running" for status in statuses):
        return "running"
    if any(status in PAUSED_STATUSES for status in statuses):
        return "paused"
    if any(status == "killed" for status in statuses):
        return "killed"
    if any(status in {"failed", "timed_out", "cleanup_failed", "worktree_failed", "setup_failed"} for status in statuses):
        return "failed"
    if any(status in {None, "pending"} for status in statuses):
        return "pending"
    return "completed"


def command_without_prompt(command: list[str]) -> list[str]:
    if command:
        return command[:-1] + ["<prompt>"]
    return command


def format_command(command: list[str]) -> str:
    return " ".join(quote_arg(part) for part in command_without_prompt(command))


def quote_arg(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:\\=-]+", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def infer_manifest_path(value: Path | None) -> Path:
    if value is None:
        default_path = Path.cwd() / "manifest.yaml"
        if default_path.exists():
            return default_path
        generated = sorted((Path.cwd() / ".ocmo").glob("*/manifest.yaml"))
        if len(generated) == 1:
            return generated[0]
        if len(generated) > 1:
            raise OcmoError("multiple generated manifests found under .ocmo/*/manifest.yaml; pass one explicitly")
        raise OcmoError("manifest not found: manifest.yaml, and no generated manifests found under .ocmo/*/manifest.yaml")
    path = value
    if path.is_dir():
        return path / "manifest.yaml"
    return path


def run_manifest_path(value: Path | None) -> Path:
    return infer_manifest_path(value)
