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

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def suggested_top_level_command(argv: list[str]) -> str | None:
    if not argv or argv[0].startswith("-"):
        return None
    command = argv[0]
    suggestions = {
        "status": "operation status",
        "run": "operation run",
        "render": "operation render",
        "validate": "operation validate",
        "list": "operation list",
        "pause": "operation pause",
        "resume": "operation resume",
        "rerun": "operation rerun",
        "kill": "operation kill",
        "erase": "operation erase",
    }
    suggestion = suggestions.get(command)
    if suggestion is None:
        return None
    suffix = " ".join(quote_arg(arg) for arg in argv[1:])
    suffix = f" {suffix}" if suffix else ""
    return f"ocmo: unknown command {command!r}. Try: ocmo {suggestion}{suffix}"


def skill_command(args: argparse.Namespace) -> int:
    if args.skill_command == "path":
        print(opencode_skill_path())
        return 0
    if args.skill_command == "install":
        install_skill(force=args.force)
        return 0
    return 1  # pragma: no cover


def add_parameter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--param", dest="params", action="append", default=[], metavar="NAME=VALUE", help="Runtime manifest/workflow parameter; can be repeated")
    parser.add_argument("--params-file", dest="params_file", type=Path, help="YAML or JSON file containing runtime parameters")
    parser.add_argument("--params-json", dest="params_json", help=argparse.SUPPRESS)


def command_params(args: argparse.Namespace, record: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = record_params(record)
    params_json = getattr(args, "params_json", None)
    if isinstance(params_json, str):
        try:
            loaded_json = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise OcmoError(f"invalid params json: {exc}") from exc
        if not isinstance(loaded_json, dict):
            raise OcmoError("params json must contain an object")
        params.update(validate_parameter_mapping(loaded_json, "params json"))
    params_file = getattr(args, "params_file", None)
    if isinstance(params_file, Path):
        if not params_file.exists():
            raise OcmoError(f"params file not found: {params_file}")
        try:
            loaded = yaml.safe_load(params_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise OcmoError(f"could not read params file: {exc}") from exc
        except yaml.YAMLError as exc:
            raise OcmoError(f"invalid params file yaml: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise OcmoError("params file must contain a mapping")
        params.update(validate_parameter_mapping(loaded, "params file"))
    for raw in getattr(args, "params", []) or []:
        if not isinstance(raw, str) or "=" not in raw:
            raise OcmoError("--param must use NAME=VALUE")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise OcmoError("--param name must be non-empty")
        validate_parameter_name(key, "--param name")
        params[key] = value
    return params


def validate_parameter_mapping(values: dict[Any, Any], source: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in values.items():
        name = str(key)
        validate_parameter_name(name, f"{source} parameter name")
        params[name] = value
    return params


def validate_parameter_name(name: str, source: str) -> None:
    if not PARAM_NAME_RE.fullmatch(name):
        raise OcmoError(f"{source} must match {PARAM_NAME_RE.pattern}")


def record_params(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return stored_params(record)


def stored_params(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    params = data.get("params")
    return dict(params) if isinstance(params, dict) else {}


def parameter_arguments(params: dict[str, Any]) -> list[str]:
    if not params:
        return []
    return ["--params-json", json.dumps(params, ensure_ascii=False)]
