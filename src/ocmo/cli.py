from __future__ import annotations

import argparse
import concurrent.futures
import os
import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import yaml


DONE_STATUSES = {"completed", "done", "skipped"}
MANIFEST_START = "OCMO_MANIFEST_START"
MANIFEST_END = "OCMO_MANIFEST_END"


class OcmoError(Exception):
    pass


@dataclass(frozen=True)
class RunOptions:
    manifest_path: Path
    select: str | None
    concurrency: int | None
    timeout_seconds: int | None
    dry_run: bool
    yes: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocmo", description="OC Mass Operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run operation items from a manifest")
    run_parser.add_argument("manifest", type=Path)
    run_parser.add_argument("--select", help="Selection: all, pending, uncompleted, IDs, or ranges")
    run_parser.add_argument("--concurrency", type=int, help="Override queue.concurrency")
    run_parser.add_argument("--timeout-seconds", type=int, help="Override runner.timeoutSeconds for each item")
    run_parser.add_argument("--dry-run", action="store_true", help="Print commands/prompts without running opencode")
    run_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for non-dry runs")

    validate_parser = subparsers.add_parser("validate", help="Validate a manifest")
    validate_parser.add_argument("manifest", type=Path)

    render_parser = subparsers.add_parser("render", help="Render prompts for selected items")
    render_parser.add_argument("manifest", type=Path)
    render_parser.add_argument("--select", help="Selection: all, pending, uncompleted, IDs, or ranges")

    plan_parser = subparsers.add_parser("plan", help="Ask opencode to convert a prompt into an ocmo manifest")
    plan_parser.add_argument("--from", dest="from_file", required=True, type=Path, help="Natural-language operation prompt")
    plan_parser.add_argument("--read", dest="read_files", action="append", default=[], type=Path, help="Read-only source file to attach/inspect")
    plan_parser.add_argument("--out", required=True, type=Path, help="Manifest output path")
    plan_parser.add_argument("--workspace", type=Path, help="Target workspace for planning; defaults to the current directory")
    plan_parser.add_argument("--model", help="opencode model")
    plan_parser.add_argument("--agent", default="plan", help="opencode agent to use for planning")
    plan_parser.add_argument("--max-attempts", type=int, default=3, help="Maximum planner correction attempts")
    plan_parser.add_argument("--interactive", action="store_true", help="Allow the planner to ask terminal questions before returning marked YAML")
    plan_parser.add_argument("--dry-run", action="store_true", help="Print the planning prompt only")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_manifest(RunOptions(args.manifest, args.select, args.concurrency, args.timeout_seconds, args.dry_run, args.yes))
        if args.command == "validate":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest, args.manifest)
            print(f"valid: {args.manifest}")
            return 0
        if args.command == "render":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest, args.manifest)
            for item in select_items(manifest, args.select):
                runs = item_runs(manifest, item)
                for run in runs:
                    print(f"# item {item['id']} / run {run['id']}")
                    print(render_prompt(manifest, item, args.manifest, run=run, runs=runs))
                    print("\n" + "=" * 80 + "\n")
            return 0
        if args.command == "plan":
            return plan_manifest(args)
    except OcmoError as exc:
        print(f"ocmo: {exc}", file=sys.stderr)
        return 2
    return 1  # pragma: no cover


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def validate_manifest(manifest: dict[str, Any], manifest_path: Path) -> None:
    validate_manifest_schema(manifest, manifest_path)
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
    for index, item in enumerate(manifest["items"], start=1):
        validate_item_run_paths(item, manifest_path, index)


def validate_manifest_schema(manifest: dict[str, Any], manifest_path: Path) -> None:
    if manifest.get("schema") != "ocmo/v1":
        raise OcmoError("manifest schema must be ocmo/v1")
    operation = require_mapping(manifest, "operation")
    require_string(operation, "id")
    require_string(operation, "workspace")
    runner = require_mapping(manifest, "runner")
    require_string(runner, "command")
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
    if isinstance(policy, dict) and policy.get("worktree") == "single" and concurrency > 1:
        raise OcmoError("policy.worktree=single requires queue.concurrency=1")
    if isinstance(policy, dict) and policy.get("worktree") == "single" and auto_worktrees["enabled"]:
        raise OcmoError("policy.worktree=single cannot be used with queue.autoWorktrees.enabled=true")
    prompt = require_mapping(manifest, "prompt")
    validate_template_value(require_string(prompt, "template"), "prompt.template")
    normalize_skills(prompt.get("skills"), "prompt.skills")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise OcmoError("items must be a non-empty list")
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise OcmoError(f"items[{index}] must be a mapping")
        item_id = item.get("id")
        if item_id is None:
            raise OcmoError(f"items[{index}].id is required")
        item_key = str(item_id)
        if item_key in seen:
            raise OcmoError(f"duplicate item id: {item_key}")
        seen.add(item_key)
        validate_item_runs(manifest, item, manifest_path, index)


