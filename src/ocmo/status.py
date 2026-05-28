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

def control_manifest_and_record(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None]:
    if getattr(args, "run_id", None):
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        record = read_json_file(path)
        manifest_value = record.get("manifestPath")
        if not isinstance(manifest_value, str):
            raise OcmoError(f"detached run is missing manifestPath: {args.run_id}")
        return Path(manifest_value), record
    return infer_manifest_path(args.manifest), None


def stop_operation_processes(manifest_path: Path, state: dict[str, Any], selected_record: dict[str, Any] | None = None) -> None:
    pids: list[int] = []
    if selected_record and isinstance(selected_record.get("pid"), int):
        pids.append(selected_record["pid"])
    for record in related_detached_records(manifest_path, include_inactive=True):
        pid = record.get("pid")
        if isinstance(pid, int):
            pids.append(pid)
    for pid in active_state_pids(state):
        pids.append(pid)
    for pid in sorted(set(pids), reverse=True):
        terminate_process_tree(pid, force=True)


def active_state_pids(state: dict[str, Any]) -> list[int]:
    pids = []
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    for work_unit in work_units.values():
        if not isinstance(work_unit, dict):
            continue
        runs = work_unit.get("runs") if isinstance(work_unit.get("runs"), dict) else {}
        for run in runs.values():
            if isinstance(run, dict) and run.get("status") == "running" and isinstance(run.get("pid"), int):
                pids.append(run["pid"])
    return pids


def terminate_process_tree(pid: int, force: bool = True) -> None:
    if pid < 1 or not process_is_alive(pid):
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return
        time.sleep(0.1)
    if force:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, PermissionError, OSError):
            return


