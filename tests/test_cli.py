from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ocmo import cli


class OcmoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text(
            "Item $work_unit_id $work_unit_title run $run_id/$run_count agent $run_agent model $run_model in $worktree_path payload $payload_json",
            encoding="utf-8",
        )
        self.manifest_path = self.root / "manifest.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_manifest(self, extra: str = "") -> Path:
        self.manifest_path.write_text(
            f"""schema: ocmo/v1
operation:
  id: test-op
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
  agent: build
  model: test-model
  timeoutSeconds: 30
queue:
  concurrency: 1
policy:
  worktree: isolated
prompt:
  template: {self.prompt.as_posix()}
state:
  path: {str((self.root / 'state.json').as_posix())}
workUnits:
  - id: "1"
    title: First
    file: docs/a.md
    payload:
      name: Alpha
  - id: "2"
    title: Second
    payload:
      name: Beta
{extra}""",
            encoding="utf-8",
        )
        return self.manifest_path

    def load(self, extra: str = "") -> dict:
        return cli.load_manifest(self.write_manifest(extra))

    def planned_manifest_text(self, template: str | None = None) -> str:
        template = template or self.prompt.as_posix()
        return f"""schema: ocmo/v1
operation:
  id: planned-op
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
queue:
  concurrency: 1
policy:
  worktree: single
prompt:
  template: {template}
state:
  path: .ocmo/state/planned-op.json
workUnits:
  - id: ITEM-001
    payload: {{}}
"""


class FakePopen:
    pid = 1234

    def __init__(self, command: list[str], returncode: int = 0, stdout: str = "", timeout: bool = False) -> None:
        self.args = command
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self._stdout_text = stdout
        self.timeout = timeout

    def communicate(self, timeout: int | None = None):
        if self.timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self._stdout_text, None

    def wait(self, timeout: int | None = None):
        if self.timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


def fake_popen_completed(returncode: int = 0, stdout: str = ""):
    def fake(command: list[str], **kwargs):
        return FakePopen(command, returncode, stdout)

    return fake


class ValidationTests(OcmoTestCase):
    def test_valid_manifest_passes(self) -> None:
        manifest = self.load()

        cli.validate_manifest(manifest, self.manifest_path)

    def test_missing_workspace_fails_validation(self) -> None:
        manifest = self.load()
        manifest["operation"]["workspace"] = str(self.root / "missing")

        with self.assertRaisesRegex(cli.OcmoError, "operation.workspace does not exist"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_single_worktree_concurrency_above_one_warns_only_for_validation(self) -> None:
        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        manifest["queue"]["concurrency"] = 2

        cli.validate_manifest(manifest, self.manifest_path)
        self.assertIn("--allow-shared-worktree-concurrency", cli.shared_worktree_concurrency_warning(manifest) or "")

    def test_validates_multi_run_steps_and_per_run_templates(self) -> None:
        review_prompt = self.root / "review.md"
        review_prompt.write_text("Review $run_id", encoding="utf-8")
        manifest = self.load()
        manifest["prompt"]["skills"] = ["code-review"]
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "implement", "agent": "build", "prompt": {"template": str(self.prompt)}},
                {"id": "review", "agent": "build", "timeoutSeconds": 5, "prompt": {"template": str(review_prompt), "skills": ["/review-skill"]}},
            ],
        }

        cli.validate_manifest(manifest, self.manifest_path)

    def test_validates_handoff_artifact_gates(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {
                    "id": "plan",
                    "produces": {
                        "handoff": {
                            "gates": {"decision": "proceed", "minConfidence": 0.9, "requireConditionsMet": True},
                        }
                    },
                }
            ],
        }

        cli.validate_manifest(manifest, self.manifest_path)

    def test_rejects_unsupported_run_mode(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "parallel", "steps": [{"id": "one"}]}

        with self.assertRaisesRegex(cli.OcmoError, "runs.mode must be sequential"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_rejects_duplicate_run_ids(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "one"}]}

        with self.assertRaisesRegex(cli.OcmoError, "duplicate run id"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_auto_worktrees_validate_git_repository(self) -> None:
        manifest = self.load()
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "cleanup": "always"}

        with mock.patch("ocmo.cli.ensure_git_repository") as ensure_git:
            cli.validate_manifest(manifest, self.manifest_path)

        ensure_git.assert_called_once_with(self.workspace)

    def test_validation_rejects_required_mapping_and_string_errors(self) -> None:
        cases = [
            ({"operation": None}, "operation must be a mapping"),
            ({"operation": {"id": "", "workspace": str(self.workspace)}}, "id must be a non-empty string"),
            ({"runner": {"command": "opencode", "timeoutSeconds": 0}}, "runner.timeoutSeconds"),
            ({"queue": {"concurrency": 0}}, "queue.concurrency"),
            ({"prompt": {"template": str(self.root / "missing.md")}}, "prompt template not found"),
            ({"workUnits": []}, "workUnits must be a non-empty list"),
            ({"workUnits": ["bad"]}, r"workUnits\[1\] must be a mapping"),
            ({"workUnits": [{}]}, r"workUnits\[1\].id is required"),
            ({"workUnits": [{"id": "1"}, {"id": "1"}]}, "duplicate work unit id"),
        ]
        for patch, message in cases:
            manifest = self.load()
            merge(manifest, patch)
            with self.subTest(message=message):
                with self.assertRaisesRegex(cli.OcmoError, message):
                    cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_work_unit_status(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["status"] = "pending"

        with self.assertRaisesRegex(cli.OcmoError, r"workUnits\[1\]\.status is not supported"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_more_invalid_run_shapes(self) -> None:
        cases = [
            ({"runs": []}, "runs must be a mapping"),
            ({"runs": {"mode": "sequential", "steps": []}}, "runs.steps must be a non-empty list"),
            ({"runs": {"mode": "sequential", "steps": ["bad"]}}, r"runs.steps\[1\] must be a mapping"),
            ({"runs": {"mode": "sequential", "steps": [{}]}}, "id is required"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "timeoutSeconds": 0}]}}, "timeoutSeconds"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": []}]}}, "prompt must be a mapping"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": {"template": ""}}]}}, "template must be a non-empty string"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": {"template": str(self.root / "missing.md")}}]}}, "prompt template not found"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": {"skills": "review"}}]}}, "prompt.skills must be a list"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": {"skills": [""]}}]}}, r"prompt.skills\[1\] must be a non-empty string"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "prompt": {"skills": ["bad skill"]}}]}}, r"prompt.skills\[1\] must be a skill name"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "agent": "review"}]}}, r"agent must be build"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": []}]}}, r"produces must be a mapping"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"bad name": {}}}]}}, r"simple artifact name"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"plan": {"path": "../plan.md"}}}]}}, r"under artifacts/"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"plan": {"type": "handoff"}}}]}}, r"type is not supported"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"plan": {"gates": []}}}]}}, r"gates must be a mapping"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"plan": {"gates": {"minConfidence": 2}}}}]}}, r"minConfidence"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "produces": {"plan": {"gates": {"requireConditionsMet": "yes"}}}}]}}, r"requireConditionsMet"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "consumes": "plan.plan"}]}}, r"consumes must be a list"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "consumes": ["plan.plan"]}]}}, r"earlier step"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "plan", "produces": {"notes": {}}}, {"id": "build", "consumes": ["plan.plan"]}]}}, r"unknown artifact"),
        ]
        for patch, message in cases:
            manifest = self.load()
            manifest["workUnits"] = [{"id": "1", **patch}]
            with self.subTest(message=message):
                with self.assertRaisesRegex(cli.OcmoError, message):
                    cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_non_build_top_level_agent(self) -> None:
        manifest = self.load()
        manifest["runner"]["agent"] = "review"

        with self.assertRaisesRegex(cli.OcmoError, "runner.agent must be build"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_auto_worktree_config_validation_errors(self) -> None:
        cases = [
            ({"enabled": "yes"}, "enabled must be a boolean"),
            ({"enabled": True, "root": ""}, "root must be a non-empty string"),
            ({"enabled": True, "cleanup": "sometimes"}, "cleanup must be never"),
            ({"enabled": True, "setup": [""]}, "setup must be a string or list"),
            ({"enabled": True, "branchPattern": "{missing}"}, "invalid queue.autoWorktrees.branchPattern"),
        ]
        for config, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(cli.OcmoError, message):
                    cli.validate_auto_worktrees(config)

    def test_auto_worktrees_config_accepts_none_bool_and_rejects_other_shapes(self) -> None:
        self.assertEqual(cli.auto_worktrees_config({"queue": {"autoWorktrees": None}}), {"enabled": False})
        self.assertEqual(cli.auto_worktrees_config({"queue": {"autoWorktrees": True}}), {"enabled": True})
        with self.assertRaisesRegex(cli.OcmoError, "autoWorktrees must be a mapping"):
            cli.auto_worktrees_config({"queue": {"autoWorktrees": "yes"}})

    def test_normalize_scripts_accepts_string_and_list(self) -> None:
        self.assertEqual(cli.normalize_scripts("echo hi", "field"), ["echo hi"])
        self.assertEqual(cli.normalize_scripts(["echo hi"], "field"), ["echo hi"])
        with self.assertRaisesRegex(cli.OcmoError, "field must be a string"):
            cli.normalize_scripts([""], "field")

    def test_git_helpers_convert_subprocess_failures_to_ocmo_errors(self) -> None:
        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("no git")):
            with self.assertRaisesRegex(cli.OcmoError, "could not inspect git repository"):
                cli.ensure_git_repository(self.workspace)
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 1)):
            with self.assertRaisesRegex(cli.OcmoError, "must be inside a git repository"):
                cli.ensure_git_repository(self.workspace)
        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("no git")):
            with self.assertRaisesRegex(cli.OcmoError, "could not detect current git branch"):
                cli.current_branch(self.workspace)
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 1, stdout="")):
            with self.assertRaisesRegex(cli.OcmoError, "baseBranch is required"):
                cli.current_branch(self.workspace)
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0, stdout="main\n")):
            self.assertEqual(cli.current_branch(self.workspace), "main")


