---
name: ocmo
description: Use when working with OCMO CLI, ocmo/v1 manifests, ocmo-workflow/v1 workflows, .ocmo operation folders, planning, validation, rendering, running, status, pause/resume/rerun, or operation/workflow cleanup.
---

# OCMO

Use this skill when the user asks about OCMO, `ocmo` commands, `ocmo/v1` manifests, `ocmo-workflow/v1` workflows, `.ocmo` folders, operation/workflow state, rendered prompts, run outputs, planning, execution, or operation/workflow control.

## Required Reference

Before advising on OCMO commands or manifests, read `README.md` in this skill directory when available. It is installed with this skill by `ocmo skill install` and is version-matched to the OCMO CLI that installed it.

If the skill README is unavailable, use these safety rules:

- Inspect existing `.ocmo/*/manifest.yaml`, `.ocmo/*/workflow.yaml`, and state files before acting.
- Prefer `ocmo operation validate`, `ocmo operation render`, `ocmo operation run --dry-run`, and `ocmo workflow run --dry-run` before real execution.
- Use `ocmo operation resume` only for strict continuation of paused work with a persisted session id.
- Use `ocmo operation rerun` or `ocmo workflow rerun` for fresh retry of failed, timed-out, killed, or unresumable work.
- Ask before starting long-running foreground or detached operations unless the user explicitly requested execution.
- Do not delete operation state, outputs, artifacts, or worktrees unless the user explicitly asks for cleanup.

## Core Model

OCMO is a deterministic queue runner for repeatable `opencode run` jobs. It selects work units from a manifest, renders a prompt for each selected work unit/run, starts isolated `opencode run` processes, and writes durable state and outputs for inspection. Workflows run multiple operation manifests sequentially and keep separate workflow state.
