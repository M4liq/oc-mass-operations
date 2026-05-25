# OCMO Skill Handbook

This handbook is installed with the `/ocmo` opencode skill by `ocmo skill install`. It is version-matched to the OCMO CLI that installed it.

Use it when working with OCMO commands, `ocmo/v1` operation manifests, `ocmo-workflow/v1` workflow files, generated `.ocmo` operation folders, state files, outputs, planning, execution, or operation/workflow control.

## Core Model

OC Mass Operations (`ocmo`) is a deterministic queue runner for repeatable `opencode` jobs.

- OCMO schedules `opencode`; it does not replace agent reasoning.
- One operation is backed by one `manifest.yaml`.
- One work unit is one independently schedulable unit of work within an operation.
- One selected work unit produces one rendered prompt per run step.
- Each run step starts a separate `opencode run` process.
- One workflow is backed by one `workflow.yaml` and runs operation manifests sequentially.
- OCMO writes durable state for status, resume, retry, and audit.
- Task-specific meaning belongs in manifest work unit payloads and prompt templates, not in OCMO itself.
- Use OCMO when one large request naturally splits into many similar units of work.

## Inspect First

Inspect before acting.

- Read `.ocmo/*/manifest.yaml` to understand intended work.
- Read `.ocmo/*/state.json` to understand actual execution status.
- Read `.ocmo/*/outputs/` when diagnosing `opencode` runs.
- Read `.ocmo/*/artifacts/` when sequential run steps pass files to later steps.
- Read workflow `workflow.yaml` and workflow state when orchestrating multiple operations.
- Read `.ocmo/runs/*.json` or use `ocmo operation list --all` / `ocmo workflow list --all` when detached runs are involved.

Prefer this workflow for existing operations:

1. Inspect manifest and state.
2. Run `ocmo operation validate <manifest>`.
3. Run `ocmo operation render [manifest-or-directory] --select <selector>` to inspect prompts.
4. Run `ocmo operation run [manifest-or-directory] --select <selector> --dry-run` before real execution.
5. Ask before starting long-running foreground or detached work unless explicitly requested.
6. Use `ocmo operation status`, `ocmo operation list`, `state.json`, outputs, and artifacts to inspect progress and results.

## Command Reference

### `ocmo operation plan`

```powershell
ocmo operation plan --from <prompt-file> [--out <manifest>] [--workspace <path>] [--read <source-file>] [--model <model>] [--max-attempts <count>] [--interactive] [--dry-run]
```

Converts a natural-language mass-operation request into an `ocmo/v1` manifest and any generated prompt templates. Planning does not execute work units.

- `--from <prompt-file>`: required natural-language operation prompt.
- `--out <manifest>`: manifest output path. If omitted, writes under `<workspace>/.ocmo/<prompt-stem>/manifest.yaml`.
- `--workspace <path>`: target workspace for planning. If omitted, defaults to the current directory.
- `--read <source-file>`: attaches a read-only source file for planner context. Can be repeated.
- `--model <model>`: opencode model for the planner.
- `--max-attempts <count>`: maximum planner correction attempts. Default is `3`.
- `--interactive`: allows the planner to ask terminal questions before returning marked YAML.
- `--dry-run`: prints the planning prompt without starting `opencode`.

Use `--read` for CSV exports, inventories, design notes, existing manifests, or other planning inputs.

### `ocmo operation validate`

```powershell
ocmo operation validate <manifest>
```

Validates manifest schema, paths, prompt templates, worktree settings, work unit run paths, and workspace assumptions.

### `ocmo operation render`

```powershell
ocmo operation render [manifest-or-directory] [--select <selector>] [--all]
```

Renders prompts for selected work units without running agents.

- `--select <selector>`: overrides the manifest default selector.
- `--all`: prints every rendered prompt instead of a compact preview.

Use `render` to inspect exact prompt text before launching agents.

### `ocmo operation run`

```powershell
ocmo operation run [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--dry-run] [--all] [--yes] [--ui auto|live|plain] [--detach] [--allow-shared-worktree-concurrency]
```

Runs selected work units.

- `--select <selector>`: overrides the manifest default selector.
- `--concurrency <count>`: overrides `queue.concurrency`.
- `--timeout-seconds <seconds>`: overrides per-process timeout.
- `--dry-run`: validates, selects, renders, and previews commands without launching agents, writing state, creating worktrees, or running setup/teardown.
- `--all`: with `--dry-run`, prints every rendered prompt instead of compact preview.
- `--yes`, `-y`: skips confirmation for foreground runs.
- `--ui auto|live|plain`: controls foreground terminal output.
- `--detach`: starts a background `ocmo operation run` with `--yes` and `--ui plain`, writes detached metadata/logs under `.ocmo/runs/`, writes a global registry entry, returns a run ID, and exits.
- `--allow-shared-worktree-concurrency`: allows concurrency above `1` when `policy.worktree: single`. Use only when selected work unit scopes are explicitly non-overlapping.

