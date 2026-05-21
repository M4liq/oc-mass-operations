# OC Mass Operations

OC Mass Operations (`ocmo`) is a deterministic queue runner for repeatable `opencode` jobs.

Use it when one large request naturally splits into many similar units of work, such as rewriting reports, reviewing documentation files, or applying the same change process across many targets.

`ocmo` does not replace `opencode`. It schedules `opencode`: the reasoning, editing, reviewing, and tool use still happens inside each isolated `opencode run` process.

## Install From Git

For now, OCMO is distributed by cloning this repository and installing from the checkout.

Windows:

```powershell
git clone https://github.com/M4liq/oc-mass-operations.git
cd oc-mass-operations
python -m pip install --user -e .
ocmo --help
```

macOS/Linux:

```bash
git clone https://github.com/M4liq/oc-mass-operations.git
cd oc-mass-operations
python3 -m pip install --user -e .
ocmo --help
```

If you prefer isolated CLI installs and already have `pipx`:

```bash
pipx install -e .
ocmo --help
```

To update later, pull the repository and reinstall from the checkout:

```bash
git pull
python -m pip install --user -e .
```

## Development Fallback

If editable install is not available locally, run directly from source:

```powershell
$env:PYTHONPATH='src'
python -m ocmo --help
```

## Planning Skill

This repository ships an optional opencode skill for turning vague mass-operation ideas into approved `ocmo plan` prompts:

```text
skills/ocmo-plan-grill/SKILL.md
```

Install it globally with:

```powershell
ocmo skill install
```

Print the target install path with:

```powershell
ocmo skill path
```

Use `ocmo skill install --force` to overwrite an existing different copy. Restart opencode after installing the skill. Running sessions keep using the already-loaded skill set.

Use the skill when you want an agent to grill a vague request, format the final planning prompt, get your approval, and then run `ocmo plan`.

## How It Works

The execution model is deliberately simple:

```text
manifest.yaml
  -> select operation items
  -> render prompt template for each selected item/run
  -> start opencode run processes
  -> write outputs and durable state
```

Core concepts:

- Operation: one mass workflow, backed by one manifest.
- Operation item: one independently schedulable unit of work.
- Manifest: generated plan describing items, prompts, queue settings, and state path.
- Prompt template: text rendered once per selected item/run and passed to `opencode run`.
- Selection: item filter such as `uncompleted`, `all`, `ITEM-001`, or `1-10`.
- State file: durable JSON state written by `ocmo run` for status, resume, and audit.

## Typical Workflow

1. Write a rough mass-operation request.

```powershell
notepad business-taxonomy-prompt.txt
```

Describe the work, item boundaries, constraints, and desired output. If the request is vague, use the `ocmo-plan-grill` skill first.

2. Generate an operation folder.

```powershell
ocmo plan --from business-taxonomy-prompt.txt
```

By default this writes a generated operation under `.ocmo/<prompt-stem>/` in the current workspace.

3. Validate and preview.

```powershell
ocmo validate .ocmo/business-taxonomy-prompt/manifest.yaml
ocmo render .ocmo/business-taxonomy-prompt --select uncompleted
ocmo run .ocmo/business-taxonomy-prompt --select uncompleted --dry-run
```

4. Run the queue.

```powershell
ocmo run .ocmo/business-taxonomy-prompt --select uncompleted --yes
```

Or start it in the background:

```powershell
ocmo run .ocmo/business-taxonomy-prompt --select uncompleted --detach
```

5. Inspect progress and results.

```powershell
ocmo status
ocmo status .ocmo/business-taxonomy-prompt
ocmo status --run-id <run-id>
```

Review `state.json` beside the manifest for durable execution status. Review per-run agent output under `outputs/` beside the manifest.

## Commands

| Task | Command |
| --- | --- |
| Generate a manifest draft | `ocmo plan --from <prompt-file>` |
| Validate generated operation | `ocmo validate <manifest>` |
| Preview rendered prompts | `ocmo render [manifest-or-directory] --select <selector>` |
| Preview execution | `ocmo run [manifest-or-directory] --select <selector> --dry-run` |
| Execute foreground | `ocmo run [manifest-or-directory] --select <selector> --yes` |
| Execute background | `ocmo run [manifest-or-directory] --select <selector> --detach` |
| Show detached runs/status | `ocmo status` or `ocmo list` |

