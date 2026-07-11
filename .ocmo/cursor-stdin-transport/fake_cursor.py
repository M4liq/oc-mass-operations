from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--output-format", "--model", "--resume"):
            index += 2
            continue
        if arg in ("-p", "--force"):
            index += 1
            continue
        positional.append(arg)
        index += 1
    has_required_flags = "-p" in argv and "stream-json" in argv
    prompt_text = positional[0] if positional else sys.stdin.read()
    result = {
        "argv": argv,
        "usedStdinTransport": not positional,
        "hasRequiredFlags": has_required_flags,
        "promptLength": len(prompt_text),
        "hasMarker": "CURSOR_STDIN_TRANSPORT_SMOKE" in prompt_text,
    }
    (Path(__file__).resolve().parent / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "smoke-cursor-session", "model": "gpt-5", "permissionMode": "default"}))
    print(json.dumps({"type": "assistant", "session_id": "smoke-cursor-session", "message": {"content": [{"type": "text", "text": "cursor stdin transport smoke completed"}]}}))
    print(json.dumps({"type": "result", "subtype": "success", "session_id": "smoke-cursor-session", "duration_ms": 5, "result": "done"}))
    return 0 if result["usedStdinTransport"] and result["hasMarker"] and has_required_flags else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
