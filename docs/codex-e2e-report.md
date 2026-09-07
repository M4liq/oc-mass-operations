# Codex provider verification - 2026-09-07

Implementation was merged from `feat/codex-provider` to `main` (15413d0).
The machine installation now loads the updated main checkout at `D:/repos/ocmo`.
SiteSmith provider configuration is unchanged.

- All 261 unit tests passed, including Codex commands, resume, event parsing,
  token accounting, validation, and permission flag opt-in.
- A real `python -m ocmo` equivalent (`cli.main`) operation invoked installed
  Codex CLI using its existing ChatGPT login; no mocked model service.
- A 27,063-character prompt travelled through stdin. The operation completed,
  returned `OCMO_CODEX_OK`, and persisted a real Codex thread ID and usage.
- The OCMO resume adapter resumed that thread and returned `OCMO_RESUME_OK`.
- Cached input was counted once: 11,596 uncached + 12,288 cached + 9 output
  = 23,893 tokens in the first successful recording.
- Playwright drives a local test button that starts the real Python process
  and displays its actual output. This is a CLI test console, not a product UI.
  Recordings, transcripts, and session identifiers are ignored under `_e2e/`.

The initial recording failed before launching Codex because the harness used
`--fresh-erase`; this was corrected to OCMO's `--fresh`. No model call occurred
in that failed attempt. A Windows text-encoding error while writing the harness
was also corrected before running it.

This verifies local orchestration and a live model round trip, not a SiteSmith
customer pipeline, deployment, or pause/restart of an interrupted operation.
The smoke prompt requests no tool calls or file edits. No customer data or
production services were modified. Codex JSON usage provides no dollar cost.

Reproduce: install Playwright for Node, then run `node _e2e/codex-smoke.cjs`
(with Playwright in Node's module path). Python must have OCMO's dependencies.
The harness sets PYTHONPATH to this worktree's src, so it tests these edits.

## Installed command verification

The installed `C:/Python312/Scripts/ocmo.exe` completed the live smoke operation
with `OCMO_SMOKE_INSTALLED=1` and PYTHONPATH removed from the child environment.
Python resolved `ocmo.cli` to `D:/repos/ocmo/src/ocmo/cli.py`. The transcript
contains `OCMO_CODEX_OK`, stdin transport for 27,063 characters, and exit code 0.
Resuming the recorded thread through the installed package returned
`OCMO_RESUME_OK`. Video: `_e2e/videos/codex-installed.mp4` (local, gitignored).
All 261 tests passed before the merge.

A normal editable reinstall failed with Windows access denied on `ocmo.exe`,
leaving the package import temporarily unavailable. Recovery installed the
editable wheel into a temporary staging directory and copied its package
metadata and editable path file into site-packages, retaining the existing
launcher. Its RECORD entry was corrected to the actual Scripts/ocmo.exe path
and hash. The installed CLI was then verified end to end as described above.
Pre-existing pip warnings about invalid `~...` distributions remain; no unrelated
packages were removed. No running operations were terminated.

To repeat installed verification in PowerShell, set
`$env:OCMO_SMOKE_INSTALLED='1'` and run `node _e2e/codex-smoke.cjs`.
