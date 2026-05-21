---
name: ocmo-plan-grill
description: Use when turning a vague mass-operation request into an OCMO planning prompt, grilling the user before running ocmo plan.
---

# OCMO Plan Grill

Use this skill when the user wants to turn a vague mass-operation idea into an `ocmo plan` request.

Your role is to sharpen the request before planning. Do not run `ocmo plan` until the user approves the final planning prompt.

## Workflow

1. Restate the user's goal in one concise sentence.
2. Ask focused questions until the operation is specific enough for safe OCMO planning.
3. Draft the exact natural-language prompt that will be passed to `ocmo plan`.
4. Ask the user to approve or revise that final prompt.
5. After approval, write the prompt to a local text file and run `ocmo plan --from <prompt-file> --workspace <workspace>` with any useful `--read` files.
6. After planning, tell the user where the manifest was written and suggest `ocmo validate`, `ocmo render`, `ocmo run --dry-run`, or `ocmo run --detach`.

## Mental Model

OC Mass Operations (`ocmo`) is a deterministic queue runner for repeatable `opencode` jobs.

- OCMO schedules `opencode`; it does not replace agent reasoning.
- One operation is backed by one manifest.
- One operation item is one independently schedulable unit of work.
- One selected item produces one rendered prompt per run step.
- Each run step starts a separate isolated `opencode run` process.
- A durable state file records item and run status for resume/audit.
- Task-specific meaning belongs in manifest item payloads and prompt templates, not in OCMO itself.
- Use OCMO when one large request naturally splits into many similar units of work.

## Questions To Ask

Ask only what matters for the current operation. Prefer short batches of questions.

- What target workspace should `ocmo plan` use?
- What is the operation goal and definition of done?
- What are the item boundaries: files, folders, Jira issues, rows, ranges, modules, docs, or other units?
- How should item IDs be formed, and should the first run target all, pending, uncompleted, exact IDs, or numeric ranges?
- Are item scopes independent and non-overlapping?
- Should items share one worktree, or should `queue.autoWorktrees.enabled: true` isolate items in native git worktrees?
- What `queue.concurrency` and timeout are appropriate?
- What should each item produce or change?
- Is one `opencode run` per item enough, or does each item need sequential phases such as analyze, implement, review, fix?
- Should one phase pass files to a later phase through `produces` and `consumes` artifacts?
- Which opencode skills must be used inside the generated item prompts?
- Should skills apply to every run through top-level `prompt.skills`, or only to specific run steps through run-level `prompt.skills`?
- What source files should be attached to planning with `ocmo plan --read`?
- What prompt constraints must every item follow?
- Should execution run in the foreground, or should it use `ocmo run --detach`?

## Manifest Reference

The planner must produce an `ocmo/v1` manifest.

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
  template: prompts/example-workflow.md
  skills:
    - code-review

state:
  path: state.json

items:
  - id: ITEM-001
    title: Example item
    status: pending
    payload:
      targetName: Example
