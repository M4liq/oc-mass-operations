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
            if should_use_prompt_file(command):
                prompt_file = write_prompt_input(plan_prompt_input_path(out_path, attempt), prompt)
                command = build_plan_command(args, prompt, workspace, interactive, prompt_file)
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
        if getattr(self.args, "reasoning_effort", None):
            print(f"ocmo: planning reasoning-effort={self.args.reasoning_effort}", file=sys.stderr)
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
        effort = getattr(self.args, "reasoning_effort", None)
        effort_suffix = f" effort={effort}" if effort else ""
        return f"ocmo plan {phase} | agent={PLAN_AGENT} model={model}{effort_suffix} elapsed={elapsed} output={self.out_path}"


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


def plan_prompt_input_path(out_path: Path, attempt: int) -> Path:
    return (out_path.parent / PROMPT_INPUT_ROOT / f"planning-attempt-{attempt}.md").resolve()


def build_plan_command(args: argparse.Namespace, prompt: str, workspace: Path, interactive: bool = False, prompt_file: Path | None = None) -> list[str]:
    command = ["opencode", "run", "--agent", PLAN_AGENT]
    if args.model:
        command += ["--model", args.model]
    if getattr(args, "reasoning_effort", None):
        command += ["--variant", str(args.reasoning_effort)]
    if interactive:
        command.append("--interactive")
    command += ["--dir", str(workspace)]
    for read_file in args.read_files:
        command += ["--file", str(read_file)]
    if prompt_file is not None:
        command += ["--file", str(prompt_file)]
        prompt = PROMPT_FILE_MESSAGE
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
  reasoningEffort: null
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
