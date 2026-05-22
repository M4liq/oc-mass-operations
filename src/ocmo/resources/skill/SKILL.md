---
name: ocmo
description: Use when working with OCMO CLI, ocmo/v1 manifests, .ocmo operation folders, planning, validation, rendering, running, status, pause/resume/rerun, or operation cleanup.
---

# OCMO

Use this skill when the user asks about OCMO, `ocmo` commands, `ocmo/v1` manifests, `.ocmo` folders, operation state, rendered prompts, run outputs, planning, execution, or operation control.

## Required Reference

Before advising on OCMO commands or manifests, read `README.md` in this skill directory when available. It is installed with this skill by `ocmo skill install` and is version-matched to the OCMO CLI that installed it.

If the skill README is unavailable, use these safety rules:

- Inspect existing `.ocmo/*/manifest.yaml` and `.ocmo/*/state.json` before acting.
- Prefer `ocmo validate`, `ocmo render`, and `ocmo run --dry-run` before real execution.
- Use `ocmo resume` only for strict continuation of paused work with a persisted session id.
- Use `ocmo rerun` for fresh retry of failed, timed-out, killed, or unresumable work.
- Ask before starting long-running foreground or detached operations unless the user explicitly requested execution.
- Do not delete operation state, outputs, artifacts, or worktrees unless the user explicitly asks for cleanup.

## Core Model

OCMO is a deterministic queue runner for repeatable `opencode run` jobs. It selects operation items from a manifest, renders a prompt for each selected item/run, starts isolated `opencode run` processes, and writes durable state and outputs for inspection.
