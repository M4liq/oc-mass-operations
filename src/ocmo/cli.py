from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from typing import Any

from . import api as _api
from .api import *


class _CliModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        # Preserve existing monkeypatch behavior for callers that patch ocmo.cli.
        for module in _api._modules:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CliModule


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    suggestion = suggested_top_level_command(argv)
    if suggestion:
        print(suggestion, file=sys.stderr)
        return 2
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
    run_parser.add_argument("--fresh", action="store_true", help="Erase operation runtime data before selecting and running work units")
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
    validate_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory; defaults to manifest.yaml or a single .ocmo/*/manifest.yaml")

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
    plan_parser.add_argument("--reasoning-effort", dest="reasoning_effort", choices=list(REASONING_EFFORT_VALUES), help="Reasoning effort (minimal|low|medium|high) passed to opencode --variant")
    plan_parser.add_argument("--max-attempts", type=int, default=3, help="Maximum planner correction attempts")
    plan_parser.add_argument("--interactive", action="store_true", help="Allow the planner to ask terminal questions before returning marked YAML")
    plan_parser.add_argument("--dry-run", action="store_true", help="Print the planning prompt only")

    status_parser = operation_subparsers.add_parser("status", help="Show work unit and run status")
    status_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    status_parser.add_argument("--run-id", help="Show one detached run session")
    status_parser.add_argument("--all", action="store_true", help="Include inactive detached run sessions")
    status_parser.add_argument("--active-or-latest", action="store_true", help="Show all active operations, or the latest inactive operation when none are active")
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

    erase_parser = operation_subparsers.add_parser("erase", help="Terminate and erase generated operation runtime data")
    erase_parser.add_argument("manifest", nargs="?", type=Path, help="Manifest path or directory")
    erase_parser.add_argument("--run-id", help="Erase by detached run id")
    erase_parser.add_argument("--force", action="store_true", help="Required for non-interactive erase")

    for operation_parameterized_parser in (run_parser, validate_parser, render_parser, status_parser, list_parser, pause_parser, resume_parser, rerun_parser, kill_parser, erase_parser):
        add_parameter_arguments(operation_parameterized_parser)

    workflow_parser = subparsers.add_parser("workflow", help="Run operation manifests sequentially")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command", required=True)

    workflow_validate_parser = workflow_subparsers.add_parser("validate", help="Validate a workflow")
    workflow_validate_parser.add_argument("workflow", type=Path, help="Workflow path or directory")

    workflow_run_parser = workflow_subparsers.add_parser("run", help="Run workflow steps sequentially")
    workflow_run_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory; defaults to workflow.yaml")
    workflow_run_parser.add_argument("--select", help="Workflow step selection: all, pending, uncompleted, IDs, or ranges")
    workflow_run_parser.add_argument("--dry-run", action="store_true", help="Preview workflow execution without running operations")
    workflow_run_parser.add_argument("--fresh", action="store_true", help="Erase workflow state and per-step operation runtime data before running")
    workflow_run_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for foreground runs")
    workflow_run_parser.add_argument("--ui", choices=("auto", "live", "plain"), default="auto", help="Terminal UI for foreground operation steps")
    workflow_run_parser.add_argument("--detach", action="store_true", help="Start workflow in the background and return immediately")

    workflow_status_parser = workflow_subparsers.add_parser("status", help="Show workflow status")
    workflow_status_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory")
    workflow_status_parser.add_argument("--run-id", help="Show one detached workflow run session")
    workflow_status_parser.add_argument("--all", action="store_true", help="Include inactive detached workflow sessions")
    workflow_status_parser.add_argument("--active-or-latest", action="store_true", help="Show all active workflows, or the latest inactive workflow when none are active")
    workflow_status_parser.add_argument("--once", action="store_true", help="Print one status snapshot and exit")
    workflow_status_parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds for continuous status")

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

    workflow_erase_parser = workflow_subparsers.add_parser("erase", help="Terminate and erase generated operation runtime data for every workflow step")
    workflow_erase_parser.add_argument("workflow", nargs="?", type=Path, help="Workflow path or directory; defaults to workflow.yaml")
    workflow_erase_parser.add_argument("--force", action="store_true", help="Required for non-interactive erase")
    workflow_erase_parser.add_argument("--keep-workflow-state", action="store_true", help="Erase per-step operations only; keep workflow state and detached records")

    for workflow_parameterized_parser in (workflow_validate_parser, workflow_run_parser, workflow_status_parser, workflow_list_parser, workflow_pause_parser, workflow_resume_parser, workflow_rerun_parser, workflow_kill_parser, workflow_erase_parser):
        add_parameter_arguments(workflow_parameterized_parser)

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
                    False,
                    False,
                    command_params(args),
                    args.fresh,
                )
            )
        if args.command == "operation" and args.operation_command == "validate":
            manifest_path = infer_manifest_path(args.manifest)
            manifest = load_manifest(manifest_path, command_params(args))
            validate_manifest(manifest, manifest_path)
            warn_shared_worktree_concurrency(manifest)
            print(f"valid: {manifest_path}")
            return 0
        if args.command == "operation" and args.operation_command == "render":
            manifest_path = infer_manifest_path(args.manifest)
            manifest = load_manifest(manifest_path, command_params(args))
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
