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
  dangerouslySkipPermissions: false

selection:
  default: uncompleted

queue:
  concurrency: 1
  order: manifest
  stopOnFailure: false

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
- `dangerouslySkipPermissions`: passes `--dangerously-skip-permissions` when true.

`selection` sets the default selector when `--select` is not passed. Recommended default is `uncompleted`.

`queue` controls scheduling:

- `concurrency`: maximum active `opencode run` processes.
- `order`: currently `manifest`; items run in manifest order.
- `stopOnFailure`: reserved for stricter failure handling.

`policy` is not interpreted deeply by `ocmo`, except for one safety rule: if `policy.worktree` is `single`, `ocmo` rejects `concurrency > 1`.

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

The prompt template should tell `opencode` exactly how to complete one item and how to stop. The template should also make clear that the agent must not work on any other item.

## Commands

Validate a manifest:

```powershell
ocmo validate examples/report-rewrite.yaml
```

Render prompts for selected items:

```powershell
ocmo render examples/report-rewrite.yaml --select WORK-001
```

Dry-run generated `opencode` commands and prompts:

```powershell
ocmo run examples/report-rewrite.yaml --select WORK-001 --dry-run
```

Run all uncompleted items:

```powershell
ocmo run examples/report-rewrite.yaml --select uncompleted --yes
```

Run specific items:

```powershell
ocmo run examples/report-rewrite.yaml --select WORK-001,WORK-002 --yes
```

Run numeric manifest IDs:

```powershell
ocmo run examples/taxonomy-docs.yaml --select 41-48,93-96 --yes
```

Override concurrency:

```powershell
ocmo run examples/report-rewrite.yaml --concurrency 1 --yes
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

## Planning From An Unstructured Request

`ocmo plan` asks `opencode` to convert a natural-language mass-operation request into an `ocmo/v1` manifest. It can attach read-only source files such as CSV exports, text files, or other planning inputs.

```powershell
ocmo plan `
  --from prompt.txt `
  --read "C:\path\to\source-data.csv" `
  --out ocmo/example-operation.yaml
```

Planning does not execute operation items. It should only produce a manifest and any prompt template needed for review.

Use `--dry-run` to inspect the planning prompt sent to `opencode`:

```powershell
ocmo plan --from prompt.txt --out ocmo/example-operation.yaml --dry-run
```

## Example: Generic Report Rewrite Workflow

See:

- `examples/report-rewrite.yaml`
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

Failed items remain selectable through `uncompleted` unless you mark them completed or skipped in the manifest.

## Safety Notes

- Always run `ocmo run --dry-run` before starting a large operation.
- Keep `concurrency: 1` for single-worktree git workflows.
- Prefer anonymized example data in committed manifests and README snippets.
- Put secrets, credentials, real customer data, and private source exports outside the repository.
- Use `ocmo plan` for structure, then review the generated manifest before running it.
