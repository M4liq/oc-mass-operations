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

`ocmo skill install` updates the installed skill directory when the bundled skill or its handbook changes. It installs both `SKILL.md` and a version-matched `README.md` handbook, installs the `/ocmo-operation-statuses` and `/ocmo-workflow-statuses` slash commands under `~/.config/opencode/commands/`, and removes the old managed `ocmo-plan-grill` skill path during migration. Restart opencode after installing the skill. Running sessions keep using the already-loaded skill and command set.

Use `/ocmo` when you want an agent to inspect OCMO manifests, validate or render operations, explain command usage, inspect state and outputs, plan mass operations, or control running work with pause/resume/rerun/kill/erase.

Use `/ocmo-operation-statuses` to ask an agent for a brief read-only summary of all active operation statuses, or the latest inactive operation when nothing is active. Use `/ocmo-workflow-statuses` for the same read-only view across workflows.

## How It Works

The execution model is deliberately simple:

```text
manifest.yaml
  -> select work units
  -> render prompt template for each selected work unit/run
  -> start opencode run processes
  -> write outputs and durable state

workflow.yaml
  -> select workflow steps
  -> run operation manifests sequentially
  -> write workflow state and reuse operation state
```

Core concepts:

- Operation: one mass operation, backed by one manifest.
- Work unit: one independently schedulable unit of work within an operation.
- Workflow: one sequential orchestration of multiple operation manifests.
- Manifest: generated plan describing work units, prompts, queue settings, and state path.
- Prompt template: text rendered once per selected work unit/run and passed to `opencode run`; very long prompts are written to runtime prompt-input files and attached with `--file` to avoid OS command-line limits.
- Selection: work unit filter such as `uncompleted`, `all`, `ITEM-001`, or `1-10`.
- State file: durable JSON state written by `ocmo operation run` or `ocmo workflow run` for status, resume, and audit.

## Manifest Quick Start

An operation starts from an `ocmo/v1` `manifest.yaml`. The manifest tells OCMO where the target workspace is, how to run `opencode`, which work units exist, which prompt template to render, and where durable state should be written.

Minimal example:

```yaml
schema: ocmo/v1

operation:
  id: docs-refresh
  description: Refresh selected documentation pages.
  workspace: C:\path\to\target-repo

runner:
  command: opencode
  agent: build
  model: openai/gpt-5.5
  reasoningEffort: high
  timeoutSeconds: 14400

selection:
  default: uncompleted

queue:
  concurrency: 1
  order: manifest
  stopOnFailure: false
  autoWorktrees:
    enabled: false

hooks:
  beforeRun: []
  afterRun: []
  onFailure: []

clean:
  beforeRun: false
  paths: []

policy:
  worktree: single

prompt:
  template: prompts/docs-refresh.md
  skills: []

state:
  path: state.json

workUnits:
  - id: DOC-001
    title: Refresh README
    payload:
      path: README.md
```

Key sections:

- `operation` identifies the operation and target workspace.
- `runner` configures the `opencode run` process used for each selected work unit.
- `selection` and `queue` control which work units run and how much parallelism is allowed.
- `hooks` optionally runs shell scripts around operation execution.
- `clean` optionally declares transient paths removed by fresh runs and erase.
- `policy` describes worktree safety assumptions.
- `prompt.template` points to the prompt rendered for each work unit.
- `workUnits[].payload` is task-specific data available to the prompt template.

## Typical Operation