```

Manifest sections:

- `schema`: current value is `ocmo/v1`.
- `operation.id`: stable operation identifier, used in state/log naming.
- `operation.workspace`: target repository or directory for `opencode run`.
- `runner.command`: normally `opencode`.
- `runner.agent`: optional, but if present it must be `build`.
- `runner.model`: optional model passed to `opencode`.
- `runner.attach`: optional `opencode serve` URL.
- `runner.timeoutSeconds`: optional per-run timeout.
- `runner.dangerouslySkipPermissions`: passes `--dangerously-skip-permissions` when true.
- `selection.default`: default selector when `--select` is omitted. Prefer `uncompleted`.
- `queue.concurrency`: maximum active operation items.
- `queue.order`: currently `manifest`.
- `queue.stopOnFailure`: reserved for stricter failure handling.
- `queue.autoWorktrees`: optional native git worktree creation per selected item.
- `policy.worktree`: worktree safety policy, usually `single` or isolated via auto worktrees.
- `prompt.template`: path to the per-item prompt template, resolved relative to `manifest.yaml`.
- `prompt.skills`: opencode skill names to require inside rendered prompts.
- `state.path`: durable state JSON file, resolved relative to `manifest.yaml`.
- `items`: explicit item list. Every item needs a unique `id`; `payload` is task-specific.

Do not use `operation.kind` or `runner.mode`; OCMO rejects those fields. Explicit top-level and run-step agents must be `build`.

## Path Resolution

Manifest paths are resolved relative to the manifest file.

- `operation.workspace` can be absolute or relative to `manifest.yaml`.
- `prompt.template` should usually be under `prompts/` beside the manifest.
- `state.path` should usually be `state.json` beside the manifest for generated operation folders.
- Generated prompt templates should be returned as file blocks and written beside the manifest.
- Required chained artifacts must live under `artifacts/` beside the manifest.

Default `ocmo plan --from prompt.txt` output layout when `--out` is omitted:

```text
<workspace>/.ocmo/<prompt-stem>/manifest.yaml
<workspace>/.ocmo/<prompt-stem>/state.json
<workspace>/.ocmo/<prompt-stem>/prompts/<generated-template>.md
<workspace>/.ocmo/<prompt-stem>/outputs/<item-id>__<run-id>.txt
<workspace>/.ocmo/<prompt-stem>/artifacts/<item-id>/<run-id>/<artifact-id>.md
```

## Prompt Templates

Prompt templates use Python `string.Template` `$name` substitutions and OCMO dotted placeholders such as `{{payload.rangeStart}}`.

Common template variables:

- `$operation_json`: JSON representation of `operation`.
- `$policy_json`: JSON representation of `policy`.
- `$item_json`: JSON representation of the current item.
- `$payload_json`: JSON representation of the current item payload.
- `$run_json`: JSON representation of the current run step.
- `$operation_id`: operation ID.
- `$workspace`: workspace value from the manifest.
- `$item_id`: current item ID.
- `$item_title`: current item title, if present.
- `$item_file`: current item file, if present.
- `$run_id`: current run step ID.
- `$run_agent`: effective agent for the current run.
- `$run_model`: effective model for the current run.
- `$run_index`: one-based index of the current run within the item.
- `$run_count`: number of runs for the current item.
- `$run_mode`: run mode, currently `sequential`.
- `$skill_instructions`: deterministic instruction block for configured skills.
- `$skill_commands`: configured skills as slash commands, one per line.
- `$skill_names`: configured skill names without leading slashes, comma-separated.
- `$worktree_path`: per-item worktree path when auto worktrees are enabled.
- `$source_workspace`: original source checkout path when auto worktrees are enabled.
- `$branch_name`: per-item branch name when auto worktrees are enabled.

Prompt templates should tell the agent exactly how to complete one item and how to stop. They must make clear that the agent must not work on any other item.

Unknown dotted `{{...}}` placeholders fail before launching agents. YAML `null` placeholder values render as empty strings.

## Using Skills Inside Item Agents

Use `prompt.skills` when every rendered item prompt must require specific opencode skills:

```yaml
prompt:
  template: prompts/example-workflow.md
  skills:
    - code-review
    - repository-audit
```

OCMO prepends deterministic instructions like:

```text
You must use the following opencode skills before doing this task, in order:
- /code-review
- /repository-audit
```

Skill names can be written with or without the leading slash. OCMO normalizes them to slash commands.

Run-specific prompt settings can replace top-level skills for one sequential step:

```yaml
items:
  - id: ITEM-001
    runs:
      mode: sequential
      steps:
        - id: implement
          agent: build
          prompt:
            skills:
              - implementation
        - id: review
          agent: build
          prompt:
            skills:
              - code-review
```

If run-level `prompt.skills` is present, it replaces top-level `prompt.skills` for that run. If run-level `prompt.template` is omitted, the top-level template is reused.

Ask whether required skills should apply to all item prompts or only to specific phases.

## Sequential Runs

By default, one selected item starts one `opencode run` process. Use `items[].runs.mode: sequential` when each item needs multiple phases.

```yaml
items:
  - id: ITEM-001
    status: pending
    payload:
      targetName: Example
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
        - id: review
          agent: build
          prompt:
            template: prompts/review.md
```

Sequential run behavior:

- `queue.concurrency` remains item-level concurrency.
- Runs within one item execute in manifest order.
- Each run starts a separate `opencode run` process.
- Auto worktrees are per item, not per run.
- Setup runs once before the first step.
- Teardown and cleanup run once after all steps succeed or after the first failed step.
- The item is marked `completed` only after every step succeeds.
- If one step fails or times out, later steps for that item are skipped.

## Passing Files Between Agents

Use native chained artifacts when one agent/run must pass a file to a later agent/run. This is the supported way to pass files from previous agents.

```yaml
items:
  - id: ITEM-001
    runs:
      mode: sequential
      steps:
        - id: plan
          agent: build
          prompt:
            template: prompts/plan.md
          produces:
            plan:
              required: true
              description: Implementation plan for this item.
        - id: implement
          agent: build
          prompt:
            template: prompts/implement.md
          consumes:
            - plan.plan
```

Rules:

- `produces.<artifact-id>` declares a file produced by a run step.
- `consumes` references earlier artifacts with `step.artifact` syntax.
- By default, `produces.plan` writes to `artifacts/<item-id>/<step-id>/plan.md` beside `manifest.yaml`.
- Custom artifact paths are allowed only under `artifacts/`.
- Artifact paths may use `$item_id`, `$run_id`, and `$artifact_id`.
- OCMO injects deterministic instructions telling the producing agent exactly which artifact file to write.
- Required artifacts must exist and be non-empty after the producing `opencode run`, or the step fails.
- Consuming steps receive the artifact content under `## Chained Inputs` in the rendered prompt.
- Use artifacts for handoff files, implementation plans, analysis summaries, review notes, generated patch instructions, or structured data needed by later phases.

Do not invent ad hoc workflow-specific file passing outside `produces` and `consumes` unless the user explicitly asks for manual files.