class SelectionAndRenderingTests(OcmoTestCase):
    def test_select_workUnits_supports_pending_uncompleted_all_ids_and_ranges(self) -> None:
        manifest = self.load()
        manifest["workUnits"].append({"id": "3"})
        state = {"workUnits": {"2": {"status": "completed"}, "3": {"status": "skipped"}}}

        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "pending", state)], ["1"])
        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "uncompleted", state)], ["1"])
        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "all")], ["1", "2", "3"])
        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "1,3")], ["1", "3"])

        numeric = {"workUnits": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        self.assertEqual([item["id"] for item in cli.select_work_units(numeric, "1-2")], ["1", "2"])

    def test_select_workUnits_treats_missing_state_as_pending(self) -> None:
        manifest = {"workUnits": [{"id": "1"}, {"id": "2"}]}

        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "pending")], ["1", "2"])
        self.assertEqual([item["id"] for item in cli.select_work_units(manifest, "uncompleted")], ["1", "2"])

    def test_select_workUnits_rejects_missing_and_descending_ranges(self) -> None:
        manifest = self.load()

        with self.assertRaisesRegex(cli.OcmoError, "selection did not match"):
            cli.select_work_units(manifest, "missing")
        with self.assertRaisesRegex(cli.OcmoError, "invalid descending range"):
            cli.select_work_units(manifest, "3-1")

    def test_implicit_default_run_uses_top_level_runner(self) -> None:
        manifest = self.load()

        runs = cli.work_unit_runs(manifest, manifest["workUnits"][0])

        self.assertEqual(runs[0]["id"], "default")
        self.assertEqual(runs[0]["agent"], "build")

    def test_render_prompt_includes_run_and_execution_context(self) -> None:
        manifest = self.load()
        item = manifest["workUnits"][0]
        run = {"id": "review", "index": 2, "mode": "sequential", "agent": "build", "model": "review-model"}

        rendered = cli.render_prompt(
            manifest,
            item,
            self.manifest_path,
            execution={"worktreePath": "C:/worktree", "branchName": "branch/a"},
            run=run,
            runs=[{"id": "implement"}, run],
        )

        self.assertIn("Item 1 First run review/2", rendered)
        self.assertIn("agent build model review-model", rendered)
        self.assertIn("C:/worktree", rendered)
        self.assertIn('"name": "Alpha"', rendered)

    def test_render_prompt_supports_brace_dotted_placeholders(self) -> None:
        self.prompt.write_text(
            "Range {{payload.rangeStart}}-{{ payload.rangeEnd }} for {{workUnit.id}}/{{work_unit_id}} in {{operation.id}} run {{run.id}} at {{execution.worktreePath}} payload {{payload}}",
            encoding="utf-8",
        )
        manifest = self.load()
        item = manifest["workUnits"][0]
        item["payload"] = {"rangeStart": 41, "rangeEnd": 48, "optional": None}
        self.prompt.write_text(self.prompt.read_text(encoding="utf-8") + " optional {{payload.optional}}", encoding="utf-8")

        rendered = cli.render_prompt(manifest, item, self.manifest_path, execution={"worktreePath": "C:/wt"})

        self.assertIn("Range 41-48 for 1/1 in test-op run default at C:/wt", rendered)
        self.assertIn('"rangeStart": 41', rendered)
        self.assertTrue(rendered.endswith("optional "))

    def test_render_prompt_rejects_unknown_brace_placeholder(self) -> None:
        self.prompt.write_text("Range {{payload.missing}}", encoding="utf-8")
        manifest = self.load()

        with self.assertRaisesRegex(cli.OcmoError, r"unresolved prompt placeholder: \{\{payload\.missing\}\}"):
            cli.render_prompt(manifest, manifest["workUnits"][0], self.manifest_path)

        self.prompt.write_text("Range {{missing.value}}", encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, r"unresolved prompt placeholder: \{\{missing\.value\}\}"):
            cli.render_prompt(manifest, manifest["workUnits"][0], self.manifest_path)

    def test_format_placeholder_value_handles_none(self) -> None:
        self.assertEqual(cli.format_placeholder_value(None), "")

    def test_compact_prompt_previews_keeps_first_two_and_last(self) -> None:
        previews = [cli.PromptPreview(str(index), "default", f"prompt {index}") for index in range(1, 6)]

        compact = cli.compact_prompt_previews(previews, False)

        self.assertEqual(compact, [previews[0], previews[1], 2, previews[-1]])
        self.assertEqual(cli.compact_prompt_previews(previews[:3], False), previews[:3])
        self.assertEqual(cli.compact_prompt_previews(previews, True), previews)

    def test_render_prompt_prepends_deterministic_skill_instructions(self) -> None:
        self.prompt.write_text("Skills: $skill_names\nCommands:\n$skill_commands\n$work_unit_id", encoding="utf-8")
        manifest = self.load()
        manifest["prompt"]["skills"] = ["analysis", "/code-review"]

        rendered = cli.render_prompt(manifest, manifest["workUnits"][0], self.manifest_path)

        self.assertTrue(rendered.startswith("You must use the following opencode skills before doing this task, in order:\n- /analysis\n- /code-review"))
        self.assertIn("Skills: analysis, code-review", rendered)
        self.assertIn("/analysis\n/code-review", rendered)

    def test_run_prompt_skills_override_top_level_skills_without_template_override(self) -> None:
        manifest = self.load()
        manifest["prompt"]["skills"] = ["implement"]
        run = {"id": "review", "index": 1, "mode": "sequential", "prompt": {"skills": ["review"]}}

        rendered = cli.render_prompt(manifest, manifest["workUnits"][0], self.manifest_path, run=run)

        self.assertIn("- /review", rendered)
        self.assertNotIn("- /implement", rendered)

    def test_render_prompt_injects_chained_artifacts(self) -> None:
        manifest = self.load()
        item = manifest["workUnits"][0]
        plan_path = self.root / "artifacts" / "1" / "plan" / "plan.md"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text("Plan content", encoding="utf-8")
        plan_run = {"id": "plan", "index": 1, "mode": "sequential", "agent": "build", "produces": {"plan": {}}}
        implement_run = {"id": "implement", "index": 2, "mode": "sequential", "agent": "build", "consumes": ["plan.plan"], "produces": {"notes": {"required": False, "description": "Optional notes"}}}

        rendered = cli.render_prompt(manifest, item, self.manifest_path, run=implement_run, runs=[plan_run, implement_run])

        self.assertIn("## Chained Inputs", rendered)
        self.assertIn("### plan.plan", rendered)
        self.assertIn("Plan content", rendered)
        self.assertIn("## Required Artifacts", rendered)
        self.assertIn("Optional notes", rendered)
        self.assertIn("artifacts/1/implement/notes.md", rendered)

    def test_artifact_helpers_validate_more_edge_cases(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "field must be build"):
            cli.validate_build_agent("", "field")
        with self.assertRaisesRegex(cli.OcmoError, "artifact names"):
            cli.validate_artifact_name("bad name", "field")
        with self.assertRaisesRegex(cli.OcmoError, "step.artifact syntax"):
            cli.parse_artifact_reference([], "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be a non-empty string"):
            cli.validate_consumes([""], "field", {"plan": {"plan"}})
        with self.assertRaisesRegex(cli.OcmoError, "must use <step-id>.<artifact-id>"):
            cli.validate_consumes(["bad"], "field", {"plan": {"plan"}})
        with self.assertRaisesRegex(cli.OcmoError, "artifact names"):
            cli.parse_artifact_reference("bad name.plan", "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be a mapping"):
            cli.validate_produces({"plan": "bad"}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "required must be a boolean"):
            cli.validate_produces({"plan": {"required": "yes"}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "description must be a string"):
            cli.validate_produces({"plan": {"description": 1}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be a non-empty string"):
            cli.validate_artifact_path_template("", "field")
        with self.assertRaisesRegex(cli.OcmoError, "relative and stay under artifacts"):
            cli.artifact_path(self.manifest_path, {"id": "1"}, "run", "plan", "artifacts/../plan.md")
        self.assertEqual(cli.validate_produces({"plan": None}, "field"), {"plan"})
        self.assertEqual(cli.produced_artifacts({}), {})
        self.assertEqual(cli.verify_required_artifacts(self.manifest_path, {"id": "1"}, {"id": "run", "produces": {"note": {"required": False}}}), {})
        self.assertEqual(cli.produced_artifact_relative_path(self.manifest_path, {"id": "1"}, "plan", "handoff", {}), "artifacts/1/plan/handoff.json")
        rendered = cli.consumed_artifacts(
            self.manifest_path,
            {"id": "1"},
            {"id": "implement", "consumes": ["plan.plan"]},
            [{"id": "plan", "produces": {"plan": {}}}, {"id": "implement"}],
        )
        self.assertIn("will be generated", rendered)

    def test_handoff_verification_applies_gates(self) -> None:
        path = self.root / "artifacts" / "1" / "plan" / "handoff.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "ocmo-handoff/v1",
                    "decision": "proceed",
                    "confidence": 0.95,
                    "handoff": "Implement the proposed fix.",
                    "conditions": [{"name": "root_cause", "met": True}],
                }
            ),
            encoding="utf-8",
        )

        verified = cli.verify_required_artifacts(
            self.manifest_path,
            {"id": "1"},
            {"id": "plan", "produces": {"handoff": {"gates": {"decision": "proceed", "minConfidence": 0.9, "requireConditionsMet": True}}}},
        )

        self.assertEqual(verified, {"handoff": "artifacts/1/plan/handoff.json"})

    def test_handoff_verification_blocks_on_low_confidence(self) -> None:
        path = self.root / "artifacts" / "1" / "plan" / "handoff.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": "ocmo-handoff/v1", "decision": "proceed", "confidence": 0.7, "handoff": "Maybe fix parser."}), encoding="utf-8")

        with self.assertRaisesRegex(cli.OcmoBlocked, "confidence"):
            cli.verify_required_artifacts(
                self.manifest_path,
                {"id": "1"},
                {"id": "plan", "produces": {"handoff": {"gates": {"minConfidence": 0.9}}}},
            )

    def test_artifact_helpers_cover_defaults_and_errors(self) -> None:
        manifest = self.load()
        item = manifest["workUnits"][0]
        self.assertEqual(cli.produced_artifacts({}), {})
        self.assertEqual(cli.validate_produces(None, "field"), set())
        self.assertEqual(cli.validate_produces({"plan": None}, "field"), {"plan"})
        self.assertEqual(cli.artifact_relative_path(self.manifest_path, item, "run", "note", "artifacts/$work_unit_id/$run_id/$artifact_id.md"), "artifacts/1/run/note.md")
        self.assertIn("will be generated", cli.consumed_artifacts(self.manifest_path, item, {"id": "implement", "consumes": ["plan.plan"]}, [{"id": "plan", "produces": {"plan": {}}}]))
        with self.assertRaisesRegex(cli.OcmoError, "artifact names"):
            cli.validate_artifact_name("bad name", "field")
        with self.assertRaisesRegex(cli.OcmoError, "must use step.artifact"):
            cli.parse_artifact_reference([], "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be build"):
            cli.validate_build_agent("", "agent")
        with self.assertRaisesRegex(cli.OcmoError, "must be a mapping"):
            cli.validate_produces({"plan": []}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "required must be a boolean"):
            cli.validate_produces({"plan": {"required": "yes"}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "description must be a string"):
            cli.validate_produces({"plan": {"description": 1}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "non-empty string"):
            cli.validate_artifact_path_template("", "path")
        with self.assertRaisesRegex(cli.OcmoError, "relative and stay under artifacts"):
            cli.artifact_path(self.manifest_path, item, "run", "note", "artifacts-link/note.md")

    def test_artifact_helpers_validate_edge_cases(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "field must be build"):
            cli.validate_build_agent("", "field")
        with self.assertRaisesRegex(cli.OcmoError, "artifact names"):
            cli.validate_artifact_name("bad name", "field")
        with self.assertRaisesRegex(cli.OcmoError, "step.artifact syntax"):
            cli.parse_artifact_reference([], "field")
        with self.assertRaisesRegex(cli.OcmoError, "artifact names"):
            cli.parse_artifact_reference("bad name.plan", "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be a mapping"):
            cli.validate_produces({"plan": "bad"}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "required must be a boolean"):
            cli.validate_produces({"plan": {"required": "yes"}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "description must be a string"):
            cli.validate_produces({"plan": {"description": 1}}, "field")
        with self.assertRaisesRegex(cli.OcmoError, "must be a non-empty string"):
            cli.validate_artifact_path_template("", "field")
        with self.assertRaisesRegex(cli.OcmoError, "relative and stay under artifacts"):
            cli.artifact_path(self.manifest_path, {"id": "1"}, "run", "plan", "artifacts/../plan.md")
        self.assertEqual(cli.validate_produces({"plan": None}, "field"), {"plan"})
        self.assertEqual(cli.produced_artifacts({}), {})
        self.assertIn("will be generated", cli.consumed_artifacts(self.manifest_path, {"id": "1"}, {"id": "implement", "consumes": ["plan.plan"]}, [{"id": "plan", "produces": {"plan": {}}}, {"id": "implement"}]))

    def test_build_command_and_format_command_hide_prompt(self) -> None:
        manifest = self.load()
        command = cli.build_command(
            manifest,
            self.manifest_path,
            "prompt with spaces",
            runner={
                "command": "opencode",
                "mode": "run",
                "agent": "build",
                "model": "m",
                "attach": "file.txt",
                "title": "My Title",
                "dangerouslySkipPermissions": True,
            },
        )

        self.assertEqual(command[-1], "prompt with spaces")
        formatted = cli.format_command(command)
        self.assertIn("--dangerously-skip-permissions", formatted)
        self.assertIn('"My Title"', formatted)
        self.assertNotIn("prompt with spaces", formatted)
        self.assertIn("<prompt>", formatted)

    def test_build_command_can_use_prompt_file_transport(self) -> None:
        manifest = self.load()
        prompt_file = self.root / "prompt-input.md"

        command = cli.build_command(manifest, self.manifest_path, "long prompt", prompt_file=prompt_file)

        self.assertIn("--file", command)
        self.assertIn(str(prompt_file), command)
        self.assertEqual(command[-1], cli.PROMPT_FILE_MESSAGE)
        self.assertNotIn("long prompt", cli.format_command(command))

    def test_prompt_input_path_is_absolute_for_relative_manifest(self) -> None:
        path = cli.prompt_input_path(Path(".ocmo/op/manifest.yaml"), "ITEM-1", "default")

        self.assertTrue(path.is_absolute())
        self.assertTrue(str(path).endswith(str(Path(".ocmo/op/prompt-inputs/ITEM-1/default.md"))))

    def test_build_command_passes_known_provider_model_and_variant(self) -> None:
        manifest = self.load()
        command = cli.build_command(
            manifest,
            self.manifest_path,
            "prompt",
            runner={
                "command": "opencode",
                "model": "github-copilot/claude-sonnet",
                "reasoningEffort": "high",
            },
        )

        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "github-copilot/claude-sonnet")
        self.assertIn("--variant", command)
        self.assertEqual(command[command.index("--variant") + 1], "high")

    def test_build_command_omits_variant_when_effort_unset(self) -> None:
        manifest = self.load()
        command = cli.build_command(
            manifest,
            self.manifest_path,
            "prompt",
            runner={"command": "opencode", "model": "openai/gpt-5.5"},
        )
        self.assertNotIn("--variant", command)

    def test_validate_manifest_rejects_unknown_provider(self) -> None:
        manifest = self.load()
        manifest["runner"]["model"] = "bogus/foo"
        with self.assertRaisesRegex(cli.OcmoError, "provider 'bogus' is not supported"):
            cli.validate_manifest_schema(manifest, self.manifest_path)

    def test_validate_manifest_accepts_known_providers(self) -> None:
        for model in ("opencode/zen", "github-copilot/claude-sonnet", "openai/gpt-5.5", "anthropic/claude-sonnet-4"):
            manifest = self.load()
            manifest["runner"]["model"] = model
            cli.validate_manifest_schema(manifest, self.manifest_path)

    def test_validate_manifest_rejects_invalid_reasoning_effort(self) -> None:
        manifest = self.load()
        manifest["runner"]["reasoningEffort"] = "extreme"
        with self.assertRaisesRegex(cli.OcmoError, "reasoningEffort must be one of"):
            cli.validate_manifest_schema(manifest, self.manifest_path)

    def test_validate_manifest_accepts_valid_reasoning_effort(self) -> None:
        for effort in cli.REASONING_EFFORT_VALUES:
            manifest = self.load()
            manifest["runner"]["reasoningEffort"] = effort
            cli.validate_manifest_schema(manifest, self.manifest_path)

    def test_per_run_step_can_override_reasoning_effort(self) -> None:
        manifest = self.load()
        manifest["runner"]["reasoningEffort"] = "low"
        runner = cli.effective_runner(manifest, {"id": "step", "reasoningEffort": "high"})
        self.assertEqual(runner["reasoningEffort"], "high")
        command = cli.build_command(manifest, self.manifest_path, "p", runner=runner)
        self.assertEqual(command[command.index("--variant") + 1], "high")


class RunManifestTests(OcmoTestCase):
    def test_dry_run_prints_each_sequential_run_without_state_or_subprocess(self) -> None:
        impl_prompt = self.root / "impl.md"
        review_prompt = self.root / "review.md"
        impl_prompt.write_text("Implement $run_id $run_index", encoding="utf-8")
        review_prompt.write_text("Review $run_id $run_index", encoding="utf-8")
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "implement", "agent": "build", "prompt": {"template": str(impl_prompt)}, "produces": {"plan": {}}},
                {"id": "review", "agent": "build", "prompt": {"template": str(review_prompt)}, "consumes": ["implement.plan"]},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.subprocess.run") as run, contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False))

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("# work unit 1 / run implement", output)
        self.assertIn("# work unit 1 / run review", output)
        self.assertIn("# produces: plan -> artifacts/1/implement/plan.md", output)
        self.assertIn("# consumes: implement.plan", output)
        self.assertIn("Implement implement 1", output)
        self.assertIn("Review review 2", output)
        run.assert_not_called()
        self.assertFalse((self.root / "state.json").exists())

    def test_run_manifest_executes_runs_in_order_and_writes_nested_state(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "two", "agent": "build"}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        calls: list[list[str]] = []

        def fake_popen(command: list[str], **kwargs):
            calls.append(command)
            return FakePopen(command, 0, f"agent output for {command[-1]}\n")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("one", calls[0][-1])
        self.assertIn("two", calls[1][-1])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["control"]["status"], "completed")
        self.assertIn("completedAt", state)
        self.assertEqual(state["workUnits"]["1"]["status"], "completed")
        self.assertEqual(state["workUnits"]["1"]["runs"]["one"]["status"], "completed")
        self.assertEqual(state["workUnits"]["1"]["runs"]["two"]["status"], "completed")
        self.assertEqual(state["workUnits"]["1"]["runs"]["one"]["outputPath"], "outputs/1__one.txt")
        self.assertEqual(state["workUnits"]["1"]["runs"]["two"]["outputPath"], "outputs/1__two.txt")
        self.assertIn("agent output for", (self.root / "outputs" / "1__one.txt").read_text(encoding="utf-8"))
        self.assertIn("[ocmo] exit code: 0", (self.root / "outputs" / "1__two.txt").read_text(encoding="utf-8"))

    def test_run_manifest_persists_opencode_token_usage(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":100,"input":40,"output":10,"reasoning":5,"cache":{"read":45,"write":0}},"cost":0.01}}',
                "not json",
                '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":50,"input":20,"output":4,"reasoning":1,"cache":{"read":25,"write":0}},"cost":0.02}}',
            ]
        )

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, stdout)), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        usage = state["workUnits"]["1"]["runs"]["default"]["usage"]
        self.assertEqual(usage["total"], 150)
        self.assertEqual(usage["input"], 60)
        self.assertEqual(usage["output"], 14)
        self.assertEqual(usage["reasoning"], 6)
        self.assertEqual(usage["cacheRead"], 70)
        self.assertEqual(usage["steps"], 2)
        self.assertEqual(usage["cost"], 0.03)

    def test_run_manifest_uses_prompt_file_for_long_prompt(self) -> None:
        long_text = "x" * (cli.PROMPT_ARG_MAX_CHARS + 1)
        self.prompt.write_text(long_text, encoding="utf-8")
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        captured: dict[str, list[str]] = {}

        def fake_popen(command: list[str], **kwargs):
            captured["command"] = command
            return FakePopen(command, 0, "ok")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        command = captured["command"]
        self.assertIn("--file", command)
        prompt_path = Path(command[command.index("--file") + 1])
        self.assertEqual(command[-1], cli.PROMPT_FILE_MESSAGE)
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), long_text)
        self.assertNotIn(long_text, command)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        run_state = state["workUnits"]["1"]["runs"]["default"]
        self.assertEqual(run_state["promptPath"], "prompt-inputs/1/default.md")

    def test_run_manifest_dry_run_previews_long_prompt_file_transport(self) -> None:
        long_text = "x" * (cli.PROMPT_ARG_MAX_CHARS + 1)
        self.prompt.write_text(long_text, encoding="utf-8")
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False))

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("# prompt transport: file when executed -> prompt-inputs/1/default.md", output)
        self.assertIn("--file", output)
        self.assertIn(long_text, output)
        self.assertFalse((self.root / "prompt-inputs").exists())

    def test_run_manifest_writes_readable_output_for_opencode_json(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"step_start","sessionID":"ses-readable","part":{"type":"step-start"}}',
                '{"type":"text","sessionID":"ses-readable","part":{"type":"text","text":"Readable answer."}}',
                '{"type":"step_finish","sessionID":"ses-readable","part":{"type":"step-finish","tokens":{"total":7,"input":5,"output":2,"cache":{"read":0,"write":0}}}}',
            ]
        )

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, stdout)), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        output = (self.root / "outputs" / "1__default.txt").read_text(encoding="utf-8")
        self.assertIn("Readable answer.", output)
        self.assertNotIn('"type":"text"', output)
        self.assertNotIn('"type":"step_finish"', output)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        run = state["workUnits"]["1"]["runs"]["default"]
        self.assertEqual(run["sessionId"], "ses-readable")
        self.assertEqual(run["usage"]["total"], 7)

    def test_run_opencode_command_streams_readable_output_before_exit(self) -> None:
        output_path = self.root / "outputs" / "stream.txt"
        lines = [
            '{"type":"text","sessionID":"ses-stream","part":{"type":"text","text":"First chunk."}}\n',
            '{"type":"text","sessionID":"ses-stream","part":{"type":"text","text":"Second chunk."}}\n',
        ]

        class StreamingStdout:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self) -> str:
                if self.index == 1:
                    current = output_path.read_text(encoding="utf-8")
                    self_test.assertIn("First chunk.", current)
                    self_test.assertNotIn('"type":"text"', current)
                if self.index >= len(lines):
                    raise StopIteration
                line = lines[self.index]
                self.index += 1
                return line

        class StreamingPopen:
            pid = 1234
            returncode = 0

            def __init__(self, command: list[str], **kwargs) -> None:
                self.args = command
                self.stdout = StreamingStdout()

            def wait(self, timeout: int | None = None):
                return self.returncode

        self_test = self
        sessions: list[str] = []
        with mock.patch("ocmo.cli.subprocess.Popen", StreamingPopen):
            completed = cli.run_opencode_command(
                ["opencode", "run", "--format", "json", "prompt"],
                self.root,
                None,
                output_path,
                on_session=sessions.append,
            )

        output = output_path.read_text(encoding="utf-8")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("First chunk.Second chunk.", output)
        self.assertIn("[ocmo] exit code: 0", output)
        self.assertEqual(sessions, ["ses-stream", "ses-stream"])

    def test_run_opencode_command_times_out_while_streaming_output(self) -> None:
        output_path = self.root / "outputs" / "stream-timeout.txt"
        first_line_written = threading.Event()
        release_reader = threading.Event()

        class BlockingStdout:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self) -> str:
                if self.index == 0:
                    self.index += 1
                    return '{"type":"text","sessionID":"ses-timeout","part":{"type":"text","text":"Before timeout."}}\n'
                first_line_written.set()
                release_reader.wait(timeout=2)
                raise StopIteration

        class TimeoutPopen:
            pid = 4321
            returncode = None

            def __init__(self, command: list[str], **kwargs) -> None:
                self.args = command
                self.stdout = BlockingStdout()

            def wait(self, timeout: int | None = None):
                first_line_written.wait(timeout=2)
                raise subprocess.TimeoutExpired(self.args, timeout)

        sessions: list[str] = []
        with mock.patch("ocmo.cli.subprocess.Popen", TimeoutPopen), mock.patch("ocmo.cli.terminate_process_tree") as terminate:
            with self.assertRaises(subprocess.TimeoutExpired):
                cli.run_opencode_command(
                    ["opencode", "run", "--format", "json", "prompt"],
                    self.root,
                    1,
                    output_path,
                    on_session=sessions.append,
                )
        release_reader.set()

        output = output_path.read_text(encoding="utf-8")
        self.assertIn("Before timeout.", output)
        self.assertNotIn('"type":"text"', output)
        self.assertIn("[ocmo] timed out after 1 seconds", output)
        self.assertEqual(sessions, ["ses-timeout"])
        terminate.assert_called_once_with(4321, force=True)

    def test_usage_parsing_ignores_non_step_events(self) -> None:
        self.assertIsNone(cli.extract_usage_delta("not json"))
        self.assertIsNone(cli.extract_usage_delta("{"))
        self.assertIsNone(cli.extract_usage_delta('{"type":"text","part":{"tokens":{"total":9}}}'))
        self.assertIsNone(cli.extract_usage_delta('{"type":"step_finish","part":{}}'))
        usage = cli.extract_usage_delta('{"type":"step_finish","part":{"tokens":{"total":9,"input":3,"output":2,"cache":{"read":4,"write":1}}}}')
        self.assertEqual(usage["total"], 9)
        self.assertEqual(usage["cacheRead"], 4)
        self.assertEqual(usage["cacheWrite"], 1)
        self.assertEqual(len(cli.extract_usage_deltas("x\n{")), 0)
        self.assertEqual(cli.usage_int(True), 0)
        self.assertEqual(cli.usage_int(1.9), 1)
        self.assertEqual(cli.usage_number(False), 0)
        self.assertEqual(cli.add_usage({"cost": 1.0}, None)["cost"], 1)
        self.assertEqual(cli.sum_usage(None)["total"], 0)
        self.assertEqual(cli.format_token_count(0), "-")
        self.assertEqual(cli.format_token_count(1_200_000), "1.2m")

    def test_run_opencode_command_reports_usage_without_session_callback(self) -> None:
        output = self.root / "outputs" / "usage.txt"
        events = ['{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":12,"input":8,"output":4}}}']
        usage: list[dict[str, object]] = []

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=FakePopen(["opencode"], 0, "\n".join(events))):
            completed = cli.run_opencode_command(["opencode", "run", "prompt"], self.root, None, output, on_usage=usage.append)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(usage[0]["total"], 12)

    def test_run_manifest_chains_required_artifacts(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "plan", "agent": "build", "produces": {"plan": {}}},
                {"id": "implement", "agent": "build", "consumes": ["plan.plan"]},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        plan_artifact = self.root / "artifacts" / "1" / "plan" / "plan.md"
        prompts: list[str] = []

        def fake_popen(command: list[str], **kwargs):
            prompts.append(command[-1])
            if "Required Artifacts" in command[-1]:
                plan_artifact.parent.mkdir(parents=True)
                plan_artifact.write_text("artifact plan", encoding="utf-8")
            return FakePopen(command, 0, "ok")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        self.assertIn("artifact plan", prompts[1])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workUnits"]["1"]["runs"]["plan"]["artifacts"], {"plan": "artifacts/1/plan/plan.md"})

    def test_run_manifest_blocks_later_runs_when_handoff_gate_fails(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "plan", "agent": "build", "produces": {"handoff": {"gates": {"decision": "proceed", "minConfidence": 0.9}}}},
                {"id": "implement", "agent": "build", "consumes": ["plan.handoff"]},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        handoff = self.root / "artifacts" / "1" / "plan" / "handoff.json"
        calls = 0

        def fake_popen(command: list[str], **kwargs):
            nonlocal calls
            calls += 1
            handoff.parent.mkdir(parents=True)
            handoff.write_text(json.dumps({"schema": "ocmo-handoff/v1", "decision": "block", "confidence": 0.75, "handoff": "Not enough evidence."}), encoding="utf-8")
            return FakePopen(command, 0, "ok")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 1)
        self.assertEqual(calls, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["control"]["status"], "blocked")
        self.assertEqual(state["workUnits"]["1"]["status"], "blocked")
        self.assertEqual(state["workUnits"]["1"]["runs"]["plan"]["status"], "blocked")
        self.assertNotIn("implement", state["workUnits"]["1"].get("runs", {}))

    def test_run_manifest_allows_later_runs_when_handoff_gate_passes(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "plan", "agent": "build", "produces": {"handoff": {"gates": {"decision": "proceed", "minConfidence": 0.9}}}},
                {"id": "implement", "agent": "build", "consumes": ["plan.handoff"]},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        handoff = self.root / "artifacts" / "1" / "plan" / "handoff.json"
        prompts: list[str] = []

        def fake_popen(command: list[str], **kwargs):
            prompts.append(command[-1])
            if "Required Artifacts" in command[-1]:
                handoff.parent.mkdir(parents=True)
                handoff.write_text(json.dumps({"schema": "ocmo-handoff/v1", "decision": "proceed", "confidence": 0.95, "handoff": "Implement minimal fix."}), encoding="utf-8")
            return FakePopen(command, 0, "ok")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Implement minimal fix", prompts[1])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workUnits"]["1"]["runs"]["plan"]["artifacts"], {"handoff": "artifacts/1/plan/handoff.json"})

    def test_run_manifest_fails_when_required_artifact_missing(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "plan", "agent": "build", "produces": {"plan": {}}}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "ok")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workUnits"]["1"]["runs"]["plan"]["status"], "failed")
        self.assertIn("required artifact", state["workUnits"]["1"]["runs"]["plan"]["error"])

    def test_run_manifest_writes_clean_utf8_output_files(self) -> None:
        manifest = self.load()
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        def fake_popen(command: list[str], **kwargs):
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["errors"], "replace")
            self.assertEqual(kwargs["env"]["NO_COLOR"], "1")
            self.assertEqual(kwargs["env"]["FORCE_COLOR"], "0")
            self.assertEqual(kwargs["env"]["TERM"], "dumb")
            return FakePopen(command, 0, "\x1b[0mhello\x1b[31m red\x1b[0m\n")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        output = (self.root / "outputs" / "1__default.txt").read_text(encoding="utf-8")
        self.assertIn("hello red", output)
        self.assertNotIn("\x1b", output)

    def test_run_manifest_stops_later_runs_after_failure(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "two"}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(7, "")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["control"]["status"], "failed")
        self.assertEqual(state["workUnits"]["1"]["status"], "failed")
        self.assertEqual(state["workUnits"]["1"]["runs"]["one"]["status"], "failed")
        self.assertNotIn("two", state["workUnits"]["1"].get("runs", {}))

    def test_timeout_marks_item_and_run_timed_out(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=FakePopen(["opencode"], 0, "", timeout=True)), mock.patch("ocmo.cli.terminate_process_tree"), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, 1, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workUnits"]["1"]["status"], "timed_out")
        self.assertEqual(state["workUnits"]["1"]["runs"]["default"]["status"], "timed_out")
        self.assertEqual(state["workUnits"]["1"]["runs"]["default"]["outputPath"], "outputs/1__default.txt")
        self.assertIn("[ocmo] timed out after 1 seconds", (self.root / "outputs" / "1__default.txt").read_text(encoding="utf-8"))

    def test_run_manifest_ctrl_c_marks_active_runs_paused_and_stops_processes(self) -> None:
        self.write_manifest()

        def interrupted_worker(manifest, manifest_path, item, state, *args):
            state.mark(str(item["id"]), "running", {"startedAt": cli.utc_now()})
            state.mark_run(str(item["id"]), "default", "running", {"pid": 222, "sessionId": "ses-1"})
            raise KeyboardInterrupt

        stderr = io.StringIO()
        with mock.patch("ocmo.cli.run_item", side_effect=interrupted_worker), mock.patch("ocmo.cli.terminate_process_tree") as terminate, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 130)
        self.assertIn("pausing active runs", stderr.getvalue())
        terminate.assert_called_once_with(222, force=True)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["control"]["status"], "paused")
        self.assertEqual(state["workUnits"]["1"]["status"], "paused")
        self.assertEqual(state["workUnits"]["1"]["runs"]["default"]["status"], "paused")

    def test_run_opencode_command_ctrl_c_terminates_child_and_logs_interrupt(self) -> None:
        class InterruptingPopen(FakePopen):
            def communicate(self, timeout: int | None = None):
                raise KeyboardInterrupt

        output = self.root / "outputs" / "interrupt.txt"
        with mock.patch("ocmo.cli.subprocess.Popen", return_value=InterruptingPopen(["opencode"], 0, "")), mock.patch("ocmo.cli.terminate_process_tree") as terminate:
            with self.assertRaises(KeyboardInterrupt):
                cli.run_opencode_command(["opencode", "run", "prompt"], self.root, None, output)

        terminate.assert_called_once_with(1234, force=True)
        self.assertIn("[ocmo] interrupted", output.read_text(encoding="utf-8"))

        before_start_output = self.root / "outputs" / "interrupt-before-start.txt"
        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=KeyboardInterrupt), mock.patch("ocmo.cli.terminate_process_tree") as terminate_before_start:
            with self.assertRaises(KeyboardInterrupt):
                cli.run_opencode_command(["opencode", "run", "prompt"], self.root, None, before_start_output)
        terminate_before_start.assert_not_called()
        self.assertIn("[ocmo] interrupted", before_start_output.read_text(encoding="utf-8"))

    def test_no_selected_workUnits_returns_zero(self) -> None:
        self.write_manifest()
        (self.root / "state.json").write_text(
            json.dumps({"workUnits": {"1": {"status": "completed"}, "2": {"status": "completed"}}}),
            encoding="utf-8",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, None, None, None, False, True))

        self.assertEqual(code, 0)
        self.assertIn("No work units selected.", stdout.getvalue())

    def test_run_manifest_rejects_invalid_cli_overrides(self) -> None:
        self.write_manifest()

        with self.assertRaisesRegex(cli.OcmoError, "concurrency must be a positive integer"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 0, None, True, False))
        with self.assertRaisesRegex(cli.OcmoError, "timeout must be a positive integer"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, 0, True, False))
        with self.assertRaisesRegex(cli.OcmoError, "--detach cannot be used with --dry-run"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False, detach=True))

    def test_detached_run_starts_child_and_writes_metadata(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        captured = {}

        class FakeProcess:
            pid = 12345

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return FakeProcess()

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.detached_run_id", return_value="ocmo-test"), mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 2, 9, False, False, detach=True))

        self.assertEqual(code, 0)
        self.assertIn("--yes", captured["command"])
        self.assertIn("--ui", captured["command"])
        self.assertNotIn("--detach", captured["command"])
        self.assertEqual(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        local_record = self.root / ".ocmo" / "runs" / "ocmo-test.json"
        global_record = registry / "ocmo-test.json"
        self.assertTrue(local_record.exists())
        self.assertTrue(global_record.exists())
        metadata = json.loads(global_record.read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], 12345)
        self.assertEqual(metadata["concurrency"], 2)
        self.assertEqual(metadata["timeoutSeconds"], 9)
        self.assertIn("status: ocmo operation status --run-id ocmo-test", stdout.getvalue())

    def test_list_shows_detached_runs_and_status_shows_operation_table(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        state = {
            "startedAt": "2026-05-21T09:59:00+00:00",
            "completedAt": "2026-05-21T10:03:00+00:00",
            "updatedAt": "now",
            "workUnits": {
                "1": {"status": "running", "startedAt": "2026-05-21T10:00:00+00:00", "runCount": 1, "runs": {"default": {"status": "running", "startedAt": "2026-05-21T10:01:00+00:00", "outputPath": "outputs/1__default.txt"}}},
                "2": {"status": "completed", "startedAt": "2026-05-21T10:00:00+00:00", "completedAt": "2026-05-21T10:02:03+00:00", "runCount": 1, "runs": {"default": {"status": "completed", "startedAt": "2026-05-21T10:00:10+00:00", "completedAt": "2026-05-21T10:02:00+00:00", "outputPath": "outputs/2__default.txt"}}},
            },
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        record = {
            "schema": "ocmo-detached-run/v1",
            "runId": "ocmo-active",
            "pid": 123,
            "startedAt": "then",
            "manifestPath": str(self.manifest_path),
            "statePath": str(self.root / "state.json"),
            "logPath": str(self.root / ".ocmo" / "runs" / "ocmo-active.log"),
        }
        (registry).mkdir()
        (registry / "ocmo-active.json").write_text(json.dumps(record), encoding="utf-8")
        local_runs = self.root / ".ocmo" / "runs"
        local_runs.mkdir(parents=True)
        (local_runs / "ocmo-active.json").write_text(json.dumps(record), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", return_value=True), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "list"]), 0)
            self.assertEqual(cli.main(["operation", "status", "--once", "--run-id", "ocmo-active"]), 0)
            self.assertEqual(cli.main(["operation", "status", "--once", str(self.manifest_path)]), 0)

        output = stdout.getvalue()
        self.assertIn("ocmo-active active", output)
        self.assertIn("OC Mass Operations: test-op", output)
        self.assertIn("selected=2 running=1 completed=1 failed=0 blocked=0 pending=0 paused=0 killed=0 tokens=- elapsed=04:00 stateUpdated=now", output)
        self.assertIn("Work Unit", output)
        self.assertIn("Progress", output)
        self.assertIn("Work Time", output)
        self.assertIn("Agent Time", output)
        self.assertIn("Tokens", output)
        self.assertRegex(output, r"1\s+running")
        self.assertIn("outputs/1__default.txt", output)
        self.assertIn("02:03", output)
        self.assertIn("01:50", output)

    def test_status_and_list_details_show_token_usage(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        registry.mkdir()
        state = {
            "updatedAt": "now",
            "workUnits": {
                "1": {
                    "status": "completed",
                    "startedAt": "2026-05-21T10:00:00+00:00",
                    "completedAt": "2026-05-21T10:00:10+00:00",
                    "runCount": 1,
                    "runs": {"default": {"status": "completed", "usage": {"total": 1500, "input": 400, "output": 100, "reasoning": 0, "cacheRead": 1000, "cacheWrite": 0, "cost": 0, "steps": 1}}},
                }
            },
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        record = {"runId": "usage", "pid": 123, "startedAt": "then", "manifestPath": str(self.manifest_path), "statePath": str(self.root / "state.json")}
        (registry / "usage.json").write_text(json.dumps(record), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", return_value=True), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "status", "--once", str(self.manifest_path)]), 0)
            self.assertEqual(cli.main(["operation", "list", "--run-id", "usage"]), 0)

        output = stdout.getvalue()
        self.assertIn("tokens=1.5k in=400 out=100 cache=1.0k", output)
        self.assertIn("400/100", output)

    def test_operation_list_discovers_state_backed_generated_operations(self) -> None:
        running_dir = self.root / ".ocmo" / "running-op"
        completed_dir = self.root / ".ocmo" / "completed-op"
        running_dir.mkdir(parents=True)
        completed_dir.mkdir(parents=True)
        manifest = self.load()
        manifest["state"]["path"] = "state.json"
        manifest["operation"]["id"] = "running-op"
        (running_dir / "manifest.yaml").write_text(yaml_dump(manifest), encoding="utf-8")
        (running_dir / "state.json").write_text(
            json.dumps({"startedAt": "2026-05-21T10:00:00+00:00", "updatedAt": "2026-05-21T10:01:00+00:00", "workUnits": {"1": {"status": "running", "runs": {"default": {"status": "running"}}}}}),
            encoding="utf-8",
        )
        manifest["operation"]["id"] = "completed-op"
        (completed_dir / "manifest.yaml").write_text(yaml_dump(manifest), encoding="utf-8")
        (completed_dir / "state.json").write_text(
            json.dumps({"startedAt": "2026-05-21T10:00:00+00:00", "completedAt": "2026-05-21T10:03:00+00:00", "updatedAt": "2026-05-21T10:03:00+00:00", "workUnits": {"1": {"status": "completed"}}}),
            encoding="utf-8",
        )

        old_cwd = Path.cwd()
        try:
            import os as test_os

            test_os.chdir(self.root)
            registry = self.root / "registry"
            registry.mkdir()
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["operation", "list"]), 0)
                self.assertEqual(cli.main(["operation", "list", "--all"]), 0)
        finally:
            test_os.chdir(old_cwd)

        output = stdout.getvalue()
        self.assertIn("running-op active kind=operation", output)
        self.assertIn("completed-op inactive kind=operation", output)
        self.assertIn("elapsed=03:00", output)

    def test_operation_status_active_or_latest_shows_active_else_latest(self) -> None:
        active_dir = self.root / ".ocmo" / "active-op"
        older_dir = self.root / ".ocmo" / "older-op"
        latest_dir = self.root / ".ocmo" / "latest-op"
        for path in (active_dir, older_dir, latest_dir):
            path.mkdir(parents=True)
        manifest = self.load()
        manifest["state"]["path"] = "state.json"
        manifest["operation"]["id"] = "active-op"
        (active_dir / "manifest.yaml").write_text(yaml_dump(manifest), encoding="utf-8")
        (active_dir / "state.json").write_text(json.dumps({"updatedAt": "2026-05-21T10:02:00+00:00", "workUnits": {"1": {"status": "running"}}}), encoding="utf-8")
        manifest["operation"]["id"] = "older-op"
        (older_dir / "manifest.yaml").write_text(yaml_dump(manifest), encoding="utf-8")
        (older_dir / "state.json").write_text(json.dumps({"updatedAt": "2026-05-21T10:01:00+00:00", "workUnits": {"1": {"status": "completed"}}}), encoding="utf-8")
        manifest["operation"]["id"] = "latest-op"
        (latest_dir / "manifest.yaml").write_text(yaml_dump(manifest), encoding="utf-8")
        (latest_dir / "state.json").write_text(json.dumps({"updatedAt": "2026-05-21T10:03:00+00:00", "workUnits": {"1": {"status": "completed"}}}), encoding="utf-8")

        old_cwd = Path.cwd()
        try:
            import os as test_os

            test_os.chdir(self.root)
            registry = self.root / "registry"
            registry.mkdir()
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["operation", "status", "--active-or-latest", "--once"]), 0)
            active_output = stdout.getvalue()
            (active_dir / "state.json").write_text(json.dumps({"updatedAt": "2026-05-21T10:02:00+00:00", "workUnits": {"1": {"status": "completed"}}}), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["operation", "status", "--active-or-latest", "--once"]), 0)
            latest_output = stdout.getvalue()
        finally:
            test_os.chdir(old_cwd)

        self.assertIn("Active OCMO operation statuses", active_output)
        self.assertIn("OC Mass Operations: active-op", active_output)
        self.assertNotIn("OC Mass Operations: latest-op", active_output)
        self.assertIn("Latest OCMO operation status", latest_output)
        self.assertIn("OC Mass Operations: latest-op", latest_output)
        self.assertNotIn("OC Mass Operations: older-op", latest_output)

    def test_status_errors_for_missing_run_id_and_filters_inactive(self) -> None:
        registry = self.root / "registry"
        registry.mkdir()
        (registry / "inactive.json").write_text(json.dumps({"runId": "inactive", "pid": 99}), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", return_value=False), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "list"]), 0)
            self.assertEqual(cli.main(["operation", "list", "--all"]), 0)
        self.assertIn("No active ocmo operations.", stdout.getvalue())
        self.assertIn("inactive inactive", stdout.getvalue())

        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "status", "--once", "--run-id", "missing"]), 2)
        self.assertIn("detached run not found", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "list", "--run-id", "missing"]), 2)
        self.assertIn("detached run not found", stderr.getvalue())

    def test_detached_helper_edge_cases(self) -> None:
        self.write_manifest()
        manifest = cli.load_manifest(self.manifest_path)
        registry = self.root / "registry"
        self.assertRegex(cli.detached_run_id(), r"^ocmo-\d{8}-\d{6}-[0-9a-f]{6}$")
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "local")}, clear=True):
            self.assertEqual(cli.global_detached_runs_dir(), self.root / "local" / "ocmo" / "runs")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("ocmo.cli.os.name", "nt"), mock.patch("pathlib.Path.home", return_value=self.root / "home"):
            self.assertEqual(cli.global_detached_runs_dir(), self.root / "home" / ".local" / "state" / "ocmo" / "runs")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("ocmo.cli.os.name", "posix"), mock.patch("pathlib.Path.home", return_value=self.root / "home"):
            self.assertEqual(cli.global_detached_runs_dir(), self.root / "home" / ".local" / "state" / "ocmo" / "runs")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("ocmo.cli.os.name", "nt"), mock.patch("pathlib.Path.home", return_value=self.root / "home"):
            self.assertEqual(cli.global_detached_runs_dir(), self.root / "home" / ".local" / "state" / "ocmo" / "runs")
        command = cli.detached_child_command(cli.RunOptions(self.manifest_path, None, None, None, False, False, allow_shared_worktree_concurrency=True))
        self.assertIn("--allow-shared-worktree-concurrency", command)

        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.detached_run_id", return_value="ocmo-fail"), mock.patch("ocmo.cli.subprocess.Popen", side_effect=OSError("nope")):
            with self.assertRaisesRegex(cli.OcmoError, "could not start detached run"):
                cli.start_detached_run(cli.RunOptions(self.manifest_path, None, None, None, False, False), manifest, 1, None)

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(cli.process_is_alive(123))
        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.os.kill", side_effect=PermissionError):
            self.assertTrue(cli.process_is_alive(123))
        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.os.kill", side_effect=OSError):
            self.assertFalse(cli.process_is_alive(123))
        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.os.kill", side_effect=SystemError):
            self.assertFalse(cli.process_is_alive(123))
        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.os.kill", return_value=None):
            self.assertTrue(cli.process_is_alive(123))
        self.assertFalse(cli.process_is_alive("bad"))

        active_task = subprocess.CompletedProcess(["tasklist"], 0, stdout='"python.exe","123","Console","1","1,024 K"\n')
        inactive_task = subprocess.CompletedProcess(["tasklist"], 0, stdout="INFO: No tasks are running which match the specified criteria.\n")
        failed_task = subprocess.CompletedProcess(["tasklist"], 1, stdout="")
        with mock.patch("ocmo.cli.os.name", "nt"), mock.patch("ocmo.cli.windows_process_is_alive", return_value=True):
            self.assertTrue(cli.process_is_alive(123))
        with mock.patch("ocmo.cli.subprocess.run", return_value=active_task):
            self.assertTrue(cli.windows_process_is_alive(123))
        with mock.patch("ocmo.cli.subprocess.run", return_value=inactive_task):
            self.assertFalse(cli.windows_process_is_alive(123))
        with mock.patch("ocmo.cli.subprocess.run", return_value=failed_task):
            self.assertFalse(cli.windows_process_is_alive(123))
        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError):
            self.assertFalse(cli.windows_process_is_alive(123))

        registry.mkdir(exist_ok=True)
        (registry / "bad.json").write_text("{", encoding="utf-8")
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}):
            self.assertEqual(cli.detached_records(include_inactive=True), [])

    def test_status_local_lookup_and_empty_state_branches(self) -> None:
        generated_dir = self.root / ".ocmo" / "generated"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        manifest = self.load()
        manifest["state"]["path"] = "missing-state.json"
        generated_manifest.write_text(yaml_dump(manifest), encoding="utf-8")
        record = {"runId": "local-only", "pid": 0, "startedAt": "then", "statePath": str(self.root / "missing.json")}
        local_path = cli.local_detached_record_path(generated_manifest, "local-only")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(json.dumps(record), encoding="utf-8")
        (local_path.parent / "bad.json").write_text("{", encoding="utf-8")

        old_cwd = Path.cwd()
        try:
            import os as test_os

            test_os.chdir(self.root)
            self.assertEqual(cli.find_detached_record("local-only"), local_path)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                cli.print_manifest_detached_runs(generated_manifest, include_inactive=True)
                cli.print_detached_record(record, details=True)
                cli.print_state_summary({"workUnits": {"x": "bad"}})
        finally:
            test_os.chdir(old_cwd)
        output = stdout.getvalue()
        self.assertIn("workUnits: none", output)
        self.assertIn("workUnits: unknown=1", output)

        inactive_record = cli.local_detached_record_path(generated_manifest, "inactive")
        inactive_record.write_text(json.dumps({"runId": "inactive", "pid": 2}), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.process_is_alive", return_value=False), contextlib.redirect_stdout(stdout):
            cli.print_manifest_detached_runs(generated_manifest, include_inactive=False)
        self.assertNotIn("detached runs:", stdout.getvalue())

        active_record = cli.local_detached_record_path(generated_manifest, "active")
        active_record.write_text(json.dumps({"runId": "active", "pid": 1}), encoding="utf-8")
        inactive_record = cli.local_detached_record_path(generated_manifest, "inactive")
        inactive_record.write_text(json.dumps({"runId": "inactive", "pid": 2}), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.process_is_alive", side_effect=lambda pid: pid == 1), contextlib.redirect_stdout(stdout):
            cli.print_manifest_detached_runs(generated_manifest, include_inactive=False)
        self.assertIn("detached runs:", stdout.getvalue())
        self.assertIn("active active", stdout.getvalue())
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.process_is_alive", return_value=False), contextlib.redirect_stdout(stdout):
            cli.print_manifest_detached_runs(generated_manifest, include_inactive=False)
        self.assertNotIn("detached runs:", stdout.getvalue())

    def test_status_without_state_shows_manifest_workUnits_as_snapshot(self) -> None:
        self.write_manifest()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "status", "--once", str(self.manifest_path)]), 0)

        output = stdout.getvalue()
        self.assertIn("OC Mass Operations: test-op", output)
        self.assertIn("selected=2 running=0 completed=0 failed=0 blocked=0 pending=2 paused=0 killed=0 tokens=- elapsed=- stateUpdated=-", output)
        self.assertIn("1          pending", output)
        self.assertRegex(output, r"2\s+pending")
        self.assertIn("First", output)

    def test_operation_status_watches_until_interrupted(self) -> None:
        self.write_manifest()
        (self.root / "state.json").write_text(json.dumps({"updatedAt": "first", "workUnits": {"1": {"status": "running"}}}), encoding="utf-8")

        slept = False

        def sleep_once(_interval: float) -> None:
            nonlocal slept
            if not slept:
                slept = True
                (self.root / "state.json").write_text(json.dumps({"updatedAt": "second", "workUnits": {"1": {"status": "completed"}}}), encoding="utf-8")
                return
            raise KeyboardInterrupt

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.time.sleep", side_effect=sleep_once), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "status", "--interval", "0.1", str(self.manifest_path)]), 130)

        output = stdout.getvalue()
        self.assertIn("stateUpdated=first", output)
        self.assertIn("stateUpdated=second", output)
        self.assertIn("running=1", output)
        self.assertIn("completed=1", output)

    def test_status_warns_when_detached_run_is_stale(self) -> None:
        self.write_manifest()
        state = {
            "workUnits": {
                "1": {"status": "running", "startedAt": "2026-05-21T10:00:00+00:00", "runs": {"default": {"status": "running"}}},
            },
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        record = {"runId": "stale", "pid": 99, "startedAt": "then", "manifestPath": str(self.manifest_path), "statePath": str(self.root / "state.json")}

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.process_is_alive", return_value=False), contextlib.redirect_stdout(stdout):
            cli.print_operation_status(self.manifest_path, include_inactive=True, selected_record=record)

        output = stdout.getvalue()
        self.assertIn("detached: stale inactive", output)
        self.assertIn("warning: detached run is inactive but state contains running work units", output)

    def test_status_multistep_progress_uses_current_run(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "analyze", "prompt": {"template": self.prompt.as_posix()}},
                {"id": "review", "prompt": {"template": self.prompt.as_posix()}},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        state = {
            "workUnits": {
                "1": {
                    "status": "running",
                    "startedAt": "2026-05-21T10:00:00+00:00",
                    "runCount": 2,
                    "runs": {
                        "analyze": {"status": "completed", "outputPath": "outputs/1__analyze.txt"},
                        "review": {"status": "running", "outputPath": "outputs/1__review.txt"},
                    },
                }
            }
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.print_operation_status(self.manifest_path, include_inactive=False)

        output = stdout.getvalue()
        self.assertIn("review", output)
        self.assertIn("2/2", output)
        self.assertIn("outputs/1__review.txt", output)

    def test_status_and_list_edge_branches(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        registry.mkdir()
        state = {"updatedAt": "later", "workUnits": {"1": {"status": "failed"}}}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        record = {
            "runId": "details",
            "pid": 123,
            "startedAt": "then",
            "manifestPath": str(self.manifest_path),
            "statePath": str(self.root / "state.json"),
            "logPath": str(self.root / "run.log"),
        }
        (registry / "details.json").write_text(json.dumps(record), encoding="utf-8")
        local_runs = self.root / ".ocmo" / "runs"
        local_runs.mkdir(parents=True)
        (local_runs / "aaa-inactive.json").write_text(json.dumps({"runId": "inactive-local", "pid": 0}), encoding="utf-8")
        (local_runs / "details.json").write_text(json.dumps(record), encoding="utf-8")
        (local_runs / "bad.json").write_text("{", encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", side_effect=lambda pid: pid == 123), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "list", "--run-id", "details"]), 0)
            self.assertEqual(cli.main(["operation", "list", str(self.manifest_path)]), 0)
            cli.print_operation_status(self.manifest_path, include_inactive=False)

        output = stdout.getvalue()
        self.assertIn("manifest:", output)
        self.assertIn("stateUpdated: later", output)
        self.assertIn("detached runs:", output)

        fallback = registry / "fallback.json"
        fallback.write_text(json.dumps({"runId": "fallback", "pid": 0, "startedAt": "then"}), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", return_value=False), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "status", "--once", "--run-id", "fallback"]), 0)
        self.assertIn("fallback inactive", stdout.getvalue())

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.related_detached_records", return_value=[{}]), contextlib.redirect_stdout(stdout):
            cli.print_operation_status(self.manifest_path, include_inactive=False, selected_record={})
        self.assertIn("OC Mass Operations: test-op", stdout.getvalue())

        related = cli.related_detached_records(self.manifest_path, include_inactive=True)
        self.assertTrue(any(record.get("runId") == "details" for record in related))

    def test_operation_status_helper_edge_cases(self) -> None:
        manifest = self.load()
        state = {
            "workUnits": {
                "1": {"status": "failed", "startedAt": "not-a-date", "error": "item error"},
                "2": {"status": "queued", "startedAt": "2026-05-21T10:00:00"},
                "state-only": {"status": "running", "worktreePath": "wt-path", "runs": {"default": "bad"}},
                "blank": {"status": "running"},
                "paused": {"status": "paused"},
                "killed": {"status": "killed"},
            }
        }

        rows = cli.operation_status_rows(manifest, state)
        counts = cli.operation_status_counts(rows)

        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["paused"], 1)
        self.assertEqual(counts["killed"], 1)
        self.assertEqual(counts["running"], 2)
        self.assertIn("state-only", [row["item"] for row in rows])
        self.assertIn("wt-path", [row["detail"] for row in rows])
        self.assertIn("-", [row["detail"] for row in rows])
        self.assertEqual(cli.parse_state_datetime("not-a-date"), None)

    def test_pause_and_kill_mark_active_runs_and_stop_processes(self) -> None:
        self.write_manifest()
        state = {
            "workUnits": {
                "1": {"status": "running", "runs": {"default": {"status": "running", "pid": 111, "sessionId": "ses-one"}}},
                "2": {"status": "running", "runs": {"default": {"status": "running", "pid": 222}}},
            }
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        stopped: list[int] = []

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.terminate_process_tree", side_effect=lambda pid, force=True: stopped.append(pid)), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "pause", str(self.manifest_path)]), 0)

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["status"], "paused")
        self.assertEqual(data["workUnits"]["2"]["status"], "paused_unresumable")
        self.assertEqual(sorted(stopped), [111, 222])
        self.assertIn("paused: test-op", stdout.getvalue())

        data["workUnits"]["1"]["status"] = "running"
        data["workUnits"]["1"]["runs"]["default"]["status"] = "running"
        (self.root / "state.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch("ocmo.cli.terminate_process_tree"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "kill", str(self.manifest_path), "--force"]), 0)
        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["status"], "killed")

    def test_resume_uses_session_id_and_refuses_unresumable_runs(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        state = {
            "workUnits": {
                "1": {"status": "paused", "runs": {"default": {"status": "paused", "sessionId": "ses-123"}}},
            }
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        commands: list[list[str]] = []

        def fake_popen(command: list[str], **kwargs):
            commands.append(command)
            return FakePopen(command, 0, '{"sessionID":"ses-123"}\n')

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "resume", str(self.manifest_path), "--yes", "--ui", "plain"]), 0)
        self.assertIn("--session", commands[0])
        self.assertIn("ses-123", commands[0])
        self.assertNotIn("--continue", commands[0])

        state["workUnits"]["1"]["status"] = "paused_unresumable"
        state["workUnits"]["1"]["runs"]["default"] = {"status": "paused_unresumable"}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "resume", str(self.manifest_path), "--yes", "--ui", "plain"]), 1)

    def test_rerun_unresumable_starts_fresh_and_clears_stale_run_state(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        state = {
            "workUnits": {
                "1": {
                    "status": "paused_unresumable",
                    "error": "old item error",
                    "completedAt": "then",
                    "runs": {
                        "default": {
                            "status": "paused_unresumable",
                            "sessionId": "stale-session",
                            "pid": 999,
                            "error": "old run error",
                            "completedAt": "then",
                            "exitCode": 1,
                        }
                    },
                }
            }
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        commands: list[list[str]] = []

        def fake_popen(command: list[str], **kwargs):
            commands.append(command)
            return FakePopen(command, 0, "fresh output\n")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "rerun", str(self.manifest_path), "--select", "unresumable", "--yes", "--ui", "plain"]), 0)

        self.assertEqual(len(commands), 1)
        self.assertNotIn("--session", commands[0])
        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["status"], "completed")
        self.assertNotIn("error", data["workUnits"]["1"])
        run_state = data["workUnits"]["1"]["runs"]["default"]
        self.assertEqual(run_state["status"], "completed")
        self.assertNotIn("sessionId", run_state)
        self.assertEqual(run_state["pid"], 1234)
        self.assertNotIn("error", run_state)

    def test_rerun_retryable_selects_timeout_failed_killed_but_not_paused(self) -> None:
        manifest = self.load(
            """  - id: "3"
    title: Third
    payload: {}
  - id: "4"
    title: Fourth
    payload: {}
  - id: "5"
    title: Fifth
    payload: {}
"""
        )
        state = {
            "workUnits": {
                "1": {"status": "timed_out", "runs": {"default": {"status": "timed_out"}}},
                "2": {"status": "paused", "runs": {"default": {"status": "paused", "sessionId": "ses-2"}}},
                "3": {"status": "failed", "runs": {"default": {"status": "failed"}}},
                "4": {"status": "killed", "runs": {"default": {"status": "killed"}}},
                "5": {"status": "running", "runs": {"default": {"status": "paused_unresumable"}}},
            }
        }
        selected = cli.select_rerun_work_units(manifest, state, "retryable")
        self.assertEqual([str(item["id"]) for item in selected], ["1", "3", "4", "5"])
        self.assertEqual([str(item["id"]) for item in cli.select_rerun_work_units(manifest, state, "timed-out")], ["1"])
        self.assertEqual([str(item["id"]) for item in cli.select_rerun_work_units(manifest, state, "failed")], ["3"])
        self.assertEqual([str(item["id"]) for item in cli.select_rerun_work_units(manifest, state, "killed")], ["4"])
        self.assertEqual([str(item["id"]) for item in cli.select_rerun_work_units(manifest, state, "all")], ["1", "2", "3", "4", "5"])
        self.assertEqual([str(item["id"]) for item in cli.select_rerun_work_units(manifest, state, "1")], ["1"])

    def test_detached_rerun_starts_rerun_child_command(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        state = {"workUnits": {"1": {"status": "timed_out", "runs": {"default": {"status": "timed_out"}}}}}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        captured = {}

        class FakeProcess:
            pid = 2468

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return FakeProcess()

        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.detached_run_id", return_value="ocmo-rerun"), mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "rerun", str(self.manifest_path), "--select", "timed-out", "--detach", "--yes"]), 0)

        self.assertIn("rerun", captured["command"])
        self.assertNotIn("resume", captured["command"])
        self.assertIn("--select", captured["command"])
        self.assertIn("timed-out", captured["command"])

    def test_erase_requires_generated_operation_and_preserves_definition_files(self) -> None:
        self.write_manifest()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "erase", str(self.manifest_path), "--force"]), 2)
        self.assertIn("generated .ocmo", stderr.getvalue())

        generated_dir = self.root / ".ocmo" / "generated"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        generated_manifest.write_text(
            f"""schema: ocmo/v1
operation:
  id: generated
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
queue:
  concurrency: 1
prompt:
  template: prompt.md
state:
  path: state.json
workUnits:
  - id: one
""",
            encoding="utf-8",
        )
        (generated_dir / "prompt.md").write_text("prompt", encoding="utf-8")
        (generated_dir / "state.json").write_text(json.dumps({"workUnits": {}}), encoding="utf-8")
        (generated_dir / "extra.txt").write_text("x", encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest), "--force"]), 0)
        self.assertTrue(generated_dir.exists())
        self.assertTrue(generated_manifest.exists())
        self.assertTrue((generated_dir / "prompt.md").exists())
        self.assertTrue((generated_dir / "extra.txt").exists())
        self.assertFalse((generated_dir / "state.json").exists())
        self.assertIn("erased runtime data:", stdout.getvalue())

    def test_erase_removes_known_runtime_files_only(self) -> None:
        self.write_manifest()
        generated_dir = self.root / ".ocmo" / "runtime-only"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        generated_manifest.write_text(
            f"""schema: ocmo/v1
operation:
  id: runtime-only
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
queue:
  concurrency: 1
prompt:
  template: prompt.md
state:
  path: state.json
workUnits:
  - id: one
""",
            encoding="utf-8",
        )
        (generated_dir / "prompt.md").write_text("prompt", encoding="utf-8")
        (generated_dir / "notes.md").write_text("notes", encoding="utf-8")
        (generated_dir / "state.json").write_text(json.dumps({"workUnits": {}}), encoding="utf-8")
        (generated_dir / "outputs").mkdir()
        (generated_dir / "outputs" / "one.txt").write_text("out", encoding="utf-8")
        (generated_dir / "artifacts").mkdir()
        (generated_dir / "artifacts" / "handoff.md").write_text("artifact", encoding="utf-8")
        (generated_dir / "prompt-inputs").mkdir()
        (generated_dir / "prompt-inputs" / "one.md").write_text("prompt input", encoding="utf-8")
        runs_dir = generated_dir / ".ocmo" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run.log").write_text("log", encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest), "--force"]), 0)

        self.assertTrue(generated_manifest.exists())
        self.assertTrue((generated_dir / "prompt.md").exists())
        self.assertTrue((generated_dir / "notes.md").exists())
        self.assertFalse((generated_dir / "state.json").exists())
        self.assertFalse((generated_dir / "outputs").exists())
        self.assertFalse((generated_dir / "artifacts").exists())
        self.assertFalse((generated_dir / "prompt-inputs").exists())
        self.assertFalse(runs_dir.exists())
        self.assertIn("erased runtime data:", stdout.getvalue())

    def test_erase_interactive_preserves_definition_files(self) -> None:
        generated_dir = self.root / ".ocmo" / "keep-interactive"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        generated_manifest.write_text(
            f"""schema: ocmo/v1
operation:
  id: keep-interactive
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
queue:
  concurrency: 1
prompt:
  template: prompt.md
state:
  path: state.json
workUnits:
  - id: one
""",
            encoding="utf-8",
        )
        (generated_dir / "prompt.md").write_text("prompt", encoding="utf-8")
        (generated_dir / "state.json").write_text(json.dumps({"workUnits": {}}), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.stop_operation_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest)]), 0)

        self.assertTrue(generated_manifest.exists())
        self.assertTrue((generated_dir / "prompt.md").exists())
        self.assertFalse((generated_dir / "state.json").exists())
        self.assertIn("erased runtime data:", stdout.getvalue())

    def test_control_edge_branches_and_helpers(self) -> None:
        self.write_manifest()
        registry = self.root / "registry"
        registry.mkdir()
        (registry / "bad.json").write_text(json.dumps({"runId": "bad", "pid": 1}), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "pause", "--run-id", "bad"]), 2)
        self.assertIn("missing manifestPath", stderr.getvalue())
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "pause", "--run-id", "missing"]), 2)
        self.assertIn("detached run not found", stderr.getvalue())

        state_path = self.root / "state.json"
        state_path.write_text(json.dumps({"workUnits": {"x": "bad", "1": {"status": "running", "runs": {"default": {"status": "completed"}}}}}), encoding="utf-8")
        self.assertEqual(cli.mark_active_runs(state_path, "paused"), 0)
        self.assertEqual(cli.mark_active_runs(self.root / "missing.json", "paused"), 0)
        self.assertEqual(cli.active_state_pids({"workUnits": {"x": "bad"}}), [])

        self.assertEqual(cli.extract_session_id("not json\n{"), None)
        self.assertEqual(cli.extract_session_id('{"session":{}}\n'), None)
        self.assertEqual(cli.extract_session_id('{"session":"bad"}\n'), None)
        self.assertEqual(cli.extract_session_id('{"session":{"id":"nested"}}\n'), "nested")
        self.assertEqual(cli.extract_session_id('{"sessionId":"direct"}\n'), "direct")

        started: list[int] = []
        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "ok")):
            completed = cli.run_opencode_command(["opencode", "run", "prompt"], self.workspace, None, self.root / "output.txt", on_start=started.append)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(started, [1234])
        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "ok")):
            self.assertEqual(cli.run_opencode_command(["opencode", "run", "prompt"], self.workspace, None, self.root / "output2.txt").returncode, 0)
        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=subprocess.TimeoutExpired(["opencode"], 1)), mock.patch("ocmo.cli.terminate_process_tree"):
            with self.assertRaises(subprocess.TimeoutExpired):
                cli.run_opencode_command(["opencode", "run", "prompt"], self.workspace, 1, self.root / "timeout.txt")

        stdout = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "kill", str(self.manifest_path)]), 1)
        self.assertIn("Cancelled.", stdout.getvalue())
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.stop_operation_processes"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "kill", str(self.manifest_path)]), 0)

        record = {"runId": "good", "pid": 333, "manifestPath": str(self.manifest_path), "statePath": str(self.root / "state.json")}
        (registry / "good.json").write_text(json.dumps(record), encoding="utf-8")
        stopped: list[int] = []
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.terminate_process_tree", side_effect=lambda pid, force=True: stopped.append(pid)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "pause", "--run-id", "good"]), 0)
        self.assertIn(333, stopped)
        local_related = cli.local_detached_record_path(self.manifest_path, "related")
        local_related.parent.mkdir(parents=True, exist_ok=True)
        local_related.write_text(json.dumps({"runId": "related", "pid": 444}), encoding="utf-8")
        (local_related.parent / "bad-pid.json").write_text(json.dumps({"runId": "bad-pid", "pid": "bad"}), encoding="utf-8")
        stopped = []
        with mock.patch("ocmo.cli.terminate_process_tree", side_effect=lambda pid, force=True: stopped.append(pid)):
            cli.stop_operation_processes(self.manifest_path, {}, None)
        self.assertIn(444, stopped)

        generated_dir = self.root / ".ocmo" / "cancel"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        generated_manifest.write_text(self.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest)]), 2)
        self.assertIn("requires --force", stderr.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as keep_definition_exit:
            cli.main(["operation", "erase", str(generated_manifest), "--force", "--keep-definition"])
        self.assertEqual(keep_definition_exit.exception.code, 2)
        self.assertIn("unrecognized arguments: --keep-definition", stderr.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as delete_definition_exit:
            cli.main(["operation", "erase", str(generated_manifest), "--force", "--delete-definition"])
        self.assertEqual(delete_definition_exit.exception.code, 2)
        self.assertIn("unrecognized arguments: --delete-definition", stderr.getvalue())
        stdout = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest)]), 1)
        self.assertTrue(generated_dir.exists())
        self.assertIn("Cancelled.", stdout.getvalue())
        local_run = cli.local_detached_record_path(generated_manifest, "good")
        local_run.parent.mkdir(parents=True, exist_ok=True)
        local_run.write_text(json.dumps({"runId": "good", "pid": 1}), encoding="utf-8")
        global_run = registry / "good.json"
        global_run.write_text(json.dumps({"runId": "good", "pid": 1}), encoding="utf-8")
        with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("pathlib.Path.unlink", side_effect=OSError):
            cli.remove_detached_records(generated_manifest)
        with mock.patch("ocmo.cli.related_detached_records", return_value=[{"runId": 123}]):
            cli.remove_detached_records(generated_manifest)
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.stop_operation_processes"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "erase", str(generated_manifest)]), 0)
        self.assertTrue(generated_dir.exists())
        self.assertTrue(generated_manifest.exists())

        self.assertEqual(cli.select_paused_work_units({"workUnits": ["bad", {"id": "1"}, {"id": "2"}]}, {"workUnits": {"1": "bad", "2": {"runs": {"default": {"status": "completed"}}}}}), [])

    def test_process_termination_helpers(self) -> None:
        cli.terminate_process_tree(0)
        with mock.patch("ocmo.cli.process_is_alive", return_value=False):
            cli.terminate_process_tree(123)

        with mock.patch("ocmo.cli.os.name", "nt"), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("ocmo.cli.subprocess.run") as run:
            cli.terminate_process_tree(123, force=False)
        self.assertNotIn("/F", run.call_args.args[0])

        with mock.patch("ocmo.cli.os.name", "nt"), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("ocmo.cli.subprocess.run", side_effect=OSError):
            cli.terminate_process_tree(123)

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("ocmo.cli.os.kill", side_effect=ProcessLookupError):
            cli.terminate_process_tree(123)

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.process_is_alive", side_effect=[True, False]), mock.patch("ocmo.cli.os.kill") as kill:
            cli.terminate_process_tree(123)
        kill.assert_called_once_with(123, cli.signal.SIGTERM)

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.process_is_alive", side_effect=[True, True, False]), mock.patch("ocmo.cli.time.sleep") as sleep, mock.patch("ocmo.cli.os.kill"):
            cli.terminate_process_tree(123)
        sleep.assert_called_once()

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("ocmo.cli.time.monotonic", side_effect=[0, 4]), mock.patch("ocmo.cli.os.kill") as kill:
            cli.terminate_process_tree(123, force=False)
        kill.assert_called_once_with(123, cli.signal.SIGTERM)

        with mock.patch("ocmo.cli.os.name", "posix"), mock.patch("ocmo.cli.process_is_alive", return_value=True), mock.patch("ocmo.cli.time.monotonic", side_effect=[0, 4]), mock.patch("ocmo.cli.os.kill", side_effect=[None, OSError]):
            cli.terminate_process_tree(123)

    def test_resume_multistep_skip_and_missing_session_branches(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "done"}, {"id": "one"}, {"id": "two"}, {"id": "three"}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        state = {
            "workUnits": {
                "1": {
                    "status": "paused",
                    "runs": {
                        "done": {"status": "completed"},
                        "one": {"status": "queued"},
                        "two": {"status": "paused", "sessionId": "ses-two"},
                    },
                }
            }
        }
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        commands: list[list[str]] = []

        def fake_popen(command: list[str], **kwargs):
            commands.append(command)
            return FakePopen(command, 0, '{"sessionID":"ses-two"}\n')

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "resume", str(self.manifest_path), "--yes", "--ui", "plain"]), 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("--session", commands[0])
        self.assertNotIn("--session", commands[1])

        state["workUnits"]["1"]["runs"]["two"] = {"status": "paused"}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["operation", "resume", str(self.manifest_path), "--yes", "--ui", "plain"]), 1)

    def test_run_item_preserves_control_status_after_terminated_child(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        def fake_run(command, run_dir, run_timeout, output_path, on_start=None, on_session=None, on_usage=None):
            state.mark_run("1", "default", "paused", {"sessionId": "ses-paused"})
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=None)

        with mock.patch("ocmo.cli.run_opencode_command", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run_item(manifest, self.manifest_path, manifest["workUnits"][0], state, None, {"enabled": False}), 1)
        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["runs"]["default"]["status"], "paused")

    def test_run_item_preserves_existing_resumed_at_on_normal_run(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)
        state.patch("1", {"resumedAt": "previous-resume"})

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "ok")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run_item(manifest, self.manifest_path, manifest["workUnits"][0], state, None, {"enabled": False}), 0)

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["resumedAt"], "previous-resume")

    def test_run_manifest_rejects_single_worktree_runtime_conflicts(self) -> None:
        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with self.assertRaisesRegex(cli.OcmoError, "policy.worktree=single cannot run with concurrency > 1"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 2, None, True, False))

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 2, None, True, False, allow_shared_worktree_concurrency=True))
        self.assertEqual(code, 0)
        self.assertIn("# work unit 1 / run default", stdout.getvalue())

        manifest["queue"]["concurrency"] = 2
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        cli.validate_manifest(manifest, self.manifest_path)
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False, allow_shared_worktree_concurrency=True))
        self.assertEqual(code, 0)

        manifest["queue"]["concurrency"] = 1
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main"}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        with mock.patch("ocmo.cli.ensure_git_repository"):
            with self.assertRaisesRegex(cli.OcmoError, "autoWorktrees.enabled=true"):
                cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 1, None, True, False, allow_shared_worktree_concurrency=True))

    def test_confirmation_decline_cancels_run(self) -> None:
        self.write_manifest()

        stdout = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), mock.patch("ocmo.cli.subprocess.run") as run, contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, False))

        self.assertEqual(code, 1)
        self.assertIn("Cancelled.", stdout.getvalue())
        run.assert_not_called()

    def test_worker_exception_is_recorded_as_failed_item(self) -> None:
        self.write_manifest()

        with mock.patch("ocmo.cli.run_item", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(state["workUnits"]["1"]["status"], "failed")
        self.assertIn("unexpected worker error", state["workUnits"]["1"]["error"])

    def test_run_item_failed_before_start_and_render_failures_are_recorded(self) -> None:
        manifest = self.load()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        bad_manifest = dict(manifest)
        bad_manifest["operation"] = {"id": "op", "workspace": str(self.root / "missing")}
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_item(bad_manifest, self.manifest_path, {"id": "1"}, state, None, {"enabled": False})
        self.assertEqual(code, 1)

        manifest["prompt"]["template"] = str(self.root / "missing-template.md")
        state = cli.StateStore(self.root / "state2.json")
        state.ensure_operation(manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, {"enabled": False})
        data = json.loads((self.root / "state2.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(data["workUnits"]["1"]["status"], "failed")
        self.assertEqual(data["workUnits"]["1"]["runs"]["default"]["status"], "failed")

    def test_run_item_oserror_from_subprocess_records_failed_run(self) -> None:
        manifest = self.load()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=OSError("cannot start")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_item(manifest, self.manifest_path, manifest["workUnits"][0], state, None, {"enabled": False})

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(data["workUnits"]["1"]["status"], "failed")
        self.assertEqual(data["workUnits"]["1"]["runs"]["default"]["status"], "failed")

    def test_cleanup_failure_after_success_marks_cleanup_failed(self) -> None:
        manifest = self.load()
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main", "cleanup": "always"}
        manifest["policy"] = {}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        def fake_run(command, **kwargs):
            if command[:3] == ["git", "worktree", "add"]:
                return subprocess.CompletedProcess(command, 0)
            if command[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(command, 4)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch("ocmo.cli.ensure_git_repository"), mock.patch("ocmo.cli.subprocess.run", side_effect=fake_run), mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(data["workUnits"]["1"]["status"], "cleanup_failed")


class WorktreeTests(OcmoTestCase):
    def test_worktree_execution_slugifies_paths_and_branch(self) -> None:
        manifest = self.load()
        manifest["operation"]["id"] = "My Operation"
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "root": "worktrees", "baseBranch": "main", "branchPattern": "ocmo/{operation_id}/{work_unit_slug}"}
        item = {"id": "Item 01 / A"}

        execution = cli.worktree_execution(manifest, self.manifest_path, item)

        self.assertEqual(execution["branchName"], "ocmo/My-Operation/Item-01-A")
        self.assertTrue(execution["worktreePath"].endswith("worktrees\\My-Operation\\Item-01-A") or execution["worktreePath"].endswith("worktrees/My-Operation/Item-01-A"))
        self.assertEqual(execution["baseBranch"], "main")

    def test_prepare_worktree_fails_when_path_exists(self) -> None:
        manifest = self.load()
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "existing"), "branchName": "b", "baseBranch": "main"}
        Path(execution["worktreePath"]).mkdir()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        code = cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True}, state)

        self.assertEqual(code, 1)
        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["status"], "worktree_failed")

    def test_cleanup_worktree_marks_removed_on_success(self) -> None:
        manifest = self.load()
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "wt"), "branchName": "b", "baseBranch": "main"}
        Path(execution["worktreePath"]).mkdir()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)) as run:
            code = cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"cleanup": "always"}, state, success=True)

        self.assertEqual(code, 0)
        run.assert_called_once()
        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workUnits"]["1"]["worktreeStatus"], "removed")

    def test_worktree_env_sets_ocmo_and_paseo_variables(self) -> None:
        env = cli.worktree_env({"sourceWorkspace": "src", "worktreePath": "wt", "branchName": "branch"})

        self.assertEqual(env["OCMO_SOURCE_WORKSPACE"], "src")
        self.assertEqual(env["OCMO_WORKTREE_PATH"], "wt")
        self.assertEqual(env["OCMO_BRANCH_NAME"], "branch")
        self.assertEqual(env["PASEO_SOURCE_CHECKOUT_PATH"], "src")
        self.assertEqual(env["PASEO_WORKTREE_PATH"], "wt")
        self.assertEqual(env["PASEO_BRANCH_NAME"], "branch")


