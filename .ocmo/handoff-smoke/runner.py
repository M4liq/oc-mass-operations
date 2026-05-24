from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    prompt = "\n".join(argv)
    if "## Required Artifacts" in prompt:
        match = re.search(r"^- handoff: (.+)$", prompt, re.MULTILINE)
        if not match:
            print("handoff path not found", file=sys.stderr)
            return 2
        path = Path(match.group(1).strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        should_proceed = '"mode": "proceed"' in prompt
        path.write_text(
            json.dumps(
                {
                    "schema": "ocmo-handoff/v1",
                    "decision": "proceed" if should_proceed else "block",
                    "confidence": 0.95 if should_proceed else 0.72,
                    "summary": "Smoke planner result.",
                    "handoff": "Implement the smoke task." if should_proceed else "Planner is not confident enough to continue.",
                    "conditions": [{"name": "smoke_condition", "met": should_proceed}],
                    "risks": [] if should_proceed else ["Confidence below gate."],
                    "nextAgentInstructions": "Write the implementation smoke output." if should_proceed else "Do not continue.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print(json.dumps({"type": "message", "text": "smoke runner completed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
