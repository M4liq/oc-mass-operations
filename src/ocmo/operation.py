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

def run_manifest(options: RunOptions) -> int:
    manifest = load_manifest(options.manifest_path, options.params)
    validate_manifest(manifest, options.manifest_path, options.allow_shared_worktree_concurrency)
    fresh = options.fresh or (not options.resume and not options.rerun and clean_before_run_enabled(manifest))
    existing_state = {} if fresh else read_json_file(state_path(manifest, options.manifest_path)) if state_path(manifest, options.manifest_path).exists() else {}
    if options.resume:
        selected = select_paused_work_units(manifest, existing_state)
    elif options.rerun:
        selected = select_rerun_work_units(manifest, existing_state, options.select)
    else:
        selected = select_work_units(manifest, options.select, existing_state)
    concurrency = options.concurrency if options.concurrency is not None else manifest.get("queue", {}).get("concurrency", 1)
    timeout_seconds = options.timeout_seconds
    auto_worktrees = auto_worktrees_config(manifest)
    if concurrency < 1:
        raise OcmoError("concurrency must be a positive integer")
    if timeout_seconds is not None and timeout_seconds < 1:
        raise OcmoError("timeout must be a positive integer")
    if manifest.get("policy", {}).get("worktree") == "single" and concurrency > 1 and not options.allow_shared_worktree_concurrency:
        raise OcmoError("policy.worktree=single cannot run with concurrency > 1; pass --allow-shared-worktree-concurrency to override")
    if manifest.get("policy", {}).get("worktree") == "single" and auto_worktrees["enabled"]:  # pragma: no cover
        raise OcmoError("policy.worktree=single cannot be used with queue.autoWorktrees.enabled=true")
    if not selected:
        print("No work units selected.")
        return 0

    if options.detach:
        if options.dry_run:
            raise OcmoError("--detach cannot be used with --dry-run")
        return start_detached_run(options, manifest, concurrency, timeout_seconds)

    if options.dry_run:
        previews = []
        hook_details = operation_hook_preview_details(manifest)
        clean_details = operation_clean_preview_details(manifest, options.manifest_path, "beforeRun") if fresh else []
        for item in selected:
            execution = worktree_execution(manifest, options.manifest_path, item) if auto_worktrees["enabled"] else {}
            run_dir = Path(execution["worktreePath"]) if execution else None
            runs = work_unit_runs(manifest, item)
            for run in runs:
                runner = effective_runner(manifest, run)
                run_timeout = timeout_seconds if timeout_seconds is not None else runner.get("timeoutSeconds")
                prompt_text = render_prompt(manifest, item, options.manifest_path, execution, run, runs)
                command = build_command(manifest, options.manifest_path, prompt_text, run_dir, runner)
                details = []
                if should_use_prompt_file(command):
                    prompt_file = prompt_input_path(options.manifest_path, str(item["id"]), str(run["id"]))
                    command = build_command(manifest, options.manifest_path, prompt_text, run_dir, runner, prompt_file)
                    details.append(("prompt transport", f"file when executed -> {relative_to_manifest(prompt_file, options.manifest_path)}"))
                if execution:
                    details.append(("worktree", str(execution["worktreePath"])))
                    details.append(("branch", str(execution["branchName"])))
                if run_timeout:
                    details.append(("timeout", f"{run_timeout} seconds"))
                for artifact_id, config in produced_artifacts(run).items():
                    details.append(("produces", f"{artifact_id} -> {produced_artifact_relative_path(options.manifest_path, item, str(run['id']), artifact_id, config)}"))
                for reference in run.get("consumes") or []:
                    details.append(("consumes", str(reference)))
                details.extend(clean_details)
                details.extend(hook_details)
                previews.append(PromptPreview(str(item["id"]), str(run["id"]), prompt_text, format_command(command), details))
        print_prompt_previews(previews, options.preview_all)
        return 0

    if not options.yes:
        timeout_text = f", timeout={timeout_seconds}s" if timeout_seconds else ""
        worktree_text = ", autoWorktrees=true" if auto_worktrees["enabled"] else ""
        fresh_text = ", fresh=true" if fresh else ""
        print(f"About to run {len(selected)} work unit(s) with concurrency={concurrency}{timeout_text}{worktree_text}{fresh_text}.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    if fresh:
        clean_operation_before_run(manifest, options.manifest_path)
    state = StateStore(state_path(manifest, options.manifest_path))
    state.ensure_operation(manifest)
    before_code = run_operation_hook(manifest, options.manifest_path, state, "beforeRun", len(selected))
    if before_code != 0:
        run_operation_hook(manifest, options.manifest_path, state, "onFailure", len(selected), "setup_failed")
        after_code = run_operation_hook(manifest, options.manifest_path, state, "afterRun", len(selected), "setup_failed")
        state.finish("cleanup_failed" if after_code != 0 else "setup_failed")
        return 1
    results: list[int] = []
    with make_run_reporter(options.ui) as reporter:
        reporter.start(manifest, selected, concurrency, auto_worktrees)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
        futures: dict[concurrent.futures.Future[int], str] = {}
        try:
            futures = {
                executor.submit(run_item, manifest, options.manifest_path, item, state, timeout_seconds, auto_worktrees, reporter, options.resume, options.rerun): str(item["id"])
                for item in selected
            }
            for future in concurrent.futures.as_completed(futures):
                item_id = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": f"unexpected worker error: {exc}"})
                    reporter.worker_error(item_id, exc)
                    results.append(1)
        except KeyboardInterrupt:
            print("ocmo: interrupted; pausing active runs", file=sys.stderr)
            interrupted_state = state.data()
            mark_active_runs(state.path, "paused")
            state.finish("paused")
            stop_operation_processes(options.manifest_path, interrupted_state)
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return 130
        else:
            executor.shutdown(wait=True)

    if any(code != 0 for code in results):
        failed_status = operation_failed_status(state.data())
        run_operation_hook(manifest, options.manifest_path, state, "onFailure", len(selected), failed_status)
        after_code = run_operation_hook(manifest, options.manifest_path, state, "afterRun", len(selected), failed_status)
        state.finish("cleanup_failed" if after_code != 0 else failed_status)
        return 1
    after_code = run_operation_hook(manifest, options.manifest_path, state, "afterRun", len(selected), "completed")
    if after_code != 0:
        state.finish("cleanup_failed")
        return 1
    state.finish("completed")
    return 0


def operation_hook_preview_details(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    details = []
    for hook in ("beforeRun", "onFailure", "afterRun"):
        for script in operation_hook_scripts(manifest, hook):
            details.append((f"hook {hook}", script))
    return details


def clean_before_run_enabled(manifest: dict[str, Any]) -> bool:
    clean = manifest.get("clean") if isinstance(manifest.get("clean"), dict) else {}
    return bool(clean.get("beforeRun"))


def operation_clean_preview_details(manifest: dict[str, Any], manifest_path: Path, event: str) -> list[tuple[str, str]]:
    details = [("clean", str(path)) for path in operation_runtime_paths(manifest, manifest_path)]
    details.extend(("clean", str(path)) for _, path in operation_configured_clean_paths(manifest, manifest_path, event))
    return details


def clean_operation_before_run(manifest: dict[str, Any], manifest_path: Path) -> None:
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    if operation_state_is_active(state) or related_detached_records(manifest_path, include_inactive=False):
        raise OcmoError("cannot clean before run while operation appears active; pause, kill, or erase it first")
    remove_detached_records(manifest_path)
    erase_operation_runtime_data(manifest, manifest_path, event="beforeRun", include_configured=True)


def operation_failed_status(state: dict[str, Any]) -> str:
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    statuses = [work_unit.get("status") for work_unit in work_units.values() if isinstance(work_unit, dict)]
    if statuses and all(status in {*BLOCKED_STATUSES, "completed"} for status in statuses) and any(status in BLOCKED_STATUSES for status in statuses):
        return "blocked"
    return "failed"


def start_detached_run(options: RunOptions, manifest: dict[str, Any], concurrency: int, timeout_seconds: int | None) -> int:
    run_id = detached_run_id()
    local_dir = detached_runs_dir(options.manifest_path)
    local_dir.mkdir(parents=True, exist_ok=True)
    log_path = local_dir / f"{run_id}.log"
    command = detached_child_command(options)
    log = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        log.close()
        raise OcmoError(f"could not start detached run: {exc}") from exc
    log.close()
    state = state_path(manifest, options.manifest_path)
    metadata = {
        "schema": "ocmo-detached-run/v1",
        "kind": "operation",
        "runId": run_id,
        "pid": process.pid,
        "startedAt": utc_now(),
        "manifestPath": str(options.manifest_path.resolve()),
        "statePath": str(state),
        "logPath": str(log_path.resolve()),
        "workspace": str(resolve_manifest_path(options.manifest_path, manifest["operation"]["workspace"])),
        "command": command,
        "select": options.select,
        "concurrency": concurrency,
        "timeoutSeconds": timeout_seconds,
        "fresh": options.fresh,
        "params": options.params,
    }
    write_detached_metadata(local_dir / f"{run_id}.json", metadata)
    write_detached_metadata(global_detached_run_path(run_id), metadata)
    print(f"detached: {run_id}")
    print(f"pid: {process.pid}")
    print(f"log: {relative_to_manifest(log_path, options.manifest_path)}")
    print(f"status: ocmo operation status --run-id {run_id}")
    return 0


def detached_child_command(options: RunOptions) -> list[str]:
    subcommand = "resume" if options.resume else "rerun" if options.rerun else "run"
    command = [sys.executable, "-m", "ocmo", "operation", subcommand, str(options.manifest_path.resolve())]
    if options.select:
        command += ["--select", options.select]
    if options.concurrency is not None:
        command += ["--concurrency", str(options.concurrency)]
    if options.timeout_seconds is not None:
        command += ["--timeout-seconds", str(options.timeout_seconds)]
    command += parameter_arguments(options.params)
    command += ["--ui", "plain", "--yes"]
    if options.allow_shared_worktree_concurrency and not options.resume:
        command.append("--allow-shared-worktree-concurrency")
    if options.fresh and not options.resume and not options.rerun:
        command.append("--fresh")
    return command


def detached_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"ocmo-{stamp}-{uuid.uuid4().hex[:6]}"


def detached_runs_dir(manifest_path: Path) -> Path:
    return manifest_path.parent / ".ocmo" / "runs"


def local_detached_record_path(manifest_path: Path, run_id: str) -> Path:
    return detached_runs_dir(manifest_path) / f"{run_id}.json"


def global_detached_runs_dir() -> Path:
    configured = os.environ.get("OCMO_RUN_REGISTRY")
    if configured:
        return Path(configured)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "ocmo" / "runs"
    return Path.home() / ".local" / "state" / "ocmo" / "runs"


def global_detached_run_path(run_id: str) -> Path:
    return global_detached_runs_dir() / f"{run_id}.json"


def write_detached_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    if os.name == "nt":
        return windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False
    return True


def windows_process_is_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any(f'"{pid}"' in line for line in result.stdout.splitlines())


def list_runs(args: argparse.Namespace) -> int:
    if args.run_id:
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        print_detached_record(read_json_file(path), details=True)
        return 0
    if args.manifest:
        print_manifest_detached_runs(infer_manifest_path(args.manifest), include_inactive=args.all, params=command_params(args))
        return 0
    records = detached_records(include_inactive=args.all, kind="operation")
    operations = discovered_operation_records(include_inactive=args.all, detached=records)
    if not records and not operations:
        qualifier = "" if args.all else " active"
        print(f"No{qualifier} ocmo operations.")
        return 0
    for record in records:
        print_detached_record(record, details=False)
    for operation in operations:
        print_operation_record(operation)
    return 0


def status_operation(args: argparse.Namespace) -> int:
    if args.active_or_latest:
        return print_active_or_latest_operation_statuses(include_inactive=args.all)
    record = None
    if args.run_id:
        path = find_detached_record(args.run_id)
        if path is None:
            raise OcmoError(f"detached run not found: {args.run_id}")
        record = read_json_file(path)
        manifest_value = record.get("manifestPath")
        if not isinstance(manifest_value, str):
            print_detached_record(record, details=True)
            return 0
        manifest_path = Path(manifest_value)
    else:
        manifest_path = infer_manifest_path(args.manifest)
    interval = getattr(args, "interval", 1.0)
    if interval <= 0:
        raise OcmoError("--interval must be greater than zero")
    params = command_params(args, record)
    if args.once:
        print_operation_status(manifest_path, include_inactive=args.all, selected_record=record, params=params)
        return 0
    return watch_operation_status(manifest_path, include_inactive=args.all, selected_record=record, interval=interval, params=params)


def print_active_or_latest_operation_statuses(include_inactive: bool = False) -> int:
    active_records = detached_records(include_inactive=False, kind="operation")
    active = operation_status_targets(active_records, discovered_operation_records(include_inactive=False, detached=active_records))
    targets = active
    if not targets:
        all_records = detached_records(include_inactive=True, kind="operation")
        all_targets = operation_status_targets(all_records, discovered_operation_records(include_inactive=True, detached=all_records))
        latest = latest_operation_status_target(all_targets)
        if latest is not None:
            targets = [latest]
    if not targets:
        print("No ocmo operations found.")
        return 0
    heading = "Active OCMO operation statuses" if active else "Latest OCMO operation status"
    print(heading)
    for index, target in enumerate(targets):
        if index:
            print()
        print_operation_status(target["manifestPath"], include_inactive=include_inactive, selected_record=target.get("record"), params=target.get("params"))
    return 0


def pause_operation(args: argparse.Namespace) -> int:
    manifest_path, record = control_manifest_and_record(args)
    manifest = load_manifest(manifest_path, command_params(args, record))
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    changed = mark_active_runs(path, "paused")
    stop_operation_processes(manifest_path, state, record)
    print(f"paused: {manifest['operation']['id']} ({changed} run(s))")
    return 0


def kill_operation(args: argparse.Namespace) -> int:
    manifest_path, record = control_manifest_and_record(args)
    manifest = load_manifest(manifest_path, command_params(args, record))
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    if not args.force and sys.stdin.isatty():
        answer = input(f"Kill operation {manifest['operation']['id']}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    changed = mark_active_runs(path, "killed")
    stop_operation_processes(manifest_path, state, record)
    print(f"killed: {manifest['operation']['id']} ({changed} run(s))")
    return 0


def erase_operation(args: argparse.Namespace) -> int:
    if args.run_id and args.manifest:
        raise OcmoError("pass either a manifest or --run-id, not both")
    manifest_path, record = control_manifest_and_record(args)
    if not is_generated_operation_manifest(manifest_path):
        raise OcmoError("erase only removes generated .ocmo/<operation>/manifest.yaml operation directories")
    manifest = load_manifest(manifest_path, command_params(args, record))
    if not args.force and sys.stdin.isatty():
        answer = input(f"Erase runtime data for generated operation {manifest_path.parent}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    if not args.force and not sys.stdin.isatty():
        raise OcmoError("erase requires --force when not interactive")
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    stop_operation_processes(manifest_path, state, record)
    remove_detached_records(manifest_path)
    erase_operation_runtime_data(manifest, manifest_path, event="erase", include_configured=True)
    print(f"erased runtime data: {manifest_path.parent}")
    return 0


def erase_operation_runtime_data(manifest: dict[str, Any], manifest_path: Path, event: str = "erase", include_configured: bool = False) -> None:
    operation_dir = manifest_path.parent.resolve()
    for path in operation_runtime_paths(manifest, manifest_path):
        erase_operation_runtime_path(path, operation_dir)
    if include_configured:
        for root, path in operation_configured_clean_paths(manifest, manifest_path, event):
            erase_configured_clean_path(path, root)


def operation_runtime_paths(manifest: dict[str, Any], manifest_path: Path) -> list[Path]:
    return [
        state_path(manifest, manifest_path),
        manifest_path.parent / "outputs",
        manifest_path.parent / ARTIFACT_ROOT,
        manifest_path.parent / PROMPT_INPUT_ROOT,
        detached_runs_dir(manifest_path),
    ]


def operation_configured_clean_paths(manifest: dict[str, Any], manifest_path: Path, event: str) -> list[tuple[Path, Path]]:
    clean = manifest.get("clean") if isinstance(manifest.get("clean"), dict) else {}
    paths = clean.get("paths") if isinstance(clean.get("paths"), list) else []
    targets: list[tuple[Path, Path]] = []
    for item in paths:
        if isinstance(item, str):
            root_name = "workspace"
            value = item
            events = {"beforeRun", "erase"}
        elif isinstance(item, dict):
            root_name = item.get("root", "workspace")
            value = item.get("path")
            when = item.get("when")
            events = {"beforeRun", "erase"} if when is None else {when} if isinstance(when, str) else set(when)
        else:  # pragma: no cover - validation rejects this.
            continue
        if event not in events:
            continue
        root = resolve_manifest_path(manifest_path, manifest["operation"]["workspace"]) if root_name == "workspace" else manifest_path.parent.resolve()
        path = (root / str(value)).resolve()
        targets.append((root.resolve(), path))
    return targets


def erase_operation_runtime_path(path: Path, operation_dir: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError:
        return
    if not resolved.is_relative_to(operation_dir) or resolved == operation_dir:
        return
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink(missing_ok=True)
    except OSError:
        pass


def erase_configured_clean_path(path: Path, root: Path) -> None:
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
    except OSError as exc:
        raise OcmoError(f"could not resolve clean path: {path}: {exc}") from exc
    if not resolved.is_relative_to(resolved_root) or resolved == resolved_root:
        raise OcmoError(f"clean path must stay under its configured root: {path}")
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink(missing_ok=True)
    except OSError as exc:
        raise OcmoError(f"could not remove clean path {path}: {exc}") from exc


def resume_operation(args: argparse.Namespace) -> int:
    manifest_path = infer_manifest_path(args.manifest)
    return run_manifest(RunOptions(manifest_path, None, None, None, False, args.yes, args.ui, False, False, args.detach, True, False, command_params(args)))


def rerun_operation(args: argparse.Namespace) -> int:
    manifest_path = infer_manifest_path(args.manifest)
    return run_manifest(
        RunOptions(
            manifest_path,
            args.select,
            args.concurrency,
            args.timeout_seconds,
            False,
            args.yes,
            args.ui,
            args.allow_shared_worktree_concurrency,
            False,
            args.detach,
            False,
            True,
            command_params(args),
        )
    )
