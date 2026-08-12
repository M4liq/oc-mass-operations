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

def install_skill(force: bool = False) -> list[Path]:
    source_dir = bundled_skill_dir()
    source_files = bundled_skill_files(source_dir)
    installed: list[Path] = []
    available = False
    for target in skill_provider_targets():
        skills_dir = target["skills_dir"]
        if skills_dir is None or not skills_dir.parent.exists():
            reason = "could not determine home directory" if skills_dir is None else f"{skills_dir.parent} not found"
            print(f"{target['label']} not available ({reason}); skipping")
            continue
        available = True
        installed.append(install_skill_to_target(target, source_files))
    if not available:
        print("no supported agent directories found; nothing installed")
    return installed


def install_skill_to_target(target: dict[str, Any], source_files: dict[Path, Any]) -> Path:
    skills_dir = target["skills_dir"]
    destination_dir = skills_dir / OCMO_SKILL_NAME
    destination = destination_dir / "SKILL.md"
    if destination.exists() and installed_skill_matches(destination_dir, source_files):
        print(f"already installed: {destination}")
        install_skill_commands(target["commands_dir"])
        remove_old_skill_installs(skills_dir)
        return destination
    action = "updated" if destination.exists() else "installed"
    destination_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, source_path in source_files.items():
        out = destination_dir / relative_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(source_path.read_bytes())
    print(f"{action}: {destination}")
    install_skill_commands(target["commands_dir"])
    remove_old_skill_installs(skills_dir)
    print(target["restart"])
    return destination


def install_skill_commands(destination_dir: Path | None) -> None:
    source_dir = bundled_command_dir()
    if destination_dir is None or not source_dir.exists():
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    for source_path in sorted((path for path in source_dir.iterdir() if path.name.endswith(".md")), key=lambda path: path.name):
        target = destination_dir / source_path.name
        if target.exists() and target.read_bytes() == source_path.read_bytes():
            print(f"already installed command: {target}")
            continue
        action = "updated" if target.exists() else "installed"
        target.write_bytes(source_path.read_bytes())
        print(f"{action} command: {target}")


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


def agent_home() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def skill_provider_targets() -> list[dict[str, Any]]:
    """Skill install targets per supported agent. ``skills_dir`` is None when the
    default location cannot be resolved (no override and no home directory)."""
    home = agent_home()
    targets: list[dict[str, Any]] = []

    opencode_skills = env_path("OCMO_OPENCODE_SKILLS_DIR")
    if opencode_skills is None and home is not None:
        opencode_skills = home / ".config" / "opencode" / "skills"
    opencode_commands = env_path("OCMO_OPENCODE_COMMANDS_DIR")
    if opencode_commands is None and opencode_skills is not None:
        opencode_commands = opencode_skills.parent / "commands"
    targets.append({
        "provider": "opencode",
        "label": "opencode",
        "skills_dir": opencode_skills,
        "commands_dir": opencode_commands,
        "restart": "restart opencode to load the skill",
    })

    claude_skills = env_path("OCMO_CLAUDE_SKILLS_DIR")
    if claude_skills is None and home is not None:
        claude_skills = home / ".claude" / "skills"
    claude_commands = env_path("OCMO_CLAUDE_COMMANDS_DIR")
    if claude_commands is None and claude_skills is not None:
        claude_commands = claude_skills.parent / "commands"
    targets.append({
        "provider": "claude-code",
        "label": "claude",
        "skills_dir": claude_skills,
        "commands_dir": claude_commands,
        "restart": "start a new claude session to load the skill",
    })

    cursor_skills = env_path("OCMO_CURSOR_SKILLS_DIR")
    if cursor_skills is None and home is not None:
        cursor_skills = home / ".cursor" / "skills"
    cursor_commands = env_path("OCMO_CURSOR_COMMANDS_DIR")
    if cursor_commands is None and cursor_skills is not None:
        cursor_commands = cursor_skills.parent / "commands"
    targets.append({
        "provider": "cursor",
        "label": "cursor",
        "skills_dir": cursor_skills,
        "commands_dir": cursor_commands,
        "restart": "start a new cursor-agent session to load the skill",
    })
    return targets


def skill_target_path(target: dict[str, Any]) -> Path | None:
    skills_dir = target["skills_dir"]
    return skills_dir / OCMO_SKILL_NAME / "SKILL.md" if skills_dir is not None else None


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


def bundled_command_dir() -> Path:
    configured = os.environ.get("OCMO_COMMAND_SOURCE")
    if configured:
        path = Path(configured)
        if path.is_dir():
            return path
        raise OcmoError(f"configured command source not found: {path}")
    resource_root = resources.files(__package__).joinpath(OCMO_COMMAND_RESOURCE)
    if resource_root.is_dir():
        return resource_root
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "ocmo" / "resources" / "commands"
        if candidate.exists():
            return candidate
    return Path("__missing_ocmo_commands__")