def mark_active_runs(path: Path, status: str) -> int:
    if not path.exists():
        return 0
    data = read_json_file(path)
    items = data.get("workUnits") if isinstance(data.get("workUnits"), dict) else {}
    changed = 0
    now = utc_now()
    for item in items.values():
        if not isinstance(item, dict):
            continue
        runs = item.get("runs") if isinstance(item.get("runs"), dict) else {}
        item_changed = False
        for run in runs.values():
            if not isinstance(run, dict) or run.get("status") != "running":
                continue
            next_status = status
            if status == "paused" and not run.get("sessionId"):
                next_status = "paused_unresumable"
            run["status"] = next_status
            run[f"{status}At"] = now
            item_changed = True
            changed += 1
        if item.get("status") == "running" or item_changed:
            item["status"] = status if status != "paused" or any(isinstance(run, dict) and run.get("status") == "paused" for run in runs.values()) else "paused_unresumable"
            item[f"{status}At"] = now
    data.setdefault("control", {})
    data["control"].update({"status": status, "updatedAt": now})
    data["updatedAt"] = now
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def select_paused_work_units(manifest: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    item_states = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    selected = []
    for item in manifest.get("workUnits", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        item_state = item_states.get(str(item["id"]), {})
        if not isinstance(item_state, dict):
            continue
        runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
        if item_state.get("status") in PAUSED_STATUSES or any(isinstance(run, dict) and run.get("status") in PAUSED_STATUSES for run in runs.values()):
            selected.append(item)
    return selected


def resume_prompt(item_id: str, run_id: str) -> str:
    return f"Continue the previous OCMO session for work unit {item_id}, run {run_id}. Preserve prior work, inspect current files and state, and finish the original assigned task."


def is_generated_operation_manifest(manifest_path: Path) -> bool:
    parts = manifest_path.resolve().parts
    return manifest_path.name == "manifest.yaml" and len(parts) >= 3 and parts[-3] == ".ocmo"


def is_generated_workflow_operation_manifest(workflow_path: Path, manifest_path: Path) -> bool:
    if is_generated_operation_manifest(manifest_path):
        return True
    workflow = workflow_path.resolve()
    workflow_parts = workflow.parts
    if workflow.name != "workflow.yaml" or len(workflow_parts) < 3 or workflow_parts[-3] != ".ocmo":
        return False
    manifest = manifest_path.resolve()
    workflow_dir = workflow.parent
    return manifest.name == "manifest.yaml" and manifest.parent != workflow_dir and manifest.is_relative_to(workflow_dir)


def remove_detached_records(manifest_path: Path) -> None:
    for record in related_detached_records(manifest_path, include_inactive=True):
        run_id = record.get("runId")
        if isinstance(run_id, str):
            for path in (global_detached_run_path(run_id), local_detached_record_path(manifest_path, run_id)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def find_detached_record(run_id: str) -> Path | None:
    path = global_detached_run_path(run_id)
    if path.exists():
        return path
    for path in [Path.cwd() / ".ocmo" / "runs" / f"{run_id}.json", *Path.cwd().glob(".ocmo/*/manifest.yaml")]:
        candidate = path if path.suffix == ".json" else local_detached_record_path(path, run_id)
        if candidate.exists():
            return candidate
    return None


def detached_records(include_inactive: bool, kind: str | None = None) -> list[dict[str, Any]]:
    records = []
    for path in sorted(global_detached_runs_dir().glob("*.json")):
        try:
            record = read_json_file(path)
        except (OSError, json.JSONDecodeError):
            continue
        record_kind = record.get("kind", "operation")
        if kind is not None and record_kind != kind:
            continue
        if detached_record_is_erased(record):
            continue
        if include_inactive or process_is_alive(record.get("pid")):
            records.append(record)
    return records


def detached_record_is_erased(record: dict[str, Any]) -> bool:
    if process_is_alive(record.get("pid")):
        return False
    state_path_value = record.get("statePath")
    if isinstance(state_path_value, str) and Path(state_path_value).exists():
        return False
    if record.get("kind") == "workflow":
        workflow_path_value = record.get("workflowPath")
        if not isinstance(workflow_path_value, str):
            return isinstance(state_path_value, str)
        if not Path(workflow_path_value).exists():
            return True
        if isinstance(state_path_value, str):
            return True
        return False
    manifest_path_value = record.get("manifestPath")
    if not isinstance(manifest_path_value, str):
        return isinstance(state_path_value, str)
    if not Path(manifest_path_value).exists():
        return True
    if isinstance(state_path_value, str):
        return True
    return False


def print_manifest_detached_runs(manifest_path: Path, include_inactive: bool, params: dict[str, Any] | None = None) -> None:
    manifest = load_manifest(manifest_path, params)
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    print(f"manifest: {manifest_path}")
    print(f"state: {path}")
    print_state_summary(state)
    related = []
    for record_path in sorted(detached_runs_dir(manifest_path).glob("*.json")):
        try:
            record = read_json_file(record_path)
        except (OSError, json.JSONDecodeError):
            continue
        if include_inactive or process_is_alive(record.get("pid")):
            related.append(record)
    if related:
        print("detached runs:")
        for record in related:
            print_detached_record(record, details=False, prefix="  ")


def print_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> None:
    print(render_operation_status(manifest_path, include_inactive, selected_record, params))


def render_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> str:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        print_operation_status_snapshot(manifest_path, include_inactive, selected_record, params)
    return stdout.getvalue().rstrip("\n")


def watch_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float, params: dict[str, Any] | None = None) -> int:
    if sys.stdout.isatty():
        try:
            from rich.live import Live
        except ImportError:
            return watch_operation_status_plain(manifest_path, include_inactive, selected_record, interval, clear_screen=True, params=params)
        try:
            with Live(render_operation_status(manifest_path, include_inactive, selected_record, params), refresh_per_second=max(1, int(1 / interval)), transient=False) as live:
                while True:
                    time.sleep(interval)
                    live.update(render_operation_status(manifest_path, include_inactive, selected_record, params))
        except KeyboardInterrupt:
            return 130
    return watch_operation_status_plain(manifest_path, include_inactive, selected_record, interval, clear_screen=False, params=params)


def watch_operation_status_plain(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float, clear_screen: bool, params: dict[str, Any] | None = None) -> int:
    first = True
    try:
        while True:
            if clear_screen:
                print("\033[H\033[J", end="")
            elif not first:
                print()
            print(render_operation_status(manifest_path, include_inactive, selected_record, params), flush=True)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130


def print_operation_status_snapshot(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> None:
    manifest = load_manifest(manifest_path, params)
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    related = [selected_record] if selected_record else related_detached_records(manifest_path, include_inactive)
    operation_id = manifest["operation"]["id"]
    print(f"OC Mass Operations: {operation_id}")
    print(f"manifest: {manifest_path}")
    print(f"state: {path}")
    for record in related:
        if record:
            print_detached_record(record, details=False, prefix="detached: ")
    rows = operation_status_rows(manifest, state)
    counts = operation_status_counts(rows)
    updated = state.get("updatedAt") or "-"
    print(
        f"selected={len(rows)} running={counts['running']} completed={counts['completed']} "
        f"failed={counts['failed']} blocked={counts['blocked']} pending={counts['pending']} paused={counts['paused']} "
        f"killed={counts['killed']} {format_usage_summary(state_usage(state))} elapsed={operation_runtime(state)} stateUpdated={updated}"
    )
    if any(row["status"] == "running" for row in rows) and related and not any(process_is_alive(record.get("pid")) for record in related if record):
        print("warning: detached run is inactive but state contains running work units; run may be stale")
    print_status_table(rows)


def print_workflow_status(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> None:
    print(render_workflow_status(workflow_path, include_inactive, selected_record, params))


def render_workflow_status(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> str:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        print_workflow_status_snapshot(workflow_path, include_inactive, selected_record, params)
    return stdout.getvalue().rstrip("\n")


def watch_workflow_status(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float, params: dict[str, Any] | None = None) -> int:
    if sys.stdout.isatty():
        try:
            from rich.live import Live
        except ImportError:
            return watch_workflow_status_plain(workflow_path, include_inactive, selected_record, interval, clear_screen=True, params=params)
        try:
            with Live(render_workflow_status(workflow_path, include_inactive, selected_record, params), refresh_per_second=max(1, int(1 / interval)), transient=False) as live:
                while True:
                    time.sleep(interval)
                    live.update(render_workflow_status(workflow_path, include_inactive, selected_record, params))
        except KeyboardInterrupt:
            return 130
    return watch_workflow_status_plain(workflow_path, include_inactive, selected_record, interval, clear_screen=False, params=params)


def watch_workflow_status_plain(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float, clear_screen: bool, params: dict[str, Any] | None = None) -> int:
    first = True
    try:
        while True:
            if clear_screen:
                print("\033[H\033[J", end="")
            elif not first:
                print()
            print(render_workflow_status(workflow_path, include_inactive, selected_record, params), flush=True)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130


def print_workflow_status_snapshot(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> None:
    workflow = load_workflow(workflow_path, params)
    validate_workflow(workflow, workflow_path)
    path = workflow_state_path(workflow, workflow_path)
    state = read_json_file(path) if path.exists() else {}
    related = [selected_record] if selected_record else related_workflow_detached_records(workflow_path, include_inactive)
    workflow_id = workflow["workflow"]["id"]
    print(f"OCMO Workflow: {workflow_id}")
    print(f"workflow: {workflow_path}")
    print(f"state: {path}")
    for record in related:
        if record:
            print_detached_record(record, details=False, prefix="detached: ")
    rows = workflow_status_rows(workflow, workflow_path, state)
    counts = operation_status_counts(rows)
    updated = state.get("updatedAt") or "-"
    print(
        f"steps={len(rows)} running={counts['running']} completed={counts['completed']} "
        f"failed={counts['failed']} blocked={counts['blocked']} pending={counts['pending']} paused={counts['paused']} "
        f"killed={counts['killed']} {format_usage_summary(workflow_usage(workflow, workflow_path))} stateUpdated={updated}"
    )
    if any(row["status"] == "running" for row in rows) and related and not any(process_is_alive(record.get("pid")) for record in related if record):
        print("warning: detached workflow is inactive but state contains running steps; run may be stale")
    print_workflow_status_table(rows)
    running_rows = [row for row in rows if row.get("status") == "running" and row.get("manifestPath")]
    if running_rows:
        print("details:")
        for row in running_rows:
            print(f"  ocmo operation status {row['manifestPath']} --once    # step {row['item']}")


def workflow_status_rows(workflow: dict[str, Any], workflow_path: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    step_states = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    rows = []
    for step in workflow.get("steps", []):
        step_id = str(step["id"])
        step_state = step_states.get(step_id) if isinstance(step_states.get(step_id), dict) else {}
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        operation = "-"
        item_progress = "-"
        tokens = "-"
        detail = str(step.get("description") or "-")
        manifest_path_str = ""
        if manifest_path.exists():
            manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
            operation = str(manifest["operation"]["id"])
            manifest_path_str = str(manifest_path.resolve())
            op_state_path = state_path(manifest, manifest_path)
            op_state = read_json_file(op_state_path) if op_state_path.exists() else {}
            op_rows = operation_status_rows(manifest, op_state)
            if op_rows:
                completed = operation_status_counts(op_rows)["completed"]
                item_progress = f"{completed}/{len(op_rows)}"
            tokens = format_usage_cell(state_usage(op_state))
        status = str(step_state.get("status") or step.get("status") or "pending")
        if step_state.get("error"):
            detail = str(step_state["error"])
        rows.append({"item": step_id, "status": status, "step": operation, "progress": item_progress, "runtime": persisted_item_runtime(step_state, datetime.now(timezone.utc)), "tokens": tokens, "detail": detail, "manifestPath": manifest_path_str})
    return rows


def print_workflow_status_table(rows: list[dict[str, str]]) -> None:
    headers = ["Step", "Status", "Operation", "Items", "Runtime", "Tokens", "Detail"]
    keys = ["item", "status", "step", "progress", "runtime", "tokens", "detail"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, key in enumerate(keys):
            widths[index] = max(widths[index], len(row[key]))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[key].ljust(widths[index]) for index, key in enumerate(keys)))


def workflow_usage(workflow: dict[str, Any], workflow_path: Path) -> dict[str, Any]:
    total: dict[str, Any] = {}
    for step in workflow.get("steps", []):
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        if not manifest_path.exists():
            continue
        manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
        path = state_path(manifest, manifest_path)
        if path.exists():
            total = add_usage(total, state_usage(read_json_file(path)))
    return total


def print_workflow_detached_runs(workflow_path: Path, include_inactive: bool, params: dict[str, Any] | None = None) -> None:
    workflow = load_workflow(workflow_path, params)
    path = workflow_state_path(workflow, workflow_path)
    state = read_json_file(path) if path.exists() else {}
    print(f"workflow: {workflow_path}")
    print(f"state: {path}")
    print_workflow_state_summary(state)
    related = related_workflow_detached_records(workflow_path, include_inactive)
    if related:
        print("detached runs:")
        for record in related:
            print_detached_record(record, details=False, prefix="  ")


def related_workflow_detached_records(workflow_path: Path, include_inactive: bool) -> list[dict[str, Any]]:
    related = []
    for record_path in sorted(workflow_detached_runs_dir(workflow_path).glob("*.json")):
        try:
            record = read_json_file(record_path)
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("kind") != "workflow":
            continue
        if include_inactive or process_is_alive(record.get("pid")):
            related.append(record)
    return related


def related_detached_records(manifest_path: Path, include_inactive: bool) -> list[dict[str, Any]]:
    related = []
    for record_path in sorted(detached_runs_dir(manifest_path).glob("*.json")):
        try:
            record = read_json_file(record_path)
        except (OSError, json.JSONDecodeError):
            continue
        if include_inactive or process_is_alive(record.get("pid")):
            related.append(record)
    return related


def discovered_operation_records(include_inactive: bool, detached: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    detached_paths = {
        str(Path(manifest_path).resolve())
        for manifest_path in (record.get("manifestPath") for record in detached or [])
        if isinstance(manifest_path, str)
    }
    records = []
    for manifest_path in sorted((Path.cwd() / ".ocmo").glob("*/manifest.yaml")):
        try:
            resolved = str(manifest_path.resolve())
            if resolved in detached_paths:
                continue
            manifest, path, state, params = load_manifest_with_persisted_params(manifest_path)
        except (OSError, json.JSONDecodeError, OcmoError):
            continue
        active = operation_state_is_active(state)
        if include_inactive or active:
            records.append({"manifestPath": str(manifest_path), "statePath": str(path), "operationId": manifest["operation"]["id"], "active": active, "state": state, "params": params})
    return records


def load_manifest_with_persisted_params(manifest_path: Path) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    fallback_state_path = manifest_path.parent / "state.json"
    fallback_state = read_json_file(fallback_state_path) if fallback_state_path.exists() else {}
    params = stored_params(fallback_state)
    manifest = load_manifest(manifest_path, params)
    path = state_path(manifest, manifest_path)
    if not path.exists():
        raise OcmoError(f"state not found: {path}")
    state = read_json_file(path)
    state_params = stored_params(state)
    if state_params and state_params != params:
        params = state_params
        manifest = load_manifest(manifest_path, params)
        path = state_path(manifest, manifest_path)
        state = read_json_file(path) if path.exists() else state
    return manifest, path, state, params


def operation_status_targets(detached: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets = []
    seen: set[str] = set()
    for record in detached:
        manifest_value = record.get("manifestPath")
        if not isinstance(manifest_value, str):
            continue
        manifest_path = Path(manifest_value)
        key = str(manifest_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        targets.append({"manifestPath": manifest_path, "record": record, "params": record_params(record), "updatedAt": operation_target_updated_at(record)})
    for record in discovered:
        manifest_value = record.get("manifestPath")
        if not isinstance(manifest_value, str):
            continue
        manifest_path = Path(manifest_value)
        key = str(manifest_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        targets.append({"manifestPath": manifest_path, "params": record.get("params") if isinstance(record.get("params"), dict) else {}, "updatedAt": state.get("updatedAt")})
    return targets


def operation_target_updated_at(record: dict[str, Any]) -> Any:
    state_path_value = record.get("statePath")
    if isinstance(state_path_value, str):
        path = Path(state_path_value)
        if path.exists():
            try:
                state = read_json_file(path)
            except (OSError, json.JSONDecodeError):
                state = {}
            if isinstance(state, dict) and state.get("updatedAt"):
                return state.get("updatedAt")
    return record.get("startedAt")


def latest_operation_status_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not targets:
        return None
    return max(targets, key=lambda target: sortable_state_time(target.get("updatedAt")))


def sortable_state_time(value: Any) -> tuple[int, str]:
    if not isinstance(value, str) or not value:
        return (0, "")
    parsed = parse_state_datetime(value)
    if parsed is not None:
        return (1, parsed.isoformat())
    return (0, value)


def operation_state_is_active(state: dict[str, Any]) -> bool:
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    for item_state in work_units.values():
        if not isinstance(item_state, dict):
            continue
        if state_status_is_active(item_state.get("status")):
            return True
        runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
        if any(isinstance(run_state, dict) and state_status_is_active(run_state.get("status")) for run_state in runs.values()):
            return True
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    return state_status_is_active(state.get("status")) or state_status_is_active(control.get("status"))


def state_status_is_active(status: Any) -> bool:
    return str(status) in {"running", "creating_worktree", "worktree_ready", "setup", "setup_completed", "cleanup"}


def discovered_workflow_records(include_inactive: bool, detached: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    detached_paths = {
        str(Path(workflow_path).resolve())
        for workflow_path in (record.get("workflowPath") for record in detached or [])
        if isinstance(workflow_path, str)
    }
    records = []
    for workflow_path in sorted((Path.cwd() / ".ocmo").glob("*/workflow.yaml")):
        try:
            resolved = str(workflow_path.resolve())
            if resolved in detached_paths:
                continue
            workflow, path, state, params = load_workflow_with_persisted_params(workflow_path)
        except (OSError, json.JSONDecodeError, OcmoError):
            continue
        active = workflow_state_is_active(state)
        if include_inactive or active:
            records.append({"workflowPath": str(workflow_path), "statePath": str(path), "workflowId": workflow["workflow"]["id"], "active": active, "state": state, "workflow": workflow, "params": params})
    return records


def load_workflow_with_persisted_params(workflow_path: Path) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    fallback_state_path = workflow_path.parent / "state.json"
    fallback_state = read_json_file(fallback_state_path) if fallback_state_path.exists() else {}
    params = stored_params(fallback_state)
    workflow = load_workflow(workflow_path, params)
    path = workflow_state_path(workflow, workflow_path)
    if not path.exists():
        raise OcmoError(f"workflow state not found: {path}")
    state = read_json_file(path)
    state_params = stored_params(state)
    if state_params and state_params != params:
        params = state_params
        workflow = load_workflow(workflow_path, params)
        path = workflow_state_path(workflow, workflow_path)
        state = read_json_file(path) if path.exists() else state
    return workflow, path, state, params


def workflow_state_is_active(state: dict[str, Any]) -> bool:
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    for step_state in steps.values():
        if isinstance(step_state, dict) and state_status_is_active(step_state.get("status")):
            return True
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    return state_status_is_active(state.get("status")) or state_status_is_active(control.get("status"))


def workflow_status_targets(detached: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets = []
    seen: set[str] = set()
    for record in detached:
        workflow_value = record.get("workflowPath")
        if not isinstance(workflow_value, str):
            continue
        workflow_path = Path(workflow_value)
        key = str(workflow_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        targets.append({"workflowPath": workflow_path, "record": record, "params": record_params(record), "updatedAt": workflow_target_updated_at(record)})
    for record in discovered:
        workflow_value = record.get("workflowPath")
        if not isinstance(workflow_value, str):
            continue
        workflow_path = Path(workflow_value)
        key = str(workflow_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        targets.append({"workflowPath": workflow_path, "params": record.get("params") if isinstance(record.get("params"), dict) else {}, "updatedAt": state.get("updatedAt")})
    return targets


def workflow_target_updated_at(record: dict[str, Any]) -> Any:
    state_path_value = record.get("statePath")
    if isinstance(state_path_value, str):
        path = Path(state_path_value)
        if path.exists():
            try:
                state = read_json_file(path)
            except (OSError, json.JSONDecodeError):
                state = {}
            if isinstance(state, dict) and state.get("updatedAt"):
                return state.get("updatedAt")
    return record.get("startedAt")


def latest_workflow_status_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not targets:
        return None
    return max(targets, key=lambda target: sortable_state_time(target.get("updatedAt")))


def print_workflow_record(record: dict[str, Any]) -> None:
    status = "active" if record.get("active") else "inactive"
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else None
    workflow_path_value = record.get("workflowPath", "-")
    if workflow is None and isinstance(workflow_path_value, str):
        try:
            workflow = load_workflow(Path(workflow_path_value))
        except OcmoError:
            workflow = None
    if workflow is not None:
        rows = workflow_status_rows(workflow, Path(workflow_path_value), state)
    else:
        rows = [{"item": str(step_id), "status": str(step_state.get("status", "unknown")) if isinstance(step_state, dict) else "unknown"} for step_id, step_state in (state.get("steps") or {}).items()]
    counts = operation_status_counts(rows)
    print(
        f"{record.get('workflowId', '<unknown>')} {status} kind=workflow workflow={workflow_path_value} "
        f"state={record.get('statePath', '-')} running={counts['running']} completed={counts['completed']} "
        f"failed={counts['failed']} pending={counts['pending']} stateUpdated={state.get('updatedAt', '-')}"
    )
    if workflow is not None:
        mappings = []
        for step in workflow.get("steps", []):
            step_id = str(step.get("id", ""))
            manifest_path = workflow_step_manifest_path(Path(workflow_path_value), step)
            if not manifest_path.exists():
                continue
            try:
                step_manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
            except OcmoError:
                continue
            op_id = step_manifest.get("operation", {}).get("id") if isinstance(step_manifest.get("operation"), dict) else None
            if step_id and op_id:
                mappings.append(f"{step_id}->{op_id}")
        if mappings:
            print(f"  steps: {', '.join(mappings)}")


def print_operation_record(record: dict[str, Any]) -> None:
    status = "active" if record.get("active") else "inactive"
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    counts = operation_status_counts(operation_state_rows(state))
    print(
        f"{record.get('operationId', '<unknown>')} {status} kind=operation manifest={record.get('manifestPath', '-')} "
        f"state={record.get('statePath', '-')} running={counts['running']} completed={counts['completed']} "
        f"failed={counts['failed']} pending={counts['pending']} elapsed={operation_runtime(state)} stateUpdated={state.get('updatedAt', '-')}"
    )


def operation_state_rows(state: dict[str, Any]) -> list[dict[str, str]]:
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    rows = []
    for item_id, item_state in work_units.items():
        status = str(item_state.get("status", "unknown")) if isinstance(item_state, dict) else "unknown"
        rows.append({"item": str(item_id), "status": status})
    return rows


def operation_status_rows(manifest: dict[str, Any], state: dict[str, Any]) -> list[dict[str, str]]:
    item_states = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    manifest_items = {str(item["id"]): item for item in manifest.get("workUnits", []) if isinstance(item, dict) and "id" in item}
    item_ids = [str(item["id"]) for item in manifest.get("workUnits", []) if isinstance(item, dict) and "id" in item]
    for item_id in item_states:
        if item_id not in manifest_items:
            item_ids.append(item_id)
    rows = []
    now = datetime.now(timezone.utc)
    for item_id in item_ids:
        item = manifest_items.get(item_id, {"id": item_id})
        item_state = item_states.get(item_id) if isinstance(item_states.get(item_id), dict) else {}
        status = str(item_state.get("status") or "pending")
        runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
        run_id, run_state = current_run_state(runs)
        total = int(item_state.get("runCount") or len(work_unit_runs(manifest, item)) or 1)
        current = total if not runs and (status in TERMINAL_STATUSES or status in DONE_STATUSES) else started_run_count(runs)
        progress = f"{current}/{total}"
        rows.append(
            {
                "item": item_id,
                "status": status,
                "step": run_id or "-",
                "progress": progress,
                "workTime": persisted_item_runtime(item_state, now),
                "agentTime": persisted_run_runtime(run_state, now),
                "tokens": format_usage_cell(item_usage(item_state)),
                "detail": status_detail(item, item_state, run_state),
            }
        )
    return rows


def current_run_state(runs: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    if not runs:
        return None, {}
    for run_id, run_state in reversed(list(runs.items())):
        if isinstance(run_state, dict) and run_state.get("status") == "running":
            return str(run_id), run_state
    run_id, run_state = list(runs.items())[-1]
    return str(run_id), run_state if isinstance(run_state, dict) else {}


def started_run_count(runs: dict[str, Any]) -> int:
    return sum(1 for run in runs.values() if isinstance(run, dict) and str(run.get("status", "")) != "queued")


def persisted_item_runtime(item_state: dict[str, Any], now: datetime) -> str:
    started = parse_state_datetime(item_state.get("startedAt"))
    if started is None:
        return "-"
    ended = parse_state_datetime(item_state.get("completedAt")) or now
    return format_duration((ended - started).total_seconds())


def persisted_run_runtime(run_state: dict[str, Any], now: datetime) -> str:
    started = parse_state_datetime(run_state.get("startedAt"))
    if started is None:
        return "-"
    ended = parse_state_datetime(run_state.get("completedAt")) or now
    return format_duration((ended - started).total_seconds())


def operation_runtime(state: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    started = parse_state_datetime(state.get("startedAt")) or earliest_state_started_at(state)
    if started is None:
        return "-"
    ended = parse_state_datetime(state.get("completedAt")) or now
    return format_duration((ended - started).total_seconds())


def earliest_state_started_at(state: dict[str, Any]) -> datetime | None:
    starts: list[datetime] = []
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    for item_state in work_units.values():
        if not isinstance(item_state, dict):
            continue
        started = parse_state_datetime(item_state.get("startedAt"))
        if started is not None:
            starts.append(started)
        runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
        for run_state in runs.values():
            if not isinstance(run_state, dict):
                continue
            started = parse_state_datetime(run_state.get("startedAt"))
            if started is not None:
                starts.append(started)
    return min(starts) if starts else None


def parse_state_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def status_detail(item: dict[str, Any], item_state: dict[str, Any], run_state: dict[str, Any]) -> str:
    for source in (run_state, item_state):
        error = source.get("error")
        if error:
            return str(error)
    output_path = run_state.get("outputPath")
    if output_path:
        return str(output_path)
    worktree_path = item_state.get("worktreePath")
    if worktree_path:
        return str(worktree_path)
    title = item.get("title")
    if title:
        return str(title)
    return "-"


def operation_status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = {"running": 0, "completed": 0, "failed": 0, "blocked": 0, "pending": 0, "paused": 0, "killed": 0}
    for row in rows:
        status = row["status"]
        if status in DONE_STATUSES:
            counts["completed"] += 1
        elif status in PAUSED_STATUSES:
            counts["paused"] += 1
        elif status == "killed":
            counts["killed"] += 1
        elif status in BLOCKED_STATUSES:
            counts["blocked"] += 1
        elif status in {"failed", "timed_out", "cleanup_failed", "worktree_failed", "setup_failed"}:
            counts["failed"] += 1
        elif status in {"queued", "pending"}:
            counts["pending"] += 1
        else:
            counts["running"] += 1
    return counts


def print_status_table(rows: list[dict[str, str]]) -> None:
    headers = ["Work Unit", "Status", "Step", "Progress", "Work Time", "Agent Time", "Tokens", "Detail"]
    keys = ["item", "status", "step", "progress", "workTime", "agentTime", "tokens", "detail"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, key in enumerate(keys):
            widths[index] = max(widths[index], len(row[key]))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[key].ljust(widths[index]) for index, key in enumerate(keys)))


def detached_record_operation_id(record: dict[str, Any]) -> str | None:
    manifest_value = record.get("manifestPath")
    if not isinstance(manifest_value, str):
        return None
    manifest_path = Path(manifest_value)
    if not manifest_path.exists():
        return None
    try:
        manifest = load_manifest(manifest_path, record_params(record))
    except OcmoError:
        return None
    op = manifest.get("operation") if isinstance(manifest.get("operation"), dict) else None
    op_id = op.get("id") if op else None
    return str(op_id) if op_id else None


def print_detached_record(record: dict[str, Any], details: bool, prefix: str = "") -> None:
    pid = record.get("pid")
    status = "active" if process_is_alive(pid) else "inactive"
    kind = record.get("kind", "operation")
    extra = ""
    if kind == "operation":
        op_id = detached_record_operation_id(record)
        if op_id:
            extra = f" operation={op_id}"
    print(f"{prefix}{record.get('runId', '<unknown>')} {status} kind={kind}{extra} pid={pid} started={record.get('startedAt', '-')}")
    if not details:
        return
    if kind == "workflow":
        print(f"workflow: {record.get('workflowPath', '-')}")
    else:
        print(f"manifest: {record.get('manifestPath', '-')}")
    print(f"state: {record.get('statePath', '-')}")
    print(f"log: {record.get('logPath', '-')}")
    path = record.get("statePath")
    if isinstance(path, str) and Path(path).exists():
        if kind == "workflow":
            print_workflow_state_summary(read_json_file(Path(path)))
        else:
            print_state_summary(read_json_file(Path(path)))


def print_state_summary(state: dict[str, Any]) -> None:
    items = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    if not items:
        print("workUnits: none")
        return
    counts: dict[str, int] = {}
    for item in items.values():
        status = str(item.get("status", "unknown")) if isinstance(item, dict) else "unknown"
        counts[status] = counts.get(status, 0) + 1
    print("workUnits: " + ", ".join(f"{status}={counts[status]}" for status in sorted(counts)))
    updated = state.get("updatedAt")
    if updated:
        print(f"stateUpdated: {updated}")
    print(format_usage_summary(state_usage(state)))


def print_workflow_state_summary(state: dict[str, Any]) -> None:
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    if not steps:
        print("steps: none")
        return
    counts: dict[str, int] = {}
    for step in steps.values():
        status = str(step.get("status", "unknown")) if isinstance(step, dict) else "unknown"
        counts[status] = counts.get(status, 0) + 1
    print("steps: " + ", ".join(f"{status}={counts[status]}" for status in sorted(counts)))
    updated = state.get("updatedAt")
    if updated:
        print(f"stateUpdated: {updated}")
