from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import *


def runner_provider(runner: dict[str, Any] | None) -> str:
    return str((runner or {}).get("provider") or DEFAULT_RUNNER_PROVIDER)


def build_claude_code_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    runner = runner or manifest["runner"]
    command = [runner.get("command", "claude"), "-p", "--output-format", "stream-json", "--verbose"]
    if runner.get("model"):
        command += ["--model", str(runner["model"])]
    if runner.get("dangerouslySkipPermissions"):
        command.append("--dangerously-skip-permissions")
    # prompt_file set means stdin transport: claude only reads stdin when no
    # positional prompt argument is given, so the prompt must stay off argv.
    if prompt_file is None:
        command.append(prompt_text)
    return command


def build_claude_code_resume_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    session_id: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    command = build_claude_code_command(manifest, manifest_path, prompt_text, run_dir, runner, prompt_file)
    command[2:2] = ["--resume", session_id]
    return command


def claude_output_event(line: str) -> dict[str, Any] | None:
    stripped = strip_ansi(line).strip()
    if not stripped.startswith("{"):
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def render_claude_output_line(line: str) -> str:
    event = claude_output_event(line)
    if event is None:
        stripped = strip_ansi(line).strip()
        return "" if stripped.startswith("{") else strip_ansi(line)
    if event.get("type") != "assistant":
        return ""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    texts = [block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    if not texts:
        return ""
    joined = "".join(texts)
    return joined + ("" if joined.endswith("\n") else "\n")


def render_claude_output_text(output: str) -> str:
    return "".join(render_claude_output_line(line) for line in output.splitlines(keepends=True))


def extract_claude_session_id(output: str) -> str | None:
    for line in output.splitlines():
        event = claude_output_event(line)
        if event is None:
            continue
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def extract_claude_usage_delta(output_line: str) -> dict[str, Any] | None:
    event = claude_output_event(output_line)
    if event is None or event.get("type") != "result":
        return None
    tokens = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    usage = empty_usage()
    usage["input"] = usage_int(tokens.get("input_tokens"))
    usage["output"] = usage_int(tokens.get("output_tokens"))
    usage["cacheRead"] = usage_int(tokens.get("cache_read_input_tokens"))
    usage["cacheWrite"] = usage_int(tokens.get("cache_creation_input_tokens"))
    usage["total"] = usage["input"] + usage["output"] + usage["cacheRead"] + usage["cacheWrite"]
    usage["cost"] = usage_number(event.get("total_cost_usd"))
    usage["steps"] = 1
    return usage


def build_cursor_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    runner = runner or manifest["runner"]
    command = [runner.get("command", "cursor-agent"), "-p", "--output-format", "stream-json"]
    if runner.get("model"):
        command += ["--model", str(runner["model"])]
    if runner.get("dangerouslySkipPermissions"):
        command.append("--force")
    # prompt_file set means stdin transport: cursor-agent only reads stdin when
    # no positional prompt argument is given, so the prompt must stay off argv.
    if prompt_file is None:
        command.append(prompt_text)
    return command


def build_cursor_resume_command(
    manifest: dict[str, Any],
    manifest_path: Path,
    prompt_text: str,
    session_id: str,
    run_dir: Path | None = None,
    runner: dict[str, Any] | None = None,
    prompt_file: Path | None = None,
) -> list[str]:
    command = build_cursor_command(manifest, manifest_path, prompt_text, run_dir, runner, prompt_file)
    command[2:2] = ["--resume", session_id]
    return command


def render_cursor_output_line(line: str) -> str:
    event = claude_output_event(line)
    if event is None:
        stripped = strip_ansi(line).strip()
        return "" if stripped.startswith("{") else strip_ansi(line)
    if event.get("type") == "result":
        return "\n"
    if event.get("type") != "assistant":
        return ""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    texts = [block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    # cursor-agent may stream assistant text as chunked delta events, so no
    # newline is forced per event; the result event terminates the message.
    return "".join(texts)


def render_cursor_output_text(output: str) -> str:
    return "".join(render_cursor_output_line(line) for line in output.splitlines(keepends=True))


def extract_cursor_session_id(output: str) -> str | None:
    # cursor-agent stream-json events carry session_id in the same shape as
    # claude-code, including the system/init event.
    return extract_claude_session_id(output)


def extract_cursor_usage_delta(output_line: str) -> dict[str, Any] | None:
    # cursor-agent stream-json result events expose no token usage fields yet.
    return None


def provider_uses_stdin_transport(provider: str) -> bool:
    return provider in ("claude-code", "cursor")


def provider_render_output_line(provider: str, line: str) -> str:
    if provider == "claude-code":
        return render_claude_output_line(line)
    if provider == "cursor":
        return render_cursor_output_line(line)
    return render_opencode_output_line(line)


def provider_render_output_text(provider: str, output: str) -> str:
    if provider == "claude-code":
        return render_claude_output_text(output)
    if provider == "cursor":
        return render_cursor_output_text(output)
    return render_opencode_output_text(output)


def provider_extract_session_id(provider: str, output: str) -> str | None:
    if provider == "claude-code":
        return extract_claude_session_id(output)
    if provider == "cursor":
        return extract_cursor_session_id(output)
    return extract_session_id(output)


def provider_extract_usage_delta(provider: str, output_line: str) -> dict[str, Any] | None:
    if provider == "claude-code":
        return extract_claude_usage_delta(output_line)
    if provider == "cursor":
        return extract_cursor_usage_delta(output_line)
    return extract_usage_delta(output_line)