## Worktree And Concurrency Safety

`queue.concurrency` is the maximum number of active operation items. It does not make run steps inside one item parallel.

`policy.worktree: single` means selected items operate in one shared workspace.

- Safe only when item scopes are explicitly non-overlapping.
- `validate` and `render` warn for `queue.concurrency > 1`.
- `ocmo run` rejects shared single-worktree concurrency unless passed `--allow-shared-worktree-concurrency`.
- `policy.worktree: single` cannot be combined with `queue.autoWorktrees.enabled: true`.

Use `queue.autoWorktrees.enabled: true` when items may edit overlapping files, when isolation matters, or when parallel item execution is desired without shared-workspace risk.

```yaml
queue:
  concurrency: 4
  autoWorktrees:
    enabled: true
    root: .ocmo/worktrees
    baseBranch: main
    branchPattern: ocmo/{operation_id}/{item_id}
    setup:
      - npm install
    teardown: []
    cleanup: never
```

Auto worktrees use native `git worktree`. They are per selected item, not per run step.

## Planner Output Rules

`ocmo plan` asks `opencode` to convert a natural-language request into a manifest. The planner always uses the `build` agent.

Planner output must contain a valid manifest. If generated prompt template files are referenced and do not already exist, the planner must return file blocks after the manifest:

```text
OCMO_MANIFEST_START
schema: ocmo/v1
...
prompt:
  template: prompts/example.md
OCMO_MANIFEST_END
OCMO_FILE_START prompts/example.md
Prompt template contents for $item_id
OCMO_FILE_END
```

Generated file paths must be relative to the manifest directory and must not use absolute paths or `..` segments.

Planning does not execute items. It should only produce a manifest and any needed prompt templates.

## Command Reference

Useful commands:

```powershell
ocmo validate <manifest>
ocmo render [manifest-or-directory] --select <selector>
ocmo run [manifest-or-directory] --select <selector> --dry-run
ocmo run [manifest-or-directory] --select <selector> --yes
ocmo run [manifest-or-directory] --select <selector> --detach
ocmo status
ocmo status --run-id <run-id>
ocmo status [manifest-or-directory]
ocmo list --all
ocmo plan --from <prompt-file> --workspace <workspace>
ocmo plan --from <prompt-file> --workspace <workspace> --read <source-file>
```

Manifest inference for `run` and `render`:

- No argument uses `manifest.yaml` in the current directory.
- If missing, exactly one `.ocmo/*/manifest.yaml` may be inferred.
- A directory argument uses `<directory>/manifest.yaml`.
- If multiple generated manifests exist, pass one explicitly.

`ocmo render` is prompt-only and read-only. `ocmo run --dry-run` validates, selects, renders, and previews commands without starting agents or writing state.

`ocmo run --detach` starts a child `ocmo run` with `--yes` and `--ui plain`, writes detached metadata/logs under `.ocmo/runs/`, writes a global registry entry, and returns a run ID. Use `ocmo status` or `ocmo list` to inspect detached runs.

## Selection Rules

Selectors match manifest item IDs:

- `all`: every item.
- `pending`: items whose manifest status is `pending`.
- `uncompleted`: items whose manifest status is not `completed`, `done`, or `skipped`.
- `WORK-001`: one exact item ID.
- `WORK-001,WORK-002`: multiple exact item IDs.
- `41-48`: numeric range matching IDs `41` through `48`.
- `41-48,93-96,141`: mixed numeric ranges and exact IDs.

## Final Planning Prompt Shape

Before running `ocmo plan`, show the user a final prompt with this information:

```text
Create an ocmo/v1 manifest for this mass operation.

Workspace:
<absolute or intended workspace>

Goal:
<what each item must accomplish and definition of done>

Items:
<how to discover or enumerate items, with IDs/ranges if known>

Execution:
- Worktree policy: <single or autoWorktrees>
- Auto worktrees: <enabled or disabled>
- Concurrency: <number>
- Timeout: <seconds or unspecified>
- Initial selector: <all|pending|uncompleted|IDs|ranges>
- Detached execution later: <yes or no>

Runs:
<single-step or sequential steps, with each step's purpose>

Skills:
<top-level prompt.skills and any run-specific prompt.skills>

Prompt requirements:
<instructions every rendered per-item prompt must contain>

Artifacts:
<required artifacts, paths, consuming steps, and handoff semantics>

Read-only planning files:
<files to pass via --read>

Safety constraints:
<non-overlap assumptions, files to avoid, review requirements, permissions>

Output requirements:
- Write generated prompt templates under prompts/ beside manifest.yaml.
- Keep task-specific logic in manifest payloads and prompt templates.
- Use agent: build for explicit agents.
- Use items[].runs.mode: sequential for multi-phase items.
- Use produces/consumes for files passed from previous agents.
- Do not use operation.kind or runner.mode.
```

Ask for approval after showing the final prompt. Only then write the prompt file and run `ocmo plan`.
