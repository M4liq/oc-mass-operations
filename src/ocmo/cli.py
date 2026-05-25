from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import yaml


DONE_STATUSES = {"completed", "done", "skipped"}
BLOCKED_STATUSES = {"blocked"}
MANIFEST_START = "OCMO_MANIFEST_START"
MANIFEST_END = "OCMO_MANIFEST_END"
FILE_START = "OCMO_FILE_START"
FILE_END = "OCMO_FILE_END"
TERMINAL_STATUSES = {"completed", "failed", "blocked", "timed_out", "cleanup_failed", "worktree_failed", "setup_failed", "paused", "paused_unresumable", "killed"}
PAUSED_STATUSES = {"paused", "paused_unresumable"}
FAILED_STATUSES = {"failed", "cleanup_failed", "worktree_failed", "setup_failed"}
RERUN_RETRYABLE_STATUSES = {*FAILED_STATUSES, *BLOCKED_STATUSES, "paused_unresumable", "timed_out", "killed"}
SHARED_WORKTREE_CONCURRENCY_WARNING = "warning: policy.worktree=single with queue.concurrency > 1 requires non-overlapping work unit scopes; ocmo run requires --allow-shared-worktree-concurrency"
MISSING_PLACEHOLDER = object()
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PLAN_AGENT = "build"
RUN_AGENT = "build"
ARTIFACT_ROOT = "artifacts"
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
OCMO_SKILL_NAME = "ocmo"
OLD_OCMO_SKILL_NAMES = ("ocmo-plan-grill",)
OCMO_SKILL_RESOURCE = "resources/skill"


class OcmoError(Exception):
    pass


class OcmoBlocked(OcmoError):
    pass


@dataclass(frozen=True)
class RunOptions:
    manifest_path: Path
    select: str | None
    concurrency: int | None
    timeout_seconds: int | None
    dry_run: bool
    yes: bool
    ui: str = "auto"
    allow_shared_worktree_concurrency: bool = False
    preview_all: bool = False
    detach: bool = False
    resume: bool = False
    rerun: bool = False


@dataclass(frozen=True)
class WorkflowOptions:
    workflow_path: Path
    select: str | None
    dry_run: bool
    yes: bool
    ui: str = "auto"
    detach: bool = False
    resume: bool = False
    rerun: bool = False


@dataclass(frozen=True)
class PromptPreview:
    work_unit_id: str
    run_id: str
    text: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocmo", description="OC Mass Operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    operation_parser = subparsers.add_parser("operation", help="Plan, inspect, run, and control one operation")
    operation_subparsers = operation_parser.add_subparsers(dest="operation_command", required=True)

    run_parser = operation_subparsers.add_parser("run", help="Run work units from a manifest")
    run_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory; defaults to manifest.yaml or a single .ocmo/*/manifest.yaml")
    run_parser.add_argument("--select", help="Selection: all, pending, uncompleted, IDs, or ranges")
    run_parser.add_argument("--concurrency", type=int, help="Override queue.concurrency")
    run_parser.add_argument("--timeout-seconds", type=int, help="Override runner.timeoutSeconds for each work unit")
    run_parser.add_argument("--dry-run", action="store_true", help="Print commands/prompts without running opencode")
    run_parser.add_argument("--all", action="store_true", help="With --dry-run, print every rendered prompt instead of a compact preview")
    run_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for non-dry runs")
    run_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for non-dry runs")
    run_parser.add_argument("--detach", action="store_true", help="Start ocmo run in the background and return immediately")
    run_parser.add_argument(
        "--allow-shared-worktree-concurrency",
        action="store_true",
        help="Allow concurrency > 1 with policy.worktree=single",
    )

    validate_parser = operation_subparsers.add_parser("validate", help="Validate an operation manifest")
    validate_parser.add_argument("manifest", type=Path)

    render_parser = operation_subparsers.add_parser("render", help="Render prompts for selected work units")
    render_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory; defaults to manifest.yaml or a single .ocmo/*/manifest.yaml")
    render_parser.add_argument("--select", help="Selection: all, pending, uncompleted, IDs, or ranges")
    render_parser.add_argument("--all", action="store_true", help="Print every rendered prompt instead of a compact preview")

    plan_parser = operation_subparsers.add_parser("plan", help="Ask opencode to convert a prompt into an ocmo manifest")
    plan_parser.add_argument("--from", dest="from_file", required=True, type=Path, help="Natural-language operation prompt")
    plan_parser.add_argument("--read", dest="read_files", action="append", default=[], type=Path, help="Read-only source file to attach/inspect")
    plan_parser.add_argument("--out", type=Path, help="Manifest output path; defaults to <workspace>/.ocmo/<prompt-stem>/manifest.yaml")
    plan_parser.add_argument("--workspace", type=Path, help="Target workspace for planning; defaults to the current directory")
    plan_parser.add_argument("--model", help="opencode model")
    plan_parser.add_argument("--max-attempts", type=int, default=3, help="Maximum planner correction attempts")
    plan_parser.add_argument("--interactive", action="store_true", help="Allow the planner to ask terminal questions before returning marked YAML")
    plan_parser.add_argument("--dry-run", action="store_true", help="Print the planning prompt only")

    status_parser = operation_subparsers.add_parser("status", help="Show work unit and run status")
    status_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    status_parser.add_argument("--run-id", help="Show one detached run session")
    status_parser.add_argument("--all", action="store_true", help="Include inactive detached run sessions")
    status_parser.add_argument("--once", action="store_true", help="Print one status snapshot and exit")
    status_parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds for continuous status")

    list_parser = operation_subparsers.add_parser("list", help="List detached operation run sessions")
    list_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    list_parser.add_argument("--run-id", help="Show one detached run session")
    list_parser.add_argument("--all", action="store_true", help="Include inactive detached run sessions")

    pause_parser = operation_subparsers.add_parser("pause", help="Stop active processes and mark the operation paused")
    pause_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    pause_parser.add_argument("--run-id", help="Pause by detached run id")

    resume_parser = operation_subparsers.add_parser("resume", help="Resume paused operation runs by opencode session id")
    resume_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    resume_parser.add_argument("--detach", action="store_true", help="Resume in the background and return immediately")
    resume_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground resume")
    resume_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground resume")

    rerun_parser = operation_subparsers.add_parser("rerun", help="Fresh-start failed, timed-out, killed, or unresumable work units")
    rerun_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    rerun_parser.add_argument("--select", default="retryable", help="Rerun selector: retryable, unresumable, timed-out, failed, killed, all, IDs, or ranges")
    rerun_parser.add_argument("--concurrency", type=int, help="Override queue.concurrency")
    rerun_parser.add_argument("--timeout-seconds", type=int, help="Override runner.timeoutSeconds for each work unit")
    rerun_parser.add_argument("--detach", action="store_true", help="Rerun in the background and return immediately")
    rerun_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground rerun")
    rerun_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground rerun")
    rerun_parser.add_argument(
        "--allow-shared-worktree-concurrency",
        action="store_true",
        help="Allow concurrency > 1 with policy.worktree=single",
    )

    kill_parser = operation_subparsers.add_parser("kill", help="Terminate active processes and mark the operation killed")
    kill_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    kill_parser.add_argument("--run-id", help="Kill by detached run id")
    kill_parser.add_argument("--force", action="store_true", help="Skip confirmation")

    erase_parser = operation_subparsers.add_parser("erase", help="Terminate and erase generated operation data")
    erase_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    erase_parser.add_argument("--run-id", help="Erase by detached run id")
    erase_parser.add_argument("--force", action="store_true", help="Required for non-interactive erase")
    erase_parser.add_argument("--delete-definition", action="store_true", help="Delete manifest and prompt templates with runtime data")
    erase_parser.add_argument("--keep-definition", action="store_true", help="Delete only known runtime data, preserving manifest and prompts")

    workflow_parser = subparsers.add_parser("workflow", help="Run operation manifests sequentially")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command", required=True)

    workflow_validate_parser = workflow_subparsers.add_parser("validate", help="Validate a workflow")
    workflow_validate_parser.add_argument("workflow", type=Path, help="Workflow path or directory")

    workflow_run_parser = workflow_subparsers.add_parser("run", help="Run workflow steps sequentially")
    workflow_run_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory; defaults to workflow.yaml")
    workflow_run_parser.add_argument("--select", help="Workflow step selection: all, pending, uncompleted, IDs, or ranges")
    workflow_run_parser.add_argument("--dry-run", action="store_true", help="Preview workflow execution without running operations")
    workflow_run_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground runs")
    workflow_run_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground operation steps")
    workflow_run_parser.add_argument("--detach", action="store_true", help="Start workflow in the background and return immediately")

    workflow_status_parser = workflow_subparsers.add_parser("status", help="Show workflow status")
    workflow_status_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_status_parser.add_argument("--run-id", help="Show one detached workflow run session")
    workflow_status_parser.add_argument("--all", action="store_true", help="Include inactive detached workflow sessions")

    workflow_list_parser = workflow_subparsers.add_parser("list", help="List detached workflow run sessions")
    workflow_list_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_list_parser.add_argument("--run-id", help="Show one detached workflow run session")
    workflow_list_parser.add_argument("--all", action="store_true", help="Include inactive workflow sessions")

    workflow_pause_parser = workflow_subparsers.add_parser("pause", help="Pause the active workflow step")
    workflow_pause_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_pause_parser.add_argument("--run-id", help="Pause by detached run id")

    workflow_resume_parser = workflow_subparsers.add_parser("resume", help="Resume a paused workflow")
    workflow_resume_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_resume_parser.add_argument("--select", help="Workflow step selection")
    workflow_resume_parser.add_argument("--detach", action="store_true", help="Resume in the background and return immediately")
    workflow_resume_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground resume")
    workflow_resume_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground operation steps")

    workflow_rerun_parser = workflow_subparsers.add_parser("rerun", help="Fresh-start retryable workflow steps")
    workflow_rerun_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_rerun_parser.add_argument("--select", default="retryable", help="Workflow step selection: retryable, failed, killed, all, IDs, or ranges")
    workflow_rerun_parser.add_argument("--detach", action="store_true", help="Rerun in the background and return immediately")
    workflow_rerun_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground rerun")
    workflow_rerun_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground operation steps")

    workflow_kill_parser = workflow_subparsers.add_parser("kill", help="Terminate active workflow processes")
    workflow_kill_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_kill_parser.add_argument("--run-id", help="Kill by detached run id")
    workflow_kill_parser.add_argument("--force", action="store_true", help="Skip confirmation")

    skill_parser = subparsers.add_parser("skill", help="Manage the bundled OCMO opencode skill")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)
    skill_install_parser = skill_subparsers.add_parser("install", help="Install the bundled opencode planning skill")
    skill_install_parser.add_argument("--force", action="store_true", help="Accepted for compatibility; install updates the bundled skill by default")
    skill_subparsers.add_parser("path", help="Print the target opencode skill path")

    args = parser.parse_args(argv)
    try:
        if args.command == "operation" and args.operation_command == "run":
            manifest_path = infer_manifest_path(args.manifest)
            return run_manifest(
                RunOptions(
                    manifest_path,
                    args.select,
                    args.concurrency,
                    args.timeout_seconds,
                    args.dry_run,
                    args.yes,
                    args.ui,
                    args.allow_shared_worktree_concurrency,
                    args.all,
                    args.detach,
                )
            )
        if args.command == "operation" and args.operation_command == "validate":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest, args.manifest)
            warn_shared_worktree_concurrency(manifest)
            print(f"valid: {args.manifest}")
            return 0
        if args.command == "operation" and args.operation_command == "render":
            manifest_path = infer_manifest_path(args.manifest)
            manifest = load_manifest(manifest_path)
            validate_manifest(manifest, manifest_path)
            warn_shared_worktree_concurrency(manifest)
            path = state_path(manifest, manifest_path)
            existing_state = read_json_file(path) if path.exists() else {}
            previews = []
            for item in select_work_units(manifest, args.select, existing_state):
                runs = work_unit_runs(manifest, item)
                for run in runs:
                    previews.append(PromptPreview(str(item["id"]), str(run["id"]), render_prompt(manifest, item, manifest_path, run=run, runs=runs)))
            print_prompt_previews(previews, args.all)
            return 0
        if args.command == "operation" and args.operation_command == "plan":
            return plan_manifest(args)
        if args.command == "operation" and args.operation_command == "status":
            return status_operation(args)
        if args.command == "operation" and args.operation_command == "list":
            return list_runs(args)
        if args.command == "operation" and args.operation_command == "pause":
            return pause_operation(args)
        if args.command == "operation" and args.operation_command == "resume":
            return resume_operation(args)
        if args.command == "operation" and args.operation_command == "rerun":
            return rerun_operation(args)
        if args.command == "operation" and args.operation_command == "kill":
            return kill_operation(args)
        if args.command == "operation" and args.operation_command == "erase":
            return erase_operation(args)
        if args.command == "workflow":
            return workflow_command(args)
        if args.command == "skill":
            return skill_command(args)
    except OcmoError as exc:
        print(f"ocmo: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ocmo: interrupted", file=sys.stderr)
        return 130
    return 1  # pragma: no cover


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def skill_command(args: argparse.Namespace) -> int:
    if args.skill_command == "path":
        print(opencode_skill_path())
        return 0
    if args.skill_command == "install":
        install_skill(force=args.force)
        return 0
    return 1  # pragma: no cover


