# OCMO Skill Handbook

This handbook is installed with the `/ocmo` opencode skill by `ocmo skill install`. It is version-matched to the OCMO CLI that installed it.

Use it when working with OCMO commands, `ocmo/v1` manifests, generated `.ocmo` operation folders, state files, outputs, planning, execution, or operation control.

## Core Model

OC Mass Operations (`ocmo`) is a deterministic queue runner for repeatable `opencode` jobs.

- OCMO schedules `opencode`; it does not replace agent reasoning.
- One operation is backed by one `manifest.yaml`.
- One operation item is one independently schedulable unit of work.
- One selected item produces one rendered prompt per run step.
- Each run step starts a separate `opencode run` process.
- OCMO writes durable state for status, resume, retry, and audit.
- Task-specific meaning belongs in manifest item payloads and prompt templates, not in OCMO itself.
- Use OCMO when one large request naturally splits into many similar units of work.

## Inspect First

Inspect before acting.

- Read `.ocmo/*/manifest.yaml` to understand intended work.
- Read `.ocmo/*/state.json` to understand actual execution status.
- Read `.ocmo/*/outputs/` when diagnosing `opencode` runs.
- Read `.ocmo/*/artifacts/` when sequential run steps pass files to later steps.
- Read `.ocmo/runs/*.json` or use `ocmo list --all` when detached runs are involved.

Prefer this workflow for existing operations:

1. Inspect manifest and state.
2. Run `ocmo validate <manifest>`.
3. Run `ocmo render [manifest-or-directory] --select <selector>` to inspect prompts.
4. Run `ocmo run [manifest-or-directory] --select <selector> --dry-run` before real execution.
5. Ask before starting long-running foreground or detached work unless explicitly requested.
6. Use `ocmo status`, `ocmo list`, `state.json`, outputs, and artifacts to inspect progress and results.

## Command Reference

### `ocmo plan`

```powershell
ocmo plan --from <prompt-file> [--out <manifest>] [--workspace <path>] [--read <source-file>] [--model <model>] [--max-attempts <count>] [--interactive] [--dry-run]
```

Converts a natural-language mass-operation request into an `ocmo/v1` manifest and any generated prompt templates. Planning does not execute operation items.

- `--from <prompt-file>`: required natural-language operation prompt.
- `--out <manifest>`: manifest output path. If omitted, writes under `<workspace>/.ocmo/<prompt-stem>/manifest.yaml`.
- `--workspace <path>`: target workspace for planning. If omitted, defaults to the current directory.
- `--read <source-file>`: attaches a read-only source file for planner context. Can be repeated.
- `--model <model>`: opencode model for the planner.
- `--max-attempts <count>`: maximum planner correction attempts. Default is `3`.
- `--interactive`: allows the planner to ask terminal questions before returning marked YAML.
- `--dry-run`: prints the planning prompt without starting `opencode`.

Use `--read` for CSV exports, inventories, design notes, existing manifests, or other planning inputs.

### `ocmo validate`

```powershell
ocmo validate <manifest>
```

Validates manifest schema, paths, prompt templates, worktree settings, item run paths, and workspace assumptions.

### `ocmo render`

```powershell
ocmo render [manifest-or-directory] [--select <selector>] [--all]
```

Renders prompts for selected items without running agents.

- `--select <selector>`: overrides the manifest default selector.
- `--all`: prints every rendered prompt instead of a compact preview.

Use `render` to inspect exact prompt text before launching agents.

### `ocmo run`

```powershell
ocmo run [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--dry-run] [--all] [--yes] [--ui auto|live|plain] [--detach] [--allow-shared-worktree-concurrency]
```

Runs selected operation items.

- `--select <selector>`: overrides the manifest default selector.
- `--concurrency <count>`: overrides `queue.concurrency`.
- `--timeout-seconds <seconds>`: overrides per-process timeout.
- `--dry-run`: validates, selects, renders, and previews commands without launching agents, writing state, creating worktrees, or running setup/teardown.
- `--all`: with `--dry-run`, prints every rendered prompt instead of compact preview.
- `--yes`, `-y`: skips confirmation for foreground runs.
- `--ui auto|live|plain`: controls foreground terminal output.
- `--detach`: starts a background `ocmo run` with `--yes` and `--ui plain`, writes detached metadata/logs under `.ocmo/runs/`, writes a global registry entry, returns a run ID, and exits.
- `--allow-shared-worktree-concurrency`: allows concurrency above `1` when `policy.worktree: single`. Use only when selected item scopes are explicitly non-overlapping.

Pressing `Ctrl+C` during foreground `ocmo run` uses pause semantics: tracked child processes are terminated, active runs are marked `paused` or `paused_unresumable`, and the command exits `130`.

### `ocmo status`

```powershell
ocmo status [manifest-or-directory] [--run-id <run-id>] [--all]
```

