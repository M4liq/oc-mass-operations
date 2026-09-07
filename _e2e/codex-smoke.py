"""Real Codex round trip through the OCMO operation and resume adapter."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import yaml
from ocmo import cli

root = Path(__file__).resolve().parent.parent
runtime = root / "_e2e" / "runtime"
runtime.mkdir(exist_ok=True)
manifest_path = runtime / "manifest.yaml"
(runtime / "prompt.md").write_text("Reply exactly OCMO_CODEX_OK. Do not use tools or change files.\n" + "Padding. " * 3000, encoding="utf-8")
manifest = {
    "schema": "ocmo/v1", "operation": {"id": "codex-smoke", "workspace": str(root)},
    "runner": {"provider": "codex", "command": shutil.which("codex"), "timeoutSeconds": 180},
    "queue": {"concurrency": 1}, "policy": {"worktree": "single"},
    "prompt": {"template": "prompt.md"}, "state": {"path": "state.json"},
    "workUnits": [{"id": "smoke", "title": "Codex smoke"}],
}
manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
args = ["operation", "run", str(manifest_path), "--yes", "--fresh"]
if os.environ.get("OCMO_SMOKE_INSTALLED") == "1":
    executable = shutil.which("ocmo")
    assert executable, "Installed ocmo command not found on PATH"
    print(f"Installed command: {executable}", flush=True)
    print(f"Imported implementation: {cli.__file__}", flush=True)
    code = subprocess.run([executable, *args], check=False).returncode
else:
    code = cli.main(args)
assert code == 0, f"Operation failed: {code}"
transcript = (runtime / "outputs" / "smoke__default.txt").read_text(encoding="utf-8")
assert "OCMO_CODEX_OK" in transcript
assert "prompt sent via stdin" in transcript
print(transcript, flush=True)
state = json.loads((runtime / "state.json").read_text(encoding="utf-8"))
print(json.dumps(state, indent=2), flush=True)
# Find the real session captured by OCMO, without changing operation state.
def find_session(value):
    if isinstance(value, dict):
        if value.get("sessionId"):
            return value["sessionId"]
        for child in value.values():
            result = find_session(child)
            if result:
                return result
    return None
session = find_session(state)
assert session, "OCMO did not persist the Codex thread"
command = cli.build_resume_command(manifest, manifest_path, "Reply exactly OCMO_RESUME_OK. Do not use tools.", session)
result = cli.run_runner_command(command, root, 180, runtime / "resume.txt", provider="codex")
assert result.returncode == 0, result.stdout
assert "OCMO_RESUME_OK" in cli.provider_render_output_text("codex", result.stdout)
print("PASS: live operation, long prompt stdin, persisted thread, live resume", flush=True)