def validate_template_value(value: str, field: str) -> None:
    if "\n" in value or "\r" in value:
        raise OcmoError(f"{field} must be a file path, not inline template text")


def validate_item_runs(manifest: dict[str, Any], item: dict[str, Any], manifest_path: Path, item_index: int) -> None:
    runs = item.get("runs")
    if runs is None:
        return
    if not isinstance(runs, dict):
        raise OcmoError(f"items[{item_index}].runs must be a mapping")
    mode = runs.get("mode", "sequential")
    if mode != "sequential":
        raise OcmoError(f"items[{item_index}].runs.mode must be sequential")
    steps = runs.get("steps")
    if not isinstance(steps, list) or not steps:
        raise OcmoError(f"items[{item_index}].runs.steps must be a non-empty list")
    seen = set()
    for step_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise OcmoError(f"items[{item_index}].runs.steps[{step_index}] must be a mapping")
        run_id = step.get("id")
        if run_id is None or not str(run_id).strip():
            raise OcmoError(f"items[{item_index}].runs.steps[{step_index}].id is required")
        run_key = str(run_id)
        if run_key in seen:
            raise OcmoError(f"duplicate run id for item {item['id']}: {run_key}")
        seen.add(run_key)
        timeout_seconds = step.get("timeoutSeconds")
        if timeout_seconds is not None and (not isinstance(timeout_seconds, int) or timeout_seconds < 1):
            raise OcmoError(f"items[{item_index}].runs.steps[{step_index}].timeoutSeconds must be a positive integer")
        prompt = step.get("prompt")
        if prompt is not None:
            if not isinstance(prompt, dict):
                raise OcmoError(f"items[{item_index}].runs.steps[{step_index}].prompt must be a mapping")
            template = prompt.get("template")
            if template is not None:
                if not isinstance(template, str) or not template.strip():
                    raise OcmoError("template must be a non-empty string")
                validate_template_value(template, f"items[{item_index}].runs.steps[{step_index}].prompt.template")
            normalize_skills(prompt.get("skills"), f"items[{item_index}].runs.steps[{step_index}].prompt.skills")


def validate_item_run_paths(item: dict[str, Any], manifest_path: Path, item_index: int) -> None:
    runs = item.get("runs")
    if runs is None:
        return
    for step_index, step in enumerate(runs["steps"], start=1):
        prompt = step.get("prompt") or {}
        template = prompt.get("template")
        if template is not None:
            template_path = resolve_manifest_path(manifest_path, template)
            if not template_path.exists():
                raise OcmoError(f"prompt template not found: {template_path}")


