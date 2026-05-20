from __future__ import annotations

import argparse
import concurrent.futures
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


class OcmoError(Exception):
    pass


@dataclass(frozen=True)
class RunOptions:
    manifest_path: Path
    select: str | None
    concurrency: int | None
    dry_run: bool
    yes: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocmo", description="OC Mass Operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run operation items from a manifest")
    run_parser.add_argument("manifest", type=Path)
    run_parser.add_argument("--select", help="Selection: all, pending, uncompleted, IDs, or ranges")
    run_parser.add_argument("--concurrency", type=int, help="Override queue.concurrency")
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
    plan_parser.add_argument("--model", help="opencode model")
    plan_parser.add_argument("--agent", default="plan", help="opencode agent to use for planning")
    plan_parser.add_argument("--dry-run", action="store_true", help="Print the planning prompt only")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_manifest(RunOptions(args.manifest, args.select, args.concurrency, args.dry_run, args.yes))
        if args.command == "validate":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest, args.manifest)
            print(f"valid: {args.manifest}")
            return 0
        if args.command == "render":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest, args.manifest)
            for item in select_items(manifest, args.select):
                print(render_prompt(manifest, item, args.manifest))
                print("\n" + "=" * 80 + "\n")
            return 0
        if args.command == "plan":
            return plan_manifest(args)
    except OcmoError as exc:
        print(f"ocmo: {exc}", file=sys.stderr)
        return 2
    return 1


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


def validate_manifest(manifest: dict[str, Any], manifest_path: Path) -> None:
    if manifest.get("schema") != "ocmo/v1":
        raise OcmoError("manifest schema must be ocmo/v1")
    operation = require_mapping(manifest, "operation")
    require_string(operation, "id")
    workspace = require_string(operation, "workspace")
    if not resolve_manifest_path(manifest_path, workspace).exists():
        raise OcmoError(f"operation.workspace does not exist: {workspace}")
    runner = require_mapping(manifest, "runner")
    require_string(runner, "command")
    queue = require_mapping(manifest, "queue")
    concurrency = queue.get("concurrency", 1)
    if not isinstance(concurrency, int) or concurrency < 1:
        raise OcmoError("queue.concurrency must be a positive integer")
    policy = manifest.get("policy", {})
    if isinstance(policy, dict) and policy.get("worktree") == "single" and concurrency > 1:
        raise OcmoError("policy.worktree=single requires queue.concurrency=1")
    prompt = require_mapping(manifest, "prompt")
    template = require_string(prompt, "template")
    template_path = resolve_manifest_path(manifest_path, template)
    if not template_path.exists():
        raise OcmoError(f"prompt template not found: {template_path}")
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


def render_prompt(manifest: dict[str, Any], item: dict[str, Any], manifest_path: Path) -> str:
    template_path = resolve_manifest_path(manifest_path, manifest["prompt"]["template"])
    template = Template(template_path.read_text(encoding="utf-8"))
    operation = manifest.get("operation", {})
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
    }
    return template.safe_substitute(context)


def build_command(manifest: dict[str, Any], manifest_path: Path, prompt_text: str) -> list[str]:
    operation = manifest["operation"]
    runner = manifest["runner"]
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
    command += ["--dir", str(resolve_manifest_path(manifest_path, operation["workspace"])), prompt_text]
    return command


def run_manifest(options: RunOptions) -> int:
    manifest = load_manifest(options.manifest_path)
    validate_manifest(manifest, options.manifest_path)
    selected = select_items(manifest, options.select)
    concurrency = options.concurrency or manifest.get("queue", {}).get("concurrency", 1)
    if concurrency < 1:
        raise OcmoError("concurrency must be a positive integer")
    if manifest.get("policy", {}).get("worktree") == "single" and concurrency > 1:
        raise OcmoError("policy.worktree=single cannot run with concurrency > 1")
    if not selected:
        print("No items selected.")
        return 0

    if options.dry_run:
        for item in selected:
            prompt_text = render_prompt(manifest, item, options.manifest_path)
            command = build_command(manifest, options.manifest_path, prompt_text)
            print(f"# item {item['id']}")
            print(format_command(command))
            print("\n--- prompt ---\n")
            print(prompt_text)
            print("\n" + "=" * 80 + "\n")
        return 0

    if not options.yes:
        print(f"About to run {len(selected)} item(s) with concurrency={concurrency}.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    state = StateStore(state_path(manifest, options.manifest_path))
    state.ensure_operation(manifest)
    results: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_item, manifest, options.manifest_path, item, state) for item in selected]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    if any(code != 0 for code in results):
        return 1
    return 0


def run_item(manifest: dict[str, Any], manifest_path: Path, item: dict[str, Any], state: "StateStore") -> int:
    item_id = str(item["id"])
    prompt_text = render_prompt(manifest, item, manifest_path)
    command = build_command(manifest, manifest_path, prompt_text)
    state.mark(item_id, "running", {"startedAt": utc_now(), "command": command_without_prompt(command)})
    print(f"[{item_id}] starting")
    completed = subprocess.run(command, cwd=str(resolve_manifest_path(manifest_path, manifest["operation"]["workspace"])))
    if completed.returncode == 0:
        state.mark(item_id, "completed", {"completedAt": utc_now(), "exitCode": 0})
        print(f"[{item_id}] completed")
    else:
        state.mark(item_id, "failed", {"completedAt": utc_now(), "exitCode": completed.returncode})
        print(f"[{item_id}] failed: exit {completed.returncode}")
    return completed.returncode


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
    missing = [path for path in args.read_files if not path.exists()]
    if missing:
        raise OcmoError(f"read-only source file not found: {missing[0]}")
    source_prompt = args.from_file.read_text(encoding="utf-8")
    planning_prompt = build_planning_prompt(source_prompt, args.read_files)
    if args.dry_run:
        print(planning_prompt)
        return 0
    command = ["opencode", "run", "--agent", args.agent]
    if args.model:
        command += ["--model", args.model]
    for read_file in args.read_files:
        command += ["--file", str(read_file)]
    command.append(planning_prompt)
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stdout, end="")
        print(completed.stderr, end="", file=sys.stderr)
        return completed.returncode
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(completed.stdout, encoding="utf-8")
    print(f"wrote: {args.out}")
    return 0


def build_planning_prompt(source_prompt: str, read_files: list[Path]) -> str:
    read_list = "\n".join(f"- {path}" for path in read_files) or "- none"
    return f"""Convert this mass-operation request into an ocmo/v1 YAML manifest.

Rules:
- Output YAML only, no Markdown fences.
- Keep operation.kind generic unless the user explicitly named a stable kind.
- Use a common ocmo/v1 envelope: operation, runner, queue, policy, prompt, state, items.
- Use queue.concurrency: 1 when the request uses one git worktree or branch-changing workflow.
- Put task-specific fields under each item's payload.
- If a required value is ambiguous, set it to NEEDS_DECISION instead of guessing.
- Refer to read-only source files only as evidence; do not modify them.

Read-only source files available to inspect:
{read_list}

Request:
{source_prompt}
"""


if __name__ == "__main__":
    raise SystemExit(main())
