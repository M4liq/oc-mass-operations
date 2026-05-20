# OC Mass Operations

OC Mass Operations (`ocmo`) is a generic queue runner for repeatable `opencode` jobs.

Use it when a large prompt naturally splits into many similar units of work, such as rewriting many reports, verifying many documentation files, or applying the same review process across many targets. Instead of opening many `opencode` UI tabs manually, `ocmo` reads a manifest, renders one prompt per item, and starts one `opencode run` process for each selected item.

`ocmo` does not replace `opencode`. It schedules `opencode`. The actual reasoning, editing, reviewing, and tool use still happens inside each `opencode run` session.

## Why This Exists

Manual orchestration becomes painful when a task has dozens or hundreds of similar items:

- You need to track which items are complete, failed, skipped, or still pending.
- You want to resume interrupted work without re-running finished items.
- You want one prompt template but different item payloads.
- You want a queue with a maximum number of concurrent `opencode` processes.
- You want to dry-run generated prompts before spending model/tool time.
- You want natural-language tasks converted into a structured manifest before execution.

`ocmo` solves only the scheduling layer. It intentionally keeps workflow-specific details in the manifest and prompt template.

## Install For Development

```powershell
python -m pip install -e .
```

If editable install is not available in your local environment, run directly from source:

```powershell
$env:PYTHONPATH='src'
python -m ocmo --help
```

## Core Concepts

- Operation: one mass workflow, backed by one manifest.
- Operation item: one independently schedulable unit of work. `ocmo` starts one `opencode run` process per item.
- Manifest: the YAML file that describes the operation, runner settings, queue settings, prompt template, state path, and items.
- Prompt template: a text file rendered once per item and passed to `opencode run`.
- Item payload: task-specific data for one item. `ocmo` does not interpret the payload beyond rendering it into the prompt.
- Selection: a filter deciding which items to run, such as `uncompleted`, `all`, `ITEM-001`, or `1-10`.
- State file: durable JSON state written by `ocmo run`, tracking item status and command metadata.

## Architecture

The execution model is deliberately simple:

```text
manifest.yaml
  -> select operation items
  -> render prompt template for each item
  -> start opencode run for each selected item
  -> update state file when each process starts/finishes
```

Each operation item is isolated at the process level. Items do not share an `opencode` conversation unless you explicitly configure `opencode` itself to attach to a shared server.

## Manifest Format

```yaml
schema: ocmo/v1

operation:
  id: example-operation
  kind: generic
  description: Process a set of example work items.
  workspace: C:\path\to\target-repo

runner:
  command: opencode
  mode: run
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
  template: .ocmo/prompts/example-workflow.md

state:
  path: .ocmo/state/example-operation.json

items:
  - id: ITEM-001
    title: ExampleItemOne
    status: pending
    payload:
      workItemKey: ITEM-001
      targetName: ExampleItemOne
      branch: feature/ITEM-001_example-item-one
      reviewer: reviewer-user
      assignee: assignee-user
```

### Manifest Sections

`schema` identifies the manifest version. Current value is `ocmo/v1`.

`operation` describes the overall workflow:

- `id`: stable operation identifier, used for state/log naming.
- `kind`: currently informational. Keep it `generic` unless your team defines a stable taxonomy.
- `description`: human-readable summary of the operation.
- `workspace`: target repository or working directory for `opencode run`.

`runner` describes how `opencode` is invoked:

- `command`: normally `opencode`.
- `mode`: normally `run`.
- `agent`: `opencode` agent name, for example `build` or `plan`.
- `model`: model identifier passed to `opencode`.
- `attach`: optional `opencode serve` URL.
- `timeoutSeconds`: optional maximum runtime for each operation item.
- `dangerouslySkipPermissions`: passes `--dangerously-skip-permissions` when true.

`selection` sets the default selector when `--select` is not passed. Recommended default is `uncompleted`.

`queue` controls scheduling:

- `concurrency`: maximum active `opencode run` processes.
- `order`: currently `manifest`; items run in manifest order.
- `stopOnFailure`: reserved for stricter failure handling.
- `autoWorktrees`: optional per-item git worktree creation and setup.

`policy` is not interpreted deeply by `ocmo`, except for worktree safety rules: if `policy.worktree` is `single`, `ocmo` rejects `concurrency > 1` and rejects `queue.autoWorktrees.enabled: true`.

`prompt.template` points to the per-item prompt template.

`state.path` points to the durable state JSON file.