This walkthrough creates and runs one operation. Workflow orchestration is covered separately in [Workflows](#workflows).

1. Write a rough mass-operation request.

```powershell
notepad business-taxonomy-prompt.txt
```

Describe the work, work unit boundaries, constraints, and desired output. Use the `/ocmo` skill when you want an agent to help inspect, plan, validate, render, run, or control an OCMO operation.

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
| Validate generated operation | `ocmo operation validate [manifest-or-directory]` |
| Preview rendered prompts | `ocmo operation render [manifest-or-directory] --select <selector>` |
| Preview execution | `ocmo operation run [manifest-or-directory] --select <selector> --dry-run` |
| Execute operation foreground | `ocmo operation run [manifest-or-directory] --select <selector> --yes` |
| Execute operation from a clean slate | `ocmo operation run [manifest-or-directory] --fresh --yes` |
| Execute operation background | `ocmo operation run [manifest-or-directory] --select <selector> --detach` |
| Watch operation status | `ocmo operation status [manifest-or-directory]` |
| Show operation status once | `ocmo operation status [manifest-or-directory] --once` |
| List operation runs and discovered operation states | `ocmo operation list [manifest-or-directory]` |
| Pause a running operation | `ocmo operation pause [manifest-or-directory]` |
| Resume a paused operation | `ocmo operation resume [manifest-or-directory] --yes` |
| Fresh-rerun broken operation work | `ocmo operation rerun [manifest-or-directory] --select retryable --yes` |
| Kill a running operation | `ocmo operation kill [manifest-or-directory] --force` |
| Erase operation runtime data | `ocmo operation erase .ocmo/<operation>/manifest.yaml --force` |
| Validate workflow | `ocmo workflow validate <workflow>` |
| Execute workflow background | `ocmo workflow run <workflow> --detach` |
| Execute workflow from a clean slate | `ocmo workflow run <workflow> --fresh --yes` |
| Watch workflow status | `ocmo workflow status <workflow>` |
| Show workflow status once | `ocmo workflow status <workflow> --once` |
| List workflow runs and discovered workflow states | `ocmo workflow list` |
| Erase all workflow operation runtime | `ocmo workflow erase <workflow> --force` |

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

Planning does not execute work units.

## Rendering And Dry Runs

`ocmo operation render` is prompt-only and read-only. Use it when you need to inspect the text sent to agents.

```powershell
ocmo operation render .ocmo/business-taxonomy-prompt --select 41-48
```

`ocmo operation run --dry-run` validates, selects, renders, and previews the effective `opencode run` commands without launching agents, writing state, creating worktrees, running hooks, or running setup/teardown.

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --dry-run
```

When many prompts are selected, preview output is compact by default: first two prompts and the last prompt. Add `--all` to print every prompt. Interactive terminals use formatted preview panels; redirected output stays plain for piping or saving.

## Running

```powershell
ocmo operation run [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--ui auto|live|plain] [--param <name=value>] [--params-file <path>] [--allow-shared-worktree-concurrency] [--fresh] [--detach] [--dry-run] [--all] [--yes]
```

Useful options:

- `--select <selector>`: overrides the manifest default selector.
- `--concurrency <count>`: overrides work-unit-level queue concurrency.
- `--timeout-seconds <seconds>`: overrides per-process timeouts.
- `--param <name=value>`: supplies a runtime parameter for `{{params.name}}` placeholders; can be repeated.
- `--params-file <path>`: loads runtime parameters from a YAML or JSON mapping; inline `--param` values override file values.
- `--ui auto|live|plain`: controls foreground terminal output.
- `--fresh`: removes operation runtime data before selecting work units, so default `uncompleted` selection behaves like a new operation.
- `--yes`, `-y`: skips confirmation for foreground runs.
- `--detach`: starts a background `ocmo operation run` and returns a run ID.

If `policy.worktree: single` uses concurrency above `1`, `ocmo operation run` requires `--allow-shared-worktree-concurrency`. Use that only when selected work unit scopes are explicitly non-overlapping.

Foreground runs show token usage after each `opencode` step completes when `opencode run --format json` emits usage metadata. `ocmo operation status` continuously refreshes operation status until interrupted, summarizes operation token usage and total operation elapsed time, and includes compact per-work-unit `Work Time`, `Agent Time`, and `Tokens` columns. `Tokens` is formatted as `input/output`.

Changing a manifest or prompt template while an operation is running does not affect already-started agent processes. It can affect queued work units or later sequential run steps because prompts are rendered immediately before each run starts. Long-prompt transport writes the prompt input file before launching the agent, so edits after launch do not change that launched run.

## Cleaning Fresh Runs

Use `--fresh` when an operation should behave like a build from clean transient outputs. Fresh runs remove OCMO runtime files before selecting work units: configured state, `outputs/`, `artifacts/`, `prompt-inputs/`, and detached run metadata. If the manifest sets `clean.beforeRun: true`, plain `ocmo operation run` behaves as a fresh run. Dry runs do not delete files; they preview cleanup targets.

```yaml
clean:
  beforeRun: true
  paths:
    - root: workspace
      path: generated/ocmo-results
      when: [beforeRun, erase]
    - root: manifest
      path: scratch
      when: erase
```

Custom clean paths are exact relative paths only. `root` is `workspace` or `manifest` and defaults to `workspace`. `when` is `beforeRun`, `erase`, or a list of those values and defaults to both. OCMO rejects absolute paths, `..`, `.`, and `.git` path components. Fresh cleanup refuses to run while the operation appears active.

## Operation Hooks

Operation manifests can define shell scripts that run once around an operation run:

```yaml
hooks:
  beforeRun:
    - ./scripts/prepare.ps1
  onFailure:
    - ./scripts/diagnose.ps1
  afterRun:
    - ./scripts/cleanup.ps1
```

- `beforeRun` runs after confirmation and state initialization, before any work unit starts.
- `onFailure` runs when the operation fails, including a failing `beforeRun` hook.
- `afterRun` runs after work units finish, and also after a failing `beforeRun` hook, for cleanup.
- Hooks do not run for `validate`, `render`, or `run --dry-run`; dry runs only preview them.
- Detached operation hooks run inside the detached child process.
- Hooks run from `operation.workspace` with shell execution, matching `queue.autoWorktrees.setup` and `teardown`.
- Hook environment includes `OCMO_OPERATION_ID`, `OCMO_MANIFEST_PATH`, `OCMO_STATE_PATH`, `OCMO_WORKSPACE`, `OCMO_HOOK`, `OCMO_SELECTED_COUNT`, and `OCMO_OPERATION_STATUS` when known.

## Status

```powershell
ocmo operation status [manifest-or-directory] [--run-id <run-id>] [--active-or-latest] [--all] [--interval <seconds>] [--once]
```

`ocmo operation status` watches by default: it reloads state and detached run metadata every second and keeps reporting until interrupted with `Ctrl+C`. Use `--interval <seconds>` to change the refresh cadence. Use `--once` for a single snapshot in scripts or logs. Use `--active-or-latest --once` for slash-command-friendly output covering all active discovered operations, or the latest inactive operation when none are active.

The status summary includes `elapsed=<duration>` for the whole operation and `stateUpdated=<timestamp>` for the last time OCMO wrote operation state. `stateUpdated` is not the transcript output file modification time. The table separates cumulative work-unit time (`Work Time`) from the current or last agent run step time (`Agent Time`).

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
  operationSelect: uncompleted

steps:
  - id: plan-docs
    manifest: ../plan-docs/manifest.yaml
  - id: implement-docs
    manifest: ../implement-docs/manifest.yaml
    concurrency: 3
    allowSharedWorktreeConcurrency: true
```

Workflow commands:

```powershell
ocmo workflow validate workflow.yaml
ocmo workflow run workflow.yaml --dry-run
ocmo workflow run workflow.yaml --fresh --yes
ocmo workflow run workflow.yaml --detach
ocmo workflow status workflow.yaml
ocmo workflow status workflow.yaml --once
ocmo workflow status --active-or-latest --once
ocmo workflow list --all
ocmo workflow pause workflow.yaml
ocmo workflow resume workflow.yaml --detach
ocmo workflow rerun workflow.yaml --detach
ocmo workflow kill workflow.yaml --force
```

Workflow `--select` selects workflow steps, not work units.

Workflow `defaults` and individual `steps` can pass operation run overrides to referenced manifests:

- `operationSelect`: operation work-unit selector, equivalent to `ocmo operation run --select`.
- `concurrency`: operation queue concurrency override, equivalent to `--concurrency`.
- `timeoutSeconds`: per-process timeout override, equivalent to `--timeout-seconds`.
- `allowSharedWorktreeConcurrency`: allows `policy.worktree: single` with concurrency above `1`, equivalent to `--allow-shared-worktree-concurrency`. Use only when selected work unit scopes are explicitly non-overlapping.
- `params`: operation-specific runtime parameters for referenced operation manifests.

Step values override workflow defaults. Operation-specific `params` merge after workflow parameters, then `defaults.params`, then `steps[].params`.

Workflows also accept `--param` and `--params-file`; the resolved parameters apply to the workflow file and every referenced operation manifest.

`ocmo workflow run --fresh` removes workflow state before step selection and runs each selected operation with operation-level fresh cleanup. Use it when a workflow should re-execute from a clean slate instead of continuing from completed workflow and operation state.

## Runtime Parameters

Operation manifests and workflow files can declare reusable parameters in a top-level `params` mapping and reference them with `{{params.name}}` placeholders. OCMO resolves parameters in memory before validation, rendering, running, status, and control commands. It does not rewrite the manifest or workflow file.

```yaml
params:
  projectName: Example
  taxonomyRoot: .ocmo/project-taxonomy

operation:
  id: project-taxonomy-scan
  workspace: "{{params.repoRoot}}"

state:
  path: "{{params.taxonomyRoot}}/scan/state.json"

workUnits:
  - id: SCAN-001
    payload:
      projectName: "{{params.projectName}}"
      outputPath: "{{params.taxonomyRoot}}/scan/artifacts/inventory.json"
```

Run or preview the same manifest against a different repository by passing parameters:

```powershell
ocmo operation run .ocmo/project-taxonomy/scan `
  --param repoRoot=D:\repos\PerfectGym `
  --param projectName=PerfectGym `
  --dry-run
```

For larger parameter sets, use a YAML or JSON mapping file:

```yaml
repoRoot: D:\repos\PerfectGym
projectName: PerfectGym
taxonomyRoot: .ocmo/project-taxonomy
```

```powershell
ocmo workflow run .ocmo/project-taxonomy/workflow.yaml --params-file perfectgym.params.yaml --dry-run
```

Parameter rules:

- Top-level `params` values are defaults.
- `--params-file` values override top-level defaults.
- Repeated `--param name=value` values override both defaults and the params file.
- A string that is exactly one placeholder, such as `"{{params.concurrency}}"`, preserves the parameter value type.
- A placeholder embedded inside a larger string is formatted as text.
- Missing parameters fail before agents are launched.
- Detached runs persist parameters in detached metadata and pass them to the background child command.

## Detached Runs

`--detach` starts a child `ocmo operation run` or `ocmo workflow run` with `--yes` and `--ui plain`, then returns immediately:

```powershell
ocmo operation run .ocmo/business-taxonomy-prompt --select uncompleted --detach
```

Detached metadata and logs are written under `.ocmo/runs/` beside the manifest or workflow. `ocmo` also writes a global registry entry so list/status commands can find active runs even when you are not in the manifest or workflow directory. `ocmo operation list` also discovers generated `.ocmo/*/manifest.yaml` operations with state files, so foreground operations appear even when they were not started with `--detach`. `ocmo workflow list` discovers `.ocmo/*/workflow.yaml` workflows the same way.

```powershell
ocmo operation list --all
ocmo operation status --run-id <run-id>
ocmo operation status .ocmo/business-taxonomy-prompt
ocmo workflow list --all
ocmo workflow status --run-id <run-id>
```

Set `OCMO_RUN_REGISTRY` to override the global registry location.

Use `ocmo operation status --run-id <run-id>` or `ocmo workflow status --run-id <run-id>` to inspect detached runs. Operation list details include token totals when usage is available, and `ocmo operation list --all` includes inactive detached sessions and inactive discovered operation states. `ocmo workflow list --all` does the same for workflows. Erased operations and workflows are removed from list output, including `--all` output.

By default `ocmo operation status` and `ocmo workflow status` watch state with a live-refreshing snapshot (Rich `Live` UI when stdout is a TTY, plain redraw otherwise). Add `--once` to print one snapshot and exit, and `--interval <seconds>` to change the refresh cadence. `--active-or-latest` shows every active operation or workflow, falling back to the most-recent inactive entry when nothing is active; the bundled `/ocmo-operation-statuses` and `/ocmo-workflow-statuses` slash commands wrap that view for use inside `opencode`.

## Operation Control

Long-running operations can be stopped, resumed, or removed:

```powershell
ocmo operation pause .ocmo/business-taxonomy-prompt
ocmo operation resume .ocmo/business-taxonomy-prompt --yes
ocmo operation rerun .ocmo/business-taxonomy-prompt --select retryable --yes
ocmo operation kill .ocmo/business-taxonomy-prompt --force
ocmo operation erase .ocmo/business-taxonomy-prompt --force
```

`pause` is stop-and-resume, not an operating-system suspend. It terminates the active detached supervisor and any tracked child `opencode run` processes, then marks running work units/runs as `paused` when their opencode `sessionId` is known. If a process had not emitted a session id yet, the run is marked `paused_unresumable`.

Pressing `Ctrl+C` during a foreground `ocmo operation run` or `ocmo workflow run` uses pause semantics: tracked child processes are terminated, active work is marked paused when possible, and the command exits with code `130`.

Pressing `Ctrl+C` during `ocmo operation plan` terminates the active planner process when ocmo owns it, prints `ocmo: interrupted`, and exits with code `130` without writing a manifest unless planning had already reached the final write step.

`resume` is strict session continuation. It resumes only paused runs that have a persisted opencode session id and starts them with `opencode run --session <sessionId>`. It never falls back to `opencode --continue` because that can resume the wrong session during concurrent work.

`rerun` is a fresh start and never uses `--session`. By default, `ocmo operation rerun` selects `retryable` work units: `paused_unresumable`, `timed_out`, `failed`, `cleanup_failed`, `worktree_failed`, `setup_failed`, and `killed`. Use `--select unresumable`, `--select timed-out`, `--select failed`, `--select killed`, `--select all`, or explicit work unit IDs/ranges to narrow or expand the fresh rerun.

`run --fresh` is different from `rerun`: it erases runtime data first, then performs normal run selection against an empty operation state. Use it when you want the whole operation or workflow to behave like a clean build output, not just retry failed work.

If an `opencode run` exceeds `timeoutSeconds`, ocmo terminates the child process tree, marks the run and work unit `timed_out`, and treats that work unit as retryable for `ocmo operation rerun --select timed-out` or the default `--select retryable`.

`kill` terminates tracked processes and marks active work units/runs as `killed`, preserving the operation directory, state, outputs, logs, and artifacts for audit.

`erase` runs the same termination step and removes operation runtime data (`state.json`, `outputs/`, `artifacts/`, `prompt-inputs/`, detached run metadata, and manifest `clean.paths` entries whose `when` includes `erase`). It never deletes operation definition files such as `manifest.yaml`, prompt templates, or notes; delete those manually if they are no longer needed. In non-interactive mode, use `--force`. It refuses manifests outside `.ocmo/<operation>/manifest.yaml`.

## Selection Rules

Selectors match manifest work unit IDs and state-backed work unit status categories:

- `all`: every work unit.
- `pending`: work units with no state status, or state status `pending`.
- `uncompleted`: work units with no state status, or state status not `completed`, `done`, or `skipped`.
- `WORK-001`: one exact work unit ID.
- `WORK-001,WORK-002`: multiple exact work unit IDs.
- `41-48`: numeric range matching IDs `41` through `48`.
- `41-48,93-96,141`: mixed numeric ranges and exact IDs.

## Work Unit Statuses

Work unit status is runtime state, not manifest data. `ocmo operation run` writes status under the manifest's configured `state.path`, usually `state.json` beside `manifest.yaml`. Do not add `workUnits[].status` to manifests; validation rejects it.

Missing state is treated as `pending`. This means a new operation, a deleted state file, or a work unit with no `state.json` entry is selectable with `--select pending` and `--select uncompleted`.

| Status | Meaning | Selector behavior |
| --- | --- | --- |
| `pending` | No run has started for the work unit, or state explicitly marks it pending. | Selected by `pending` and `uncompleted`. |
| `running` | A run step is currently executing or was last recorded as active. | Selected by `uncompleted`. |
| `completed` | Every run step for the work unit succeeded. | Treated as done; excluded from `uncompleted`. |
| `done` | Manually accepted as done in state. | Treated as done; excluded from `uncompleted`. |
| `skipped` | Manually skipped in state. | Treated as done; excluded from `uncompleted`. |
| `failed` | A run failed before start, failed to start, or exited non-zero. | Selected by `uncompleted`; retryable. |
| `timed_out` | A run exceeded `timeoutSeconds` and was terminated. | Selected by `uncompleted`; retryable. |
| `blocked` | A handoff gate blocked later sequential run steps. | Selected by `uncompleted`; retryable. |
| `cleanup_failed` | The work unit completed, but configured cleanup failed. | Selected by `uncompleted`; retryable. |
| `worktree_failed` | Auto-worktree preparation, setup, or cleanup failed before the work could safely continue. | Selected by `uncompleted`; retryable. |
| `setup_failed` | A setup command failed before agent work started. | Selected by `uncompleted`; retryable. |
| `paused` | Work was stopped and has an opencode `sessionId`, so `ocmo operation resume` can continue it. | Selected by `uncompleted`; resumable, not selected by default `rerun --select retryable`. |
| `paused_unresumable` | Work was stopped before a usable session id was captured. | Selected by `uncompleted`; retryable. |
| `killed` | Work was explicitly terminated with `ocmo operation kill`. | Selected by `uncompleted`; retryable. |

Status groups used by selectors:

- Done statuses are `completed`, `done`, and `skipped`.
- Failed statuses are `failed`, `cleanup_failed`, `worktree_failed`, and `setup_failed`.
- `ocmo operation rerun --select retryable` selects failed statuses plus `blocked`, `paused_unresumable`, `timed_out`, and `killed`.
- `ocmo operation resume` is only for `paused` work with persisted opencode session ids; use `rerun` for `paused_unresumable` and other retryable statuses.

## State And Outputs

`ocmo operation run` writes durable state to the manifest's configured state path. State records execution facts such as work unit status, run status, start/completion times, exit codes, output paths, and worktree metadata.

Per-run `opencode` stdout/stderr is written under `outputs/` beside the manifest. This keeps concurrent agent output from corrupting the terminal UI and makes long-running work inspectable after the fact.

Failed work units remain selectable through `uncompleted` until their state status is completed, done, or skipped.

## Manifest Reference

The current operation manifest schema is `ocmo/v1`. Generated operation manifests usually live at `.ocmo/<operation>/manifest.yaml`, but commands accepting `[manifest-or-directory]` can also receive the operation directory.

Required shape:

```yaml
schema: ocmo/v1
operation: {}
runner: {}
selection: {}
queue: {}
policy: {}
prompt: {}
state: {}
workUnits: []
```

Manifest rules:

- `schema` must be `ocmo/v1`.
- `params` is an optional mapping of runtime parameter defaults. Values are available as `{{params.name}}` in manifests and workflows.
- `operation.id` is the stable operation identifier.
- `operation.description` should explain the operation goal in human-readable terms.
- `operation.workspace` is the target repository or directory where `opencode run` executes.
- `runner.command` is normally `opencode`.
- `runner.agent` is normally `build`; explicit run-step `agent` values must also be `build`.
- `runner.model` is optional and is passed to `opencode` when set. Use the `provider/model` form, where `provider` is one of `opencode`, `github-copilot`, `openai`, or `anthropic` (e.g. `github-copilot/claude-sonnet`, `openai/gpt-5.5`, `opencode/big-pickle`). A bare model id is also accepted and lets `opencode` resolve the default provider.
- `runner.reasoningEffort` is optional and forwards to `opencode --variant`; allowed values are `minimal`, `low`, `medium`, `high`. Per-run step overrides are supported.
- `runner.attach` is an optional `opencode serve` URL.
- `runner.timeoutSeconds` controls the per-run timeout unless overridden from the CLI.
- `runner.dangerouslySkipPermissions` passes `--dangerously-skip-permissions` when true.
- `selection.default` is used when `--select` is omitted. Prefer `uncompleted` for repeatable operations.
- `queue.concurrency` is maximum active work units, not maximum run steps inside one work unit.
- `queue.order` is currently `manifest`.
- `queue.stopOnFailure` controls whether the queue stops after failed work.
- `queue.autoWorktrees` configures native git worktree creation for isolated work-unit execution.
- `policy.worktree` is usually `single` or `per-work-unit`.
- `prompt.template` is resolved relative to `manifest.yaml`.
- `prompt.skills` lists opencode skills required in rendered work-unit prompts.
- `state.path` is resolved relative to `manifest.yaml`.
- Every `workUnits[]` entry needs a unique `id`.
- Do not use `workUnits[].status`; work unit status is stored in `state.json`.
- Work unit `payload` is task-specific data made available to the prompt template.

### Work Units

`workUnits[]` is the queue input for an operation. Each entry should describe one independently schedulable piece of work that can be rendered into a prompt and run without requiring OCMO to understand the task domain.

Common fields:

- `id`: stable unique identifier used by selectors, state, outputs, artifacts, and worktree branch names.
- `title`: short human-readable label shown in rendered prompts and status output.
- `payload`: task-specific data consumed by the prompt template. OCMO treats this as schema-free YAML/JSON data.
- `runs`: optional ordered run plan for multi-phase work inside one work unit.

Example:

```yaml
workUnits:
  - id: DOC-001
    title: Refresh README install instructions
    payload:
      path: README.md
      section: Install From Git

  - id: DOC-002
    title: Refresh workflow guide
    payload:
      path: docs/workflows.md
      section: Running workflows
```

Good work units have clear boundaries. Prefer payloads that identify the exact target files, records, entities, or ranges the agent may touch. If two selected work units may edit the same files, use concurrency `1` or enable auto worktrees so parallel agents do not collide in one checkout.

Selection uses work unit IDs and state statuses. For example, `--select DOC-001` runs one work unit, while `--select uncompleted` selects work units whose state status is not `completed`, `done`, or `skipped`. Missing state is treated as `pending`.

`queue.concurrency` is work-unit-level concurrency. If a work unit defines sequential `runs`, those run steps execute in order inside that one work unit and do not become independently queued work.

Prompt templates support Python `string.Template` variables such as `$operation_id`, `$workspace`, `$work_unit_id`, `$work_unit_title`, `$payload_json`, and `$work_unit_json`. They also support dotted placeholders such as `{{payload.path}}`. Unknown dotted placeholders fail before launching agents.

Use `prompt.skills` when every rendered work-unit prompt should require specific opencode skills:

```yaml
prompt:
  template: prompts/docs-refresh.md
  skills:
    - code-review
```

By default, one selected work unit starts one `opencode run` process. Use `workUnits[].runs.mode: sequential` when a work unit needs multiple ordered phases:

```yaml
workUnits:
  - id: DOC-001
    title: Refresh README
    payload:
      path: README.md
    runs:
      mode: sequential
      steps:
        - id: analyze
          agent: build
          prompt:
            template: prompts/analyze.md
        - id: implement
          agent: build
          prompt:
            template: prompts/implement.md
```

Sequential run steps can pass files with `produces` and `consumes`. Produced artifacts are written under `artifacts/<work-unit-id>/<step-id>/` beside the manifest unless a custom path under `artifacts/` is configured.

Use a `handoff` artifact when an earlier run must explicitly authorize the next run. Handoff artifacts default to `.json`, must use schema `ocmo-handoff/v1`, and can gate later sequential runs by decision, confidence, and conditions. If a gate fails, OCMO marks the run and work unit `blocked` and does not start later runs.

```yaml
workUnits:
  - id: BUG-123
    title: Fix parser bug
    runs:
      mode: sequential
      steps:
        - id: plan
          agent: build
          produces:
            handoff:
              required: true
              gates:
                decision: proceed
                minConfidence: 0.9
                requireConditionsMet: true
        - id: implement
          agent: build
          consumes:
            - plan.handoff
```

Example handoff JSON:

```json
{
  "schema": "ocmo-handoff/v1",
  "decision": "proceed",
  "confidence": 0.92,
  "summary": "Root cause identified.",
  "handoff": "Implement the minimal parser span fix and add regression coverage.",
  "conditions": [
    {"name": "root_cause_identified", "met": true, "evidence": "Failing behavior traced to span end normalization."}
  ],
  "risks": [],
  "nextAgentInstructions": "Add regression test first, then implement the fix."
}
```

Worktree safety matters when using concurrency. With `policy.worktree: single`, selected work units operate in one shared workspace, so concurrency above `1` is rejected unless you pass `--allow-shared-worktree-concurrency`. Use `queue.autoWorktrees.enabled: true` when work units may edit overlapping files or when parallel isolation is required.

The installed `/ocmo` skill includes a fuller, version-matched operational handbook for agents. If the installed skill docs look stale, run:

```powershell
ocmo skill install
```

## Examples

Reference examples are available in:

- `examples/report-rewrite.yaml`
- `examples/report-rewrite-auto-worktrees.yaml`
- `examples/report-rewrite-multi-agent.yaml`
- `examples/prompts/report-rewrite.md`