Pressing `Ctrl+C` during foreground `ocmo operation run` uses pause semantics: tracked child processes are terminated, active runs are marked `paused` or `paused_unresumable`, and the command exits `130`.

### `ocmo operation status`

```powershell
ocmo operation status [manifest-or-directory] [--run-id <run-id>] [--all] [--interval <seconds>] [--once]
```

Continuously refreshes work unit/run status and detached run information until interrupted with `Ctrl+C`.

- No argument lists active detached runs from the global registry when no manifest can be inferred.
- `[manifest-or-directory]` shows status for one operation.
- `--run-id <run-id>` resolves detached metadata and shows that run's operation status.
- `--all` includes inactive detached run sessions.
- `--interval <seconds>` changes the refresh cadence from the default `1` second.
- `--once` prints a single snapshot and exits. Use it for scripts or logs.

When `opencode run --format json` reports step usage, status shows operation token totals and a per-work-unit `Tokens` column formatted as `input/output`.

### `ocmo operation list`

```powershell
ocmo operation list [manifest-or-directory] [--run-id <run-id>] [--all]
```

Lists detached operation run sessions. Use `--all` to include inactive sessions. `ocmo operation list --run-id <run-id>` includes a state summary with token totals when usage is available.

### `ocmo operation pause`

```powershell
ocmo operation pause [manifest-or-directory] [--run-id <run-id>]
```

Stops active tracked processes and marks running work units/runs as paused.

- `pause` is stop-and-resume, not an operating-system suspend.
- Runs with a known opencode `sessionId` are marked `paused`.
- Runs without a known session id are marked `paused_unresumable`.

### `ocmo operation resume`

```powershell
ocmo operation resume [manifest-or-directory] [--detach] [--yes] [--ui auto|live|plain]
```

Strictly resumes paused runs by persisted opencode session id.

- Uses `opencode run --session <sessionId>`.
- Never falls back to `opencode --continue`.
- Fails for `paused_unresumable` work.
- Use `ocmo operation rerun` for fresh retry of unresumable work.

### `ocmo operation rerun`

```powershell
ocmo operation rerun [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--detach] [--yes] [--ui auto|live|plain] [--allow-shared-worktree-concurrency]
```

Fresh-starts selected work units. It never uses `--session`.

- Default selector is `retryable`.
- `retryable` includes `paused_unresumable`, `timed_out`, `failed`, `cleanup_failed`, `worktree_failed`, `setup_failed`, and `killed`.
- Use `--select unresumable`, `--select paused_unresumable`, `--select timed-out`, `--select timed_out`, `--select failed`, `--select killed`, `--select all`, exact work unit IDs, or numeric ranges.

Use rerun for failed, timed-out, killed, or unresumable work. Use resume for clean paused work with session ids.

### `ocmo operation kill`

```powershell
ocmo operation kill [manifest-or-directory] [--run-id <run-id>] [--force]
```

Terminates active tracked processes and marks active work `killed`, preserving operation files, state, outputs, logs, and artifacts for audit.

### `ocmo operation erase`

```powershell
ocmo operation erase [manifest-or-directory] [--run-id <run-id>] --force
```

Terminates tracked processes and deletes a generated operation directory such as `.ocmo/<operation>/`.

- Requires `--force` when non-interactive.
- Refuses manifests outside `.ocmo/<operation>/manifest.yaml`.
- Use only when the user wants to remove operation files.

### `ocmo skill`

```powershell
ocmo skill install [--force]
ocmo skill path
```

- `install`: installs or updates the bundled `/ocmo` opencode skill and this handbook under `~/.config/opencode/skills/ocmo/`.
- `path`: prints the installed `SKILL.md` path.
- `--force`: accepted for compatibility; install updates bundled skill files by default.

Restart opencode after installing or updating the skill.

## Manifest Inference

Commands accepting `[manifest-or-directory]` resolve manifests as follows:

- No argument uses `manifest.yaml` in the current directory.
- A directory argument uses `<directory>/manifest.yaml`.
- If missing, exactly one `.ocmo/*/manifest.yaml` may be inferred.
- If multiple generated manifests exist, pass one explicitly.

## Workflow Reference

The current workflow schema is `ocmo-workflow/v1`. Workflows are sequential only and orchestrate existing operation manifests.

