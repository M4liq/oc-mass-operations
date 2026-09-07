import json
import unittest
from pathlib import Path
from unittest.mock import patch
from ocmo import cli

class CodexTests(unittest.TestCase):
    def setUp(self):
        self.runner = {"provider": "codex", "command": "codex", "model": "test-model", "reasoningEffort": "high"}
        self.manifest = {"runner": self.runner}

    def test_commands_and_resume(self):
        with patch("ocmo.providers.resolve_executable_command", side_effect=lambda x: x):
            command = cli.build_command(self.manifest, Path("manifest.yaml"), "--hello")
            self.assertEqual(command, ["codex", "exec", "--json", "--model", "test-model", "-c", 'model_reasoning_effort="high"', "--", "--hello"])
            command = cli.build_resume_command(self.manifest, Path("manifest.yaml"), "hello", "session-id", prompt_file=Path("prompt.md"))
            self.assertEqual(command[:3], ["codex", "exec", "resume"])
            self.assertEqual(command[-3:], ["--", "session-id", "-"])
            self.assertTrue(cli.provider_uses_stdin_transport("codex"))
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.runner["dangerouslySkipPermissions"] = True
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", cli.build_command(self.manifest, Path("manifest.yaml"), "hello"))

    def test_events(self):
        events = [
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.started", "item": {"type": "agent_message", "text": "partial"}},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "secret"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 20}},
        ]
        output = "\n".join(map(json.dumps, events))
        self.assertEqual(cli.provider_render_output_text("codex", output), "done\n")
        self.assertEqual(cli.provider_extract_session_id("codex", output), "abc")
        usage = cli.provider_extract_usage_delta("codex", json.dumps(events[-1]))
        self.assertEqual((usage["input"], usage["cacheRead"], usage["output"], usage["total"]), (30, 70, 20, 120))
        self.assertEqual(cli.provider_render_output_line("codex", '{"type":"turn.failed","error":{"message":"denied"}}'), "denied\n")
        for line in ["{broken", "[]", '{"type":"turn.completed","usage":null}']:
            self.assertIsNone(cli.provider_extract_usage_delta("codex", line))
        self.assertIsNone(cli.provider_extract_session_id("codex", "{}"))

    def test_validation_and_warnings(self):
        cli.validate_runner_provider("codex", "runner.provider")
        cli.validate_model_value("test-model", "runner.model", "codex")
        with self.assertRaises(cli.OcmoError):
            cli.validate_model_value("openai/test-model", "runner.model", "codex")
        self.assertEqual(cli.provider_runner_warnings(self.manifest), [])
        self.runner["agent"] = "build"
        self.assertEqual(cli.provider_runner_warnings(self.manifest), ["warning: runner.agent is ignored when runner.provider=codex"])
