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

## OCMO Skill

This repository ships an optional opencode skill for working with OCMO commands, manifests, generated operation folders, state, and operation control:

```text
src/ocmo/resources/skill/SKILL.md
src/ocmo/resources/skill/README.md
```

Install it globally with:

```powershell
ocmo skill install
```

Print the target install path with:

```powershell
ocmo skill path
```

`ocmo skill install` updates the installed skill directory when the bundled skill or its handbook changes. It installs both `SKILL.md` and a version-matched `README.md` handbook, and it removes the old managed `ocmo-plan-grill` skill path during migration. Restart opencode after installing the skill. Running sessions keep using the already-loaded skill set.

Use `/ocmo` when you want an agent to inspect OCMO manifests, validate or render operations, explain command usage, inspect state and outputs, plan mass operations, or control running work with pause/resume/rerun/kill/erase.

## How It Works

The execution model is deliberately simple:

```text
manifest.yaml
  -> select operation items
  -> render prompt template for each selected item/run
  -> start opencode run processes
  -> write outputs and durable state

workflow.yaml
  -> select workflow steps
  -> run operation manifests sequentially
  -> write workflow state and reuse operation state
```

Core concepts:

- Operation: one mass operation, backed by one manifest.
- Operation item: one independently schedulable unit of work.
- Workflow: one sequential orchestration of multiple operation manifests.
- Manifest: generated plan describing items, prompts, queue settings, and state path.
- Prompt template: text rendered once per selected item/run and passed to `opencode run`.
- Selection: item filter such as `uncompleted`, `all`, `ITEM-001`, or `1-10`.
- State file: durable JSON state written by `ocmo operation run` or `ocmo workflow run` for status, resume, and audit.

## Typical Operation

