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
        if arg in ("-p", "--verbose", "--dangerously-skip-permissions"):
            index += 1
            continue
        positional.append(arg)
        index += 1
    has_required_flags = "-p" in argv and "--verbose" in argv and "stream-json" in argv
    prompt_text = positional[0] if positional else sys.stdin.read()
    result = {
        "argv": argv,
        "usedStdinTransport": not positional,
        "hasRequiredFlags": has_required_flags,
        "promptLength": len(prompt_text),
        "hasMarker": "CLAUDE_STDIN_TRANSPORT_SMOKE" in prompt_text,
    }
    (Path(__file__).resolve().parent / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "smoke-session"}))
    print(json.dumps({"type": "assistant", "session_id": "smoke-session", "message": {"content": [{"type": "text", "text": "claude stdin transport smoke completed"}]}}))
    print(json.dumps({"type": "result", "subtype": "success", "session_id": "smoke-session", "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2}}))
    return 0 if result["usedStdinTransport"] and result["hasMarker"] and has_required_flags else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