def item_runs(manifest: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    runs = item.get("runs")
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
        if key not in {"id", "index", "mode", "prompt"}:
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
        str(config.get("branchPattern", "ocmo/{operation_id}/{item_id}")).format(operation_id="operation", item_id="item", item_slug="item")
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


def select_items(manifest: dict[str, Any], selector: str | None) -> list[dict[str, Any]]:
    items = manifest["items"]
    selector = selector or manifest.get("selection", {}).get("default") or "uncompleted"
    selector = selector.strip()
    if selector == "all":
        return items
    if selector == "pending":
        return [item for item in items if str(item.get("status", "pending")).lower() == "pending"]
    if selector == "uncompleted":
        return [item for item in items if str(item.get("status", "pending")).lower() not in DONE_STATUSES]

    requested = expand_selector(selector)
    selected = [item for item in items if str(item.get("id")) in requested]
    missing = requested - {str(item.get("id")) for item in selected}
    if missing:
        raise OcmoError(f"selection did not match manifest item ids: {', '.join(sorted(missing))}")
    return selected


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
    run = run or item_runs(manifest, item)[0]
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
        "item_json": json.dumps(item, indent=2, ensure_ascii=False),
        "payload_json": json.dumps(item.get("payload", {}), indent=2, ensure_ascii=False),
        "operation_id": str(operation.get("id", "")),
        "operation_kind": str(operation.get("kind", "generic")),
        "workspace": str(operation.get("workspace", "")),
        "item_id": str(item.get("id", "")),
        "item_title": str(item.get("title", "")),
        "item_file": str(item.get("file", "")),
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
    rendered = template.safe_substitute(context)
    if rendered_skill_instructions:
        return f"{rendered_skill_instructions}\n\n{rendered}"
    return rendered


def build_command(manifest: dict[str, Any], manifest_path: Path, prompt_text: str, run_dir: Path | None = None, runner: dict[str, Any] | None = None) -> list[str]:
    operation = manifest["operation"]
    runner = runner or manifest["runner"]
    command = [runner.get("command", "opencode"), runner.get("mode", "run")]
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
    command += ["--dir", str(run_dir or resolve_manifest_path(manifest_path, operation["workspace"])), prompt_text]
    return command


def run_manifest(options: RunOptions) -> int:
    manifest = load_manifest(options.manifest_path)
    validate_manifest(manifest, options.manifest_path)
    selected = select_items(manifest, options.select)
    concurrency = options.concurrency if options.concurrency is not None else manifest.get("queue", {}).get("concurrency", 1)
    timeout_seconds = options.timeout_seconds
    auto_worktrees = auto_worktrees_config(manifest)
    if concurrency < 1:
        raise OcmoError("concurrency must be a positive integer")
    if timeout_seconds is not None and timeout_seconds < 1:
        raise OcmoError("timeout must be a positive integer")
    if manifest.get("policy", {}).get("worktree") == "single" and concurrency > 1:
        raise OcmoError("policy.worktree=single cannot run with concurrency > 1")
    if manifest.get("policy", {}).get("worktree") == "single" and auto_worktrees["enabled"]:  # pragma: no cover
        raise OcmoError("policy.worktree=single cannot be used with queue.autoWorktrees.enabled=true")
    if not selected:
        print("No items selected.")
        return 0

    if options.dry_run:
        for item in selected:
            execution = worktree_execution(manifest, options.manifest_path, item) if auto_worktrees["enabled"] else {}
            run_dir = Path(execution["worktreePath"]) if execution else None
            runs = item_runs(manifest, item)
            for run in runs:
                runner = effective_runner(manifest, run)
                run_timeout = timeout_seconds if timeout_seconds is not None else runner.get("timeoutSeconds")
                prompt_text = render_prompt(manifest, item, options.manifest_path, execution, run, runs)
                command = build_command(manifest, options.manifest_path, prompt_text, run_dir, runner)
                print(f"# item {item['id']} / run {run['id']}")
                if execution:
                    print(f"# worktree: {execution['worktreePath']}")
                    print(f"# branch: {execution['branchName']}")
                print(format_command(command))
                if run_timeout:
                    print(f"# timeout: {run_timeout} seconds")
                print("\n--- prompt ---\n")
                print(prompt_text)
                print("\n" + "=" * 80 + "\n")
        return 0

    if not options.yes:
        timeout_text = f", timeout={timeout_seconds}s" if timeout_seconds else ""
        worktree_text = ", autoWorktrees=true" if auto_worktrees["enabled"] else ""
        print(f"About to run {len(selected)} item(s) with concurrency={concurrency}{timeout_text}{worktree_text}.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    state = StateStore(state_path(manifest, options.manifest_path))
    state.ensure_operation(manifest)
    results: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(run_item, manifest, options.manifest_path, item, state, timeout_seconds, auto_worktrees): str(item["id"])
            for item in selected
        }
        for future in concurrent.futures.as_completed(futures):
            item_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": f"unexpected worker error: {exc}"})
                print(f"[{item_id}] unexpected worker error: {exc}")
                results.append(1)

    if any(code != 0 for code in results):
        return 1
    return 0


def run_item(manifest: dict[str, Any], manifest_path: Path, item: dict[str, Any], state: "StateStore", timeout_seconds: int | None, auto_worktrees: dict[str, Any]) -> int:
    item_id = str(item["id"])
    execution: dict[str, Any] = {}
    try:
        execution = worktree_execution(manifest, manifest_path, item) if auto_worktrees["enabled"] else {}
        run_dir = Path(execution["worktreePath"]) if execution else resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])
        if execution:
            worktree_code = prepare_worktree(manifest, manifest_path, item, execution, auto_worktrees, state)
            if worktree_code != 0:
                return worktree_code
        runs = item_runs(manifest, item)
    except (OSError, OcmoError) as exc:
        state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        print(f"[{item_id}] failed before start: {exc}")
        return 1
    state.mark(item_id, "running", {"startedAt": utc_now(), "runCount": len(runs), **execution})
    for run in runs:
        runner = effective_runner(manifest, run)
        run_timeout = timeout_seconds if timeout_seconds is not None else runner.get("timeoutSeconds")
        try:
            prompt_text = render_prompt(manifest, item, manifest_path, execution, run, runs)
            command = build_command(manifest, manifest_path, prompt_text, run_dir, runner)
        except (OSError, OcmoError) as exc:
            state.mark_run(item_id, str(run["id"]), "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc)})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
            print(f"[{item_id}/{run['id']}] failed before start: {exc}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False)
            return cleanup_code or 1
        state.mark_run(item_id, str(run["id"]), "running", {"startedAt": utc_now(), "command": command_without_prompt(command), "timeoutSeconds": run_timeout})
        print(f"[{item_id}/{run['id']}] starting")
        try:
            completed = subprocess.run(command, cwd=str(run_dir), timeout=run_timeout)
        except subprocess.TimeoutExpired:
            state.mark_run(item_id, str(run["id"]), "timed_out", {"completedAt": utc_now(), "exitCode": None, "timeoutSeconds": run_timeout})
            state.mark(item_id, "timed_out", {"completedAt": utc_now(), "exitCode": None, "timeoutSeconds": run_timeout, **execution})
            print(f"[{item_id}/{run['id']}] timed out after {run_timeout} seconds")
            cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False)
            return 124
        except OSError as exc:
            state.mark_run(item_id, str(run["id"]), "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc)})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
            print(f"[{item_id}/{run['id']}] failed to start: {exc}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False)
            return cleanup_code or 1
        if completed.returncode == 0:
            state.mark_run(item_id, str(run["id"]), "completed", {"completedAt": utc_now(), "exitCode": 0})
            print(f"[{item_id}/{run['id']}] completed")
        else:
            state.mark_run(item_id, str(run["id"]), "failed", {"completedAt": utc_now(), "exitCode": completed.returncode})
            state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": completed.returncode, **execution})
            print(f"[{item_id}/{run['id']}] failed: exit {completed.returncode}")
            cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=False)
            return cleanup_code or completed.returncode
    state.mark(item_id, "completed", {"completedAt": utc_now(), "exitCode": 0, **execution})
    print(f"[{item_id}] completed")
    cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, auto_worktrees, state, success=True)
    if cleanup_code != 0:
        state.mark(item_id, "cleanup_failed", {"completedAt": utc_now(), "exitCode": cleanup_code, **execution})
        print(f"[{item_id}] cleanup failed: exit {cleanup_code}")
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
    branch_pattern = config.get("branchPattern", "ocmo/{operation_id}/{item_id}")
    try:
        branch_name = branch_pattern.format(operation_id=slugify(operation_id), item_id=item_slug, item_slug=item_slug)
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


def prepare_worktree(manifest: dict[str, Any], manifest_path: Path, item: dict[str, Any], execution: dict[str, Any], config: dict[str, Any], state: "StateStore") -> int:
    item_id = str(item["id"])
    source_workspace = Path(execution["sourceWorkspace"])
    worktree_path = Path(execution["worktreePath"])
    branch_name = execution["branchName"]
    base_branch = execution["baseBranch"]
    state.mark(item_id, "creating_worktree", {"worktreeStartedAt": utc_now(), **execution})
    print(f"[{item_id}] creating worktree {worktree_path}")
    if worktree_path.exists():
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": f"worktree path already exists: {worktree_path}", **execution})
        print(f"[{item_id}] worktree path already exists: {worktree_path}")
        return 1
    try:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        print(f"[{item_id}] could not create worktree parent: {exc}")
        return 1
    command = ["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_branch]
    try:
        completed = subprocess.run(command, cwd=str(source_workspace))
    except OSError as exc:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": 1, "error": str(exc), **execution})
        print(f"[{item_id}] worktree creation failed: {exc}")
        return 1
    if completed.returncode != 0:
        state.mark(item_id, "worktree_failed", {"completedAt": utc_now(), "exitCode": completed.returncode, **execution})
        print(f"[{item_id}] worktree creation failed: exit {completed.returncode}")
        return completed.returncode
    state.mark(item_id, "worktree_ready", {"worktreeCompletedAt": utc_now(), **execution})
    setup_code = run_scripts("setup", normalize_scripts(config.get("setup"), "queue.autoWorktrees.setup"), worktree_path, execution, item_id)
    if setup_code != 0:
        state.mark(item_id, "setup_failed", {"completedAt": utc_now(), "exitCode": setup_code, **execution})
        cleanup_code = cleanup_worktree(manifest, manifest_path, item, execution, config, state, success=False)
        if cleanup_code != 0:
            return cleanup_code
        return setup_code
    if config.get("setup"):
        state.mark(item_id, "setup_completed", {"setupCompletedAt": utc_now(), **execution})
    return 0


def cleanup_worktree(manifest: dict[str, Any], manifest_path: Path, item: dict[str, Any], execution: dict[str, Any], config: dict[str, Any], state: "StateStore", success: bool) -> int:
    if not execution:
        return 0
    cleanup = config.get("cleanup", "never")
    should_cleanup = cleanup == "always" or (cleanup == "onSuccess" and success)
    if not should_cleanup:
        return 0
    item_id = str(item["id"])
    worktree_path = Path(execution["worktreePath"])
    teardown_code = run_scripts("teardown", normalize_scripts(config.get("teardown"), "queue.autoWorktrees.teardown"), worktree_path, execution, item_id)
    if teardown_code != 0:
        state.patch(item_id, {"teardownStatus": "failed", "teardownCompletedAt": utc_now(), "teardownExitCode": teardown_code, **execution})
        return teardown_code
    source_workspace = resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])
    try:
        completed = subprocess.run(["git", "worktree", "remove", str(worktree_path)], cwd=str(source_workspace))
    except OSError as exc:
        state.patch(item_id, {"worktreeStatus": "remove_failed", "worktreeRemoveExitCode": 1, "worktreeRemoveError": str(exc), **execution})
        return 1
    if completed.returncode == 0:
        state.patch(item_id, {"worktreeStatus": "removed", "worktreeRemovedAt": utc_now(), **execution})
        return 0
    else:
        state.patch(item_id, {"worktreeStatus": "remove_failed", "worktreeRemoveExitCode": completed.returncode, **execution})
        return completed.returncode