This walkthrough creates and runs one operation. Workflow orchestration is covered separately in [Workflows](#workflows).

1. Write a rough mass-operation request.

```powershell
notepad business-taxonomy-prompt.txt
```

Describe the work, item boundaries, constraints, and desired output. Use the `/ocmo` skill when you want an agent to help inspect, plan, validate, render, run, or control an OCMO operation.

2. Generate an operation folder.

```powershell
ocmo operation plan --from business-taxonomy-prompt.txt
```

By default this writes a generated operation under `.ocmo/<prompt-stem>/` in the current workspace.

3. Validate and preview.

```powershell
ocmo operation validate .ocmo/business-taxonomy-prompt/manifest.yaml
ocmo operation render .ocmo/business-taxonomy-prompt --select uncompleted
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --dry-run
```

4. Run the queue.

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --yes
```

Or start it in the background:

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --detach
```

5. Inspect progress and results.

```powershell
ocmo operation list
ocmo operation status .ocmo/business-taxonomy-prompt
ocmo operation status --run-id <run-id>
```

Review `state.json` beside the manifest for durable execution status, including per-run token usage when `opencode` reports it. Review per-run agent output under `outputs/` beside the manifest.

## Commands

| Task | Command |
| --- | --- |
| Generate a manifest draft | `ocmo operation plan --from <prompt-file>` |
| Validate generated operation | `ocmo operation validate <manifest>` |
| Preview rendered prompts | `ocmo operation render [manifest-or-directory] --select <selector>` |
| Preview execution | `ocmo operation run [manifest-or-directory] --select <selector> --dry-run` |
| Execute operation foreground | `ocmo operation run [manifest-or-directory] --select <selector> --yes` |
| Execute operation background | `ocmo operation run [manifest-or-directory] --select <selector> --detach` |
| Show operation status | `ocmo operation status [manifest-or-directory]` |
| List detached operation runs | `ocmo operation list [manifest-or-directory]` |
| Pause a running operation | `ocmo operation pause [manifest-or-directory]` |
| Resume a paused operation | `ocmo operation resume [manifest-or-directory] --yes` |
| Fresh-rerun broken operation work | `ocmo operation rerun [manifest-or-directory] --select retryable --yes` |
| Kill a running operation | `ocmo operation kill [manifest-or-directory] --force` |
| Erase a generated operation | `ocmo operation erase .ocmo/<operation>/manifest.yaml --force` |
| Validate workflow | `ocmo workflow validate <workflow>` |
| Execute workflow background | `ocmo workflow run <workflow> --detach` |
| Show workflow status | `ocmo workflow status <workflow>` |

## Planning

```powershell
ocmo operation plan --from <prompt-file> [--out <manifest>] [--workspace <path>] [--read <source-file>] [--model <model>] [--interactive] [--dry-run]
```

`ocmo operation plan` asks `opencode` to convert a natural-language mass-operation request into an `ocmo/v1` manifest and any generated prompt templates.

- `--workspace` sets the target repository for planning. If omitted, it defaults to the current directory.
- `--out` sets the manifest output path. If omitted, the manifest is written under `<workspace>/.ocmo/<prompt-stem>/manifest.yaml`.
- `--read` attaches read-only source files such as CSV exports, inventories, or planning inputs.
- `--dry-run` prints the planning prompt without starting `opencode`.
- `--interactive` allows the planner to ask terminal questions before returning the final manifest.

Planning does not execute operation items.

## Rendering And Dry Runs

`ocmo operation render` is prompt-only and read-only. Use it when you need to inspect the text sent to agents.

```powershell
ocmo operation render .ocmo/business-taxonomy-prompt --select 41-48
```

`ocmo operation run --dry-run` validates, selects, renders, and previews the effective `opencode run` commands without launching agents, writing state, creating worktrees, or running setup/teardown.

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --dry-run
```

When many prompts are selected, preview output is compact by default: first two prompts and the last prompt. Add `--all` to print every prompt.

## Running

```powershell
ocmo operation run [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--ui auto|live|plain] [--allow-shared-worktree-concurrency] [--detach] [--dry-run] [--all] [--yes]
```

Useful options:

- `--select <selector>`: overrides the manifest default selector.
- `--concurrency <count>`: overrides item-level queue concurrency.
- `--timeout-seconds <seconds>`: overrides per-process timeouts.
- `--ui auto|live|plain`: controls foreground terminal output.
- `--yes`, `-y`: skips confirmation for foreground runs.
- `--detach`: starts a background `ocmo operation run` and returns a run ID.

If `policy.worktree: single` uses concurrency above `1`, `ocmo operation run` requires `--allow-shared-worktree-concurrency`. Use that only when selected item scopes are explicitly non-overlapping.

Foreground runs show token usage after each `opencode` step completes when `opencode run --format json` emits usage metadata. `ocmo operation status` summarizes operation token usage and includes a compact per-item `Tokens` column formatted as `input/output`.

## Workflows

Workflows run operation manifests sequentially and keep workflow-level state separate from each operation's state.

```yaml
schema: ocmo-workflow/v1

workflow:
  id: release-cleanup
  description: Run operations in order.

state:
  path: state.json

defaults:
  stopOnFailure: true

steps:
  - id: plan-docs
    manifest: ../plan-docs/manifest.yaml
  - id: implement-docs
    manifest: ../implement-docs/manifest.yaml
```

Workflow commands:

```powershell
ocmo workflow validate workflow.yaml
ocmo workflow run workflow.yaml --dry-run
ocmo workflow run workflow.yaml --detach
ocmo workflow status workflow.yaml
ocmo workflow list --all
ocmo workflow pause workflow.yaml
ocmo workflow resume workflow.yaml --detach
ocmo workflow rerun workflow.yaml --detach
ocmo workflow kill workflow.yaml --force
```

Workflow `--select` selects workflow steps, not operation items. Referenced operation manifests control their own item selection, concurrency, timeouts, and worktree safety policy.

## Detached Runs

`--detach` starts a child `ocmo operation run` or `ocmo workflow run` with `--yes` and `--ui plain`, then returns immediately:

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --detach
```

Detached metadata and logs are written under `.ocmo/runs/` beside the manifest or workflow. `ocmo` also writes a global registry entry so list/status commands can find active runs even when you are not in the manifest or workflow directory.

```powershell
ocmo operation list --all
ocmo operation status --run-id <run-id>
ocmo operation status .ocmo/business-taxonomy-prompt
ocmo workflow list --all
ocmo workflow status --run-id <run-id>
```

Set `OCMO_RUN_REGISTRY` to override the global registry location.

Use `ocmo operation status --run-id <run-id>` or `ocmo workflow status --run-id <run-id>` to inspect detached runs. Operation list details include token totals when usage is available.

## Operation Control

Long-running operations can be stopped, resumed, or removed:

```powershell
ocmo operation pause .ocmo/business-taxonomy-prompt
ocmo operation resume .ocmo/business-taxonomy-prompt --yes
ocmo operation rerun .ocmo/business-taxonomy-prompt --select retryable --yes
ocmo operation kill .ocmo/business-taxonomy-prompt --force
ocmo operation erase .ocmo/business-taxonomy-prompt --force
```

`pause` is stop-and-resume, not an operating-system suspend. It terminates the active detached supervisor and any tracked child `opencode run` processes, then marks running items/runs as `paused` when their opencode `sessionId` is known. If a process had not emitted a session id yet, the run is marked `paused_unresumable`.

Pressing `Ctrl+C` during a foreground `ocmo operation run` or `ocmo workflow run` uses pause semantics: tracked child processes are terminated, active work is marked paused when possible, and the command exits with code `130`.

Pressing `Ctrl+C` during `ocmo operation plan` terminates the active planner process when ocmo owns it, prints `ocmo: interrupted`, and exits with code `130` without writing a manifest unless planning had already reached the final write step.

`resume` is strict session continuation. It resumes only paused runs that have a persisted opencode session id and starts them with `opencode run --session <sessionId>`. It never falls back to `opencode --continue` because that can resume the wrong session during concurrent work.

`rerun` is a fresh start and never uses `--session`. By default, `ocmo operation rerun` selects `retryable` items: `paused_unresumable`, `timed_out`, `failed`, `cleanup_failed`, `worktree_failed`, `setup_failed`, and `killed`. Use `--select unresumable`, `--select timed-out`, `--select failed`, `--select killed`, `--select all`, or explicit item IDs/ranges to narrow or expand the fresh rerun.

If an `opencode run` exceeds `timeoutSeconds`, ocmo terminates the child process tree, marks the run and item `timed_out`, and treats that item as retryable for `ocmo operation rerun --select timed-out` or the default `--select retryable`.

`kill` terminates tracked processes and marks active items/runs as `killed`, preserving the operation directory, state, outputs, logs, and artifacts for audit.

`erase` runs the same termination step and then deletes the generated operation directory, such as `.ocmo/business-taxonomy-prompt/`. It requires `--force` when non-interactive and refuses manifests outside `.ocmo/<operation>/manifest.yaml`.

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

`ocmo operation run` writes durable state to the manifest's configured state path. State records execution facts such as item status, run status, start/completion times, exit codes, output paths, and worktree metadata.

Per-run `opencode` stdout/stderr is written under `outputs/` beside the manifest. This keeps concurrent agent output from corrupting the terminal UI and makes long-running work inspectable after the fact.

Failed items remain selectable through `uncompleted` unless you mark them completed or skipped in the manifest.

## Examples

Reference examples are available in:

- `examples/report-rewrite.yaml`
- `examples/report-rewrite-auto-worktrees.yaml`
- `examples/report-rewrite-multi-agent.yaml`
- `examples/prompts/report-rewrite.md`