`items` contains operation items. Every item must have a unique `id`. The `payload` object is task-specific and can contain anything your prompt template needs.

## Path Resolution

Manifest paths are resolved relative to the manifest file.

For example, if the manifest is `examples/report-rewrite.yaml`, then:

```yaml
operation:
  workspace: ..
prompt:
  template: prompts/report-rewrite.md
```

means:

- workspace is the repository root, one directory above `examples/`
- prompt template is `examples/prompts/report-rewrite.md`

## Prompt Templates

Prompt templates use Python `string.Template` variables.

Example:

```text
You are executing one operation item.

Operation:
$operation_json

Policy:
$policy_json

Item:
$item_json

Payload:
$payload_json

Work only on $item_id $item_title.
```

Available variables:

- `$operation_json`: JSON representation of `operation`
- `$policy_json`: JSON representation of `policy`
- `$item_json`: JSON representation of the current item
- `$payload_json`: JSON representation of the current item payload
- `$operation_id`: operation ID
- `$operation_kind`: operation kind
- `$workspace`: workspace value from the manifest
- `$item_id`: current item ID
- `$item_title`: current item title, if present
- `$item_file`: current item file, if present
- `$worktree_path`: created per-item worktree path when auto worktrees are enabled
- `$source_workspace`: original `operation.workspace` path when auto worktrees are enabled
- `$branch_name`: created per-item branch name when auto worktrees are enabled

The prompt template should tell `opencode` exactly how to complete one item and how to stop. The template should also make clear that the agent must not work on any other item.

## Command Reference

Use these commands to validate manifests, inspect prompts, preview execution, run queues, and generate manifests from rough requests.

| Task | Command |
| --- | --- |
| Check manifest validity | `ocmo validate <manifest>` |
| Inspect rendered prompt text | `ocmo render <manifest> --select <selector>` |
| Inspect the full execution plan | `ocmo run <manifest> --select <selector> --dry-run` |
| Execute selected items | `ocmo run <manifest> --select <selector> --yes` |
| Generate a manifest draft | `ocmo plan --from <prompt-file> --out <manifest>` |

### `ocmo validate`

```powershell
ocmo validate <manifest>
```

Example:

```powershell
ocmo validate examples/report-rewrite.yaml
```

`validate` loads the manifest and checks the static configuration before any queue work starts. It verifies the manifest schema, workspace path, runner settings, queue settings, prompt template path, item IDs, concurrency policy, timeout configuration, and auto-worktree configuration.

`validate` does not render prompts, start `opencode`, create worktrees, run setup or teardown commands, write state, edit files, or mark items.

### `ocmo render`

```powershell
ocmo render <manifest> [--select <selector>]
```

Example:

```powershell
ocmo render examples/report-rewrite.yaml --select WORK-001
```

`render` is a prompt-only preview. It validates the manifest, applies selection rules, renders `prompt.template` once for each selected item, and prints the resulting prompt text to stdout.

`render` is intentionally read-only. It does not start `opencode`, build or print the final `opencode run` command, create worktrees, run setup or teardown commands, write state, edit files, apply concurrency, apply timeouts, or mark items.

Use `render` when you only need to inspect the text that will be sent to an agent. Use `ocmo run --dry-run` when you need to inspect the actual execution command, timeout, and auto-worktree path or branch.

Templates are rendered with these variables:

- `$operation_json`: JSON representation of `operation`.
- `$policy_json`: JSON representation of `policy`.
- `$item_json`: JSON representation of the selected item.
- `$payload_json`: JSON representation of `item.payload`.
- `$operation_id`: `operation.id`.
- `$operation_kind`: `operation.kind`, defaulting to `generic`.
- `$workspace`: `operation.workspace` as written in the manifest.
- `$item_id`: selected item ID.
- `$item_title`: selected item title, or an empty string.
- `$item_file`: selected item file, or an empty string.
- `$worktree_path`: generated worktree path when an execution context provides one; empty for `ocmo render`.
- `$source_workspace`: source workspace path when an execution context provides one; otherwise `operation.workspace`.
- `$branch_name`: generated branch name when an execution context provides one; empty for `ocmo render`.

### `ocmo run`

```powershell
ocmo run <manifest> [--select <selector>] [--concurrency <count>] [--timeout-seconds <seconds>] [--dry-run] [--yes]
```

Examples:

```powershell
ocmo run examples/report-rewrite.yaml --select uncompleted --yes
ocmo run examples/report-rewrite.yaml --select WORK-001,WORK-002 --yes
ocmo run examples/taxonomy-docs.yaml --select 41-48,93-96 --yes
ocmo run examples/report-rewrite.yaml --concurrency 1 --yes
ocmo run examples/report-rewrite.yaml --timeout-seconds 7200 --yes
```

`run` validates the manifest, selects items, renders one prompt per item, and starts one `opencode run` process per selected item. It writes durable state to `state.path` and marks each item as it moves through the queue.

If `queue.autoWorktrees.enabled: true`, `run` also creates one native git worktree per selected item, runs configured setup commands, runs `opencode` inside that worktree, and applies configured teardown and cleanup behavior.

`run` asks for confirmation before starting work unless `--yes` or `-y` is provided.

Options:

- `--select <selector>`: overrides `selection.default`; accepts `all`, `pending`, `uncompleted`, exact IDs, comma-separated IDs, numeric ranges, or mixed ranges and IDs.
- `--concurrency <count>`: overrides `queue.concurrency` for this invocation.
- `--timeout-seconds <seconds>`: overrides `runner.timeoutSeconds` for this invocation.
- `--dry-run`: previews execution without starting work or writing state.
- `--yes`, `-y`: skips the confirmation prompt for non-dry runs.

`--concurrency` and `--timeout-seconds` must be positive integers. If `policy.worktree: single`, concurrency must be `1`. `policy.worktree: single` cannot be combined with `queue.autoWorktrees.enabled: true`.

### `ocmo run --dry-run`

```powershell
ocmo run <manifest> --select <selector> --dry-run
```

Example:

```powershell
ocmo run examples/report-rewrite.yaml --select WORK-001 --dry-run
```

`--dry-run` validates the manifest, applies selection rules, renders the prompt for each selected item, and prints the `opencode run` command that would be launched.

When auto worktrees are enabled, `--dry-run` also prints the planned worktree path and branch name. When a timeout is configured, it prints the effective timeout.

`--dry-run` does not start `opencode`, create worktrees, run setup or teardown commands, write state, edit files, clean up worktrees, or mark items.

### `ocmo plan`

```powershell
ocmo plan --from <prompt-file> --out <manifest> [--read <source-file>] [--model <model>] [--agent <agent>] [--dry-run]
```

Example:

```powershell
ocmo plan `
  --from prompt.txt `
  --read "C:\path\to\source-data.csv" `
  --out ocmo/example-operation.yaml
```

`plan` asks `opencode` to convert a natural-language mass-operation request into an `ocmo/v1` manifest. It can attach read-only source files such as CSV exports, text files, or other planning inputs.

Planning does not execute operation items. It should only produce a manifest and any prompt template needed for review.

Use `--dry-run` to print the planning prompt without starting `opencode`:

```powershell
ocmo plan --from prompt.txt --out ocmo/example-operation.yaml --dry-run
```

## Selection Rules

Selectors decide which manifest items are passed to the queue.

- `all`: every item in the manifest
- `pending`: items whose manifest status is `pending`
- `uncompleted`: items whose manifest status is not `completed`, `done`, or `skipped`
- `WORK-001`: one exact item ID
- `WORK-001,WORK-002`: multiple exact item IDs
- `41-48`: numeric range, matching item IDs `41`, `42`, ..., `48`
- `41-48,93-96,141`: multiple numeric ranges and exact IDs

Selection matches manifest item IDs, not filenames. If you want numeric taxonomy selections, give the corresponding items numeric IDs in the manifest.

## Concurrency

`queue.concurrency` is the maximum number of active `opencode run` processes.

With `concurrency: 1`, items run serially:

```text
WORK-001 starts
WORK-001 finishes
WORK-002 starts
WORK-002 finishes
```

With `concurrency: 3`, up to three items run at once:

```text
WORK-001 starts
WORK-002 starts
WORK-003 starts
WORK-004 starts when one earlier item exits
```

Use `concurrency: 1` for workflows that use one mutable git worktree. A single worktree cannot safely support multiple branch-changing agents at the same time because they would share the same files, branch, index, and working tree state.

For this reason, `ocmo` enforces:

```yaml
policy:
  worktree: single
queue:
  concurrency: 1
```

If you pass `--concurrency 2` with `policy.worktree: single`, `ocmo` fails before starting work.

Parallel execution is appropriate only when items do not mutate shared state or when each item has a separate workspace/worktree.

## Auto Worktrees

Set `queue.autoWorktrees.enabled: true` to have `ocmo` create one git worktree per selected item. Each selected item gets its own branch, setup commands, and isolated `opencode run --dir <worktree>` process.

```yaml
queue:
  concurrency: 3
  autoWorktrees:
    enabled: true
    root: .ocmo/worktrees
    baseBranch: main
    branchPattern: ocmo/{operation_id}/{item_id}
    setup:
      - npm ci
    teardown: []
    cleanup: never