Shows operation item/run status and detached run information.

- No argument lists active detached runs from the global registry when no manifest can be inferred.
- `[manifest-or-directory]` shows status for one operation.
- `--run-id <run-id>` resolves detached metadata and shows that run's operation status.
- `--all` includes inactive detached run sessions.

### `ocmo list`

```powershell
ocmo list [manifest-or-directory] [--run-id <run-id>] [--all]
```

Lists detached run sessions. Use `--all` to include inactive sessions.

### `ocmo pause`

```powershell
ocmo pause [manifest-or-directory] [--run-id <run-id>]
```

Stops active tracked processes and marks running items/runs as paused.

- `pause` is stop-and-resume, not an operating-system suspend.
- Runs with a known opencode `sessionId` are marked `paused`.
- Runs without a known session id are marked `paused_unresumable`.

### `ocmo resume`

```powershell
ocmo resume [manifest-or-directory] [--detach] [--yes] [--ui auto|live|plain]
```

Strictly resumes paused runs by persisted opencode session id.

- Uses `opencode run --session <sessionId>`.
- Never falls back to `opencode --continue`.
- Fails for `paused_unresumable` work.
- Use `ocmo rerun` for fresh retry of unresumable work.

### `ocmo rerun`

```powershell
ocmo rerun [manifest-or-directory] [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--detach] [--yes] [--ui auto|live|plain] [--allow-shared-worktree-concurrency]
```

Fresh-starts selected operation items. It never uses `--session`.

- Default selector is `retryable`.
- `retryable` includes `paused_unresumable`, `timed_out`, `failed`, `cleanup_failed`, `worktree_failed`, `setup_failed`, and `killed`.
- Use `--select unresumable`, `--select paused_unresumable`, `--select timed-out`, `--select timed_out`, `--select failed`, `--select killed`, `--select all`, exact item IDs, or numeric ranges.

Use rerun for failed, timed-out, killed, or unresumable work. Use resume for clean paused work with session ids.

### `ocmo kill`

```powershell
ocmo kill [manifest-or-directory] [--run-id <run-id>] [--force]
```

Terminates active tracked processes and marks active work `killed`, preserving operation files, state, outputs, logs, and artifacts for audit.

### `ocmo erase`

```powershell
ocmo erase [manifest-or-directory] [--run-id <run-id>] --force
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

## Selection Rules

General selectors match manifest item IDs or item status categories.

- `all`: every item.
- `pending`: items whose manifest status is `pending`.
- `uncompleted`: items whose manifest status is not `completed`, `done`, or `skipped`.
- `WORK-001`: one exact item ID.
- `WORK-001,WORK-002`: multiple exact item IDs.
- `41-48`: numeric range matching IDs `41` through `48`.
- `41-48,93-96,141`: mixed numeric ranges and exact IDs.

Rerun-specific selectors include:

- `retryable`: failed, timed out, killed, and unresumable work.
- `unresumable` or `paused_unresumable`: paused work without a session id.
- `timed-out` or `timed_out`: timed-out work.
- `failed`: failed work.
- `killed`: killed work.

## Manifest Reference

The current manifest schema is `ocmo/v1`.

```yaml
schema: ocmo/v1

operation:
  id: example-operation
  description: Process a set of example work items.
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
    branchPattern: ocmo/{operation_id}/{item_id}
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

items:
  - id: ITEM-001
    title: Example item
    status: pending
    payload:
      targetName: Example
```

Manifest rules:

- `schema` must be `ocmo/v1`.
- `operation.id` is the stable operation identifier.
- `operation.workspace` is the target repository or directory for `opencode run`.
- `runner.command` is normally `opencode`.
- Explicit `runner.agent` and run-step `agent` values must be `build`.
- `runner.model` is optional.
- `runner.attach` is an optional `opencode serve` URL.
- `runner.timeoutSeconds` is an optional per-run timeout.
- `runner.dangerouslySkipPermissions` passes `--dangerously-skip-permissions` when true.
- `selection.default` is used when `--select` is omitted. Prefer `uncompleted`.
- `queue.concurrency` is maximum active operation items.
- `queue.order` is currently `manifest`.
- `queue.stopOnFailure` is reserved for stricter failure handling.
- `queue.autoWorktrees` configures native git worktree creation per selected item.
- `policy.worktree` is usually `single` or isolated through auto worktrees.
- `prompt.template` is resolved relative to `manifest.yaml`.
- `prompt.skills` lists opencode skills required in rendered item prompts.
- `state.path` is resolved relative to `manifest.yaml`.
- Every item needs a unique `id`.
- Item `payload` is task-specific and should contain the data needed by the prompt template.
- Do not use `operation.kind` or `runner.mode`; OCMO rejects those fields.

## Prompt Templates

Prompt templates use Python `string.Template` `$name` substitutions and OCMO dotted placeholders such as `{{payload.targetName}}`.

Common variables:

- `$operation_json`
- `$policy_json`
- `$item_json`
- `$payload_json`
- `$run_json`
- `$operation_id`
- `$workspace`
- `$item_id`
- `$item_title`
- `$item_file`
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

Prompt templates should tell the agent exactly how to complete one item and how to stop. They must make clear that the agent must not work on any other item.

Unknown dotted placeholders fail before launching agents. YAML `null` placeholder values render as empty strings.

## Skills Inside Item Prompts

Use `prompt.skills` when every rendered item prompt must require specific opencode skills.

```yaml
prompt:
  template: prompts/example.md
  skills:
    - code-review