```yaml
schema: ocmo-workflow/v1

workflow:
  id: example-workflow
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

Workflow facts:

- Workflow `--select` selects workflow steps, not work units.
- Referenced operation manifests control their own work unit selection, concurrency, timeouts, and worktree safety policy.
- Workflow state records orchestration status; operation state remains authoritative for work units, runs, sessions, outputs, artifacts, and token usage.
- `ocmo workflow rerun` defaults to retryable workflow steps, then delegates work unit selection to each referenced operation.
- `ocmo workflow pause` and `ocmo workflow kill` delegate to the active operation step and preserve operation files.

## Selection Rules

General selectors match manifest work unit IDs or state-backed work unit status categories.

- `all`: every work unit.
- `pending`: work units with no state status, or state status `pending`.
- `uncompleted`: work units with no state status, or state status not `completed`, `done`, or `skipped`.
- `WORK-001`: one exact work unit ID.
- `WORK-001,WORK-002`: multiple exact work unit IDs.
- `41-48`: numeric range matching IDs `41` through `48`.
- `41-48,93-96,141`: mixed numeric ranges and exact IDs.

Rerun-specific selectors include:

- `retryable`: failed, timed out, killed, and unresumable work.
- `unresumable` or `paused_unresumable`: paused work without a session id.
- `timed-out` or `timed_out`: timed-out work.
- `failed`: failed work.
- `killed`: killed work.

## Work Unit Statuses

Work unit status is runtime state, not manifest data. `ocmo operation run` writes status under the manifest's configured `state.path`, usually `state.json` beside `manifest.yaml`. Do not add `workUnits[].status` to manifests; validation rejects it.

Missing state is treated as `pending`. A new operation, deleted state file, or work unit with no `state.json` entry is selectable with `--select pending` and `--select uncompleted`.

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

## Manifest Reference

The current manifest schema is `ocmo/v1`.

```yaml
schema: ocmo/v1

operation:
  id: example-operation
  description: Process a set of example work units.
  workspace: C:\path\to\target-repo

runner:
  command: opencode
  agent: build
  model: openai/gpt-5.5
  attach: null
  timeoutSeconds: 14400
  dangerouslySkipPermissions: false

selection:
  default: uncompleted

queue:
  concurrency: 1
  order: manifest
  stopOnFailure: false
  autoWorktrees:
    enabled: false
    root: .ocmo/worktrees
    baseBranch: main
    branchPattern: ocmo/{operation_id}/{work_unit_id}
    setup: []
    teardown: []
    cleanup: never

policy:
  worktree: single
  baseBranch: main

prompt:
  template: prompts/example.md
  skills: []

state:
  path: state.json

workUnits:
  - id: ITEM-001
    title: Example work unit
    payload:
      targetName: Example
```

Manifest rules:

- `schema` must be `ocmo/v1`.
- `operation.id` is the stable operation identifier.
- `operation.description` should explain the operation goal in human-readable terms.
- `operation.workspace` is the target repository or directory for `opencode run`.
- `runner.command` is normally `opencode`.
- Explicit `runner.agent` and run-step `agent` values must be `build`.
- `runner.model` is optional.
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
- `prompt.skills` lists opencode skills required in rendered work unit prompts.
- `state.path` is resolved relative to `manifest.yaml`.
- Every work unit needs a unique `id`.
- Do not use `workUnits[].status`; work unit status is stored in `state.json`.
- Work unit `payload` is task-specific and should contain the data needed by the prompt template.
- Do not use `operation.kind` or `runner.mode`; OCMO rejects those fields.

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

## Prompt Templates

Prompt templates use Python `string.Template` `$name` substitutions and OCMO dotted placeholders such as `{{payload.targetName}}`.

Common variables:

- `$operation_json`
- `$policy_json`
- `$work_unit_json`
- `$payload_json`
- `$run_json`
- `$operation_id`
- `$workspace`
- `$work_unit_id`
- `$work_unit_title`
- `$work_unit_file`
- `$run_id`
- `$run_agent`
- `$run_model`
- `$run_index`
- `$run_count`
- `$run_mode`
- `$skill_instructions`
- `$skill_commands`
- `$skill_names`
- `$worktree_path`
- `$source_workspace`
- `$branch_name`

Prompt templates should tell the agent exactly how to complete one work unit and how to stop. They must make clear that the agent must not work on any other work unit.

Unknown dotted placeholders fail before launching agents. YAML `null` placeholder values render as empty strings.

## Skills Inside Work Unit Prompts

Use `prompt.skills` when every rendered work unit prompt must require specific opencode skills.

```yaml
prompt:
  template: prompts/example.md
  skills:
    - code-review
```

Skill names can be written with or without the leading slash. OCMO normalizes them to slash commands.

Run-specific `prompt.skills` can replace top-level skills for one sequential step.

## Sequential Runs And Artifacts

By default, one selected work unit starts one `opencode run` process. Use `workUnits[].runs.mode: sequential` when each work unit needs multiple phases.

```yaml
workUnits:
  - id: ITEM-001
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

Sequential run facts:

- Runs within one work unit execute in manifest order.
- Each run starts a separate `opencode run` process.
- `queue.concurrency` remains work-unit-level concurrency.
- Auto worktrees are per work unit, not per run.
- Setup runs once before the first step.
- Teardown and cleanup run once after all steps succeed or after the first failed step.
- The work unit is `completed` only after every step succeeds.
- If one step fails or times out, later steps for that work unit are skipped.

Use `produces` and `consumes` for files passed between sequential run steps.

```yaml
workUnits:
  - id: ITEM-001
    runs:
      mode: sequential
      steps:
        - id: plan
          agent: build
          produces:
            plan:
              required: true
              description: Implementation plan for this work unit.
        - id: implement
          agent: build
          consumes:
            - plan.plan
```

Artifact rules:

- `produces.<artifact-id>` declares a file produced by a run step.
- `consumes` references earlier artifacts with `step.artifact` syntax.
- By default, `produces.plan` writes to `artifacts/<work-unit-id>/<step-id>/plan.md` beside `manifest.yaml`.
- By default, `handoff` artifacts and artifacts with `gates` write to `.json` instead of `.md`.
- Custom artifact paths are allowed only under `artifacts/`.
- Required artifacts must exist and be non-empty after the producing run, or the step fails.
- Consuming steps receive artifact content under `## Chained Inputs` in the rendered prompt.

Use a `handoff` artifact when one run must decide whether later sequential runs are safe to start. Handoff artifacts must be JSON objects with schema `ocmo-handoff/v1`. If a handoff gate fails, OCMO marks the run and work unit `blocked` and does not start later runs.

```yaml
workUnits:
  - id: BUG-123
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

Handoff JSON shape:

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

Gate rules:

- `gates.decision` requires exact `decision` match.
- `gates.minConfidence` requires numeric `confidence` between `0` and `1` and at least the configured value.
- `gates.requireConditionsMet: true` requires every `conditions[].met` value to be `true`.
- Invalid handoff JSON is a failure; valid JSON that does not satisfy a gate is blocked.

## Worktree And Concurrency Safety

`queue.concurrency` is work-unit-level concurrency. It does not make run steps inside one work unit parallel.

`policy.worktree: single` means selected work units operate in one shared workspace.

- Safe only when work unit scopes are explicitly non-overlapping.
- `validate` and `render` warn for `queue.concurrency > 1`.
- `ocmo operation run` rejects shared single-worktree concurrency above `1` unless passed `--allow-shared-worktree-concurrency`.
- `policy.worktree: single` cannot be combined with `queue.autoWorktrees.enabled: true`.

Use `queue.autoWorktrees.enabled: true` when work units may edit overlapping files, when isolation matters, or when parallel work unit execution is desired without shared-workspace risk. Auto worktrees use native `git worktree`; they are per work unit, not per run step.

## State And Outputs

`ocmo operation run` writes durable state to the manifest's configured state path. State records execution facts such as work unit status, run status, start/completion times, exit codes, output paths, worktree metadata, process IDs, opencode session IDs, and per-run token usage when `opencode` reports it.

Per-run `opencode` stdout/stderr is written under `outputs/` beside the manifest. This keeps concurrent agent output from corrupting the terminal UI and makes long-running work inspectable after the fact.

Generated operation layout usually looks like:

```text
<workspace>/.ocmo/<operation>/manifest.yaml
<workspace>/.ocmo/<operation>/state.json
<workspace>/.ocmo/<operation>/prompts/<template>.md
<workspace>/.ocmo/<operation>/outputs/<work-unit-id>__<run-id>.txt
<workspace>/.ocmo/<operation>/artifacts/<work-unit-id>/<run-id>/<artifact-id>.md
<workspace>/.ocmo/<operation>/.ocmo/runs/<detached-run-id>.json
```

Failed work units remain selectable through `uncompleted` until their state status is `completed`, `done`, or `skipped`.

## Planning Checklist

Before running `ocmo operation plan`, make sure the request identifies enough of:

- Target workspace.
- Operation goal and definition of done.
- Work unit boundaries and work unit IDs.
- Whether work unit scopes overlap.
- Worktree policy and concurrency.
- Timeout expectations.
- Prompt constraints every work unit must follow.
- Whether one run step is enough or sequential phases are needed.
- Whether artifacts should pass information between phases.
- Which opencode skills, if any, should be required in rendered work unit prompts.
- Files to attach with `--read`.

If the request is vague, ask focused questions. Do not turn this into a long grilling ritual by default; ask only what is necessary for safe planning or execution.

Do not run `ocmo operation plan`, `ocmo operation run`, `ocmo operation resume`, `ocmo operation rerun`, `ocmo operation kill`, `ocmo operation erase`, or workflow control commands unless the user asked for that action or approved the relevant operation/workflow.
