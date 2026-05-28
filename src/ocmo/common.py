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
PROMPT_INPUT_ROOT = "prompt-inputs"
PROMPT_ARG_MAX_CHARS = 24_000
PROMPT_FILE_MESSAGE = "Read the attached prompt file and follow its instructions exactly."
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
KNOWN_MODEL_PROVIDERS = ("opencode", "github-copilot", "openai", "anthropic")
REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high")
OCMO_SKILL_NAME = "ocmo"
OLD_OCMO_SKILL_NAMES = ("ocmo-plan-grill",)
OCMO_SKILL_RESOURCE = "resources/skill"
OCMO_COMMAND_RESOURCE = "resources/commands"
PARAM_PLACEHOLDER_RE = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
PARAM_PLACEHOLDER_EXACT_RE = re.compile(r"^\s*\{\{\s*params\.([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}\s*$")
PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
RESOLVED_PARAMS_KEY = "__ocmoResolvedParams"


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
    params: dict[str, Any] = field(default_factory=dict)


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
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptPreview:
    work_unit_id: str
    run_id: str
    text: str
    command: str | None = None
    details: list[tuple[str, str]] = field(default_factory=list)