```

Skill names can be written with or without the leading slash. OCMO normalizes them to slash commands.

Run-specific `prompt.skills` can replace top-level skills for one sequential step.

## Sequential Runs And Artifacts

By default, one selected item starts one `opencode run` process. Use `items[].runs.mode: sequential` when each item needs multiple phases.

```yaml
items:
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

- Runs within one item execute in manifest order.
- Each run starts a separate `opencode run` process.
- `queue.concurrency` remains item-level concurrency.
- Auto worktrees are per item, not per run.
- Setup runs once before the first step.
- Teardown and cleanup run once after all steps succeed or after the first failed step.
- The item is `completed` only after every step succeeds.
- If one step fails or times out, later steps for that item are skipped.

Use `produces` and `consumes` for files passed between sequential run steps.

```yaml
items:
  - id: ITEM-001
    runs:
      mode: sequential
      steps:
        - id: plan
          agent: build
          produces:
            plan:
              required: true
              description: Implementation plan for this item.
        - id: implement
          agent: build
          consumes:
            - plan.plan
```

Artifact rules:

- `produces.<artifact-id>` declares a file produced by a run step.
- `consumes` references earlier artifacts with `step.artifact` syntax.
- By default, `produces.plan` writes to `artifacts/<item-id>/<step-id>/plan.md` beside `manifest.yaml`.
- Custom artifact paths are allowed only under `artifacts/`.
- Required artifacts must exist and be non-empty after the producing run, or the step fails.
- Consuming steps receive artifact content under `## Chained Inputs` in the rendered prompt.

## Worktree And Concurrency Safety

`queue.concurrency` is item-level concurrency. It does not make run steps inside one item parallel.

`policy.worktree: single` means selected items operate in one shared workspace.

- Safe only when item scopes are explicitly non-overlapping.
- `validate` and `render` warn for `queue.concurrency > 1`.
- `ocmo run` rejects shared single-worktree concurrency above `1` unless passed `--allow-shared-worktree-concurrency`.
- `policy.worktree: single` cannot be combined with `queue.autoWorktrees.enabled: true`.

Use `queue.autoWorktrees.enabled: true` when items may edit overlapping files, when isolation matters, or when parallel item execution is desired without shared-workspace risk. Auto worktrees use native `git worktree`; they are per item, not per run step.

## State And Outputs

`ocmo run` writes durable state to the manifest's configured state path. State records execution facts such as item status, run status, start/completion times, exit codes, output paths, worktree metadata, process IDs, and opencode session IDs.

Per-run `opencode` stdout/stderr is written under `outputs/` beside the manifest. This keeps concurrent agent output from corrupting the terminal UI and makes long-running work inspectable after the fact.

Generated operation layout usually looks like:

```text
<workspace>/.ocmo/<operation>/manifest.yaml
<workspace>/.ocmo/<operation>/state.json
<workspace>/.ocmo/<operation>/prompts/<template>.md
<workspace>/.ocmo/<operation>/outputs/<item-id>__<run-id>.txt
<workspace>/.ocmo/<operation>/artifacts/<item-id>/<run-id>/<artifact-id>.md
<workspace>/.ocmo/<operation>/.ocmo/runs/<detached-run-id>.json
```

Failed items remain selectable through `uncompleted` unless you mark them completed or skipped in the manifest.

## Planning Checklist

Before running `ocmo plan`, make sure the request identifies enough of:

- Target workspace.
- Operation goal and definition of done.
- Item boundaries and item IDs.
- Whether item scopes overlap.
- Worktree policy and concurrency.
- Timeout expectations.
- Prompt constraints every item must follow.
- Whether one run step is enough or sequential phases are needed.
- Whether artifacts should pass information between phases.
- Which opencode skills, if any, should be required in rendered item prompts.
- Files to attach with `--read`.

If the request is vague, ask focused questions. Do not turn this into a long grilling ritual by default; ask only what is necessary for safe planning or execution.

Do not run `ocmo plan`, `ocmo run`, `ocmo resume`, `ocmo rerun`, `ocmo kill`, or `ocmo erase` unless the user asked for that action or approved the relevant operation.
