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

def install_skill(force: bool = False) -> Path:
    source_dir = bundled_skill_dir()
    destination = opencode_skill_path()
    destination_dir = destination.parent
    source_files = bundled_skill_files(source_dir)
    if destination.exists():
        if installed_skill_matches(destination_dir, source_files):
            print(f"already installed: {destination}")
            install_opencode_commands()
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
    install_opencode_commands()
    remove_old_skill_installs(destination_dir.parent)
    print("restart opencode to load the skill")
    return destination


def install_opencode_commands() -> None:
    source_dir = bundled_command_dir()
    if not source_dir.exists():
        return
    destination_dir = opencode_commands_dir()
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


def opencode_skill_path() -> Path:
    root = os.environ.get("OCMO_OPENCODE_SKILLS_DIR")
    skills_dir = Path(root) if root else Path.home() / ".config" / "opencode" / "skills"
    return skills_dir / OCMO_SKILL_NAME / "SKILL.md"


def opencode_commands_dir() -> Path:
    root = os.environ.get("OCMO_OPENCODE_COMMANDS_DIR")
    if root:
        return Path(root)
    skills_root = os.environ.get("OCMO_OPENCODE_SKILLS_DIR")
    if skills_root:
        return Path(skills_root).parent / "commands"
    return Path.home() / ".config" / "opencode" / "commands"


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