class CliEntrypointTests(OcmoTestCase):
    def test_main_validate_and_render(self) -> None:
        self.write_manifest()
        manifest_dir = self.root / "operation"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text(self.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            validate_code = cli.main(["operation", "validate", str(self.manifest_path)])
        with contextlib.redirect_stdout(stdout):
            validate_dir_code = cli.main(["operation", "validate", str(manifest_dir)])
        with contextlib.redirect_stdout(stdout):
            render_code = cli.main(["operation", "render", str(self.manifest_path), "--select", "1"])

        self.assertEqual(validate_code, 0)
        self.assertEqual(validate_dir_code, 0)
        self.assertEqual(render_code, 0)
        output = stdout.getvalue()
        self.assertIn("valid:", output)
        self.assertIn(str(manifest_dir / "manifest.yaml"), output)
        self.assertIn("# work unit 1 / run default", output)

    def test_main_suggests_operation_namespace_for_top_level_commands(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = cli.main(["status", "--run-id", "ocmo-test"])

        self.assertEqual(code, 2)
        self.assertIn("Try: ocmo operation status --run-id ocmo-test", stderr.getvalue())

    def test_main_validate_and_render_warn_for_shared_single_worktree_concurrency(self) -> None:
        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        manifest["queue"]["concurrency"] = 2
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            validate_code = cli.main(["operation", "validate", str(self.manifest_path)])
            render_code = cli.main(["operation", "render", str(self.manifest_path), "--select", "1"])

        self.assertEqual(validate_code, 0)
        self.assertEqual(render_code, 0)
        self.assertIn("valid:", stdout.getvalue())
        self.assertIn("# work unit 1 / run default", stdout.getvalue())
        self.assertEqual(stderr.getvalue().count("--allow-shared-worktree-concurrency"), 2)

    def test_main_render_defaults_manifest_and_accepts_directory(self) -> None:
        self.write_manifest()
        manifest_dir = self.root / "operation"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text(self.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "render", "--select", "1"])
        self.assertEqual(code, 0)
        self.assertIn("# work unit 1 / run default", stdout.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "render", str(manifest_dir), "--select", "1"])
        self.assertEqual(code, 0)
        self.assertIn("# work unit 1 / run default", stdout.getvalue())

    def test_main_render_and_run_infer_single_generated_manifest(self) -> None:
        self.write_manifest()
        generated_dir = self.root / ".ocmo" / "generated-op"
        generated_dir.mkdir(parents=True)
        generated_manifest = generated_dir / "manifest.yaml"
        generated_manifest.write_text(self.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.manifest_path.unlink()

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "render", "--select", "1"])
        self.assertEqual(code, 0)
        self.assertIn("# work unit 1 / run default", stdout.getvalue())

        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["operation", "run", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].manifest_path, generated_manifest)

    def test_main_render_compacts_many_prompts_unless_all_is_set(self) -> None:
        manifest = self.load()
        manifest["workUnits"] = [
            {"id": f"ITEM-{index}", "payload": {"name": f"Item {index}"}}
            for index in range(1, 6)
        ]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "render", str(self.manifest_path), "--select", "all"])

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("# work unit ITEM-1 / run default", output)
        self.assertIn("# work unit ITEM-2 / run default", output)
        self.assertIn("# work unit ITEM-5 / run default", output)
        self.assertIn("# ... 2 prompt(s) omitted ...", output)
        self.assertNotIn("# work unit ITEM-3 / run default", output)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "render", str(self.manifest_path), "--select", "all", "--all"])

        self.assertEqual(code, 0)
        self.assertIn("# work unit ITEM-3 / run default", stdout.getvalue())

    def test_main_run_defaults_manifest_and_accepts_directory(self) -> None:
        self.write_manifest()
        manifest_dir = self.root / "operation"
        manifest_dir.mkdir()

        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["operation", "run", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].manifest_path, self.manifest_path)

        with mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["operation", "run", str(manifest_dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].manifest_path, manifest_dir / "manifest.yaml")

        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["operation", "run", "--allow-shared-worktree-concurrency", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.args[0].allow_shared_worktree_concurrency)

        with mock.patch("ocmo.cli.Path.cwd", return_value=self.root), mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["operation", "run", "--dry-run", "--all"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.args[0].preview_all)

        self.assertEqual(cli.infer_manifest_path(self.manifest_path), self.manifest_path)
        self.assertEqual(cli.run_manifest_path(self.manifest_path), self.manifest_path)

    def test_main_returns_two_for_ocmo_errors(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = cli.main(["operation", "validate", str(self.root / "missing.yaml")])

        self.assertEqual(code, 2)
        self.assertIn("manifest not found", stderr.getvalue())

    def test_main_render_and_run_report_missing_or_ambiguous_default_manifest(self) -> None:
        empty_root = self.root / "empty"
        empty_root.mkdir()
        for command in ("run", "render"):
            stderr = io.StringIO()
            with mock.patch("ocmo.cli.Path.cwd", return_value=empty_root), contextlib.redirect_stderr(stderr):
                code = cli.main(["operation", command])
            self.assertEqual(code, 2)
            self.assertIn("no generated manifests", stderr.getvalue())

        generated_root = empty_root / ".ocmo"
        (generated_root / "one").mkdir(parents=True)
        (generated_root / "two").mkdir(parents=True)
        (generated_root / "one" / "manifest.yaml").write_text("schema: ocmo/v1\n", encoding="utf-8")
        (generated_root / "two" / "manifest.yaml").write_text("schema: ocmo/v1\n", encoding="utf-8")

        for command in ("run", "render"):
            stderr = io.StringIO()
            with mock.patch("ocmo.cli.Path.cwd", return_value=empty_root), contextlib.redirect_stderr(stderr):
                code = cli.main(["operation", command])
            self.assertEqual(code, 2)
            self.assertIn("multiple generated manifests", stderr.getvalue())

    def test_plan_dry_run_prints_prompt_without_subprocess(self) -> None:
        request = self.root / "request.txt"
        request.write_text("Rewrite the reports", encoding="utf-8")
        stdout = io.StringIO()

        with mock.patch("ocmo.cli.subprocess.run") as run, contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "plan", "--from", str(request), "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("Convert this mass-operation request", stdout.getvalue())
        self.assertIn("Rewrite the reports", stdout.getvalue())
        run.assert_not_called()

    def test_skill_path_prints_opencode_skill_destination(self) -> None:
        skills_dir = self.root / "skills-dir"
        stdout = io.StringIO()

        with mock.patch.dict("os.environ", {"OCMO_OPENCODE_SKILLS_DIR": str(skills_dir)}, clear=True), contextlib.redirect_stdout(stdout):
            code = cli.main(["skill", "path"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), str(skills_dir / "ocmo" / "SKILL.md"))

    def test_skill_install_writes_source_and_is_idempotent(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("skill text\n", encoding="utf-8")
        (source / "README.md").write_text("handbook text\n", encoding="utf-8")
        command_source = self.root / "source-commands"
        command_source.mkdir()
        (command_source / "ocmo-operation-statuses.md").write_text("command text\n", encoding="utf-8")
        skills_dir = self.root / "skills-dir"
        commands_dir = self.root / "commands-dir"
        destination = skills_dir / "ocmo" / "SKILL.md"
        handbook = skills_dir / "ocmo" / "README.md"
        command_destination = commands_dir / "ocmo-operation-statuses.md"
        stdout = io.StringIO()
        env = {"OCMO_SKILL_SOURCE": str(source), "OCMO_COMMAND_SOURCE": str(command_source), "OCMO_OPENCODE_SKILLS_DIR": str(skills_dir), "OCMO_OPENCODE_COMMANDS_DIR": str(commands_dir)}

        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(stdout):
            first_code = cli.main(["skill", "install"])
            second_code = cli.main(["skill", "install"])

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(destination.read_text(encoding="utf-8"), "skill text\n")
        self.assertEqual(handbook.read_text(encoding="utf-8"), "handbook text\n")
        self.assertEqual(command_destination.read_text(encoding="utf-8"), "command text\n")
        output = stdout.getvalue()
        self.assertIn("installed:", output)
        self.assertIn("installed command:", output)
        self.assertIn("already installed:", output)
        self.assertIn("already installed command:", output)
        self.assertIn("restart opencode", output)

    def test_skill_install_updates_different_existing_file(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("new skill\n", encoding="utf-8")
        (source / "README.md").write_text("new handbook\n", encoding="utf-8")
        skills_dir = self.root / "skills-dir"
        destination = skills_dir / "ocmo" / "SKILL.md"
        handbook = skills_dir / "ocmo" / "README.md"
        destination.parent.mkdir(parents=True)
        destination.write_text("old skill\n", encoding="utf-8")
        handbook.write_text("old handbook\n", encoding="utf-8")
        env = {"OCMO_SKILL_SOURCE": str(source), "OCMO_OPENCODE_SKILLS_DIR": str(skills_dir)}

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(stdout):
            code = cli.main(["skill", "install"])

        self.assertEqual(code, 0)
        self.assertIn("updated:", stdout.getvalue())
        self.assertEqual(destination.read_text(encoding="utf-8"), "new skill\n")
        self.assertEqual(handbook.read_text(encoding="utf-8"), "new handbook\n")

    def test_skill_install_removes_old_managed_skill(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("new skill\n", encoding="utf-8")
        skills_dir = self.root / "skills-dir"
        old_destination = skills_dir / "ocmo-plan-grill" / "SKILL.md"
        old_destination.parent.mkdir(parents=True)
        old_destination.write_text("old skill\n", encoding="utf-8")
        env = {"OCMO_SKILL_SOURCE": str(source), "OCMO_OPENCODE_SKILLS_DIR": str(skills_dir)}

        stdout = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(stdout):
            code = cli.main(["skill", "install"])

        self.assertEqual(code, 0)
        self.assertFalse(old_destination.exists())
        self.assertFalse(old_destination.parent.exists())
        self.assertIn("removed old skill:", stdout.getvalue())

    def test_skill_install_keeps_old_skill_directory_when_not_empty(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("new skill\n", encoding="utf-8")
        skills_dir = self.root / "skills-dir"
        old_destination = skills_dir / "ocmo-plan-grill" / "SKILL.md"
        old_destination.parent.mkdir(parents=True)
        old_destination.write_text("old skill\n", encoding="utf-8")
        marker = old_destination.parent / "notes.txt"
        marker.write_text("keep\n", encoding="utf-8")
        env = {"OCMO_SKILL_SOURCE": str(source), "OCMO_OPENCODE_SKILLS_DIR": str(skills_dir)}

        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["skill", "install"])

        self.assertEqual(code, 0)
        self.assertFalse(old_destination.exists())
        self.assertTrue(marker.exists())

    def test_skill_install_copies_nested_skill_files(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("skill\n", encoding="utf-8")
        nested = source / "reference" / "commands.md"
        nested.parent.mkdir()
        nested.write_text("commands\n", encoding="utf-8")
        skills_dir = self.root / "skills-dir"
        env = {"OCMO_SKILL_SOURCE": str(source), "OCMO_OPENCODE_SKILLS_DIR": str(skills_dir)}

        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["skill", "install"])

        self.assertEqual(code, 0)
        self.assertEqual((skills_dir / "ocmo" / "reference" / "commands.md").read_text(encoding="utf-8"), "commands\n")

    def test_skill_install_errors_when_source_directory_has_no_skill_file(self) -> None:
        source = self.root / "source-skill"
        source.mkdir()

        with self.assertRaisesRegex(cli.OcmoError, "bundled skill file not found"):
            cli.bundled_skill_files(source)

    def test_skill_source_errors_when_configured_path_is_missing(self) -> None:
        with mock.patch.dict("os.environ", {"OCMO_SKILL_SOURCE": str(self.root / "missing.md")}, clear=True):
            with self.assertRaisesRegex(cli.OcmoError, "configured skill source not found"):
                cli.bundled_skill_path()

    def test_skill_source_accepts_configured_skill_file(self) -> None:
        source = self.root / "source-skill" / "SKILL.md"
        source.parent.mkdir()
        source.write_text("skill\n", encoding="utf-8")

        with mock.patch.dict("os.environ", {"OCMO_SKILL_SOURCE": str(source)}, clear=True):
            self.assertEqual(cli.bundled_skill_dir(), source.parent)

    def test_skill_source_accepts_configured_file_path(self) -> None:
        source = self.root / "skill-source" / "SKILL.md"
        source.parent.mkdir()
        source.write_text("skill\n", encoding="utf-8")

        with mock.patch.dict("os.environ", {"OCMO_SKILL_SOURCE": str(source)}, clear=True):
            self.assertEqual(cli.bundled_skill_path(), source)

    def test_bundled_skill_files_collects_nested_files(self) -> None:
        source = self.root / "skill-source"
        nested = source / "reference"
        nested.mkdir(parents=True)
        (source / "SKILL.md").write_text("skill\n", encoding="utf-8")
        (source / "z-extra.md").write_text("extra\n", encoding="utf-8")
        (nested / "guide.md").write_text("guide\n", encoding="utf-8")

        files = cli.bundled_skill_files(source)

        self.assertIn(Path("SKILL.md"), files)
        self.assertIn(Path("reference") / "guide.md", files)
        self.assertIn(Path("z-extra.md"), files)

    def test_bundled_skill_files_requires_skill_md(self) -> None:
        source = self.root / "skill-source"
        source.mkdir()

        with self.assertRaisesRegex(cli.OcmoError, "SKILL.md"):
            cli.bundled_skill_files(source)

    def test_skill_source_finds_repo_skill_without_override(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cli.bundled_skill_path().name, "SKILL.md")
            self.assertEqual(cli.bundled_skill_dir().joinpath("README.md").name, "README.md")

    def test_skill_source_errors_when_repo_skill_cannot_be_found(self) -> None:
        fake_cli = self.root / "isolated" / "src" / "ocmo" / "cli.py"
        missing_resources = self.root / "missing-resources"

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("ocmo.cli.__file__", str(fake_cli)), mock.patch("ocmo.cli.resources.files", return_value=missing_resources):
            with self.assertRaisesRegex(cli.OcmoError, "bundled skill directory not found"):
                cli.bundled_skill_path()

    def test_skill_source_falls_back_to_repo_skill_directory(self) -> None:
        fake_cli = self.root / "repo" / "src" / "ocmo" / "cli.py"
        skill_dir = self.root / "repo" / "skills" / "ocmo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill\n", encoding="utf-8")
        missing_resources = self.root / "missing-resources"

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("ocmo.cli.__file__", str(fake_cli)), mock.patch("ocmo.cli.resources.files", return_value=missing_resources):
            self.assertEqual(cli.bundled_skill_dir(), skill_dir)


class EdgeCaseCoverageTests(OcmoTestCase):
    def test_load_manifest_rejects_missing_non_mapping_and_bad_schema(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "manifest not found"):
            cli.load_manifest(self.root / "missing.yaml")

        self.manifest_path.write_text("- not\n- mapping\n", encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, "YAML mapping"):
            cli.load_manifest(self.manifest_path)

        manifest = self.load()
        manifest["schema"] = "ocmo/v2"
        with self.assertRaisesRegex(cli.OcmoError, "ocmo/v1"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_required_mapping_and_string_errors(self) -> None:
        manifest = self.load()
        manifest["operation"] = []
        with self.assertRaisesRegex(cli.OcmoError, "operation must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["operation"]["id"] = ""
        with self.assertRaisesRegex(cli.OcmoError, "id must be a non-empty string"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["operation"]["kind"] = "generic"
        with self.assertRaisesRegex(cli.OcmoError, "operation.kind is no longer supported"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["runner"] = []
        with self.assertRaisesRegex(cli.OcmoError, "runner must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["runner"]["command"] = ""
        with self.assertRaisesRegex(cli.OcmoError, "command must be a non-empty string"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["runner"]["mode"] = "run"
        with self.assertRaisesRegex(cli.OcmoError, "runner.mode is no longer supported"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_invalid_timeout_concurrency_prompt_and_workUnits(self) -> None:
        manifest = self.load()
        manifest["runner"]["timeoutSeconds"] = 0
        with self.assertRaisesRegex(cli.OcmoError, "runner.timeoutSeconds"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["queue"]["concurrency"] = 0
        with self.assertRaisesRegex(cli.OcmoError, "queue.concurrency"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["prompt"]["template"] = str(self.root / "missing.md")
        with self.assertRaisesRegex(cli.OcmoError, "prompt template not found"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"] = []
        with self.assertRaisesRegex(cli.OcmoError, "workUnits must be a non-empty list"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"] = ["bad"]
        with self.assertRaisesRegex(cli.OcmoError, r"workUnits\[1\] must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        del manifest["workUnits"][0]["id"]
        with self.assertRaisesRegex(cli.OcmoError, r"workUnits\[1\].id is required"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][1]["id"] = "1"
        with self.assertRaisesRegex(cli.OcmoError, "duplicate work unit id"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_invalid_runs_shape_and_prompt(self) -> None:
        manifest = self.load()
        manifest["workUnits"][0]["runs"] = []
        with self.assertRaisesRegex(cli.OcmoError, "runs must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": []}
        with self.assertRaisesRegex(cli.OcmoError, "runs.steps must be a non-empty list"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": ["bad"]}
        with self.assertRaisesRegex(cli.OcmoError, r"runs.steps\[1\] must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": ""}]}
        with self.assertRaisesRegex(cli.OcmoError, "id is required"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "x", "prompt": []}]}
        with self.assertRaisesRegex(cli.OcmoError, "prompt must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["workUnits"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "x", "prompt": {"template": str(self.root / "missing.md")}}]}
        with self.assertRaisesRegex(cli.OcmoError, "prompt template not found"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_auto_worktree_config_validation_edges(self) -> None:
        self.assertEqual({"enabled": False}, cli.auto_worktrees_config({"queue": {"autoWorktrees": None}}))
        self.assertEqual({"enabled": True}, cli.auto_worktrees_config({"queue": {"autoWorktrees": True}}))

        with self.assertRaisesRegex(cli.OcmoError, "queue.autoWorktrees must be a mapping"):
            cli.auto_worktrees_config({"queue": {"autoWorktrees": "yes"}})
        with self.assertRaisesRegex(cli.OcmoError, "enabled must be a boolean"):
            cli.validate_auto_worktrees({"enabled": "yes"})
        with self.assertRaisesRegex(cli.OcmoError, "root must be a non-empty string"):
            cli.validate_auto_worktrees({"enabled": True, "root": ""})
        with self.assertRaisesRegex(cli.OcmoError, "cleanup must be never"):
            cli.validate_auto_worktrees({"enabled": True, "cleanup": "sometimes"})
        with self.assertRaisesRegex(cli.OcmoError, "branchPattern"):
            cli.validate_auto_worktrees({"enabled": True, "branchPattern": "{missing}"})

        self.assertEqual(["one"], cli.normalize_scripts("one", "field"))
        self.assertEqual(["one", "two"], cli.normalize_scripts(["one", "two"], "field"))
        with self.assertRaisesRegex(cli.OcmoError, "field must be"):
            cli.normalize_scripts([""], "field")

    def test_git_helpers_report_subprocess_errors(self) -> None:
        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("missing git")):
            with self.assertRaisesRegex(cli.OcmoError, "could not inspect git repository"):
                cli.ensure_git_repository(self.workspace)

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 1)):
            with self.assertRaisesRegex(cli.OcmoError, "must be inside a git repository"):
                cli.ensure_git_repository(self.workspace)

        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("missing git")):
            with self.assertRaisesRegex(cli.OcmoError, "could not detect current git branch"):
                cli.current_branch(self.workspace)

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 1, stdout="")):
            with self.assertRaisesRegex(cli.OcmoError, "baseBranch is required"):
                cli.current_branch(self.workspace)

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0, stdout="main\n")):
            self.assertEqual("main", cli.current_branch(self.workspace))

    def test_resolve_manifest_path_absolute_relative_and_quote_helpers(self) -> None:
        relative = cli.resolve_manifest_path(self.manifest_path, "prompt.md")
        self.assertEqual(relative, self.prompt.resolve())
        self.assertEqual(cli.resolve_manifest_path(self.manifest_path, str(self.prompt)), self.prompt)
        self.assertEqual(cli.resolve_worktree_root(self.workspace, str(self.root)), self.root)
        self.assertEqual(cli.quote_arg("abc_DEF-1/2:3=4"), "abc_DEF-1/2:3=4")
        self.assertEqual(cli.quote_arg('a "quote"'), '"a \\"quote\\""')
        self.assertEqual(cli.command_without_prompt([]), [])

    def test_main_run_cancel_and_unknown_command_fallback(self) -> None:
        self.write_manifest()
        stdout = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(stdout):
            code = cli.main(["operation", "run", str(self.manifest_path), "--select", "1"])
        self.assertEqual(code, 1)
        self.assertIn("Cancelled.", stdout.getvalue())

        args = mock.Mock(command="unknown")
        parser = mock.Mock()
        parser.parse_args.return_value = args
        subparsers = mock.Mock()
        parser.add_subparsers.return_value = subparsers
        subparsers.add_parser.return_value = mock.Mock()
        with mock.patch("argparse.ArgumentParser", return_value=parser):
            self.assertEqual(cli.main([]), 1)

    def test_run_manifest_concurrency_policy_errors_and_worker_exception(self) -> None:
        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, "cannot run with concurrency"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 2, None, True, False))

        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main"}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        with mock.patch("ocmo.cli.ensure_git_repository"):
            with self.assertRaisesRegex(cli.OcmoError, "autoWorktrees"):
                cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False))

        manifest = self.load()
        manifest["workUnits"] = [manifest["workUnits"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.run_item", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))
        self.assertEqual(code, 1)
        self.assertIn("unexpected worker error", stdout.getvalue())

    def test_run_item_prestart_render_oserror_and_start_oserror(self) -> None:
        manifest = self.load()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.worktree_execution", side_effect=cli.OcmoError("bad worktree")):
            code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, {"enabled": True})
        self.assertEqual(code, 1)

        self.prompt.unlink()
        code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, {"enabled": False})
        self.assertEqual(code, 1)
        self.prompt.write_text("Prompt $run_id", encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.Popen", side_effect=OSError("cannot start")):
            code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, {"enabled": False})
        self.assertEqual(code, 1)

    def test_run_item_auto_worktree_prepare_and_cleanup_failure(self) -> None:
        manifest = self.load()
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main", "cleanup": "always"}
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.prepare_worktree", return_value=9):
            code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, cli.auto_worktrees_config(manifest))
        self.assertEqual(code, 9)

        with mock.patch("ocmo.cli.prepare_worktree", return_value=0), mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "")), mock.patch("ocmo.cli.cleanup_worktree", return_value=8):
            code = cli.run_item(manifest, self.manifest_path, {"id": "1"}, state, None, cli.auto_worktrees_config(manifest))
        self.assertEqual(code, 8)

    def test_prepare_worktree_success_setup_failures_and_os_errors(self) -> None:
        manifest = self.load()
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "new-wt"), "branchName": "b", "baseBranch": "main"}
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("git missing")):
            code = cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True}, state)
        self.assertEqual(code, 1)

        execution["worktreePath"] = str(self.root / "new-wt-2")
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 5)):
            code = cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True}, state)
        self.assertEqual(code, 5)

        execution["worktreePath"] = str(self.root / "new-wt-3")
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)), mock.patch("ocmo.cli.run_scripts", return_value=6), mock.patch("ocmo.cli.cleanup_worktree", return_value=0):
            code = cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True, "setup": "setup"}, state)
        self.assertEqual(code, 6)

        execution["worktreePath"] = str(self.root / "new-wt-4")
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)), mock.patch("ocmo.cli.run_scripts", return_value=0):
            code = cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True, "setup": "setup"}, state)
        self.assertEqual(code, 0)

    def test_cleanup_worktree_skip_teardown_and_remove_failures(self) -> None:
        manifest = self.load()
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "wt"), "branchName": "b", "baseBranch": "main"}
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        self.assertEqual(cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, {}, {"cleanup": "always"}, state, True), 0)
        self.assertEqual(cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"cleanup": "never"}, state, True), 0)

        with mock.patch("ocmo.cli.run_scripts", return_value=4):
            code = cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"cleanup": "always", "teardown": "bad"}, state, True)
        self.assertEqual(code, 4)

        with mock.patch("ocmo.cli.run_scripts", return_value=0), mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("remove error")):
            code = cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"cleanup": "always"}, state, True)
        self.assertEqual(code, 1)

        with mock.patch("ocmo.cli.run_scripts", return_value=0), mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 3)):
            code = cli.cleanup_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"cleanup": "always"}, state, True)
        self.assertEqual(code, 3)

    def test_run_scripts_success_failure_and_oserror(self) -> None:
        execution = {"sourceWorkspace": "src", "worktreePath": "wt", "branchName": "branch"}
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["cmd"], 0)):
            self.assertEqual(cli.run_scripts("setup", ["ok"], self.root, execution, "1"), 0)
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["cmd"], 5)):
            self.assertEqual(cli.run_scripts("setup", ["bad"], self.root, execution, "1"), 5)
        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("missing shell")):
            self.assertEqual(cli.run_scripts("setup", ["bad"], self.root, execution, "1"), 1)

    def test_run_reporter_helpers_and_ui_selection(self) -> None:
        stdout = io.StringIO()
        reporter = cli.PlainRunReporter()

        with contextlib.redirect_stdout(stdout), reporter:
            reporter.start({}, [], 1, {"enabled": False})
            reporter.item("1", "queued")
            reporter.run("1", "default", "starting")
            reporter.worker_error("1", RuntimeError("boom"))
            reporter.subprocess_output("1", "default", subprocess.CompletedProcess(["cmd"], 0))

        output = stdout.getvalue()
        self.assertIn("[1/default] starting", output)
        self.assertIn("unexpected worker error: boom", output)
        self.assertEqual(cli.format_duration(3661), "01:01:01")
        self.assertEqual(cli.format_duration(61), "01:01")
        self.assertEqual(cli.item_runtime({"started": None}, 40), "-")
        self.assertEqual(cli.item_runtime({"started": 10, "ended": None}, 40), "00:30")
        self.assertEqual(cli.item_runtime({"started": 10, "ended": 20}, 40), "00:10")
        self.assertEqual(cli.strip_ansi("\x1b[0mred\x1b[31m"), "red")
        self.assertEqual(cli.subprocess_run_kwargs(reporter), {})

        live_like = mock.Mock(captures_subprocess_output=True)
        self.assertEqual(cli.subprocess_run_kwargs(live_like), {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"})
        with mock.patch("sys.stdout.isatty", return_value=False):
            self.assertIsInstance(cli.make_run_reporter("auto"), cli.PlainRunReporter)
        with mock.patch("sys.stdout.isatty", return_value=True), mock.patch.dict("sys.modules", {"rich": None}):
            self.assertIsInstance(cli.make_run_reporter("auto"), cli.PlainRunReporter)
        self.assertIsInstance(cli.make_run_reporter("plain"), cli.PlainRunReporter)
        self.assertIsInstance(cli.make_run_reporter("live"), cli.LiveRunReporter)

    def test_state_path_default_and_state_store_existing_file(self) -> None:
        manifest = self.load()
        del manifest["state"]
        self.assertEqual(cli.state_path(manifest, self.manifest_path), (self.root / ".ocmo" / "state" / "test-op.json").resolve())

        state_path = self.root / "state.json"
        state_path.write_text(json.dumps({"workUnits": {"1": {"old": True}}}), encoding="utf-8")
        store = cli.StateStore(state_path)
        store.mark("1", "running", {"new": True})
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(data["workUnits"]["1"]["old"])
        self.assertTrue(data["workUnits"]["1"]["new"])

    def test_plan_manifest_missing_prompt_success_and_failure(self) -> None:
        missing = self.root / "missing.txt"
        with self.assertRaisesRegex(cli.OcmoError, "prompt file not found"):
            cli.plan_manifest(mock.Mock(from_file=missing, read_files=[], out=self.root / "out.yaml", model=None, agent="plan", dry_run=True))

        prompt_for_bad_attempts = self.root / "request-bad-attempts.txt"
        prompt_for_bad_attempts.write_text("Request", encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, "max-attempts"):
            cli.plan_manifest(mock.Mock(from_file=prompt_for_bad_attempts, read_files=[], out=self.root / "out.yaml", model=None, agent="plan", dry_run=True, max_attempts=0))

        with self.assertRaisesRegex(cli.OcmoError, "YAML mapping"):
            cli.load_manifest_text("- not\n- mapping\n")

        prompt_for_missing_read = self.root / "request-missing-read.txt"
        prompt_for_missing_read.write_text("Request", encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, "read-only source file not found"):
            cli.plan_manifest(mock.Mock(from_file=prompt_for_missing_read, read_files=[self.root / "missing.md"], out=self.root / "out.yaml", model=None, agent="plan", dry_run=True))

        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        read_file = self.root / "read.md"
        read_file.write_text("Read", encoding="utf-8")
        out = self.root / "out.yaml"
        completed = subprocess.CompletedProcess(["opencode"], 0, stdout=self.planned_manifest_text(), stderr="")
        with mock.patch("ocmo.cli.subprocess.run", return_value=completed) as run:
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[read_file], out=out, model="m", agent="plan", dry_run=False))
        self.assertEqual(code, 0)
        self.assertIn("--file", run.call_args.args[0])
        self.assertIn(str(read_file), run.call_args.args[0])
        self.assertEqual(out.read_text(encoding="utf-8"), self.planned_manifest_text())

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

        failed = subprocess.CompletedProcess(["opencode"], 3, stdout="out", stderr="err")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("ocmo.cli.subprocess.run", return_value=failed), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="plan", dry_run=False))
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue(), "out")
        self.assertTrue(stderr.getvalue().endswith("err"))

    def test_plan_manifest_retries_with_validation_feedback(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned.yaml"
        calls = []

        def fake_plan_run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="apiVersion: ocmo/v1\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout=self.planned_manifest_text(), stderr="")

        stderr = io.StringIO()
        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_plan_run), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="plan", dry_run=False, max_attempts=2))

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("planner output invalid on attempt 1", stderr.getvalue())
        self.assertIn("Validation error", calls[1][-1])
        self.assertIn("manifest schema must be ocmo/v1", calls[1][-1])
        self.assertEqual(out.read_text(encoding="utf-8"), self.planned_manifest_text())

    def test_plan_manifest_uses_prompt_file_for_long_prompt(self) -> None:
        prompt = self.root / "request-long.txt"
        prompt.write_text("x" * (cli.PROMPT_ARG_MAX_CHARS + 1), encoding="utf-8")
        out = self.root / "planned.yaml"
        captured: dict[str, list[str]] = {}

        def fake_plan_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout=self.planned_manifest_text(), stderr="")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_plan_run), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="plan", dry_run=False, max_attempts=1, workspace=self.workspace, interactive=False))

        self.assertEqual(code, 0)
        command = captured["command"]
        self.assertIn("--file", command)
        prompt_path = Path(command[command.index("--file") + 1])
        self.assertEqual(command[-1], cli.PROMPT_FILE_MESSAGE)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Convert this mass-operation request", prompt_text)
        self.assertIn("x" * 100, prompt_text)

    def test_plan_manifest_uses_workspace_for_dir_and_prompt(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned.yaml"
        workspace = self.root / "target"
        workspace.mkdir()
        captured = {}

        def fake_plan_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout=self.planned_manifest_text(), stderr="")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_plan_run), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="plan", dry_run=False, max_attempts=1, workspace=workspace, interactive=False))

        self.assertEqual(code, 0)
        self.assertIn("--dir", captured["command"])
        self.assertIn(str(workspace.resolve()), captured["command"])
        self.assertIn(f"operation.workspace must be exactly: {workspace.resolve()}", captured["command"][-1])

    def test_plan_manifest_defaults_output_under_workspace_artifact_folder(self) -> None:
        prompt = self.root / "business-taxonomy-prompt.txt"
        prompt.write_text("Request", encoding="utf-8")
        expected = self.workspace / ".ocmo" / "business-taxonomy-prompt" / "manifest.yaml"
        captured = {}

        def fake_plan_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout=self.planned_manifest_text(), stderr="")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_plan_run), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["operation", "plan", "--from", str(prompt), "--workspace", str(self.workspace), "--max-attempts", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(expected.read_text(encoding="utf-8"), self.planned_manifest_text())
        self.assertIn("--agent", captured["command"])
        self.assertIn("build", captured["command"])
        self.assertIn("prompts/example.md", captured["command"][-1])
        self.assertIn("state.json", captured["command"][-1])
        self.assertIn("ocmo run --allow-shared-worktree-concurrency", captured["command"][-1])

    def test_plan_accepts_shared_single_worktree_concurrency(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Rate docs in non-overlapping folders with concurrency 3", encoding="utf-8")
        out = self.root / "planned.yaml"
        manifest_text = self.planned_manifest_text().replace("concurrency: 1", "concurrency: 3")

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0, stdout=manifest_text, stderr="")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, dry_run=False, max_attempts=1, workspace=self.workspace, interactive=False))

        self.assertEqual(code, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), manifest_text)
        cli.validate_manifest_schema(cli.load_manifest(out), out)

    def test_plan_manifest_writes_generated_prompt_template_files(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned" / "manifest.yaml"
        manifest_text = self.planned_manifest_text("prompts/generated.md")
        output = f"""{cli.MANIFEST_START}
{manifest_text}{cli.MANIFEST_END}
{cli.FILE_START} prompts/generated.md
Generated template for $work_unit_id
{cli.FILE_END}
"""

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0, stdout=output, stderr="")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="build", dry_run=False, max_attempts=1, workspace=self.workspace, interactive=False))

        self.assertEqual(code, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), manifest_text)
        self.assertEqual((out.parent / "prompts" / "generated.md").read_text(encoding="utf-8"), "Generated template for $work_unit_id\n")

    def test_plan_output_rejects_unsafe_duplicate_and_missing_generated_files(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "require OCMO_MANIFEST_START"):
            cli.parse_plan_output(f"{cli.FILE_START} prompts/a.md\nA\n{cli.FILE_END}\n", require_manifest_markers=False)

        with self.assertRaisesRegex(cli.OcmoError, "file blocks must use"):
            cli.extract_plan_files(f"{cli.FILE_START} prompts/a.md\nA\n")

        unsafe = f"{cli.MANIFEST_START}\n{self.planned_manifest_text()}{cli.MANIFEST_END}\n{cli.FILE_START} ../bad.md\nA\n{cli.FILE_END}\n"
        with self.assertRaisesRegex(cli.OcmoError, "relative and stay under"):
            cli.parse_plan_output(unsafe, require_manifest_markers=False)

        duplicate = f"{cli.FILE_START} prompts/a.md\nA\n{cli.FILE_END}\n{cli.FILE_START} prompts/a.md\nB\n{cli.FILE_END}\n"
        with self.assertRaisesRegex(cli.OcmoError, "duplicate"):
            cli.extract_plan_files(duplicate)

        manifest = cli.load_manifest_text(self.planned_manifest_text("prompts/missing.md"))
        with self.assertRaisesRegex(cli.OcmoError, "did not generate"):
            cli.validate_generated_plan_files(manifest, self.root / "planned" / "manifest.yaml", {})

        manifest = cli.load_manifest_text(self.planned_manifest_text(str(self.root / "absent.md")))
        with self.assertRaisesRegex(cli.OcmoError, "did not generate"):
            cli.validate_generated_plan_files(manifest, self.root / "planned" / "manifest.yaml", {})

        multi_run = cli.load_manifest_text(
            self.planned_manifest_text("prompts/default.md")
            + "  - id: ITEM-002\n"
            + "    payload: {}\n"
            + "    runs:\n"
            + "      mode: sequential\n"
            + "      steps:\n"
            + "        - id: review\n"
            + "          prompt:\n"
            + "            template: prompts/review.md\n"
        )
        self.assertEqual(cli.plan_template_paths(multi_run), ["prompts/default.md", "prompts/review.md"])
        multi_run["workUnits"][1]["runs"]["steps"].append({"id": "no-prompt"})
        multi_run["workUnits"].append({"id": "bad-runs", "runs": {"steps": "not-a-list"}})
        self.assertEqual(cli.plan_template_paths(multi_run), ["prompts/default.md", "prompts/review.md"])

    def test_plan_manifest_interactive_extracts_marked_yaml(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned.yaml"
        marked = f"Question before final YAML\n{cli.MANIFEST_START}\n{self.planned_manifest_text()}{cli.MANIFEST_END}\n"
        process = mock.Mock()
        process.stdout = iter(marked.splitlines(keepends=True))
        process.wait.return_value = 0

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=process) as popen, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model="m", agent="plan", dry_run=False, max_attempts=1, workspace=self.workspace, interactive=True))

        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertIn("--interactive", command)
        self.assertIn("--dir", command)
        self.assertEqual(out.read_text(encoding="utf-8"), self.planned_manifest_text())
        self.assertIn("Question before final YAML", stdout.getvalue())

    def test_plan_main_ctrl_c_returns_130(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")

        stderr = io.StringIO()
        with mock.patch("ocmo.cli.subprocess.run", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(stderr):
            code = cli.main(["operation", "plan", "--from", str(prompt), "--out", str(self.root / "out.yaml")])

        self.assertEqual(code, 130)
        self.assertIn("ocmo: interrupted", stderr.getvalue())

    def test_plan_interactive_ctrl_c_terminates_child(self) -> None:
        class InterruptingStdout:
            def __iter__(self):
                return self

            def __next__(self):
                raise KeyboardInterrupt

        process = mock.Mock()
        process.pid = 4321
        process.stdout = InterruptingStdout()

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=process), mock.patch("ocmo.cli.terminate_process_tree") as terminate:
            with self.assertRaises(KeyboardInterrupt):
                cli.run_plan_command(["opencode", "run", "prompt"], interactive=True)

        terminate.assert_called_once_with(4321, force=True)

    def test_plan_reporter_selection_and_plain_output(self) -> None:
        args = mock.Mock(agent="build", model=None)
        with mock.patch("sys.stderr.isatty", return_value=False):
            self.assertIsInstance(cli.make_plan_reporter(args, self.workspace, self.manifest_path, False), cli.PlainPlanReporter)
        with mock.patch("sys.stderr.isatty", return_value=True), mock.patch.dict("sys.modules", {"rich": None}):
            self.assertIsInstance(cli.make_plan_reporter(args, self.workspace, self.manifest_path, False), cli.PlainPlanReporter)
        self.assertIsInstance(cli.make_plan_reporter(args, self.workspace, self.manifest_path, True), cli.PlainPlanReporter)

        stdout = io.StringIO()
        stderr = io.StringIO()
        reporter = cli.PlainPlanReporter(args, self.workspace, self.manifest_path)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), reporter:
            reporter.attempt(1, 3)
            reporter.invalid(1, 3, "bad yaml")
            reporter.wrote(self.manifest_path, {Path("prompts/a.md"): "A"})
        self.assertIn("planner attempt 1/3", stderr.getvalue())
        self.assertIn("planner output invalid", stderr.getvalue())
        self.assertIn("wrote:", stdout.getvalue())

    def test_plan_manifest_interactive_rejects_missing_markers(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        process = mock.Mock()
        process.stdout = iter(["schema: ocmo/v1\n"])
        process.wait.return_value = 0

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=process), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(cli.OcmoError, "planner did not produce"):
                cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=self.root / "out.yaml", model=None, agent="plan", dry_run=False, max_attempts=1, workspace=self.workspace, interactive=True))

    def test_extract_marked_manifest_rejects_empty_manifest(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "empty manifest"):
            cli.extract_marked_manifest(f"{cli.MANIFEST_START}\n{cli.MANIFEST_END}")

    def test_plan_manifest_rejects_repeated_invalid_output_without_writing(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned.yaml"
        invalid = "schema: ocmo/v1\noperation:\n  id: op\n  workspace: .\nrunner:\n  command: opencode\nqueue:\n  concurrency: 1\nprompt:\n  template: |\n    inline text\nworkUnits:\n  - id: one\n"

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0, stdout=invalid, stderr="")):
            with self.assertRaisesRegex(cli.OcmoError, "planner did not produce"):
                cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="plan", dry_run=False, max_attempts=1))

        self.assertFalse(out.exists())

    def test_plan_manifest_rejects_invalid_max_attempts(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")

        with self.assertRaisesRegex(cli.OcmoError, "max-attempts"):
            cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=self.root / "out.yaml", model=None, agent="plan", dry_run=False, max_attempts=0))

    def test_load_manifest_text_rejects_non_mapping(self) -> None:
        with self.assertRaisesRegex(cli.OcmoError, "YAML mapping"):
            cli.load_manifest_text("- bad\n")

    def test_planning_prompt_includes_schema_constraints(self) -> None:
        prompt = cli.build_planning_prompt("Request", [], self.workspace, interactive=True)

        self.assertIn("schema: ocmo/v1", prompt)
        self.assertIn("Do not use apiVersion", prompt)
        self.assertIn("must be file paths, not inline YAML block text", prompt)
        self.assertIn("Do not include workUnits[].status", prompt)
        self.assertNotIn("status: pending", prompt)
        self.assertIn(f"operation.workspace must be exactly: {self.workspace}", prompt)
        self.assertIn(cli.MANIFEST_START, prompt)
        self.assertNotIn("kind: generic", prompt)
        self.assertNotIn("mode: run", prompt)

    def test_schema_validation_rejects_inline_template_text(self) -> None:
        manifest = self.load()
        manifest["prompt"]["template"] = "Inline\nPrompt"

        with self.assertRaisesRegex(cli.OcmoError, "inline template text"):
            cli.validate_manifest_schema(manifest, self.manifest_path)

    def test_final_coverage_branches(self) -> None:
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)):
            cli.ensure_git_repository(self.workspace)

        manifest = self.load()
        command = cli.build_command(manifest, self.manifest_path, "prompt", runner={"command": "opencode", "mode": "run"})
        self.assertNotIn("--agent", command)
        self.assertNotIn("--model", command)

        manifest["runner"].pop("timeoutSeconds")
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main"}
        manifest["policy"] = {}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.ensure_git_repository"), contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False))
        self.assertEqual(code, 0)
        self.assertIn("# worktree:", stdout.getvalue())
        self.assertIn("# branch:", stdout.getvalue())
        self.assertNotIn("# timeout:", stdout.getvalue())

        self.write_manifest()
        with mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.run_item", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, False)), 0)

        manifest = self.load()
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "branchPattern": "{missing}", "baseBranch": "main"}
        with self.assertRaisesRegex(cli.OcmoError, "invalid queue.autoWorktrees.branchPattern"):
            cli.worktree_execution(manifest, self.manifest_path, {"id": "1"})

        state = cli.StateStore(self.root / "state-mkdir.json")
        state.ensure_operation(manifest)
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "mkdir-fail" / "wt"), "branchName": "b", "baseBranch": "main"}
        with mock.patch("pathlib.Path.mkdir", autospec=True, side_effect=mkdir_fails_for(Path(execution["worktreePath"]).parent)):
            self.assertEqual(cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True}, state), 1)

        state = cli.StateStore(self.root / "state-setup-cleanup.json")
        state.ensure_operation(manifest)
        execution["worktreePath"] = str(self.root / "setup-cleanup")
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)), mock.patch("ocmo.cli.run_scripts", return_value=6), mock.patch("ocmo.cli.cleanup_worktree", return_value=7):
            self.assertEqual(cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"enabled": True, "setup": "setup"}, state), 7)

    def test_final_branch_coverage_for_dry_run_commands_and_planning(self) -> None:
        manifest = self.load()
        manifest["runner"] = {"command": "opencode"}
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "baseBranch": "main"}
        manifest["policy"] = {}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.ensure_git_repository"), contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, True, False))
        self.assertEqual(code, 0)
        self.assertIn("# worktree:", stdout.getvalue())
        self.assertIn("# branch:", stdout.getvalue())
        self.assertNotIn("# timeout:", stdout.getvalue())

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)):
            cli.ensure_git_repository(self.workspace)

        prompt = self.root / "request.txt"
        read_file = self.root / "context.md"
        prompt.write_text("Request", encoding="utf-8")
        read_file.write_text("Context", encoding="utf-8")
        out = self.root / "planned.yaml"
        captured = {}

        def fake_plan_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout=self.planned_manifest_text(), stderr="")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_plan_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[read_file], out=out, model=None, agent="plan", dry_run=False))
        self.assertEqual(code, 0)
        self.assertIn("--file", captured["command"])

        with self.assertRaisesRegex(cli.OcmoError, "read-only source file not found"):
            cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[self.root / "missing.md"], out=out, model=None, agent="plan", dry_run=False))

    def test_final_branch_coverage_for_confirmation_worktrees_and_invalid_branch(self) -> None:
        self.write_manifest()
        with mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.subprocess.Popen", side_effect=fake_popen_completed(0, "")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, False)), 0)

        manifest = self.load()
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "branchPattern": "{missing}", "baseBranch": "main"}
        with self.assertRaisesRegex(cli.OcmoError, "invalid queue.autoWorktrees.branchPattern"):
            cli.worktree_execution(manifest, self.manifest_path, {"id": "1"})

        state = cli.StateStore(self.root / "state-final.json")
        state.ensure_operation(manifest)
        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "cannot-mkdir" / "wt"), "branchName": "b", "baseBranch": "main"}
        with mock.patch("pathlib.Path.mkdir", autospec=True, side_effect=mkdir_fails_for(Path(execution["worktreePath"]).parent)):
            self.assertEqual(cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {}, state), 1)

        execution = {"sourceWorkspace": str(self.workspace), "worktreePath": str(self.root / "setup-cleanup"), "branchName": "b", "baseBranch": "main"}
        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["git"], 0)), mock.patch("ocmo.cli.run_scripts", return_value=6), mock.patch("ocmo.cli.cleanup_worktree", return_value=7):
            self.assertEqual(cli.prepare_worktree(manifest, self.manifest_path, {"id": "1"}, execution, {"setup": "setup"}, state), 7)