policy:
  worktree: per-item
  baseBranch: main
```

`autoWorktrees` fields:

- `enabled`: when true, creates one git worktree per selected item.
- `root`: directory for generated worktrees. Relative paths are resolved under `operation.workspace`.
- `baseBranch`: branch or commit used as the worktree base. Defaults to `policy.baseBranch`, then the current branch.
- `branchPattern`: branch name template. Supports `{operation_id}`, `{item_id}`, and `{item_slug}`. These values are slugified before interpolation so generated branch names stay filesystem- and git-friendly.
- `setup`: shell command or list of shell commands run inside the worktree after creation.
- `teardown`: shell command or list of shell commands run before cleanup removes a worktree.
- `cleanup`: `never`, `onSuccess`, or `always`. The default is `never` so completed work remains available for review.

Setup and teardown commands receive these environment variables:

- `OCMO_SOURCE_WORKSPACE`: original `operation.workspace` path.
- `OCMO_WORKTREE_PATH`: generated worktree path.
- `OCMO_BRANCH_NAME`: generated branch name.
- `PASEO_SOURCE_CHECKOUT_PATH`: alias for the original workspace path.
- `PASEO_WORKTREE_PATH`: alias for the generated worktree path.
- `PASEO_BRANCH_NAME`: alias for the generated branch name.

`ocmo` uses native `git worktree` commands. It does not require the Paseo daemon and does not start Paseo services or terminals. If the target worktree path already exists, `ocmo` fails that item instead of reusing the directory.

If cleanup is requested with `cleanup: onSuccess` or `cleanup: always` and teardown or worktree removal fails after a successful item run, `ocmo` marks the item as `cleanup_failed` and returns a non-zero exit code.

`--dry-run` prints the planned worktree path, branch, timeout, command, and prompt. It does not create a worktree, run setup, start `opencode`, write state, or clean anything up.

## Timeouts

Use `runner.timeoutSeconds` to prevent stale or runaway `opencode run` processes.

```yaml
runner:
  command: opencode
  mode: run
  timeoutSeconds: 14400
```

The timeout applies per operation item. If an item exceeds the timeout, `ocmo` stops waiting for that `opencode run`, marks the item as `timed_out` in the state file, prints a timeout message, and returns a non-zero process exit code for the run.

You can override the manifest value at runtime:

```powershell
ocmo run examples/report-rewrite.yaml --timeout-seconds 7200 --yes
```

`--dry-run` prints the effective timeout but does not start any process or write timeout state.

## Example: Generic Report Rewrite Workflow

See:

- `examples/report-rewrite.yaml`
- `examples/report-rewrite-auto-worktrees.yaml`
- `examples/prompts/report-rewrite.md`

The example models a generic workflow where each report-like artifact is processed independently:

```text
Rewrite selected work items from a tracker export.
Use one branch and review request per item.
Use a single git worktree.
Run an `opencode` review pass after each rewrite.
If review finds issues, run a fix pass and review again.
```

All identifiers in the example are synthetic. They are intended to show the manifest shape without embedding real tracker IDs, employee names, reviewer handles, report names, or local filesystem paths.

Because the example uses a single git worktree, it sets `queue.concurrency: 1`.

To run the same shape of workflow with isolated per-item worktrees, set `policy.worktree: per-item`, set `queue.autoWorktrees.enabled: true`, and increase `queue.concurrency` to the number of parallel items you want.

## Example Item

```yaml
items:
  - id: WORK-001
    title: ExampleReportOne
    status: pending
    payload:
      workItemKey: WORK-001
      targetName: ExampleReportOne
      branch: feature/WORK-001_example-report-one
      reviewer: reviewer-user
      assignee: assignee-user
```

`ocmo` does not know what `targetName`, `reviewer`, or `assignee` mean. It renders them into the prompt, and the `opencode` agent follows the prompt.

## State

`ocmo run` writes durable state to `state.path`.

State is separate from the manifest. The manifest defines intended work. The state file records execution facts such as:

- item status
- start time
- completion time
- exit code
- command metadata
- worktree path and branch metadata when auto worktrees are enabled

Failed items remain selectable through `uncompleted` unless you mark them completed or skipped in the manifest.
