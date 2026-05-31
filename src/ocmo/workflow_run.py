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

def workflow_command(args: argparse.Namespace) -> int:
    command = args.workflow_command
    params = command_params(args)
    if command == "validate":
        workflow_path = infer_workflow_path(args.workflow)
        workflow = load_workflow(workflow_path, params)
        validate_workflow(workflow, workflow_path)
        print(f"valid: {workflow_path}")
        return 0
    if command == "run":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, args.dry_run, args.yes, args.ui, args.detach, False, False, params, args.fresh))
    if command == "status":
        return status_workflow(args)
    if command == "list":
        return list_workflow_runs(args)
    if command == "pause":
        return pause_workflow(args)
    if command == "resume":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, False, args.yes, args.ui, args.detach, True, False, params))
    if command == "rerun":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, False, args.yes, args.ui, args.detach, False, True, params))
    if command == "kill":
        return kill_workflow(args)
    if command == "erase":
        return erase_workflow(args)
    return 1  # pragma: no cover


def run_workflow(options: WorkflowOptions) -> int:
    workflow = load_workflow(options.workflow_path, options.params)
    validate_workflow(workflow, options.workflow_path)
    state_path_value = workflow_state_path(workflow, options.workflow_path)
    existing_state = {} if options.fresh else read_json_file(state_path_value) if state_path_value.exists() else {}
    selected = select_workflow_steps(workflow, existing_state, options.select or ("retryable" if options.rerun else "uncompleted"))
    if not selected:
        print("No workflow steps selected.")
        return 0
    if options.detach:
        if options.dry_run:
            raise OcmoError("--detach cannot be used with --dry-run")
        return start_detached_workflow(options, workflow)
    if options.dry_run:
        if options.fresh:
            print(f"# fresh: would erase workflow state {state_path_value}")
        print_workflow_dry_run(workflow, options.workflow_path, selected, options.rerun, options.resume)
        return 0
    if not options.yes:
        print(f"About to run {len(selected)} workflow step(s) sequentially.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    if options.fresh:
        clean_workflow_before_run(workflow, options.workflow_path, selected)
    state = WorkflowStateStore(state_path_value)
    state.ensure_workflow(workflow)
    for step in selected:
        state.prepare_step(workflow, options.workflow_path, step, options.rerun)
    results: list[int] = []
    try:
        for step in selected:
            code = run_workflow_step(workflow, options.workflow_path, step, state, options)
            results.append(code)
            if code != 0 and workflow_step_stop_on_failure(workflow, step):
                state.finish("failed")
                return 1
    except KeyboardInterrupt:
        print("ocmo: interrupted; pausing active workflow step", file=sys.stderr)
        pause_workflow_path(options.workflow_path, params=resolved_params(workflow))
        return 130
    state.finish("completed" if all(code == 0 for code in results) else "failed")
    return 0 if all(code == 0 for code in results) else 1


def run_workflow_step(workflow: dict[str, Any], workflow_path: Path, step: dict[str, Any], state: "WorkflowStateStore", options: WorkflowOptions) -> int:
    step_id = str(step["id"])
    operation_params = workflow_step_operation_params(workflow, step)
    manifest_path = workflow_step_manifest_path(workflow_path, step)
    manifest = load_manifest(manifest_path, operation_params)
    operation_state_path = state_path(manifest, manifest_path)
    operation_id = str(manifest["operation"]["id"])
    state.mark_step(
        step_id,
        "running",
        {
            "manifestPath": str(manifest_path.resolve()),
            "statePath": str(operation_state_path),
            "operationId": operation_id,
            "startedAt": utc_now(),
        },
    )
    resume_step = options.resume and operation_has_paused_work(manifest, manifest_path)
    run_options = RunOptions(
        manifest_path,
        workflow_step_operation_select(workflow, step),
        workflow_step_concurrency(workflow, step),
        workflow_step_timeout_seconds(workflow, step),
        False,
        True,
        options.ui,
        workflow_step_allow_shared_worktree_concurrency(workflow, step),
        False,
        False,
        resume_step,
        options.rerun,
        operation_params,
        options.fresh,
    )
    code = run_manifest(run_options)
    status = "completed" if code == 0 else workflow_failed_step_status(operation_state_path)
    patch: dict[str, Any] = {"completedAt": utc_now(), "exitCode": code}
    if code != 0:
        patch["error"] = f"operation exited with code {code}"
    state.mark_step(step_id, status, patch)
    return code


def clean_workflow_before_run(workflow: dict[str, Any], workflow_path: Path, selected: list[dict[str, Any]]) -> None:
    path = workflow_state_path(workflow, workflow_path)
    state = read_json_file(path) if path.exists() else {}
    if workflow_state_is_active(state) or related_workflow_detached_records(workflow_path, include_inactive=False):
        raise OcmoError("cannot clean before run while workflow appears active; pause, kill, or erase it first")
    remove_workflow_detached_records(workflow_path)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise OcmoError(f"could not remove workflow state {path}: {exc}") from exc
    for step in selected:
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
        clean_operation_before_run(manifest, manifest_path)


def workflow_failed_step_status(operation_state_path: Path) -> str:
    if operation_state_path.exists():
        state = read_json_file(operation_state_path)
        work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
        statuses = [work_unit.get("status") for work_unit in work_units.values() if isinstance(work_unit, dict)]
        if any(status in PAUSED_STATUSES for status in statuses):
            return "paused" if any(status == "paused" for status in statuses) else "paused_unresumable"
        if any(status == "killed" for status in statuses):
            return "killed"
        if any(status == "timed_out" for status in statuses):
            return "timed_out"
        if any(status in BLOCKED_STATUSES for status in statuses):
            return "blocked"
    return "failed"


def operation_has_paused_work(manifest: dict[str, Any], manifest_path: Path) -> bool:
    path = state_path(manifest, manifest_path)
    if not path.exists():
        return False
    state = read_json_file(path)
    return bool(select_paused_work_units(manifest, state))


def start_detached_workflow(options: WorkflowOptions, workflow: dict[str, Any]) -> int:
    run_id = detached_workflow_run_id()
    local_dir = workflow_detached_runs_dir(options.workflow_path)
    local_dir.mkdir(parents=True, exist_ok=True)
    log_path = local_dir / f"{run_id}.log"
    command = detached_workflow_child_command(options)
    log = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        log.close()
        raise OcmoError(f"could not start detached workflow: {exc}") from exc
    log.close()
    metadata = {
        "schema": "ocmo-detached-run/v1",
        "kind": "workflow",
        "runId": run_id,
        "pid": process.pid,
        "startedAt": utc_now(),
        "workflowPath": str(options.workflow_path.resolve()),
        "statePath": str(workflow_state_path(workflow, options.workflow_path)),
        "logPath": str(log_path.resolve()),
        "command": command,
        "select": options.select,
        "fresh": options.fresh,
        "params": options.params,
    }
    write_detached_metadata(local_dir / f"{run_id}.json", metadata)
    write_detached_metadata(global_detached_run_path(run_id), metadata)
    print(f"detached: {run_id}")
    print(f"pid: {process.pid}")
    print(f"log: {relative_to_workflow(log_path, options.workflow_path)}")
    print(f"status: ocmo workflow status --run-id {run_id}")
    return 0


def detached_workflow_child_command(options: WorkflowOptions) -> list[str]:
    subcommand = "resume" if options.resume else "rerun" if options.rerun else "run"
    command = [sys.executable, "-m", "ocmo", "workflow", subcommand, str(options.workflow_path.resolve())]
    if options.select:
        command += ["--select", options.select]
    command += parameter_arguments(options.params)
    command += ["--ui", "plain", "--yes"]
    if options.fresh and not options.resume and not options.rerun:
        command.append("--fresh")
    return command


def detached_workflow_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"ocmo-workflow-{stamp}-{uuid.uuid4().hex[:6]}"


def status_workflow(args: argparse.Namespace) -> int:
    if args.active_or_latest:
        return print_active_or_latest_workflow_statuses(include_inactive=args.all)
    record = None
    if args.run_id:
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        record = read_json_file(path)
        if record.get("kind") != "workflow":
            raise OcmoError(f"detached run is not a workflow: {args.run_id}")
        workflow_value = record.get("workflowPath")
        if not isinstance(workflow_value, str):
            print_detached_record(record, details=True)
            return 0
        workflow_path = Path(workflow_value)
    else:
        workflow_path = infer_workflow_path(args.workflow)
    interval = getattr(args, "interval", 1.0)
    if interval <= 0:
        raise OcmoError("--interval must be greater than zero")
    params = command_params(args, record)
    if args.once:
        print_workflow_status(workflow_path, include_inactive=args.all, selected_record=record, params=params)
        return 0
    return watch_workflow_status(workflow_path, include_inactive=args.all, selected_record=record, interval=interval, params=params)


def print_active_or_latest_workflow_statuses(include_inactive: bool = False) -> int:
    active_records = detached_records(include_inactive=False, kind="workflow")
    active = workflow_status_targets(active_records, discovered_workflow_records(include_inactive=False, detached=active_records))
    targets = active
    if not targets:
        all_records = detached_records(include_inactive=True, kind="workflow")
        all_targets = workflow_status_targets(all_records, discovered_workflow_records(include_inactive=True, detached=all_records))
        latest = latest_workflow_status_target(all_targets)
        if latest is not None:
            targets = [latest]
    if not targets:
        print("No ocmo workflows found.")
        return 0
    heading = "Active OCMO workflow statuses" if active else "Latest OCMO workflow status"
    print(heading)
    for index, target in enumerate(targets):
        if index:
            print()
        print_workflow_status(target["workflowPath"], include_inactive=include_inactive, selected_record=target.get("record"), params=target.get("params"))
    return 0


def list_workflow_runs(args: argparse.Namespace) -> int:
    if args.run_id:
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        record = read_json_file(path)
        if record.get("kind") != "workflow":
            raise OcmoError(f"detached run is not a workflow: {args.run_id}")
        print_detached_record(record, details=True)
        return 0
    if args.workflow:
        print_workflow_detached_runs(infer_workflow_path(args.workflow), include_inactive=args.all, params=command_params(args))
        return 0
    records = detached_records(include_inactive=args.all, kind="workflow")
    workflows = discovered_workflow_records(include_inactive=args.all, detached=records)
    if not records and not workflows:
        qualifier = "" if args.all else " active"
        print(f"No{qualifier} ocmo workflows.")
        return 0
    for record in records:
        print_detached_record(record, details=False)
    for workflow in workflows:
        print_workflow_record(workflow)
    return 0


def pause_workflow(args: argparse.Namespace) -> int:
    workflow_path, record = control_workflow_and_record(args)
    params = command_params(args, record)
    changed = pause_workflow_path(workflow_path, record, params)
    workflow = load_workflow(workflow_path, params)
    print(f"paused: {workflow['workflow']['id']} ({changed} step(s))")
    return 0


def kill_workflow(args: argparse.Namespace) -> int:
    workflow_path, record = control_workflow_and_record(args)
    params = command_params(args, record)
    workflow = load_workflow(workflow_path, params)
    if not args.force and sys.stdin.isatty():
        answer = input(f"Kill workflow {workflow['workflow']['id']}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    changed = mark_active_workflow_steps(workflow_path, "killed", params)
    stop_workflow_processes(workflow_path, record, "killed", params)
    print(f"killed: {workflow['workflow']['id']} ({changed} step(s))")
    return 0


def erase_workflow(args: argparse.Namespace) -> int:
    workflow_path = infer_workflow_path(args.workflow)
    return erase_workflow_path(workflow_path, args.force, args.keep_workflow_state, command_params(args))


def erase_workflow_path(workflow_path: Path, force: bool, keep_workflow_state: bool, params: dict[str, Any] | None = None) -> int:
    workflow = load_workflow(workflow_path, params)
    if workflow.get("schema") != "ocmo-workflow/v1":
        raise OcmoError("workflow schema must be ocmo-workflow/v1")
    metadata = require_mapping(workflow, "workflow")
    require_string(metadata, "id")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise OcmoError("steps must be a non-empty list")
    if not force and sys.stdin.isatty():
        answer = input(f"Erase runtime data for workflow {workflow['workflow']['id']} ({len(steps)} step(s))? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    if not force and not sys.stdin.isatty():
        raise OcmoError("erase requires --force when not interactive")
    stop_workflow_processes(workflow_path, None, "killed", params)
    erased = 0
    skipped_missing = 0
    skipped_non_generated = 0
    for step in steps:
        step_id = str(step["id"])
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        if not manifest_path.exists():
            print(f"skip {step_id}: manifest not found ({manifest_path})")
            skipped_missing += 1
            continue
        if not is_generated_workflow_operation_manifest(workflow_path, manifest_path):
            print(f"skip {step_id}: not a generated workflow operation manifest ({manifest_path})")
            skipped_non_generated += 1
            continue
        manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step))
        op_state_path = state_path(manifest, manifest_path)
        op_state = read_json_file(op_state_path) if op_state_path.exists() else {}
        stop_operation_processes(manifest_path, op_state)
        remove_detached_records(manifest_path)
        erase_operation_runtime_data(manifest, manifest_path)
        print(f"erased {step_id}: {manifest_path.parent}")
        erased += 1
    if not keep_workflow_state:
        remove_workflow_detached_records(workflow_path)
        state_file = workflow_state_path(workflow, workflow_path)
        try:
            state_file.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"cleared workflow state: {state_file}")
    summary = f"erased {erased} operation(s)"
    if skipped_missing:
        summary += f"; skipped {skipped_missing} missing"
    if skipped_non_generated:
        summary += f"; skipped {skipped_non_generated} non-generated"
    print(summary)
    return 0