def install_skill(force: bool = False) -> Path:
    source_dir = bundled_skill_dir()
    destination = opencode_skill_path()
    destination_dir = destination.parent
    source_files = bundled_skill_files(source_dir)
    if destination.exists():
        if installed_skill_matches(destination_dir, source_files):
            print(f"already installed: {destination}")
            remove_old_skill_installs(destination_dir.parent)
            return destination
        action = "updated"
    else:
        action = "installed"
    destination_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, source_path in source_files.items():
        target = destination_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source_path.read_bytes())
    print(f"{action}: {destination}")
    remove_old_skill_installs(destination_dir.parent)
    print("restart opencode to load the skill")
    return destination


def bundled_skill_files(source_dir: Any) -> dict[Path, Any]:
    files: dict[Path, Any] = {}

    def collect(path: Any, relative_to_source: Path) -> None:
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            child_relative = relative_to_source / child.name
            if child.is_file():
                files[child_relative] = child
            elif child.is_dir():  # pragma: no branch
                collect(child, child_relative)

    collect(source_dir, Path())
    if Path("SKILL.md") not in files:
        raise OcmoError("bundled skill file not found: SKILL.md")
    return files


def installed_skill_matches(destination_dir: Path, source_files: dict[Path, Any]) -> bool:
    for relative_path, source_path in source_files.items():
        target = destination_dir / relative_path
        if not target.exists() or target.read_bytes() != source_path.read_bytes():
            return False
    return True


def remove_old_skill_installs(skills_dir: Path) -> None:
    for skill_name in OLD_OCMO_SKILL_NAMES:
        old_dir = skills_dir / skill_name
        old_skill = old_dir / "SKILL.md"
        if not old_skill.exists():
            continue
        old_skill.unlink()
        try:
            old_dir.rmdir()
        except OSError:
            pass
        print(f"removed old skill: {old_skill}")


def opencode_skill_path() -> Path:
    root = os.environ.get("OCMO_OPENCODE_SKILLS_DIR")
    skills_dir = Path(root) if root else Path.home() / ".config" / "opencode" / "skills"
    return skills_dir / OCMO_SKILL_NAME / "SKILL.md"


def bundled_skill_path() -> Path:
    return bundled_skill_dir() / "SKILL.md"