def run_scripts(kind: str, scripts: list[str], cwd: Path, execution: dict[str, Any], item_id: str) -> int:
    for script in scripts:
        print(f"[{item_id}] {kind}: {script}")
        try:
            completed = subprocess.run(script, cwd=str(cwd), shell=True, env=worktree_env(execution))
        except OSError as exc:
            print(f"[{item_id}] {kind} failed: {exc}")
            return 1
        if completed.returncode != 0:
            print(f"[{item_id}] {kind} failed: exit {completed.returncode}")
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


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def ensure_operation(self, manifest: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("schema", "ocmo-state/v1")
            data.setdefault("operationId", manifest["operation"]["id"])
            data.setdefault("items", {})
            data["updatedAt"] = utc_now()
            self._write(data)

    def mark(self, item_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("items", {})
            item_state = data["items"].setdefault(item_id, {})
            item_state.update(patch)
            item_state["status"] = status
            data["updatedAt"] = utc_now()
            self._write(data)

    def mark_run(self, item_id: str, run_id: str, status: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("items", {})
            item_state = data["items"].setdefault(item_id, {})
            runs = item_state.setdefault("runs", {})
            run_state = runs.setdefault(run_id, {})
            run_state.update(patch)
            run_state["status"] = status
            data["updatedAt"] = utc_now()
            self._write(data)

    def patch(self, item_id: str, patch: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data.setdefault("items", {})
            item_state = data["items"].setdefault(item_id, {})
            item_state.update(patch)
            data["updatedAt"] = utc_now()
            self._write(data)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
    configured_interactive = getattr(args, "interactive", False)
    interactive = configured_interactive if isinstance(configured_interactive, bool) else False
    source_prompt = args.from_file.read_text(encoding="utf-8")
    planning_prompt = build_planning_prompt(source_prompt, args.read_files, workspace, interactive)
    if args.dry_run:
        print(planning_prompt)
        return 0
    print(f"ocmo: planning with agent={args.agent} model={args.model or '<opencode-default>'}", file=sys.stderr)
    print(f"ocmo: planning workspace={workspace}", file=sys.stderr)
    feedback = None
    previous_output = ""
    for attempt in range(1, max_attempts + 1):
        print(f"ocmo: planner attempt {attempt}/{max_attempts}", file=sys.stderr)
        prompt = planning_prompt if feedback is None else build_planning_feedback_prompt(planning_prompt, previous_output, feedback, interactive)
        command = build_plan_command(args, prompt, workspace, interactive)
        returncode, output, error_output = run_plan_command(command, interactive)
        if returncode != 0:
            print(output, end="")
            print(error_output, end="", file=sys.stderr)
            return returncode
        previous_output = output
        try:
            manifest_text = extract_marked_manifest(output) if interactive else output
            manifest = load_manifest_text(manifest_text)
            validate_manifest_schema(manifest, args.out)
        except (OcmoError, yaml.YAMLError) as exc:
            feedback = str(exc)
            print(f"ocmo: planner output invalid on attempt {attempt}/{max_attempts}: {feedback}", file=sys.stderr)
            continue
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(manifest_text, encoding="utf-8")
        print(f"wrote: {args.out}")
        return 0
    raise OcmoError(f"planner did not produce a valid ocmo/v1 manifest after {max_attempts} attempts: {feedback}")


def plan_workspace(args: argparse.Namespace) -> Path:
    configured = getattr(args, "workspace", None)
    if isinstance(configured, Path):
        return configured.resolve()
    return Path.cwd().resolve()


def build_plan_command(args: argparse.Namespace, prompt: str, workspace: Path, interactive: bool = False) -> list[str]:
    command = ["opencode", "run", "--agent", args.agent]
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
        completed = subprocess.run(command, capture_output=True, text=True)
        return completed.returncode, completed.stdout, completed.stderr
    process = subprocess.Popen(command, stdin=None, stdout=subprocess.PIPE, stderr=None, text=True)
    output_parts = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        output_parts.append(line)
    return process.wait(), "".join(output_parts), ""


def extract_marked_manifest(text: str) -> str:
    start = text.find(MANIFEST_START)
    end = text.find(MANIFEST_END)
    if start == -1 or end == -1 or end <= start:
        raise OcmoError(f"interactive planner output must contain {MANIFEST_START} and {MANIFEST_END} markers")
    manifest = text[start + len(MANIFEST_START) : end].strip()
    if not manifest:
        raise OcmoError("interactive planner returned an empty manifest")
    return manifest + "\n"


def build_planning_feedback_prompt(original_prompt: str, invalid_yaml: str, error: str, interactive: bool = False) -> str:
    final_instruction = (
        f"Return corrected YAML between {MANIFEST_START} and {MANIFEST_END} markers."
        if interactive
        else "Return corrected YAML only, no Markdown fences."
    )
    return f"""{original_prompt}

Your previous response was invalid for ocmo/v1.

Validation error:
{error}

Previous YAML:
{invalid_yaml}

{final_instruction}
"""


def build_planning_prompt(source_prompt: str, read_files: list[Path], workspace: Path | None = None, interactive: bool = False) -> str:
    read_list = "\n".join(f"- {path}" for path in read_files) or "- none"
    workspace = workspace or Path.cwd().resolve()
    output_rule = (
        f"You may ask clarifying questions in the terminal before producing the manifest. When ready, output the final YAML between exact {MANIFEST_START} and {MANIFEST_END} markers."
        if interactive
        else "Output YAML only, no Markdown fences."
    )
    return f"""Convert this mass-operation request into an ocmo/v1 YAML manifest.

Rules:
- {output_rule}
- The top-level schema field must be exactly: schema: ocmo/v1.
- Do not use apiVersion.
- operation.workspace must be exactly: {workspace}
- Keep operation.kind generic unless the user explicitly named a stable kind.
- Use a common ocmo/v1 envelope: operation, runner, queue, policy, prompt, state, items.
- Do not invent unsupported top-level sections or custom policy/runner/state fields.
- runner.command must usually be opencode.
- runner.mode must usually be run.
- Use queue.concurrency: 1 when the request uses one git worktree or branch-changing workflow.
- Use queue.autoWorktrees.enabled: true only when the user wants ocmo to create one git worktree per item.
- Put task-specific fields under each item's payload.
- If one item needs multiple agents or prompt phases, use items[].runs.mode: sequential and put runs under items[].runs.steps.
- Use per-run prompt.template values when different agents need different instructions.
- Use the top-level prompt.template only when every run can share the same template.
- prompt.template and per-run prompt.template must be file paths, not inline YAML block text.
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
  kind: generic
  description: Example operation description.
  workspace: {workspace}
runner:
  command: opencode
  mode: run
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
  template: .ocmo/prompts/example.md
  skills: []
state:
  path: .ocmo/state/example-operation.json
items:
  - id: ITEM-001
    title: Example item
    status: pending
    payload: {{}}

Read-only source files available to inspect:
{read_list}

Request:
{source_prompt}
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