def remove_workflow_detached_records(workflow_path: Path) -> None:
    for record in related_workflow_detached_records(workflow_path, include_inactive=True):
        run_id = record.get("runId")
        if not isinstance(run_id, str):
            continue
        for path in (global_detached_run_path(run_id), workflow_detached_runs_dir(workflow_path) / f"{run_id}.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def pause_workflow_path(workflow_path: Path, record: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> int:
    effective_params = params or record_params(record)
    changed = mark_active_workflow_steps(workflow_path, "paused", effective_params)
    stop_workflow_processes(workflow_path, record, "paused", effective_params)
    return changed


def control_workflow_and_record(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None]:
    if getattr(args, "run_id", None):
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        record = read_json_file(path)
        if record.get("kind") != "workflow":
            raise OcmoError(f"detached run is not a workflow: {args.run_id}")
        workflow_value = record.get("workflowPath")
        if not isinstance(workflow_value, str):
            raise OcmoError(f"detached workflow is missing workflowPath: {args.run_id}")
        return Path(workflow_value), record
    return infer_workflow_path(args.workflow), None


def stop_workflow_processes(workflow_path: Path, selected_record: dict[str, Any] | None = None, operation_status: str = "paused", params: dict[str, Any] | None = None) -> None:
    if selected_record and isinstance(selected_record.get("pid"), int):
        terminate_process_tree(selected_record["pid"], force=True)
    for record in related_workflow_detached_records(workflow_path, include_inactive=True):
        pid = record.get("pid")
        if isinstance(pid, int):
            terminate_process_tree(pid, force=True)
    workflow = load_workflow(workflow_path, params or record_params(selected_record))
    workflow_steps = {str(step.get("id")): step for step in workflow.get("steps", []) if isinstance(step, dict)}
    state_file = workflow_state_path(workflow, workflow_path)
    state = read_json_file(state_file) if state_file.exists() else {}
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    for step_id, step_state in steps.items():
        if not isinstance(step_state, dict) or step_state.get("status") not in {"running", "paused", "killed"}:
            continue
        manifest_value = step_state.get("manifestPath")
        if isinstance(manifest_value, str):
            manifest_path = Path(manifest_value)
            step = workflow_steps.get(str(step_id))
            manifest = load_manifest(manifest_path, workflow_step_operation_params(workflow, step) if step else resolved_params(workflow))
            operation_state = read_json_file(state_path(manifest, manifest_path)) if state_path(manifest, manifest_path).exists() else {}
            mark_active_runs(state_path(manifest, manifest_path), operation_status)
            stop_operation_processes(manifest_path, operation_state)


def mark_active_workflow_steps(workflow_path: Path, status: str, params: dict[str, Any] | None = None) -> int:
    workflow = load_workflow(workflow_path, params)
    path = workflow_state_path(workflow, workflow_path)
    if not path.exists():
        return 0

    def update(data: dict[str, Any]) -> int:
        steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
        changed = 0
        now = utc_now()
        for step in steps.values():
            if isinstance(step, dict) and step.get("status") == "running":
                step["status"] = status
                step[f"{status}At"] = now
                changed += 1
        data.setdefault("control", {}).update({"status": status, "updatedAt": now})
        data["status"] = status
        data.pop("completedAt", None)
        data["updatedAt"] = now
        return changed

    return update_json_state_file(path, update)