def bundled_skill_dir() -> Path:
    configured = os.environ.get("OCMO_SKILL_SOURCE")
    if configured:
        path = Path(configured)
        if path.is_file():
            return path.parent
        if (path / "SKILL.md").exists():
            return path
        raise OcmoError(f"configured skill source not found: {path}")
    resource_root = resources.files(__package__).joinpath(OCMO_SKILL_RESOURCE)
    if resource_root.joinpath("SKILL.md").is_file():
        return resource_root
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "skills" / OCMO_SKILL_NAME
        if (candidate / "SKILL.md").exists():
            return candidate
    raise OcmoError("bundled skill directory not found; reinstall ocmo or install from the cloned repository")


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise OcmoError(f"manifest not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise OcmoError("manifest must be a YAML mapping")
    return data


def load_manifest_text(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise OcmoError("manifest must be a YAML mapping")
    return data


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
    if "mode" in runner:
        raise OcmoError("runner.mode is no longer supported; ocmo always uses opencode run")
    timeout_seconds = runner.get("timeoutSeconds")
    if timeout_seconds is not None and (not isinstance(timeout_seconds, int) or timeout_seconds < 1):
        raise OcmoError("runner.timeoutSeconds must be a positive integer")
    queue = require_mapping(manifest, "queue")
    concurrency = queue.get("concurrency", 1)
    if not isinstance(concurrency, int) or concurrency < 1:
        raise OcmoError("queue.concurrency must be a positive integer")
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


def load_workflow(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise OcmoError(f"workflow not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OcmoError(f"invalid workflow yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise OcmoError("workflow must be a mapping")
    return data


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
        manifest = load_manifest(manifest_path)
        validate_manifest(manifest, manifest_path)


def validate_workflow_options(options: dict[str, Any], field: str) -> None:
    for key in ("operationSelect", "concurrency", "timeoutSeconds", "allowSharedWorktreeConcurrency"):
        if key in options:
            raise OcmoError(f"{field}.{key} is not supported in workflows; configure operation selection and execution policy in the operation manifest")
    value = options.get("stopOnFailure")
    if value is not None and not isinstance(value, bool):
        raise OcmoError(f"{field}.stopOnFailure must be a boolean")


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
        manifest_path = workflow_step_manifest_path(workflow_path, step)
        print(f"- {step['id']}: ocmo operation {action} {quote_arg(str(manifest_path))}")


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


def render_prompt(
    manifest: dict[str, Any],
    item: dict[str, Any],
    manifest_path: Path,
    execution: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    runs: list[dict[str, Any]] | None = None,
) -> str:
    run = run or work_unit_runs(manifest, item)[0]
    runs = runs or [run]
    runner = effective_runner(manifest, run)
    prompt = effective_prompt(manifest, run)
    skills = normalize_skills(prompt.get("skills"), "prompt.skills")
    rendered_skill_instructions = skill_instructions(skills)
    template_path = resolve_manifest_path(manifest_path, prompt["template"])
    template = Template(template_path.read_text(encoding="utf-8"))
    operation = manifest.get("operation", {})
    execution = execution or {}
    context = {
        "operation_json": json.dumps(operation, indent=2, ensure_ascii=False),
        "policy_json": json.dumps(manifest.get("policy", {}), indent=2, ensure_ascii=False),
        "work_unit_json": json.dumps(item, indent=2, ensure_ascii=False),
        "payload_json": json.dumps(item.get("payload", {}), indent=2, ensure_ascii=False),
        "operation_id": str(operation.get("id", "")),
        "workspace": str(operation.get("workspace", "")),
        "work_unit_id": str(item.get("id", "")),
        "work_unit_title": str(item.get("title", "")),
        "work_unit_file": str(item.get("file", "")),
        "worktree_path": str(execution.get("worktreePath", "")),
        "source_workspace": str(execution.get("sourceWorkspace", operation.get("workspace", ""))),
        "branch_name": str(execution.get("branchName", "")),
        "run_json": json.dumps(run, indent=2, ensure_ascii=False),
        "run_id": str(run.get("id", "")),
        "run_agent": str(runner.get("agent", "")),
        "run_model": str(runner.get("model", "")),
        "run_index": str(run.get("index", "")),
        "run_count": str(len(runs)),
        "run_mode": str(run.get("mode", "sequential")),
        "skill_instructions": rendered_skill_instructions,
        "skill_commands": "\n".join(f"/{skill}" for skill in skills),
        "skill_names": ", ".join(skills),
    }
    rendered = render_brace_placeholders(template.safe_substitute(context), context, manifest, item, operation, execution, run)
    sections = []
    chained_inputs = consumed_artifacts(manifest_path, item, run, runs)
    if chained_inputs:
        sections.append(chained_inputs)
    sections.append(rendered)
    required_artifacts = artifact_instructions(manifest_path, item, run)
    if required_artifacts:
        sections.append(required_artifacts)
    rendered = "\n\n".join(sections)
    if rendered_skill_instructions:
        return f"{rendered_skill_instructions}\n\n{rendered}"
    return rendered


def render_brace_placeholders(
    text: str,
    context: dict[str, str],
    manifest: dict[str, Any],
    item: dict[str, Any],
    operation: dict[str, Any],
    execution: dict[str, Any],
    run: dict[str, Any],
) -> str:
    roots = {
        "manifest": manifest,
        "operation": operation,
        "policy": manifest.get("policy", {}),
        "workUnit": item,
        "payload": item.get("payload", {}),
        "execution": execution,
        "run": run,
    }

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in context:
            return context[name]
        value = resolve_brace_placeholder(name, roots)
        if value is MISSING_PLACEHOLDER:
            raise OcmoError(f"unresolved prompt placeholder: {{{{{name}}}}}")
        return format_placeholder_value(value)

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}", replace, text)


def resolve_brace_placeholder(name: str, roots: dict[str, Any]) -> Any:
    parts = name.split(".")
    if parts[0] not in roots:
        return MISSING_PLACEHOLDER
    current = roots[parts[0]]
    for part in parts[1:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING_PLACEHOLDER
    return current


def format_placeholder_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return str(value)


def compact_prompt_previews(previews: list[PromptPreview], show_all: bool) -> list[PromptPreview | int]:
    if show_all or len(previews) <= 3:
        return previews
    omitted = len(previews) - 3
    return [previews[0], previews[1], omitted, previews[-1]]


def print_prompt_previews(previews: list[PromptPreview], show_all: bool) -> None:
    for preview in compact_prompt_previews(previews, show_all):
        if isinstance(preview, int):
            print(f"# ... {preview} prompt(s) omitted ...")
            print("\n" + "=" * 80 + "\n")
            continue
        print(f"# work unit {preview.work_unit_id} / run {preview.run_id}")
        print(preview.text)
        print("\n" + "=" * 80 + "\n")


def build_command(manifest: dict[str, Any], manifest_path: Path, prompt_text: str, run_dir: Path | None = None, runner: dict[str, Any] | None = None) -> list[str]:
    operation = manifest["operation"]
    runner = runner or manifest["runner"]
    command = [runner.get("command", "opencode"), "run"]
    if runner.get("agent"):
        command += ["--agent", str(runner["agent"])]
    if runner.get("model"):
        command += ["--model", str(runner["model"])]
    if runner.get("attach"):
        command += ["--attach", str(runner["attach"])]
    if runner.get("title"):
        command += ["--title", str(runner["title"])]
    if runner.get("dangerouslySkipPermissions"):
        command.append("--dangerously-skip-permissions")
    command += ["--format", "json"]
    command += ["--dir", str(run_dir or resolve_manifest_path(manifest_path, operation["workspace"])), prompt_text]
    return command


def build_resume_command(manifest: dict[str, Any], manifest_path: Path, prompt_text: str, session_id: str, run_dir: Path | None = None, runner: dict[str, Any] | None = None) -> list[str]:
    command = build_command(manifest, manifest_path, prompt_text, run_dir, runner)
    command[2:2] = ["--session", session_id]
    return command


class PlainRunReporter:
    captures_subprocess_output = False

    def __enter__(self) -> "PlainRunReporter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def start(self, manifest: dict[str, Any], selected: list[dict[str, Any]], concurrency: int, auto_worktrees: dict[str, Any]) -> None:
        return None

    def item(self, item_id: str, status: str, detail: str = "") -> None:
        if detail:
            print(f"[{item_id}] {detail}")

    def run(self, item_id: str, run_id: str, status: str, detail: str = "") -> None:
        message = detail or status.replace("_", " ")
        print(f"[{item_id}/{run_id}] {message}")

    def usage(self, item_id: str, run_id: str, usage: dict[str, Any]) -> None:
        return None

    def worker_error(self, item_id: str, error: Exception) -> None:
        print(f"[{item_id}] unexpected worker error: {error}")

    def subprocess_output(self, item_id: str, run_id: str, completed: subprocess.CompletedProcess) -> None:
        return None


class LiveRunReporter(PlainRunReporter):  # pragma: no cover
    captures_subprocess_output = True

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}
        self.events: deque[str] = deque(maxlen=10)
        self.operation_id = ""
        self.concurrency = 1
        self.auto_worktrees = False
        self.started = time.monotonic()
        self.live: Any = None
        self.console: Any = None
        self.closed = threading.Event()
        self.ticker: threading.Thread | None = None

    def __enter__(self) -> "LiveRunReporter":
        try:
            from rich.console import Console
            from rich.live import Live
        except ImportError as exc:  # pragma: no cover - dependency is declared for normal installs
            raise OcmoError("live run UI requires the rich package") from exc
        self.console = Console()
        self.live = Live(self.render(), console=self.console, refresh_per_second=4, transient=False)
        self.live.__enter__()
        self.closed.clear()
        self.ticker = threading.Thread(target=self.tick, daemon=True)
        self.ticker.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.closed.set()
        if self.ticker:
            self.ticker.join(timeout=1)
        if self.live:
            self.live.update(self.render())
            self.live.__exit__(exc_type, exc, traceback)

    def tick(self) -> None:
        while not self.closed.wait(1):
            self.refresh()

    def start(self, manifest: dict[str, Any], selected: list[dict[str, Any]], concurrency: int, auto_worktrees: dict[str, Any]) -> None:
        with self.lock:
            self.operation_id = str(manifest["operation"]["id"])
            self.concurrency = concurrency
            self.auto_worktrees = bool(auto_worktrees.get("enabled"))
            self.started = time.monotonic()
            for item in selected:
                item_id = str(item["id"])
                self.items[item_id] = {
                    "status": "queued",
                    "step": "-",
                    "progress": f"0/{len(work_unit_runs(manifest, item))}",
                    "detail": str(item.get("title") or "-"),
                    "started": None,
                    "ended": None,
                }
            self.events.appendleft(f"selected {len(selected)} work unit(s), concurrency={concurrency}")
        self.refresh()

    def item(self, item_id: str, status: str, detail: str = "") -> None:
        with self.lock:
            item = self.items.setdefault(item_id, {"progress": "0/1", "step": "-", "detail": "-", "started": None, "ended": None})
            item["status"] = status
            if status in {"worktree_removed", "cleanup"} and item.get("progress", "0/1").split("/", 1)[0] == item.get("progress", "0/1").split("/", 1)[-1]:
                item["status"] = "completed"
            if detail:
                item["detail"] = detail
                self.events.appendleft(f"{item_id}: {detail}")
            if status in {"running", "creating_worktree", "setup", "cleanup"} and item.get("started") is None:
                item["started"] = time.monotonic()
            if item["status"] in TERMINAL_STATUSES and item.get("ended") is None:
                item["ended"] = time.monotonic()
        self.refresh()

    def run(self, item_id: str, run_id: str, status: str, detail: str = "") -> None:
        with self.lock:
            item = self.items.setdefault(item_id, {"progress": "0/1", "step": "-", "detail": "-", "started": None, "ended": None})
            if item.get("started") is None:
                item["started"] = time.monotonic()
            if status == "completed":
                item["status"] = "running"
            else:
                item["status"] = "running" if status == "running" else status
            item["step"] = run_id
            run_detail = detail or status.replace("_", " ")
            item["detail"] = run_detail
            progress = item.get("progress", "0/1")
            total = progress.split("/", 1)[-1] if "/" in progress else "1"
            if status == "running":
                current = item.get("runIndex", 0) + 1
                item["runIndex"] = current
                item["progress"] = f"{current}/{total}"
            if status in TERMINAL_STATUSES and item.get("ended") is None:
                item["ended"] = time.monotonic()
            self.events.appendleft(f"{item_id}/{run_id}: {run_detail}")
        self.refresh()

    def usage(self, item_id: str, run_id: str, usage: dict[str, Any]) -> None:
        with self.lock:
            item = self.items.setdefault(item_id, {"progress": "0/1", "step": "-", "detail": "-", "started": None, "ended": None})
            item["usage"] = usage
            item["step"] = run_id
        self.refresh()

    def worker_error(self, item_id: str, error: Exception) -> None:
        self.item(item_id, "failed", f"unexpected worker error: {error}")

    def subprocess_output(self, item_id: str, run_id: str, completed: subprocess.CompletedProcess) -> None:
        text = "\n".join(part for part in [getattr(completed, "stdout", ""), getattr(completed, "stderr", "")] if part)
        rendered = render_opencode_output_text(text)
        last_line = next((line.strip() for line in reversed(rendered.splitlines()) if line.strip()), "")
        if last_line:
            with self.lock:
                self.events.appendleft(f"{item_id}/{run_id}: {last_line[:120]}")
            self.refresh()

    def refresh(self) -> None:
        if self.live:
            self.live.update(self.render())

    def render(self) -> Any:
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        elapsed = format_duration(time.monotonic() - self.started)
        counts = self.status_counts()
        title = Text(f"OC Mass Operations: {self.operation_id or '-'}", style="bold cyan")
        summary = Text(
            f"selected={len(self.items)} running={counts['running']} completed={counts['completed']} "
            f"failed={counts['failed']} pending={counts['pending']} concurrency={self.concurrency} elapsed={elapsed} "
            f"{format_usage_summary(sum_usage(item.get('usage') for item in self.items.values()))}"
        )
        if self.auto_worktrees:
            summary.append(" autoWorktrees=true")
        table = Table(expand=True)
        table.add_column("Work Unit", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Step", no_wrap=True)
        table.add_column("Progress", no_wrap=True)
        table.add_column("Runtime", no_wrap=True)
        table.add_column("Tokens", no_wrap=True)
        table.add_column("Detail")
        with self.lock:
            rows = list(self.items.items())
            events = list(self.events)
        for item_id, item in rows:
            runtime = item_runtime(item, time.monotonic())
            table.add_row(
                item_id,
                str(item.get("status", "-")),
                str(item.get("step", "-")),
                str(item.get("progress", "-")),
                runtime,
                format_usage_cell(item.get("usage")),
                str(item.get("detail", "-")),
            )
        event_text = "\n".join(events) if events else "No events yet."
        return Group(title, summary, table, Panel(event_text, title="Recent Events"))

    def status_counts(self) -> dict[str, int]:
        counts = {"running": 0, "completed": 0, "failed": 0, "pending": 0}
        with self.lock:
            statuses = [str(item.get("status", "queued")) for item in self.items.values()]
        for status in statuses:
            if status in {"completed"}:
                counts["completed"] += 1
            elif status in {"failed", "timed_out", "cleanup_failed", "worktree_failed", "setup_failed"}:
                counts["failed"] += 1
            elif status in {"queued"}:
                counts["pending"] += 1
            else:
                counts["running"] += 1
        return counts


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def item_runtime(item: dict[str, Any], now: float) -> str:
    started = item.get("started")
    if started is None:
        return "-"
    ended = item.get("ended")
    return format_duration((ended if ended is not None else now) - started)


def make_run_reporter(ui: str) -> PlainRunReporter:
    if ui == "plain":
        return PlainRunReporter()
    if ui == "auto" and not sys.stdout.isatty():
        return PlainRunReporter()
    if ui == "auto":
        try:
            import rich  # noqa: F401
        except ImportError:
            return PlainRunReporter()
    return LiveRunReporter()  # pragma: no cover


def subprocess_run_kwargs(reporter: PlainRunReporter) -> dict[str, Any]:
    if reporter.captures_subprocess_output:
        return {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    return {}


def run_output_path(manifest_path: Path, item_id: str, run_id: str) -> Path:
    return manifest_path.parent / "outputs" / f"{slugify(item_id)}__{slugify(run_id)}.txt"


def relative_to_manifest(path: Path, manifest_path: Path) -> str:
    return path.resolve().relative_to(manifest_path.parent.resolve()).as_posix()


def verify_required_artifacts(manifest_path: Path, item: dict[str, Any], run: dict[str, Any]) -> dict[str, str]:
    produced = produced_artifacts(run)
    verified: dict[str, str] = {}
    for artifact_id, config in produced.items():
        path = default_artifact_path(manifest_path, item, str(run["id"]), artifact_id, config)
        relative = relative_to_manifest(path, manifest_path)
        if config.get("required", True) and (not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip()):
            raise OcmoError(f"required artifact was not written or is empty: {relative}")
        if path.exists():
            if is_handoff_artifact(artifact_id, config):
                verify_handoff_artifact(path, relative, config)
            verified[artifact_id] = relative
    return verified


def verify_handoff_artifact(path: Path, relative: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OcmoError(f"handoff artifact is not valid JSON: {relative}: {exc}") from exc
    if not isinstance(handoff, dict):
        raise OcmoError(f"handoff artifact must be a JSON object: {relative}")
    if handoff.get("schema") != "ocmo-handoff/v1":
        raise OcmoError(f"handoff artifact schema must be ocmo-handoff/v1: {relative}")
    decision = handoff.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        raise OcmoError(f"handoff artifact decision must be a non-empty string: {relative}")
    confidence = handoff.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < 0 or confidence > 1:
        raise OcmoError(f"handoff artifact confidence must be a number between 0 and 1: {relative}")
    if not isinstance(handoff.get("handoff"), str) or not handoff.get("handoff", "").strip():
        raise OcmoError(f"handoff artifact handoff must be a non-empty string: {relative}")
    gates = config.get("gates") if isinstance(config.get("gates"), dict) else {}
    required_decision = gates.get("decision")
    if required_decision is not None and decision != required_decision:
        raise OcmoBlocked(f"handoff gate blocked {relative}: decision {decision!r} does not match {required_decision!r}")
    min_confidence = gates.get("minConfidence")
    if min_confidence is not None and confidence < min_confidence:
        raise OcmoBlocked(f"handoff gate blocked {relative}: confidence {confidence} is below {min_confidence}")
    if gates.get("requireConditionsMet"):
        conditions = handoff.get("conditions", [])
        if not isinstance(conditions, list):
            raise OcmoError(f"handoff artifact conditions must be a list: {relative}")
        for index, condition in enumerate(conditions, start=1):
            if not isinstance(condition, dict) or condition.get("met") is not True:
                raise OcmoBlocked(f"handoff gate blocked {relative}: conditions[{index}].met is not true")
    return handoff


def run_opencode_command(
    command: list[str],
    run_dir: Path,
    run_timeout: int | None,
    output_path: Path,
    on_start: Any | None = None,
    on_session: Any | None = None,
    on_usage: Any | None = None,
) -> subprocess.CompletedProcess:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = opencode_capture_env()
    with output_path.open("w", encoding="utf-8") as output:
        output.write(f"$ {format_command(command)}\n\n")
        output.flush()
        process: subprocess.Popen[str] | None = None
        transcript_needs_newline = False

        def write_transcript(text: str) -> None:
            nonlocal transcript_needs_newline
            if not text:
                return
            output.write(text)
            transcript_needs_newline = not text.endswith("\n")
            output.flush()

        def write_ocmo_line(text: str) -> None:
            nonlocal transcript_needs_newline
            if transcript_needs_newline:
                output.write("\n")
                transcript_needs_newline = False
            output.write(text)
            output.flush()

        try:
            process = subprocess.Popen(
                command,
                cwd=str(run_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            if on_start:
                on_start(process.pid)
            if on_session:
                chunks = []
                read_error: list[BaseException] = []

                def read_stdout() -> None:
                    try:
                        assert process is not None
                        assert process.stdout is not None
                        for line in process.stdout:
                            chunks.append(line)
                            write_transcript(render_opencode_output_line(line))
                            session_id = extract_session_id(line)
                            if session_id:
                                on_session(session_id)
                            usage = extract_usage_delta(line)
                            if usage and on_usage:
                                on_usage(usage)
                    except BaseException as exc:  # pragma: no cover - defensive; re-raised by caller
                        read_error.append(exc)

                reader = threading.Thread(target=read_stdout, daemon=True)
                reader.start()
                process.wait(timeout=run_timeout)
                reader.join(timeout=5)
                if read_error:
                    raise read_error[0]
                stdout = "".join(chunks)
            else:
                stdout, _ = process.communicate(timeout=run_timeout)
                if on_usage:
                    for usage in extract_usage_deltas(stdout or ""):
                        on_usage(usage)
                for line in (stdout or "").splitlines(keepends=True):
                    write_transcript(render_opencode_output_line(line))
        except subprocess.TimeoutExpired:
            if process is not None:
                terminate_process_tree(process.pid, force=True)
            write_ocmo_line(f"\n[ocmo] timed out after {run_timeout} seconds\n")
            raise
        except KeyboardInterrupt:
            if process is not None:
                terminate_process_tree(process.pid, force=True)
            write_ocmo_line("\n[ocmo] interrupted\n")
            raise
        except OSError as exc:
            write_ocmo_line(f"\n[ocmo] failed to start: {exc}\n")
            raise
        cleaned_stdout = strip_ansi(stdout or "")
        write_ocmo_line(f"\n[ocmo] exit code: {process.returncode}\n")
        return subprocess.CompletedProcess(command, int(process.returncode or 0), stdout=cleaned_stdout, stderr=None)


def render_opencode_output_line(line: str) -> str:
    stripped = strip_ansi(line).strip()
    if not stripped.startswith("{"):
        return strip_ansi(line)
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return strip_ansi(line)
    if not isinstance(event, dict):
        return ""
    part = event.get("part")
    if isinstance(part, dict):
        text = part.get("text")
        if isinstance(text, str):
            return text
    message = event.get("message")
    if isinstance(message, str):
        return message + ("" if message.endswith("\n") else "\n")
    return ""


def render_opencode_output_text(output: str) -> str:
    return "".join(render_opencode_output_line(line) for line in output.splitlines(keepends=True))


def extract_session_id(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        direct = event.get("sessionId") or event.get("sessionID")
        if isinstance(direct, str) and direct:
            return direct
        session = event.get("session")
        if isinstance(session, dict):
            value = session.get("id") or session.get("sessionId") or session.get("sessionID")
            if isinstance(value, str) and value:
                return value
    return None


def empty_usage() -> dict[str, Any]:
    return {"input": 0, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0, "cost": 0, "steps": 0}


def extract_usage_deltas(output: str) -> list[dict[str, Any]]:
    return [usage for line in output.splitlines() if (usage := extract_usage_delta(line))]


def extract_usage_delta(output_line: str) -> dict[str, Any] | None:
    line = output_line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    part = event.get("part") if isinstance(event, dict) else None
    if not isinstance(part, dict):
        return None
    if event.get("type") != "step_finish" and part.get("type") != "step-finish":
        return None
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    usage = empty_usage()
    usage["input"] = usage_int(tokens.get("input"))
    usage["output"] = usage_int(tokens.get("output"))
    usage["reasoning"] = usage_int(tokens.get("reasoning"))
    usage["cacheRead"] = usage_int(cache.get("read"))
    usage["cacheWrite"] = usage_int(cache.get("write"))
    usage["total"] = usage_int(tokens.get("total"))
    usage["cost"] = usage_number(part.get("cost"))
    usage["steps"] = 1
    return usage


def usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def usage_number(value: Any) -> int | float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    return 0


def add_usage(base: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any]:
    result = empty_usage()
    for source in (base, delta):
        if not isinstance(source, dict):
            continue
        for key in result:
            result[key] += usage_number(source.get(key))
    if isinstance(result["cost"], float) and result["cost"].is_integer():
        result["cost"] = int(result["cost"])
    return result


def sum_usage(usages: Any) -> dict[str, Any]:
    result = empty_usage()
    if not usages:
        return result
    for usage in usages:
        result = add_usage(result, usage if isinstance(usage, dict) else None)
    return result


def item_usage(item_state: dict[str, Any]) -> dict[str, Any]:
    runs = item_state.get("runs") if isinstance(item_state.get("runs"), dict) else {}
    return sum_usage(run.get("usage") for run in runs.values() if isinstance(run, dict))


def state_usage(state: dict[str, Any]) -> dict[str, Any]:
    work_units = state.get("workUnits") if isinstance(state.get("workUnits"), dict) else {}
    return sum_usage(item_usage(work_unit) for work_unit in work_units.values() if isinstance(work_unit, dict))


def format_token_count(value: Any) -> str:
    count = usage_int(value)
    if count <= 0:
        return "-"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}m"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_usage_summary(usage: dict[str, Any] | None) -> str:
    usage = usage if isinstance(usage, dict) else empty_usage()
    if usage_int(usage.get("steps")) == 0 and usage_int(usage.get("total")) == 0:
        return "tokens=-"
    cache_total = usage_int(usage.get("cacheRead")) + usage_int(usage.get("cacheWrite"))
    return (
        f"tokens={format_token_count(usage.get('total'))} "
        f"in={format_token_count(usage.get('input'))} "
        f"out={format_token_count(usage.get('output'))} "
        f"cache={format_token_count(cache_total)}"
    )


def format_usage_cell(usage: Any) -> str:
    if not isinstance(usage, dict) or (usage_int(usage.get("steps")) == 0 and usage_int(usage.get("total")) == 0):
        return "-"
    return f"{format_token_count(usage.get('input'))}/{format_token_count(usage.get('output'))}"


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def opencode_capture_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env["FORCE_COLOR"] = "0"
    env["TERM"] = "dumb"
    return env


def run_manifest(options: RunOptions) -> int:
    manifest = load_manifest(options.manifest_path)
    validate_manifest(manifest, options.manifest_path, options.allow_shared_worktree_concurrency)
    existing_state = read_json_file(state_path(manifest, options.manifest_path)) if state_path(manifest, options.manifest_path).exists() else {}
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
        for item in selected:
            execution = worktree_execution(manifest, options.manifest_path, item) if auto_worktrees["enabled"] else {}
            run_dir = Path(execution["worktreePath"]) if execution else None
            runs = work_unit_runs(manifest, item)
            for run in runs:
                runner = effective_runner(manifest, run)
                run_timeout = timeout_seconds if timeout_seconds is not None else runner.get("timeoutSeconds")
                prompt_text = render_prompt(manifest, item, options.manifest_path, execution, run, runs)
                command = build_command(manifest, options.manifest_path, prompt_text, run_dir, runner)
                header = [format_command(command)]
                if execution:
                    header.append(f"# worktree: {execution['worktreePath']}")
                    header.append(f"# branch: {execution['branchName']}")
                if run_timeout:
                    header.append(f"# timeout: {run_timeout} seconds")
                for artifact_id, config in produced_artifacts(run).items():
                    header.append(f"# produces: {artifact_id} -> {produced_artifact_relative_path(options.manifest_path, item, str(run['id']), artifact_id, config)}")
                for reference in run.get("consumes") or []:
                    header.append(f"# consumes: {reference}")
                previews.append(PromptPreview(str(item["id"]), str(run["id"]), "\n".join(header) + "\n\n--- prompt ---\n\n" + prompt_text))
        print_prompt_previews(previews, options.preview_all)
        return 0

    if not options.yes:
        timeout_text = f", timeout={timeout_seconds}s" if timeout_seconds else ""
        worktree_text = ", autoWorktrees=true" if auto_worktrees["enabled"] else ""
        print(f"About to run {len(selected)} work unit(s) with concurrency={concurrency}{timeout_text}{worktree_text}.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    state = StateStore(state_path(manifest, options.manifest_path))
    state.ensure_operation(manifest)
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
        state.finish(operation_failed_status(state.data()))
        return 1
    state.finish("completed")
    return 0


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
    }
    write_detached_metadata(local_dir / f"{run_id}.json", metadata)
    write_detached_metadata(global_detached_run_path(run_id), metadata)
    print(f"detached: {run_id}")
    print(f"pid: {process.pid}")
    print(f"log: {relative_to_manifest(log_path, options.manifest_path)}")
    print(f"status: ocmo status --run-id {run_id}")
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
    command += ["--ui", "plain", "--yes"]
    if options.allow_shared_worktree_concurrency and not options.resume:
        command.append("--allow-shared-worktree-concurrency")
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
        print_manifest_detached_runs(infer_manifest_path(args.manifest), include_inactive=args.all)
        return 0
    records = detached_records(include_inactive=args.all, kind="operation")
    if not records:
        qualifier = "" if args.all else " active"
        print(f"No{qualifier} detached ocmo runs.")
        return 0
    for record in records:
        print_detached_record(record, details=False)
    return 0


def status_operation(args: argparse.Namespace) -> int:
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
    if args.once:
        print_operation_status(manifest_path, include_inactive=args.all, selected_record=record)
        return 0
    return watch_operation_status(manifest_path, include_inactive=args.all, selected_record=record, interval=interval)


def pause_operation(args: argparse.Namespace) -> int:
    manifest_path, record = control_manifest_and_record(args)
    manifest = load_manifest(manifest_path)
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    changed = mark_active_runs(path, "paused")
    stop_operation_processes(manifest_path, state, record)
    print(f"paused: {manifest['operation']['id']} ({changed} run(s))")
    return 0


def kill_operation(args: argparse.Namespace) -> int:
    manifest_path, record = control_manifest_and_record(args)
    manifest = load_manifest(manifest_path)
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
    manifest_path, record = control_manifest_and_record(args)
    if not is_generated_operation_manifest(manifest_path):
        raise OcmoError("erase only removes generated .ocmo/<operation>/manifest.yaml operation directories")
    manifest = load_manifest(manifest_path)
    if args.delete_definition and args.keep_definition:
        raise OcmoError("erase accepts only one of --delete-definition or --keep-definition")
    delete_definition = args.delete_definition
    if args.force and not (args.delete_definition or args.keep_definition):
        raise OcmoError("erase --force requires --keep-definition or --delete-definition")
    if not args.force and sys.stdin.isatty():
        answer = input(f"Erase runtime data for generated operation {manifest_path.parent}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
        if not (args.delete_definition or args.keep_definition):
            answer = input("Delete operation definition files too? This removes manifest and prompts. [y/N] ").strip().lower()
            delete_definition = answer in {"y", "yes"}
    if not args.force and not sys.stdin.isatty():
        raise OcmoError("erase requires --force when not interactive")
    path = state_path(manifest, manifest_path)
    state = read_json_file(path) if path.exists() else {}
    stop_operation_processes(manifest_path, state, record)
    remove_detached_records(manifest_path)
    if delete_definition:
        erased = manifest_path.parent
        shutil.rmtree(erased)
        print(f"erased: {erased}")
    else:
        erase_operation_runtime_data(manifest, manifest_path)
        print(f"erased runtime data: {manifest_path.parent}")
    return 0


def erase_operation_runtime_data(manifest: dict[str, Any], manifest_path: Path) -> None:
    operation_dir = manifest_path.parent.resolve()
    runtime_paths = [
        state_path(manifest, manifest_path),
        manifest_path.parent / "outputs",
        manifest_path.parent / ARTIFACT_ROOT,
        detached_runs_dir(manifest_path),
    ]
    for path in runtime_paths:
        erase_operation_runtime_path(path, operation_dir)


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


def resume_operation(args: argparse.Namespace) -> int:
    manifest_path = infer_manifest_path(args.manifest)
    return run_manifest(RunOptions(manifest_path, None, None, None, False, args.yes, args.ui, False, False, args.detach, True))


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
        )
    )


def workflow_command(args: argparse.Namespace) -> int:
    command = args.workflow_command
    if command == "validate":
        workflow_path = infer_workflow_path(args.workflow)
        workflow = load_workflow(workflow_path)
        validate_workflow(workflow, workflow_path)
        print(f"valid: {workflow_path}")
        return 0
    if command == "run":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, args.dry_run, args.yes, args.ui, args.detach))
    if command == "status":
        return status_workflow(args)
    if command == "list":
        return list_workflow_runs(args)
    if command == "pause":
        return pause_workflow(args)
    if command == "resume":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, False, args.yes, args.ui, args.detach, True))
    if command == "rerun":
        return run_workflow(WorkflowOptions(infer_workflow_path(args.workflow), args.select, False, args.yes, args.ui, args.detach, False, True))
    if command == "kill":
        return kill_workflow(args)
    return 1  # pragma: no cover


def run_workflow(options: WorkflowOptions) -> int:
    workflow = load_workflow(options.workflow_path)
    validate_workflow(workflow, options.workflow_path)
    state_path_value = workflow_state_path(workflow, options.workflow_path)
    existing_state = read_json_file(state_path_value) if state_path_value.exists() else {}
    selected = select_workflow_steps(workflow, existing_state, options.select or ("retryable" if options.rerun else "uncompleted"))
    if not selected:
        print("No workflow steps selected.")
        return 0
    if options.detach:
        if options.dry_run:
            raise OcmoError("--detach cannot be used with --dry-run")
        return start_detached_workflow(options, workflow)
    if options.dry_run:
        print_workflow_dry_run(workflow, options.workflow_path, selected, options.rerun, options.resume)
        return 0
    if not options.yes:
        print(f"About to run {len(selected)} workflow step(s) sequentially.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
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
        pause_workflow_path(options.workflow_path)
        return 130
    state.finish("completed" if all(code == 0 for code in results) else "failed")
    return 0 if all(code == 0 for code in results) else 1


def run_workflow_step(workflow: dict[str, Any], workflow_path: Path, step: dict[str, Any], state: "WorkflowStateStore", options: WorkflowOptions) -> int:
    step_id = str(step["id"])
    manifest_path = workflow_step_manifest_path(workflow_path, step)
    manifest = load_manifest(manifest_path)
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
        None,
        None,
        None,
        False,
        True,
        options.ui,
        False,
        False,
        False,
        resume_step,
        options.rerun,
    )
    code = run_manifest(run_options)
    status = "completed" if code == 0 else workflow_failed_step_status(operation_state_path)
    patch: dict[str, Any] = {"completedAt": utc_now(), "exitCode": code}
    if code != 0:
        patch["error"] = f"operation exited with code {code}"
    state.mark_step(step_id, status, patch)
    return code


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
    command += ["--ui", "plain", "--yes"]
    return command


def detached_workflow_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"ocmo-workflow-{stamp}-{uuid.uuid4().hex[:6]}"


def status_workflow(args: argparse.Namespace) -> int:
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
    print_workflow_status(workflow_path, args.all, record)
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
        print_workflow_detached_runs(infer_workflow_path(args.workflow), include_inactive=args.all)
        return 0
    records = detached_records(include_inactive=args.all, kind="workflow")
    if not records:
        qualifier = "" if args.all else " active"
        print(f"No{qualifier} detached ocmo workflow runs.")
        return 0
    for record in records:
        print_detached_record(record, details=False)
    return 0


def pause_workflow(args: argparse.Namespace) -> int:
    workflow_path, record = control_workflow_and_record(args)
    changed = pause_workflow_path(workflow_path, record)
    workflow = load_workflow(workflow_path)
    print(f"paused: {workflow['workflow']['id']} ({changed} step(s))")
    return 0


def kill_workflow(args: argparse.Namespace) -> int:
    workflow_path, record = control_workflow_and_record(args)
    workflow = load_workflow(workflow_path)
    if not args.force and sys.stdin.isatty():
        answer = input(f"Kill workflow {workflow['workflow']['id']}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    changed = mark_active_workflow_steps(workflow_path, "killed")
    stop_workflow_processes(workflow_path, record, "killed")
    print(f"killed: {workflow['workflow']['id']} ({changed} step(s))")
    return 0


def pause_workflow_path(workflow_path: Path, record: dict[str, Any] | None = None) -> int:
    changed = mark_active_workflow_steps(workflow_path, "paused")
    stop_workflow_processes(workflow_path, record, "paused")
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


def stop_workflow_processes(workflow_path: Path, selected_record: dict[str, Any] | None = None, operation_status: str = "paused") -> None:
    if selected_record and isinstance(selected_record.get("pid"), int):
        terminate_process_tree(selected_record["pid"], force=True)
    for record in related_workflow_detached_records(workflow_path, include_inactive=True):
        pid = record.get("pid")
        if isinstance(pid, int):
            terminate_process_tree(pid, force=True)
    workflow = load_workflow(workflow_path)
    state_file = workflow_state_path(workflow, workflow_path)
    state = read_json_file(state_file) if state_file.exists() else {}
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    for step_state in steps.values():
        if not isinstance(step_state, dict) or step_state.get("status") not in {"running", "paused", "killed"}:
            continue
        manifest_value = step_state.get("manifestPath")
        if isinstance(manifest_value, str):
            manifest_path = Path(manifest_value)
            manifest = load_manifest(manifest_path)
            operation_state = read_json_file(state_path(manifest, manifest_path)) if state_path(manifest, manifest_path).exists() else {}
            mark_active_runs(state_path(manifest, manifest_path), operation_status)
            stop_operation_processes(manifest_path, operation_state)


def mark_active_workflow_steps(workflow_path: Path, status: str) -> int:
    workflow = load_workflow(workflow_path)
    path = workflow_state_path(workflow, workflow_path)
    if not path.exists():
        return 0
    data = read_json_file(path)
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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


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
        if include_inactive or process_is_alive(record.get("pid")):
            records.append(record)
    return records


def print_manifest_detached_runs(manifest_path: Path, include_inactive: bool) -> None:
    manifest = load_manifest(manifest_path)
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


def print_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None) -> None:
    print(render_operation_status(manifest_path, include_inactive, selected_record))


def render_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None) -> str:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        print_operation_status_snapshot(manifest_path, include_inactive, selected_record)
    return stdout.getvalue().rstrip("\n")


def watch_operation_status(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float) -> int:
    if sys.stdout.isatty():
        try:
            from rich.live import Live
        except ImportError:
            return watch_operation_status_plain(manifest_path, include_inactive, selected_record, interval, clear_screen=True)
        try:
            with Live(render_operation_status(manifest_path, include_inactive, selected_record), refresh_per_second=max(1, int(1 / interval)), transient=False) as live:
                while True:
                    time.sleep(interval)
                    live.update(render_operation_status(manifest_path, include_inactive, selected_record))
        except KeyboardInterrupt:
            return 130
    return watch_operation_status_plain(manifest_path, include_inactive, selected_record, interval, clear_screen=False)


def watch_operation_status_plain(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None, interval: float, clear_screen: bool) -> int:
    first = True
    try:
        while True:
            if clear_screen:
                print("\033[H\033[J", end="")
            elif not first:
                print()
            print(render_operation_status(manifest_path, include_inactive, selected_record), flush=True)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130


def print_operation_status_snapshot(manifest_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None) -> None:
    manifest = load_manifest(manifest_path)
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
        f"killed={counts['killed']} {format_usage_summary(state_usage(state))} updated={updated}"
    )
    if any(row["status"] == "running" for row in rows) and related and not any(process_is_alive(record.get("pid")) for record in related if record):
        print("warning: detached run is inactive but state contains running work units; run may be stale")
    print_status_table(rows)


def print_workflow_status(workflow_path: Path, include_inactive: bool, selected_record: dict[str, Any] | None = None) -> None:
    workflow = load_workflow(workflow_path)
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
        f"killed={counts['killed']} {format_usage_summary(workflow_usage(workflow, workflow_path))} updated={updated}"
    )
    if any(row["status"] == "running" for row in rows) and related and not any(process_is_alive(record.get("pid")) for record in related if record):
        print("warning: detached workflow is inactive but state contains running steps; run may be stale")
    print_workflow_status_table(rows)


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
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
            operation = str(manifest["operation"]["id"])
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
        rows.append({"item": step_id, "status": status, "step": operation, "progress": item_progress, "runtime": persisted_item_runtime(step_state, datetime.now(timezone.utc)), "tokens": tokens, "detail": detail})
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
        manifest = load_manifest(manifest_path)
        path = state_path(manifest, manifest_path)
        if path.exists():
            total = add_usage(total, state_usage(read_json_file(path)))
    return total


