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