class WorkflowTests(OcmoTestCase):
    def write_operation_manifest(self, name: str, state_name: str | None = None) -> Path:
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        prompt = directory / "prompt.md"
        prompt.write_text("Item $work_unit_id", encoding="utf-8")
        manifest = directory / "manifest.yaml"
        manifest.write_text(
            f"""schema: ocmo/v1
operation:
  id: {name}
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
  agent: build
queue:
  concurrency: 1
policy:
  worktree: isolated
prompt:
  template: {prompt.as_posix()}
state:
  path: {(directory / (state_name or 'state.json')).as_posix()}
workUnits:
  - id: "1"
    payload: {{}}
""",
            encoding="utf-8",
        )
        return manifest

    def write_workflow(self, extra: str = "") -> tuple[Path, Path, Path]:
        first = self.write_operation_manifest("first")
        second = self.write_operation_manifest("second")
        workflow = self.root / "workflow.yaml"
        workflow.write_text(
            f"""schema: ocmo-workflow/v1
workflow:
  id: test-workflow
state:
  path: workflow-state.json
defaults:
  stopOnFailure: true
steps:
  - id: first
    manifest: {first.as_posix()}
  - id: second
    manifest: {second.as_posix()}
{extra}""",
            encoding="utf-8",
        )
        return workflow, first, second

    def test_workflow_validate_and_dry_run(self) -> None:
        workflow, _, _ = self.write_workflow()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "validate", str(workflow)]), 0)
            self.assertEqual(cli.main(["workflow", "run", str(workflow), "--dry-run"]), 0)

        output = stdout.getvalue()
        self.assertIn("valid:", output)
        self.assertIn("ocmo operation run", output)
        self.assertNotIn("--select", output)

    def test_workflow_rejects_operation_overrides(self) -> None:
        for extra in ("defaults:\n  operationSelect: uncompleted\n", "defaults:\n  concurrency: 2\n", "defaults:\n  timeoutSeconds: 30\n", "defaults:\n  allowSharedWorktreeConcurrency: true\n"):
            workflow, _, _ = self.write_workflow(extra=extra)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(["workflow", "validate", str(workflow)]), 2)

            self.assertIn("is not supported in workflows", stderr.getvalue())

    def test_workflow_rejects_step_operation_overrides(self) -> None:
        workflow, _, _ = self.write_workflow(
            extra="""  - id: third
    manifest: missing.yaml
    timeoutSeconds: 30
"""
        )
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["workflow", "validate", str(workflow)]), 2)

        self.assertIn("steps[3].timeoutSeconds is not supported in workflows", stderr.getvalue())

    def test_workflow_run_writes_step_state_and_stops_on_failure(self) -> None:
        workflow, first, second = self.write_workflow()
        calls = []

        def fake_run(options):
            calls.append(options)
            return 1 if options.manifest_path == first else 0

        with mock.patch("ocmo.cli.run_manifest", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["workflow", "run", str(workflow), "--yes", "--ui", "plain"])

        self.assertEqual(code, 1)
        self.assertEqual([call.manifest_path for call in calls], [first])
        state = json.loads((self.root / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["steps"]["first"]["status"], "failed")

    def test_workflow_rerun_delegates_operation_selection(self) -> None:
        workflow, first, _ = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps({"schema": "ocmo-workflow-state/v1", "workflowId": "test-workflow", "steps": {"first": {"status": "failed"}}}),
            encoding="utf-8",
        )
        calls = []

        with mock.patch("ocmo.cli.run_manifest", side_effect=lambda options: calls.append(options) or 0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["workflow", "rerun", str(workflow), "--yes", "--ui", "plain"]), 0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].manifest_path, first)
        self.assertIsNone(calls[0].select)
        self.assertTrue(calls[0].rerun)

    def test_workflow_step_rerun_clears_stale_terminal_fields(self) -> None:
        workflow, first, _ = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps(
                {
                    "schema": "ocmo-workflow-state/v1",
                    "workflowId": "test-workflow",
                    "steps": {"first": {"status": "paused", "pausedAt": "then", "completedAt": "old", "exitCode": 130}},
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("ocmo.cli.run_manifest", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["workflow", "rerun", str(workflow), "--select", "first", "--yes", "--ui", "plain"]), 0)

        state = json.loads((self.root / "workflow-state.json").read_text(encoding="utf-8"))
        first_step = state["steps"]["first"]
        self.assertEqual(first_step["manifestPath"], str(first.resolve()))
        self.assertEqual(first_step["status"], "completed")
        self.assertNotIn("pausedAt", first_step)
        self.assertEqual(first_step["exitCode"], 0)

    def test_workflow_finish_keeps_pending_overall_status_when_steps_remain(self) -> None:
        workflow, first, second = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps(
                {
                    "schema": "ocmo-workflow-state/v1",
                    "workflowId": "test-workflow",
                    "steps": {
                        "first": {"status": "killed", "manifestPath": str(first)},
                        "second": {"status": "pending", "manifestPath": str(second)},
                    },
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("ocmo.cli.run_manifest", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["workflow", "rerun", str(workflow), "--yes", "--ui", "plain"]), 0)

        state = json.loads((self.root / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["steps"]["first"]["status"], "completed")
        self.assertEqual(state["steps"]["second"]["status"], "pending")
        self.assertEqual(state["status"], "pending")
        self.assertNotIn("completedAt", state)

    def test_workflow_run_prepares_selected_steps_before_execution(self) -> None:
        workflow, first, second = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps(
                {
                    "schema": "ocmo-workflow-state/v1",
                    "workflowId": "test-workflow",
                    "steps": {
                        "first": {"status": "completed", "startedAt": "old", "completedAt": "old", "exitCode": 0},
                        "second": {"status": "completed", "startedAt": "old", "completedAt": "old", "exitCode": 0},
                    },
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("ocmo.cli.run_manifest", return_value=1), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["workflow", "run", str(workflow), "--select", "all", "--yes", "--ui", "plain"]), 1)

        state = json.loads((self.root / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["steps"]["first"]["status"], "failed")
        self.assertEqual(state["steps"]["second"]["status"], "pending")
        self.assertEqual(state["steps"]["second"]["manifestPath"], str(second.resolve()))
        self.assertNotIn("startedAt", state["steps"]["second"])
        self.assertNotIn("completedAt", state["steps"]["second"])

    def test_workflow_kill_marks_active_operation_killed(self) -> None:
        workflow, first, _ = self.write_workflow()
        manifest = cli.load_manifest(first)
        operation_state_path = cli.state_path(manifest, first)
        operation_state_path.write_text(
            json.dumps({"workUnits": {"1": {"status": "running", "runs": {"default": {"status": "running", "pid": 222}}}}}),
            encoding="utf-8",
        )
        (self.root / "workflow-state.json").write_text(
            json.dumps(
                {
                    "schema": "ocmo-workflow-state/v1",
                    "workflowId": "test-workflow",
                    "completedAt": "old",
                    "steps": {"first": {"status": "running", "manifestPath": str(first), "statePath": str(operation_state_path)}},
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("ocmo.cli.terminate_process_tree"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["workflow", "kill", str(workflow), "--force"]), 0)

        operation_state = json.loads(operation_state_path.read_text(encoding="utf-8"))
        workflow_state = json.loads((self.root / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertNotIn("completedAt", workflow_state)
        self.assertEqual(operation_state["workUnits"]["1"]["status"], "killed")
        self.assertEqual(operation_state["workUnits"]["1"]["runs"]["default"]["status"], "killed")

    def test_workflow_status_aggregates_operation_usage(self) -> None:
        workflow, first, _ = self.write_workflow()
        manifest = cli.load_manifest(first)
        Path(manifest["state"]["path"]).write_text(
            json.dumps(
                {
                    "schema": "ocmo-state/v1",
                    "workUnits": {
                        "1": {
                            "status": "completed",
                            "runs": {"default": {"status": "completed", "usage": {"input": 10, "output": 5, "total": 15, "steps": 1}}},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "status", str(workflow), "--once"]), 0)

        output = stdout.getvalue()
        self.assertIn("OCMO Workflow: test-workflow", output)
        self.assertIn("tokens=15", output)
        self.assertIn("first", output)

    def test_workflow_status_includes_per_step_operation_hints(self) -> None:
        workflow, first, _ = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps({"steps": {"first": {"status": "running"}}}),
            encoding="utf-8",
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "status", str(workflow), "--once"]), 0)

        output = stdout.getvalue()
        self.assertIn("details:", output)
        self.assertIn(str(first.resolve()), output)
        self.assertIn("--once    # step first", output)
        self.assertNotIn("# step second", output)

    def test_workflow_status_omits_hint_when_no_steps_running(self) -> None:
        workflow, _, _ = self.write_workflow()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "status", str(workflow), "--once"]), 0)

        self.assertNotIn("details:", stdout.getvalue())

    def test_print_detached_record_appends_operation_id_for_operation_kind(self) -> None:
        manifest_path = self.write_operation_manifest("alpha-op")
        record = {
            "schema": "ocmo-detached-run/v1",
            "kind": "operation",
            "runId": "op-run-1",
            "pid": 0,
            "startedAt": "then",
            "manifestPath": str(manifest_path),
        }
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            cli.print_detached_record(record, details=False)

        output = stdout.getvalue()
        self.assertIn("kind=operation operation=alpha-op", output)

    def test_print_detached_record_workflow_kind_has_no_operation_field(self) -> None:
        record = {
            "schema": "ocmo-detached-run/v1",
            "kind": "workflow",
            "runId": "wf-run-1",
            "pid": 0,
            "startedAt": "then",
            "workflowPath": str(self.root / "workflow.yaml"),
        }
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            cli.print_detached_record(record, details=False)

        self.assertNotIn("operation=", stdout.getvalue())

    def test_workflow_list_emits_step_to_operation_mapping(self) -> None:
        workflow, _, _ = self.write_workflow()
        record = {
            "manifestPath": None,
            "workflowPath": str(workflow),
            "workflowId": "test-workflow",
            "active": False,
            "state": {"steps": {"first": {"status": "completed"}}, "updatedAt": "then"},
            "statePath": str(self.root / "workflow-state.json"),
        }
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            cli.print_workflow_record(record)

        output = stdout.getvalue()
        self.assertIn("steps: first->first, second->second", output)

    def test_workflow_detach_writes_kind_aware_record(self) -> None:
        workflow, _, _ = self.write_workflow()
        process = mock.Mock(pid=123)
        stdout = io.StringIO()

        with mock.patch("ocmo.cli.subprocess.Popen", return_value=process), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "run", str(workflow), "--detach"]), 0)

        record_path = next((self.root / ".ocmo" / "runs").glob("ocmo-workflow-*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "workflow")
        self.assertEqual(record["command"][2:5], ["ocmo", "workflow", "run"])

    def test_workflow_list_filters_kind_and_prints_state_details(self) -> None:
        workflow, _, _ = self.write_workflow()
        state_path = self.root / "workflow-state.json"
        state_path.write_text(json.dumps({"steps": {"first": {"status": "completed"}}}), encoding="utf-8")
        runs_dir = self.root / ".ocmo" / "runs"
        runs_dir.mkdir(parents=True)
        workflow_record = {
            "schema": "ocmo-detached-run/v1",
            "kind": "workflow",
            "runId": "wf",
            "pid": 0,
            "startedAt": "then",
            "workflowPath": str(workflow),
            "statePath": str(state_path),
            "logPath": str(self.root / "wf.log"),
        }
        operation_record = {"schema": "ocmo-detached-run/v1", "kind": "operation", "runId": "op", "pid": 0, "startedAt": "then"}
        global_dir = self.root / "registry"
        global_dir.mkdir()
        (global_dir / "wf.json").write_text(json.dumps(workflow_record), encoding="utf-8")
        (global_dir / "op.json").write_text(json.dumps(operation_record), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.dict("ocmo.cli.os.environ", {"OCMO_RUN_REGISTRY": str(global_dir)}), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "list", "--all"]), 0)
            self.assertEqual(cli.main(["workflow", "list", "--run-id", "wf"]), 0)

        output = stdout.getvalue()
        self.assertIn("wf inactive kind=workflow", output)
        self.assertNotIn("op inactive", output)
        self.assertIn("workflow:", output)
        self.assertIn("steps: completed=1", output)

    def test_workflow_status_interval_must_be_positive(self) -> None:
        workflow, _, _ = self.write_workflow()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["workflow", "status", str(workflow), "--interval", "0"]), 2)

        self.assertIn("--interval must be greater than zero", stderr.getvalue())

    def test_workflow_status_watches_until_interrupted(self) -> None:
        workflow, _, _ = self.write_workflow()
        (self.root / "workflow-state.json").write_text(
            json.dumps({"updatedAt": "first", "steps": {"first": {"status": "running"}}}),
            encoding="utf-8",
        )

        slept = False

        def sleep_once(_interval: float) -> None:
            nonlocal slept
            if not slept:
                slept = True
                (self.root / "workflow-state.json").write_text(
                    json.dumps({"updatedAt": "second", "steps": {"first": {"status": "completed"}}}),
                    encoding="utf-8",
                )
                return
            raise KeyboardInterrupt

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.time.sleep", side_effect=sleep_once), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "status", str(workflow), "--interval", "0.1"]), 130)

        output = stdout.getvalue()
        self.assertIn("stateUpdated=first", output)
        self.assertIn("stateUpdated=second", output)
        self.assertIn("running=1", output)

    def test_workflow_status_active_or_latest_shows_active_else_latest(self) -> None:
        active_dir = self.root / ".ocmo" / "active-wf"
        latest_dir = self.root / ".ocmo" / "latest-wf"
        older_dir = self.root / ".ocmo" / "older-wf"
        for directory in (active_dir, older_dir, latest_dir):
            directory.mkdir(parents=True)

        def write_pair(directory: Path, workflow_id: str, updated: str, step_status: str) -> None:
            manifest = self.write_operation_manifest(directory.name + "-op", state_name="state.json")
            (directory / "workflow.yaml").write_text(
                f"""schema: ocmo-workflow/v1
workflow:
  id: {workflow_id}
state:
  path: state.json
steps:
  - id: only
    manifest: {manifest.as_posix()}
""",
                encoding="utf-8",
            )
            (directory / "state.json").write_text(
                json.dumps({"updatedAt": updated, "steps": {"only": {"status": step_status}}}),
                encoding="utf-8",
            )

        write_pair(active_dir, "active-wf", "2026-05-21T10:02:00+00:00", "running")
        write_pair(older_dir, "older-wf", "2026-05-21T10:01:00+00:00", "completed")
        write_pair(latest_dir, "latest-wf", "2026-05-21T10:03:00+00:00", "completed")

        old_cwd = Path.cwd()
        try:
            import os as test_os

            test_os.chdir(self.root)
            registry = self.root / "registry"
            registry.mkdir()
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["workflow", "status", "--active-or-latest", "--once"]), 0)
            active_output = stdout.getvalue()

            (active_dir / "state.json").write_text(
                json.dumps({"updatedAt": "2026-05-21T10:02:00+00:00", "steps": {"only": {"status": "completed"}}}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["workflow", "status", "--active-or-latest", "--once"]), 0)
            latest_output = stdout.getvalue()

            for directory in (active_dir, older_dir, latest_dir):
                (directory / "state.json").unlink()
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["workflow", "status", "--active-or-latest", "--once"]), 0)
            empty_output = stdout.getvalue()
        finally:
            test_os.chdir(old_cwd)

        self.assertIn("Active OCMO workflow statuses", active_output)
        self.assertIn("OCMO Workflow: active-wf", active_output)
        self.assertNotIn("OCMO Workflow: latest-wf", active_output)
        self.assertIn("Latest OCMO workflow status", latest_output)
        self.assertIn("OCMO Workflow: latest-wf", latest_output)
        self.assertNotIn("OCMO Workflow: older-wf", latest_output)
        self.assertIn("No ocmo workflows found.", empty_output)

    def test_workflow_list_surfaces_discovered_workflows(self) -> None:
        workflow_dir = self.root / ".ocmo" / "discovered-wf"
        workflow_dir.mkdir(parents=True)
        manifest = self.write_operation_manifest("discovered-wf-op", state_name="state.json")
        (workflow_dir / "workflow.yaml").write_text(
            f"""schema: ocmo-workflow/v1
workflow:
  id: discovered-wf
state:
  path: state.json
steps:
  - id: only
    manifest: {manifest.as_posix()}
""",
            encoding="utf-8",
        )
        (workflow_dir / "state.json").write_text(
            json.dumps({"updatedAt": "2026-05-21T10:00:00+00:00", "steps": {"only": {"status": "running"}}}),
            encoding="utf-8",
        )

        old_cwd = Path.cwd()
        try:
            import os as test_os

            test_os.chdir(self.root)
            registry = self.root / "registry"
            registry.mkdir()
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {"OCMO_RUN_REGISTRY": str(registry)}), contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["workflow", "list"]), 0)
                self.assertEqual(cli.main(["workflow", "list", "--all"]), 0)
        finally:
            test_os.chdir(old_cwd)

        output = stdout.getvalue()
        self.assertIn("discovered-wf active kind=workflow", output)
        self.assertIn("stateUpdated=2026-05-21T10:00:00+00:00", output)

    def write_generated_operation_manifest(self, name: str) -> Path:
        generated_dir = self.root / ".ocmo" / name
        generated_dir.mkdir(parents=True, exist_ok=True)
        prompt = generated_dir / "prompt.md"
        prompt.write_text("Item $work_unit_id", encoding="utf-8")
        manifest = generated_dir / "manifest.yaml"
        manifest.write_text(
            f"""schema: ocmo/v1
operation:
  id: {name}
  workspace: {self.workspace.as_posix()}
runner:
  command: opencode
queue:
  concurrency: 1
prompt:
  template: {prompt.as_posix()}
state:
  path: {(generated_dir / 'state.json').as_posix()}
workUnits:
  - id: "1"
""",
            encoding="utf-8",
        )
        return manifest

    def write_generated_workflow(self) -> tuple[Path, Path, Path]:
        first = self.write_generated_operation_manifest("first")
        second = self.write_generated_operation_manifest("second")
        workflow = self.root / "workflow.yaml"
        workflow.write_text(
            f"""schema: ocmo-workflow/v1
workflow:
  id: erase-wf
state:
  path: workflow-state.json
steps:
  - id: first
    manifest: {first.as_posix()}
  - id: second
    manifest: {second.as_posix()}
""",
            encoding="utf-8",
        )
        return workflow, first, second

    def seed_operation_runtime(self, manifest_path: Path) -> dict[str, Path]:
        directory = manifest_path.parent
        state_file = directory / "state.json"
        outputs = directory / "outputs"
        artifacts = directory / "artifacts"
        prompt_inputs = directory / "prompt-inputs"
        runs_dir = directory / ".ocmo" / "runs"
        state_file.write_text(json.dumps({"workUnits": {}}), encoding="utf-8")
        outputs.mkdir(exist_ok=True)
        (outputs / "1.txt").write_text("out", encoding="utf-8")
        artifacts.mkdir(exist_ok=True)
        (artifacts / "handoff.md").write_text("artifact", encoding="utf-8")
        prompt_inputs.mkdir(exist_ok=True)
        (prompt_inputs / "1.md").write_text("prompt input", encoding="utf-8")
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run.log").write_text("log", encoding="utf-8")
        return {"state": state_file, "outputs": outputs, "artifacts": artifacts, "prompt_inputs": prompt_inputs, "runs": runs_dir}

    def test_workflow_erase_wipes_all_operation_runtime(self) -> None:
        workflow, first, second = self.write_generated_workflow()
        first_paths = self.seed_operation_runtime(first)
        second_paths = self.seed_operation_runtime(second)
        workflow_state = self.root / "workflow-state.json"
        workflow_state.write_text(json.dumps({"steps": {"first": {"status": "completed"}}}), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), mock.patch("ocmo.cli.stop_workflow_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "erase", str(workflow), "--force"]), 0)

        for paths in (first_paths, second_paths):
            self.assertFalse(paths["state"].exists())
            self.assertFalse(paths["outputs"].exists())
            self.assertFalse(paths["artifacts"].exists())
            self.assertFalse(paths["prompt_inputs"].exists())
            self.assertFalse(paths["runs"].exists())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue((first.parent / "prompt.md").exists())
        self.assertTrue((second.parent / "prompt.md").exists())
        self.assertFalse(workflow_state.exists())
        output = stdout.getvalue()
        self.assertIn("erased 2 operation(s)", output)
        self.assertIn("cleared workflow state", output)

    def test_workflow_erase_requires_force_when_non_interactive(self) -> None:
        workflow, _, _ = self.write_generated_workflow()
        stderr = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["workflow", "erase", str(workflow)]), 2)
        self.assertIn("erase requires --force", stderr.getvalue())

    def test_workflow_erase_skips_non_generated_manifest(self) -> None:
        first = self.write_generated_operation_manifest("first")
        second = self.write_operation_manifest("hand-authored")
        workflow = self.root / "workflow.yaml"
        workflow.write_text(
            f"""schema: ocmo-workflow/v1
workflow:
  id: mixed-wf
state:
  path: workflow-state.json
steps:
  - id: first
    manifest: {first.as_posix()}
  - id: second
    manifest: {second.as_posix()}
""",
            encoding="utf-8",
        )
        first_paths = self.seed_operation_runtime(first)
        hand_state = second.parent / "state.json"
        hand_state.write_text(json.dumps({"workUnits": {}}), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), mock.patch("ocmo.cli.stop_workflow_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "erase", str(workflow), "--force"]), 0)

        self.assertFalse(first_paths["state"].exists())
        self.assertTrue(hand_state.exists())
        output = stdout.getvalue()
        self.assertIn("erased 1 operation(s)", output)
        self.assertIn("skipped 1 non-generated", output)

    def test_workflow_erase_keep_workflow_state_flag(self) -> None:
        workflow, first, _ = self.write_generated_workflow()
        self.seed_operation_runtime(first)
        workflow_state = self.root / "workflow-state.json"
        workflow_state.write_text(json.dumps({"steps": {"first": {"status": "completed"}}}), encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), mock.patch("ocmo.cli.stop_workflow_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "erase", str(workflow), "--force", "--keep-workflow-state"]), 0)

        self.assertTrue(workflow_state.exists())
        self.assertNotIn("cleared workflow state", stdout.getvalue())

    def test_workflow_erase_missing_manifest_is_reported(self) -> None:
        workflow = self.root / "workflow.yaml"
        first = self.write_generated_operation_manifest("first")
        missing = self.root / "missing.yaml"
        workflow.write_text(
            f"""schema: ocmo-workflow/v1
workflow:
  id: missing-wf
state:
  path: workflow-state.json
steps:
  - id: first
    manifest: {first.as_posix()}
  - id: gone
    manifest: {missing.as_posix()}
""",
            encoding="utf-8",
        )
        self.seed_operation_runtime(first)
        stdout = io.StringIO()
        with mock.patch("ocmo.cli.stop_operation_processes"), mock.patch("ocmo.cli.stop_workflow_processes"), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["workflow", "erase", str(workflow), "--force"]), 0)
        output = stdout.getvalue()
        self.assertIn("skipped 1 missing", output)
        self.assertIn("erased 1 operation(s)", output)


def yaml_dump(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False)


def merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = value


def mkdir_fails_for(blocked_path: Path):
    original_mkdir = Path.mkdir

    def fake_mkdir(path: Path, *args, **kwargs):
        if path == blocked_path:
            raise OSError("mkdir failed")
        return original_mkdir(path, *args, **kwargs)

    return fake_mkdir


if __name__ == "__main__":
    unittest.main()