def print_workflow_detached_runs(workflow_path: Path, include_inactive: bool) -> None:
    workflow = load_workflow(workflow_path)
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
                "runtime": persisted_item_runtime(item_state, now),
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
    headers = ["Work Unit", "Status", "Step", "Progress", "Runtime", "Tokens", "Detail"]
    keys = ["item", "status", "step", "progress", "runtime", "tokens", "detail"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, key in enumerate(keys):
            widths[index] = max(widths[index], len(row[key]))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[key].ljust(widths[index]) for index, key in enumerate(keys)))


def print_detached_record(record: dict[str, Any], details: bool, prefix: str = "") -> None:
    pid = record.get("pid")
    status = "active" if process_is_alive(pid) else "inactive"
    kind = record.get("kind", "operation")
    print(f"{prefix}{record.get('runId', '<unknown>')} {status} kind={kind} pid={pid} started={record.get('startedAt', '-')}")
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
        print(f"updated: {updated}")
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
        print(f"updated: {updated}")


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
            if use_session_resume:
                session_id = previous_run_state.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise OcmoError(f"paused run is missing opencode sessionId: {item_id}/{run_id}")
                prompt_text = resume_prompt(item_id, run_id)
                command = build_resume_command(manifest, manifest_path, prompt_text, session_id, run_dir, runner)
            else:
                prompt_text = render_prompt(manifest, item, manifest_path, execution, run, runs)
                command = build_command(manifest, manifest_path, prompt_text, run_dir, runner)
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
            data.setdefault("schema", "ocmo-state/v1")
            data.setdefault("operationId", manifest["operation"]["id"])
            data.setdefault("workUnits", {})
            data["control"] = {"status": "running", "updatedAt": utc_now()}
            data["updatedAt"] = utc_now()
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
            if status == "completed":
                data["completedAt"] = now
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
        manifest = load_manifest(manifest_path)
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


