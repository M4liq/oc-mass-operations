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

def run_item(
    manifest: dict[str, Any],
    manifest_path: Path,
    item: dict[str, Any],
    state: "StateStore",
    timeout_seconds: int | None,
    auto_worktrees: dict[str, Any],
    reporter: PlainRunReporter | None = None,
    resume: bool = False,
    rerun: bool = False,
) -> int:
    reporter = reporter or PlainRunReporter()
    item_id = str(item["id"])
    execution: dict[str, Any] = {}
    try:
        execution = worktree_execution(manifest, manifest_path, item) if auto_worktrees["enabled"] else {}
        run_dir = Path(execution["worktreePath"]) if execution else resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])
        if execution:
            worktree_code = prepare_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, reporter)
            if worktree_code != 0:
                return worktree_code
        runs = work_unit_runs(manifest, item)
    except (OSError, OcmoError) as exc:
        state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        reporter.item(item_id, "failed", f"failed before start: {exc}")
        return 1
    item_state = state.item(item_id)
    if rerun:
        state.clear_item_terminal_fields(item_id)
    item_running_patch = {"startedAt": utc_now() if rerun else item_state.get("startedAt") or utc_now(), "runCount": len(runs), **execution}
    if resume:
        item_running_patch["resumedAt"] = utc_now()
    elif not rerun and item_state.get("resumedAt"):
        item_running_patch["resumedAt"] = item_state.get("resumedAt")
    state.mark(item_id, "running", item_running_patch)
    reporter.item(item_id, "running", "running")
    resumed_paused_run = False
    for run in runs:
        runner = effective_runner(manifest, run)
        run_id = str(run["id"])
        previous_run_state = state.run(item_id, run_id)
        use_session_resume = False
        if resume:
            previous_status = previous_run_state.get("status")
            if previous_status == "completed":
                continue
            if previous_status == "paused_unresumable":
                state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": f"paused run is not resumable: {run_id}", **execution})
                reporter.run(item_id, run_id, "failed", "paused run is not resumable")
                return 1
            if previous_status == "paused":
                use_session_resume = True
                resumed_paused_run = True
            elif not resumed_paused_run:
                continue
        run_timeout = timeout_seconds if timeout_seconds is not None else runner.get("timeoutSeconds")
        try:
            prompt_file: Path | None = None
            if use_session_resume:
                session_id = previous_run_state.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise OcmoError(f"paused run is missing opencode sessionId: {item_id}/{run_id}")
                prompt_text = resume_prompt(item_id, run_id)
                command = build_resume_command(manifest, manifest_path, prompt_text, session_id, run_dir, runner)
            else:
                prompt_text = render_prompt(manifest, item, manifest_path, execution, run, runs)
                command, prompt_file = build_transport_command(manifest, manifest_path, prompt_text, prompt_input_path(manifest_path, item_id, run_id), run_dir, runner)
        except (OSError, OcmoError) as exc:
            state.mark_run(item_id, run_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc)})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
            reporter.run(item_id, run_id, "failed", f"failed before start: {exc}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
            return cleanup_code or 1
        output_path = run_output_path(manifest_path, item_id, run_id)
        running_run_state = {
            "startedAt": utc_now(),
            "command": command_without_prompt(command),
            "timeoutSeconds": run_timeout,
            "outputPath": relative_to_manifest(output_path, manifest_path),
            "artifacts": {
                artifact_id: produced_artifact_relative_path(manifest_path, item, run_id, artifact_id, config)
                for artifact_id, config in produced_artifacts(run).items()
            },
        }
        if prompt_file is not None:
            running_run_state["promptPath"] = relative_to_manifest(prompt_file, manifest_path)
        if resume:
            running_run_state["sessionId"] = previous_run_state.get("sessionId")
            running_run_state["resumedAt"] = utc_now()
        elif not rerun:
            running_run_state["sessionId"] = previous_run_state.get("sessionId")
            running_run_state["resumedAt"] = previous_run_state.get("resumedAt")
        if rerun:
            state.replace_run(item_id, run_id, "running", running_run_state)
        else:
            state.mark_run(item_id, run_id, "running", running_run_state)
        reporter.run(item_id, run_id, "running", "starting")
        usage_total = empty_usage()

        def record_usage(delta: dict[str, Any], item_id: str = item_id, run_id: str = run_id) -> None:
            nonlocal usage_total
            usage_total = add_usage(usage_total, delta)
            state.patch_run(item_id, run_id, {"usage": usage_total})
            reporter.usage(item_id, run_id, usage_total)

        try:
            completed = run_opencode_command(
                command,
                run_dir,
                run_timeout,
                output_path,
                on_start=lambda pid, item_id=item_id, run_id=run_id: state.mark_run(item_id, run_id, "running", {"pid": pid}),
                on_session=lambda session_id, item_id=item_id, run_id=run_id: state.patch_run(item_id, run_id, {"sessionId": session_id}),
                on_usage=record_usage,
            )
        except subprocess.TimeoutExpired:
            state.mark_run(item_id, run_id, "timed_out", {"completedAt": utc_now(), "exitCode": None, "timeoutSeconds": run_timeout})
            state.mark(item_id, "timed_out", {"completedAt": utc_now(), "exitCode": None, "timeoutSeconds": run_timeout, **execution})
            reporter.run(item_id, run_id, "timed_out", f"timed out after {run_timeout} seconds")
            cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
            return 124
        except OSError as exc:
            state.mark_run(item_id, run_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc)})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
            reporter.run(item_id, run_id, "failed", f"failed to start: {exc}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
            return cleanup_code or 1
        reporter.subprocess_output(item_id, run_id, completed)
        session_id = extract_session_id(completed.stdout or "")
        if session_id:
            state.patch_run(item_id, run_id, {"sessionId": session_id})
        if completed.returncode == 0:
            try:
                artifacts = verify_required_artifacts(manifest_path, item, run)
            except OcmoBlocked as exc:
                state.mark_run(item_id, run_id, "blocked", {"completedAt": utc_now(), "exitCode": 0, "error": str(exc)})
                state.mark(item_id, "blocked", {"completedAt": utc_now(), "exitCode": 0, "error": str(exc), **execution})
                reporter.run(item_id, run_id, "blocked", str(exc))
                cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
                return cleanup_code or 1
            except OcmoError as exc:
                state.mark_run(item_id, run_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc)})
                state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
                reporter.run(item_id, run_id, "failed", str(exc))
                cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
                return cleanup_code or 1
            completion_patch: dict[str, Any] = {"completedAt": utc_now(), "exitCode": 0, "artifacts": artifacts}
            if usage_total.get("steps"):
                completion_patch["usage"] = usage_total
            state.mark_run(item_id, run_id, "completed", completion_patch)
            usage_detail = f"completed {format_usage_summary(usage_total)}" if usage_total.get("steps") else "completed"
            reporter.run(item_id, run_id, "running", usage_detail)
        else:
            latest_status = state.run(item_id, run_id).get("status")
            if latest_status in PAUSED_STATUSES or latest_status == "killed":
                reporter.run(item_id, run_id, str(latest_status), str(latest_status).replace("_", " "))
                return 1
            state.mark_run(item_id, run_id, "failed", {"completedAt": utc_now(), "exitCode": completed.returncode})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": completed.returncode, **execution})
            reporter.run(item_id, run_id, "failed", f"failed: exit {completed.returncode}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False, reporter=reporter)
            return cleanup_code or completed.returncode
    state.mark(item_id, "completed", {"completedAt": utc_now(), "exitCode": 0, **execution})
    reporter.item(item_id, "completed", "completed")
    cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=True, reporter=reporter)
    if cleanup_code != 0:
        state.mark(item_id, "cleanup_failed", {"completedAt": utc_now(), "exitCode": cleanup_code, **execution})
        reporter.item(item_id, "cleanup_failed", f"cleanup failed: exit {cleanup_code}")
        return cleanup_code
    return 0