## Planning

```powershell
ocmo plan --from <prompt-file> [--out <manifest>] [--workspace <path>] [--read <source-file>] [--model <model>] [--interactive] [--dry-run]
```

`ocmo plan` asks `opencode` to convert a natural-language mass-operation request into an `ocmo/v1` manifest and any generated prompt templates.

- `--workspace` sets the target repository for planning. If omitted, it defaults to the current directory.
- `--out` sets the manifest output path. If omitted, the manifest is written under `<workspace>/.ocmo/<prompt-stem>/manifest.yaml`.
- `--read` attaches read-only source files such as CSV exports, inventories, or planning inputs.
- `--dry-run` prints the planning prompt without starting `opencode`.
- `--interactive` allows the planner to ask terminal questions before returning the final manifest.

Planning does not execute operation items.

## Rendering And Dry Runs

`ocmo render` is prompt-only and read-only. Use it when you need to inspect the text sent to agents.

```powershell
ocmo render .ocmo/business-taxonomy-prompt --select 41-48
```

`ocmo run --dry-run` validates, selects, renders, and previews the effective `opencode run` commands without launching agents, writing state, creating worktrees, or running setup/teardown.

```powershell
ocmo run .ocmo/business-taxonomy-prompt --select uncompleted --dry-run
```

When many prompts are selected, preview output is compact by default: first two prompts and the last prompt. Add `--all` to print every prompt.

## Running

```powershell
ocmo run [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--ui auto|live|plain] [--allow-shared-worktree-concurrency] [--detach] [--dry-run] [--all] [--yes]
```

Useful options:

- `--select <selector>`: overrides the manifest default selector.
- `--concurrency <count>`: overrides item-level queue concurrency.
- `--timeout-seconds <seconds>`: overrides per-process timeouts.
- `--ui auto|live|plain`: controls foreground terminal output.
- `--yes`, `-y`: skips confirmation for foreground runs.
- `--detach`: starts a background `ocmo run` and returns a run ID.

If `policy.worktree: single` uses concurrency above `1`, `ocmo run` requires `--allow-shared-worktree-concurrency`. Use that only when selected item scopes are explicitly non-overlapping.

## Detached Runs

`--detach` starts a child `ocmo run` with `--yes` and `--ui plain`, then returns immediately:

```powershell
ocmo run .ocmo/business-taxonomy-prompt --select uncompleted --detach
```

Detached metadata and logs are written under `.ocmo/runs/` beside the manifest. `ocmo` also writes a global registry entry so `ocmo status` can list active runs even when you are not in the manifest directory.

```powershell
ocmo status
ocmo list --all
ocmo status --run-id <run-id>
ocmo status .ocmo/business-taxonomy-prompt
```

Set `OCMO_RUN_REGISTRY` to override the global registry location.

## Selection Rules

Selectors match manifest item IDs:

- `all`: every item.
- `pending`: items whose manifest status is `pending`.
- `uncompleted`: items whose manifest status is not `completed`, `done`, or `skipped`.
- `WORK-001`: one exact item ID.
- `WORK-001,WORK-002`: multiple exact item IDs.
- `41-48`: numeric range matching IDs `41` through `48`.
- `41-48,93-96,141`: mixed numeric ranges and exact IDs.

## State And Outputs

`ocmo run` writes durable state to the manifest's configured state path. State records execution facts such as item status, run status, start/completion times, exit codes, output paths, and worktree metadata.

Per-run `opencode` stdout/stderr is written under `outputs/` beside the manifest. This keeps concurrent agent output from corrupting the terminal UI and makes long-running work inspectable after the fact.

Failed items remain selectable through `uncompleted` unless you mark them completed or skipped in the manifest.

## Examples

Reference examples are available in:

- `examples/report-rewrite.yaml`
- `examples/report-rewrite-auto-worktrees.yaml`
- `examples/report-rewrite-multi-agent.yaml`
- `examples/prompts/report-rewrite.md`