def plan_manifest(args: argparse.Namespace) -> int:
    if not args.from_file.exists():
        raise OcmoError(f"prompt file not found: {args.from_file}")
    configured_max_attempts = getattr(args, "max_attempts", 3)
    max_attempts = configured_max_attempts if isinstance(configured_max_attempts, int) else 3
    if max_attempts < 1:
        raise OcmoError("--max-attempts must be a positive integer")
    missing = [path for path in args.read_files if not path.exists()]
    if missing:
        raise OcmoError(f"read-only source file not found: {missing[0]}")
    workspace = plan_workspace(args)
    out_path = plan_output_path(args, workspace)
    artifact_dir = plan_artifact_dir(args, workspace, out_path)
    configured_interactive = getattr(args, "interactive", False)
    interactive = configured_interactive if isinstance(configured_interactive, bool) else False
    source_prompt = args.from_file.read_text(encoding="utf-8")
    planning_prompt = build_planning_prompt(source_prompt, args.read_files, workspace, interactive, artifact_dir)
    if args.dry_run:
        print(planning_prompt)
        return 0
    feedback = None
    previous_output = ""
    with make_plan_reporter(args, workspace, out_path, interactive) as reporter:
        for attempt in range(1, max_attempts + 1):
            reporter.attempt(attempt, max_attempts)
            prompt = planning_prompt if feedback is None else build_planning_feedback_prompt(planning_prompt, previous_output, feedback, interactive)
            command = build_plan_command(args, prompt, workspace, interactive)
            returncode, output, error_output = run_plan_command(command, interactive)
            if returncode != 0:
                print(output, end="")
                print(error_output, end="", file=sys.stderr)
                return returncode
            previous_output = output
            try:
                manifest_text, generated_files = parse_plan_output(output, interactive)
                manifest = load_manifest_text(manifest_text)
                validate_manifest_schema(manifest, out_path, allow_shared_worktree_concurrency=True)
                validate_generated_plan_files(manifest, out_path, generated_files)
            except (OcmoError, yaml.YAMLError) as exc:
                feedback = str(exc)
                reporter.invalid(attempt, max_attempts, feedback)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_generated_plan_files(out_path, generated_files)
            out_path.write_text(manifest_text, encoding="utf-8")
            reporter.wrote(out_path, generated_files)
            return 0
    raise OcmoError(f"planner did not produce a valid ocmo/v1 manifest after {max_attempts} attempts: {feedback}")


