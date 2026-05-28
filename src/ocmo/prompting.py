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
        "run_reasoning_effort": str(runner.get("reasoningEffort", "")),
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
    if use_rich_stdout():
        print_rich_prompt_previews(previews, show_all)
        return
    for preview in compact_prompt_previews(previews, show_all):
        if isinstance(preview, int):
            print(f"# ... {preview} prompt(s) omitted ...")
            print("\n" + "=" * 80 + "\n")
            continue
        print(f"# work unit {preview.work_unit_id} / run {preview.run_id}")
        print(format_plain_prompt_preview(preview))
        print("\n" + "=" * 80 + "\n")


def format_plain_prompt_preview(preview: PromptPreview) -> str:
    if not preview.command and not preview.details:
        return preview.text
    lines = []
    if preview.command:
        lines.append(preview.command)
    lines.extend(f"# {label}: {value}" for label, value in preview.details)
    lines.append("")
    lines.append("--- prompt ---")
    lines.append("")
    lines.append(preview.text)
    return "\n".join(lines)


def use_rich_stdout() -> bool:
    if not sys.stdout.isatty():
        return False
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def print_rich_prompt_previews(previews: list[PromptPreview], show_all: bool) -> None:  # pragma: no cover
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    for preview in compact_prompt_previews(previews, show_all):
        if isinstance(preview, int):
            console.print(Panel(f"{preview} prompt(s) omitted; pass --all to print everything", title="Omitted", border_style="yellow"))
            continue
        renderables: list[Any] = []
        if preview.command or preview.details:
            table = Table.grid(padding=(0, 1))
            table.add_column(style="bold cyan", no_wrap=True)
            table.add_column()
            if preview.command:
                table.add_row("command", Text(preview.command, overflow="fold"))
            for label, value in preview.details:
                table.add_row(label, Text(value, overflow="fold"))
            renderables.append(table)
        renderables.append(Markdown(preview.text))
        console.print(Panel(Group(*renderables), title=f"work unit {preview.work_unit_id} / run {preview.run_id}", border_style="cyan"))


def build_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    operation = manifest["operation"]
    runner = runner or manifest["runner"]
    command = [runner.get("command", "opencode"), "run"]
    if runner.get("agent"):
        command += ["--agent", str(runner["agent"])]
    if runner.get("model"):
        command += ["--model", str(runner["model"])]
    if runner.get("reasoningEffort"):
        command += ["--variant", str(runner["reasoningEffort"])]
    if runner.get("attach"):
        command += ["--attach", str(runner["attach"])]
    if runner.get("title"):
        command += ["--title", str(runner["title"])]
    if runner.get("dangerouslySkipPermissions"):
        command.append("--dangerously-skip-permissions")
    command += ["--format", "json"]
    if prompt_file is not None:
        command += ["--file", str(prompt_file)]
        prompt_text = PROMPT_FILE_MESSAGE
    command += ["--dir", str(run_dir or resolve_manifest_path(manifest_path, operation["workspace"])), prompt_text]
    return command


def build_resume_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    session_id: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    command = build_command(manifest, manifest_path, prompt_text, run_dir, runner, prompt_file)
    command[2:2] = ["--session", session_id]
    return command


def command_line_length(command: list[str]) -> int:
    return sum(len(part) for part in command) + max(len(command) - 1, 0)


def should_use_prompt_file(command: list[str]) -> bool:
    return command_line_length(command) > PROMPT_ARG_MAX_CHARS


def prompt_input_path(manifest_path: Path, item_id: str, run_id: str) -> Path:
    return (manifest_path.parent / PROMPT_INPUT_ROOT / slugify(item_id) / f"{slugify(run_id)}.md").resolve()


def write_prompt_input(path: Path, prompt_text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt_text, encoding="utf-8")
    return path


def build_transport_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    prompt_file: Path,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
) -> tuple[list[str], Path | None]:
    command = build_command(manifest, manifest_path, prompt_text, run_dir, runner)
    if not should_use_prompt_file(command):
        return command, None
    written_prompt_file = write_prompt_input(prompt_file, prompt_text)
    return build_command(manifest, manifest_path, prompt_text, run_dir, runner, written_prompt_file), written_prompt_file