def worktree_execution(manifest: dict[str, Any], manifest_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    config = auto_worktrees_config(manifest)
    source_workspace = resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])
    root_value = config.get("root", ".ocmo/worktrees")
    root = resolve_worktree_root(source_workspace, root_value)
    operation_id = str(manifest["operation"]["id"])
    item_id = str(item["id"])
    item_slug = slugify(item_id)
    branch_pattern = config.get("branchPattern", "ocmo/{operation_id}/{work_unit_id}")
    try:
        branch_name = branch_pattern.format(operation_id=slugify(operation_id), work_unit_id=item_slug, work_unit_slug=item_slug)
    except (KeyError, ValueError, IndexError) as exc:
        raise OcmoError(f"invalid queue.autoWorktrees.branchPattern: {exc}") from exc
    worktree_path = root / slugify(operation_id) / item_slug
    base_branch = config.get("baseBranch") or manifest.get("policy", {}).get("baseBranch") or current_branch(source_workspace)
    return {
        "sourceWorkspace": str(source_workspace),
        "worktreePath": str(worktree_path),
        "branchName": branch_name,
        "baseBranch": base_branch,
    }


def resolve_worktree_root(source_workspace: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (source_workspace / path).resolve()


def prepare_worktree(
    manifest: dict[str, Any],
    manifest_path: Path,
    item: dict[str, Any],
    execution: dict[str, Any],
    config: dict[str, Any],
    state: "StateStore",
    reporter: PlainRunReporter | None = None,
) -> int:
    reporter = reporter or PlainRunReporter()
    item_id = str(item["id"])
    source_workspace = Path(execution["sourceWorkspace"])
    worktree_path = Path(execution["worktreePath"])
    branch_name = execution["branchName"]
    base_branch = execution["baseBranch"]
    state.mark(item_id, "creating_worktree", {"worktreeStartedAt": utc_now(), **execution})
    reporter.item(item_id, "creating_worktree", f"creating worktree {worktree_path}")
    if worktree_path.exists():
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": f"worktree path already exists: {worktree_path}", **execution})
        reporter.item(item_id, "worktree_failed", f"worktree path already exists: {worktree_path}")
        return 1
    try:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        reporter.item(item_id, "worktree_failed", f"could not create worktree parent: {exc}")
        return 1
    command = ["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_branch]
    try:
        completed = subprocess.run(command, cwd=str(source_workspace))
    except OSError as exc:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        reporter.item(item_id, "worktree_failed", f"worktree creation failed: {exc}")
        return 1
    if completed.returncode != 0:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": completed.returncode, **execution})
        reporter.item(item_id, "worktree_failed", f"worktree creation failed: exit {completed.returncode}")
        return completed.returncode
    state.mark(item_id, "worktree_ready", {"worktreeCompletedAt": utc_now(), **execution})
    setup_code = run_scripts("setup", normalize_scripts(config.get("setup"), "queue.autoWorktrees.setup"), worktree_path, execution, item_id, reporter)
    if setup_code != 0:
        state.mark(item_id, "setup_failed", {"completedAt": utc_now(), "exitCode": setup_code, **execution})
        cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, config, state, success=False, reporter=reporter)
        if cleanup_code != 0:
            return cleanup_code
        return setup_code
    if config.get("setup"):
        state.mark(item_id, "setup_completed", {"setupCompletedAt": utc_now(), **execution})
        reporter.item(item_id, "setup_completed", "setup completed")
    return 0


def cleanup_worktree(
    manifest: dict[str, Any],
    manifest_path: Path,
    item: dict[str, Any],
    execution: dict[str, Any],
    config: dict[str, Any],
    state: "StateStore",
    success: bool,
    reporter: PlainRunReporter | None = None,
) -> int:
    reporter = reporter or PlainRunReporter()
    if not execution:
        return 0
    cleanup = config.get("cleanup", "never")
    should_cleanup = cleanup == "always" or (cleanup == "onSuccess" and success)
    if not should_cleanup:
        return 0
    item_id = str(item["id"])
    worktree_path = Path(execution["worktreePath"])
    reporter.item(item_id, "cleanup", "cleanup")
    teardown_code = run_scripts("teardown", normalize_scripts(config.get("teardown"), "queue.autoWorktrees.teardown"), worktree_path, execution, item_id, reporter)
    if teardown_code != 0:
        state.patch(item_id, {"teardownStatus": "failed", "teardownCompletedAt": utc_now(), "teardownExitCode": teardown_code, **execution})
        return teardown_code
    source_workspace = resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])
    try:
        completed = subprocess.run(["git", "worktree", "remove", str(worktree_path)], cwd=str(source_workspace))
    except OSError as exc:
        state.patch(item_id, {"worktreeStatus": "remove_failed", "worktreeRemoveExitCode": 1, "worktreeRemoveError": str(exc), **execution})
        reporter.item(item_id, "cleanup_failed", f"worktree remove failed: {exc}")
        return 1
    if completed.returncode == 0:
        state.patch(item_id, {"worktreeStatus": "removed", "worktreeRemovedAt": utc_now(), **execution})
        reporter.item(item_id, "cleanup", "worktree removed")
        return 0
    else:
        state.patch(item_id, {"worktreeStatus": "remove_failed", "worktreeRemoveExitCode": completed.returncode, **execution})
        reporter.item(item_id, "cleanup_failed", f"worktree remove failed: exit {completed.returncode}")
        return completed.returncode