class PlainPlanReporter:
    def __init__(self, args: argparse.Namespace, workspace: Path, out_path: Path) -> None:
        self.args = args
        self.workspace = workspace
        self.out_path = out_path

    def __enter__(self) -> "PlainPlanReporter":
        print(f"ocmo: planning with agent={PLAN_AGENT} model={self.args.model or '<opencode-default>'}", file=sys.stderr)
        print(f"ocmo: planning workspace={self.workspace}", file=sys.stderr)
        print(f"ocmo: planning output={self.out_path}", file=sys.stderr)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def attempt(self, attempt: int, max_attempts: int) -> None:
        print(f"ocmo: planner attempt {attempt}/{max_attempts}", file=sys.stderr)

    def invalid(self, attempt: int, max_attempts: int, feedback: str) -> None:
        print(f"ocmo: planner output invalid on attempt {attempt}/{max_attempts}: {feedback}", file=sys.stderr)

    def wrote(self, out_path: Path, generated_files: dict[Path, str]) -> None:
        print(f"wrote: {out_path}")
        for relative_path in sorted(generated_files, key=str):
            print(f"wrote: {out_path.parent / relative_path}")


class RichPlanReporter(PlainPlanReporter):  # pragma: no cover
    def __init__(self, args: argparse.Namespace, workspace: Path, out_path: Path) -> None:
        super().__init__(args, workspace, out_path)
        self.started = time.monotonic()
        self.status: Any = None

    def __enter__(self) -> "RichPlanReporter":
        from rich.console import Console

        console = Console(stderr=True)
        self.status = console.status(self.status_text("starting"), spinner="dots")
        self.status.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.status:
            self.status.__exit__(exc_type, exc, traceback)

    def attempt(self, attempt: int, max_attempts: int) -> None:
        if self.status:
            self.status.update(self.status_text(f"attempt {attempt}/{max_attempts}"))

    def invalid(self, attempt: int, max_attempts: int, feedback: str) -> None:
        if self.status:
            self.status.update(self.status_text(f"attempt {attempt}/{max_attempts} invalid: {feedback[:80]}"))

    def wrote(self, out_path: Path, generated_files: dict[Path, str]) -> None:
        if self.status:
            self.status.update(self.status_text("writing files"))
        super().wrote(out_path, generated_files)

    def status_text(self, phase: str) -> str:
        elapsed = format_duration(time.monotonic() - self.started)
        model = self.args.model or "<opencode-default>"
        return f"ocmo plan {phase} | agent={PLAN_AGENT} model={model} elapsed={elapsed} output={self.out_path}"


def make_plan_reporter(args: argparse.Namespace, workspace: Path, out_path: Path, interactive: bool) -> PlainPlanReporter:
    if interactive or not sys.stderr.isatty():
        return PlainPlanReporter(args, workspace, out_path)
    try:
        import rich  # noqa: F401
    except ImportError:
        return PlainPlanReporter(args, workspace, out_path)
    return RichPlanReporter(args, workspace, out_path)  # pragma: no cover


def plan_workspace(args: argparse.Namespace) -> Path:
    configured = getattr(args, "workspace", None)
    if isinstance(configured, Path):
        return configured.resolve()
    return Path.cwd().resolve()


def plan_output_path(args: argparse.Namespace, workspace: Path) -> Path:
    configured = getattr(args, "out", None)
    if isinstance(configured, Path):
        return configured.resolve()
    return workspace / ".ocmo" / args.from_file.stem / "manifest.yaml"


def plan_artifact_dir(args: argparse.Namespace, workspace: Path, out_path: Path) -> Path:
    if isinstance(getattr(args, "out", None), Path):
        return out_path.parent
    return workspace / ".ocmo" / args.from_file.stem


def build_plan_command(args: argparse.Namespace, prompt: str, workspace: Path, interactive: bool = False) -> list[str]:
    command = ["opencode", "run", "--agent", PLAN_AGENT]
    if args.model:
        command += ["--model", args.model]
    if interactive:
        command.append("--interactive")
    command += ["--dir", str(workspace)]
    for read_file in args.read_files:
        command += ["--file", str(read_file)]
    command.append(prompt)
    return command