def run_scripts(kind: str, scripts: list[str], cwd: Path, execution: dict[str, Any], item_id: str, reporter: PlainRunReporter | None = None) -> int:
    reporter = reporter or PlainRunReporter()
    for script in scripts:
        reporter.item(item_id, kind, f"{kind}: {script}")
        try:
            completed = subprocess.run(script, cwd=str(cwd), shell=True, env=worktree_env(execution))
        except OSError as exc:
            reporter.item(item_id, f"{kind}_failed", f"{kind} failed: {exc}")
            return 1
        if completed.returncode != 0:
            reporter.item(item_id, f"{kind}_failed", f"{kind} failed: exit {completed.returncode}")
            return completed.returncode
    return 0


def worktree_env(execution: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["OCMO_SOURCE_WORKSPACE"] = execution["sourceWorkspace"]
    env["OCMO_WORKTREE_PATH"] = execution["worktreePath"]
    env["OCMO_BRANCH_NAME"] = execution["branchName"]
    env["PASEO_SOURCE_CHECKOUT_PATH"] = execution["sourceWorkspace"]
    env["PASEO_WORKTREE_PATH"] = execution["worktreePath"]
    env["PASEO_BRANCH_NAME"] = execution["branchName"]
    return env


def current_branch(path: Path) -> str:
    try:
        completed = subprocess.run(["git", "branch", "--show-current"], cwd=str(path), capture_output=True, text=True)
    except OSError as exc:
        raise OcmoError(f"could not detect current git branch: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise OcmoError("queue.autoWorktrees.baseBranch is required when current git branch cannot be detected")
    return completed.stdout.strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return slug.strip("-._") or "item"