def run_plan_command(command: list[str], interactive: bool) -> tuple[int, str, str]:
    if not interactive:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return completed.returncode, completed.stdout, completed.stderr
    process = subprocess.Popen(command, stdin=None, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8", errors="replace")
    output_parts = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            output_parts.append(line)
        return process.wait(), "".join(output_parts), ""
    except KeyboardInterrupt:
        terminate_process_tree(process.pid, force=True)
        raise


def parse_plan_output(text: str, require_manifest_markers: bool) -> tuple[str, dict[Path, str]]:
    has_manifest_markers = MANIFEST_START in text or MANIFEST_END in text
    if require_manifest_markers or has_manifest_markers:
        manifest_text = extract_marked_manifest(text)
    elif FILE_START in text or FILE_END in text:
        raise OcmoError(f"planner file blocks require {MANIFEST_START} and {MANIFEST_END} markers")
    else:
        manifest_text = text
    return manifest_text, extract_plan_files(text)


def extract_marked_manifest(text: str) -> str:
    start = text.find(MANIFEST_START)
    end = text.find(MANIFEST_END)
    if start == -1 or end == -1 or end <= start:
        raise OcmoError(f"interactive planner output must contain {MANIFEST_START} and {MANIFEST_END} markers")
    manifest = text[start + len(MANIFEST_START) : end].strip()
    if not manifest:
        raise OcmoError("interactive planner returned an empty manifest")
    return manifest + "\n"


def extract_plan_files(text: str) -> dict[Path, str]:
    pattern = re.compile(rf"^{FILE_START}\s+(.+?)\s*$\r?\n(.*?)^\s*{FILE_END}\s*$", re.MULTILINE | re.DOTALL)
    files: dict[Path, str] = {}
    for match in pattern.finditer(text):
        relative_path = safe_plan_file_path(match.group(1).strip())
        content = match.group(2)
        if relative_path in files:
            raise OcmoError(f"duplicate generated file block: {relative_path}")
        files[relative_path] = content
    if FILE_START in text and not files:
        raise OcmoError(f"planner file blocks must use {FILE_START} <relative-path> and {FILE_END} markers")
    return files


def safe_plan_file_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or path.drive or ".." in path.parts or path == Path("."):
        raise OcmoError(f"generated file path must be relative and stay under the manifest directory: {value}")
    return path


def validate_generated_plan_files(manifest: dict[str, Any], manifest_path: Path, generated_files: dict[Path, str]) -> None:
    missing = []
    for template in plan_template_paths(manifest):
        template_path = Path(template)
        if template_path.is_absolute():
            if not template_path.exists():
                missing.append(template)
            continue
        relative_template = safe_plan_file_path(template)
        resolved_template = resolve_manifest_path(manifest_path, template)
        if relative_template not in generated_files and not resolved_template.exists():
            missing.append(template)
    if missing:
        raise OcmoError(f"planner referenced prompt template but did not generate it: {missing[0]}")


def plan_template_paths(manifest: dict[str, Any]) -> list[str]:
    templates = [str(manifest["prompt"]["template"])]
    for item in manifest.get("workUnits", []):
        runs = item.get("runs") if isinstance(item, dict) else None
        if not isinstance(runs, dict):
            continue
        steps = runs.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            prompt = step.get("prompt") if isinstance(step, dict) else None
            if isinstance(prompt, dict) and "template" in prompt:
                templates.append(str(prompt["template"]))
    return templates


def write_generated_plan_files(manifest_path: Path, generated_files: dict[Path, str]) -> None:
    for relative_path, content in generated_files.items():
        path = manifest_path.parent / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_planning_feedback_prompt(original_prompt: str, invalid_yaml: str, error: str, interactive: bool = False) -> str:
    final_instruction = (
        f"Return corrected YAML between {MANIFEST_START} and {MANIFEST_END} markers, followed by any required {FILE_START} file blocks."
        if interactive
        else f"Return corrected output only, no Markdown fences. If the manifest references generated prompt templates, wrap the YAML in {MANIFEST_START}/{MANIFEST_END} and include {FILE_START} file blocks."
    )
    return f"""{original_prompt}

Your previous response was invalid for ocmo/v1.

Validation error:
{error}

Previous YAML:
{invalid_yaml}

{final_instruction}
"""


def build_planning_prompt(source_prompt: str, read_files: list[Path], workspace: Path | None = None, interactive: bool = False, artifact_dir: Path | None = None) -> str:
    read_list = "\n".join(f"- {path}" for path in read_files) or "- none"
    workspace = workspace or Path.cwd().resolve()
    artifact_dir = artifact_dir or workspace / ".ocmo" / "planned-operation"
    prompt_dir = artifact_dir / "prompts"
    state_path = artifact_dir / "state.json"
    output_rule = (
        f"You may ask clarifying questions in the terminal before producing the manifest. When ready, output the final YAML between exact {MANIFEST_START} and {MANIFEST_END} markers, followed by any generated prompt template file blocks."
        if interactive
        else f"If the manifest references generated prompt template files, output a bundle: YAML between exact {MANIFEST_START} and {MANIFEST_END} markers, then one file block per generated file. If no files are generated, output YAML only. Never use Markdown fences."
    )
    return f"""Convert this mass-operation request into an ocmo/v1 YAML manifest.

Rules:
- {output_rule}
- The top-level schema field must be exactly: schema: ocmo/v1.
- Do not use apiVersion.
- operation.workspace must be exactly: {workspace}
- Use a common ocmo/v1 envelope: operation, runner, queue, policy, prompt, state, workUnits.
- Do not invent unsupported top-level sections or custom policy/runner/state fields.
- runner.command must usually be opencode.
- Use queue.concurrency: 1 when the request uses one git worktree or branch-changing workflow.
- You may use policy.worktree: single with queue.concurrency > 1 only when the request explicitly says work unit scopes are non-overlapping and safe to run in one shared workspace; the operator must run it with ocmo run --allow-shared-worktree-concurrency.
- Use queue.autoWorktrees.enabled: true only when the user wants ocmo to create one git worktree per work unit.
- Put task-specific fields under each work unit's payload.
- Do not include workUnits[].status; runtime status is stored in state.json.
- If one work unit needs multiple prompt phases, use workUnits[].runs.mode: sequential and put runs under workUnits[].runs.steps.
- Use agent: build for every explicit top-level or per-run agent value.
- Use per-run prompt.template values when different phases need different instructions.
- Use produces and consumes when one sequential phase should hand a deliberate file artifact to a later phase.
- Produced artifacts default to artifacts/<work-unit-id>/<step-id>/<artifact-id>.md and custom artifact paths must stay under artifacts/.
- Use the top-level prompt.template only when every run can share the same template.
- prompt.template and per-run prompt.template must be file paths, not inline YAML block text.
- Put generated prompt templates under: {prompt_dir}
- Use prompt template paths relative to the manifest file, for example: prompts/example.md
- Every generated prompt template referenced by the manifest must be included after the manifest as a file block:
  {FILE_START} prompts/example.md
  <template content>
  {FILE_END}
- Use this state path unless the user explicitly requested a different state location: {state_path}
- Use a state path relative to the manifest file, for example: state.json
- Use prompt.skills when a run must require opencode skills; list skill names without prose, for example [code-review].
- Use per-run prompt.skills when different sequential runs need different required skills.
- Do not use runs.mode: parallel; it is reserved for future support.
- If a required value is ambiguous and you can ask terminal questions, ask before producing final YAML.
- Do not leave NEEDS_DECISION in required runtime fields when the user can answer.
- If a required value is still ambiguous after questions, set it to NEEDS_DECISION instead of guessing.
- Refer to read-only source files only as evidence; do not modify them.

Canonical shape:
schema: ocmo/v1
operation:
  id: example-operation
  description: Example operation description.
  workspace: {workspace}
runner:
  command: opencode
  agent: build
  model: null
  attach: null
  timeoutSeconds: 14400
  dangerouslySkipPermissions: false
selection:
  default: uncompleted
queue:
  concurrency: 1
  order: manifest
  stopOnFailure: false
  autoWorktrees:
    enabled: false
policy:
  worktree: single
prompt:
  template: prompts/example.md
  skills: []
state:
  path: state.json
workUnits:
  - id: ITEM-001
    title: Example work unit
    payload: {{}}

Read-only source files available to inspect:
{read_list}

Request:
{source_prompt}
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
