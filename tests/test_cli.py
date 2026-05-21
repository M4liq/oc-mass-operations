from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
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
            "Item $item_id $item_title run $run_id/$run_count agent $run_agent model $run_model in $worktree_path payload $payload_json",
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
items:
  - id: "1"
    title: First
    status: pending
    file: docs/a.md
    payload:
      name: Alpha
  - id: "2"
    title: Second
    status: completed
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
items:
  - id: ITEM-001
    status: pending
    payload: {{}}
"""


class ValidationTests(OcmoTestCase):
    def test_valid_manifest_passes(self) -> None:
        manifest = self.load()

        cli.validate_manifest(manifest, self.manifest_path)

    def test_missing_workspace_fails_validation(self) -> None:
        manifest = self.load()
        manifest["operation"]["workspace"] = str(self.root / "missing")

        with self.assertRaisesRegex(cli.OcmoError, "operation.workspace does not exist"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_single_worktree_rejects_concurrency_above_one(self) -> None:
        manifest = self.load()
        manifest["policy"]["worktree"] = "single"
        manifest["queue"]["concurrency"] = 2

        with self.assertRaisesRegex(cli.OcmoError, "policy.worktree=single requires queue.concurrency=1"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validates_multi_run_steps_and_per_run_templates(self) -> None:
        review_prompt = self.root / "review.md"
        review_prompt.write_text("Review $run_id", encoding="utf-8")
        manifest = self.load()
        manifest["prompt"]["skills"] = ["code-review"]
        manifest["items"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "implement", "agent": "build", "prompt": {"template": str(self.prompt)}},
                {"id": "review", "agent": "build", "timeoutSeconds": 5, "prompt": {"template": str(review_prompt), "skills": ["/review-skill"]}},
            ],
        }

        cli.validate_manifest(manifest, self.manifest_path)

    def test_rejects_unsupported_run_mode(self) -> None:
        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "parallel", "steps": [{"id": "one"}]}

        with self.assertRaisesRegex(cli.OcmoError, "runs.mode must be sequential"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_rejects_duplicate_run_ids(self) -> None:
        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "one"}]}

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
            ({"items": []}, "items must be a non-empty list"),
            ({"items": ["bad"]}, r"items\[1\] must be a mapping"),
            ({"items": [{}]}, r"items\[1\].id is required"),
            ({"items": [{"id": "1"}, {"id": "1"}]}, "duplicate item id"),
        ]
        for patch, message in cases:
            manifest = self.load()
            merge(manifest, patch)
            with self.subTest(message=message):
                with self.assertRaisesRegex(cli.OcmoError, message):
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
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "consumes": "plan.plan"}]}}, r"consumes must be a list"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "x", "consumes": ["plan.plan"]}]}}, r"earlier step"),
            ({"runs": {"mode": "sequential", "steps": [{"id": "plan", "produces": {"notes": {}}}, {"id": "build", "consumes": ["plan.plan"]}]}}, r"unknown artifact"),
        ]
        for patch, message in cases:
            manifest = self.load()
            manifest["items"] = [{"id": "1", **patch}]
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
    def test_select_items_supports_pending_uncompleted_all_ids_and_ranges(self) -> None:
        manifest = self.load()
        manifest["items"].append({"id": "3", "status": "skipped"})

        self.assertEqual([item["id"] for item in cli.select_items(manifest, "pending")], ["1"])
        self.assertEqual([item["id"] for item in cli.select_items(manifest, "uncompleted")], ["1"])
        self.assertEqual([item["id"] for item in cli.select_items(manifest, "all")], ["1", "2", "3"])
        self.assertEqual([item["id"] for item in cli.select_items(manifest, "1,3")], ["1", "3"])

        numeric = {"items": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        self.assertEqual([item["id"] for item in cli.select_items(numeric, "1-2")], ["1", "2"])

    def test_select_items_rejects_missing_and_descending_ranges(self) -> None:
        manifest = self.load()

        with self.assertRaisesRegex(cli.OcmoError, "selection did not match"):
            cli.select_items(manifest, "missing")
        with self.assertRaisesRegex(cli.OcmoError, "invalid descending range"):
            cli.select_items(manifest, "3-1")

    def test_implicit_default_run_uses_top_level_runner(self) -> None:
        manifest = self.load()

        runs = cli.item_runs(manifest, manifest["items"][0])

        self.assertEqual(runs[0]["id"], "default")
        self.assertEqual(runs[0]["agent"], "build")

    def test_render_prompt_includes_run_and_execution_context(self) -> None:
        manifest = self.load()
        item = manifest["items"][0]
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
            "Range {{payload.rangeStart}}-{{ payload.rangeEnd }} for {{item.id}}/{{item_id}} in {{operation.id}} run {{run.id}} at {{execution.worktreePath}} payload {{payload}}",
            encoding="utf-8",
        )
        manifest = self.load()
        item = manifest["items"][0]
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
            cli.render_prompt(manifest, manifest["items"][0], self.manifest_path)

        self.prompt.write_text("Range {{missing.value}}", encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, r"unresolved prompt placeholder: \{\{missing\.value\}\}"):
            cli.render_prompt(manifest, manifest["items"][0], self.manifest_path)

    def test_format_placeholder_value_handles_none(self) -> None:
        self.assertEqual(cli.format_placeholder_value(None), "")

    def test_compact_prompt_previews_keeps_first_two_and_last(self) -> None:
        previews = [cli.PromptPreview(str(index), "default", f"prompt {index}") for index in range(1, 6)]

        compact = cli.compact_prompt_previews(previews, False)

        self.assertEqual(compact, [previews[0], previews[1], 2, previews[-1]])
        self.assertEqual(cli.compact_prompt_previews(previews[:3], False), previews[:3])
        self.assertEqual(cli.compact_prompt_previews(previews, True), previews)

    def test_render_prompt_prepends_deterministic_skill_instructions(self) -> None:
        self.prompt.write_text("Skills: $skill_names\nCommands:\n$skill_commands\n$item_id", encoding="utf-8")
        manifest = self.load()
        manifest["prompt"]["skills"] = ["analysis", "/code-review"]

        rendered = cli.render_prompt(manifest, manifest["items"][0], self.manifest_path)

        self.assertTrue(rendered.startswith("You must use the following opencode skills before doing this task, in order:\n- /analysis\n- /code-review"))
        self.assertIn("Skills: analysis, code-review", rendered)
        self.assertIn("/analysis\n/code-review", rendered)

    def test_run_prompt_skills_override_top_level_skills_without_template_override(self) -> None:
        manifest = self.load()
        manifest["prompt"]["skills"] = ["implement"]
        run = {"id": "review", "index": 1, "mode": "sequential", "prompt": {"skills": ["review"]}}

        rendered = cli.render_prompt(manifest, manifest["items"][0], self.manifest_path, run=run)

        self.assertIn("- /review", rendered)
        self.assertNotIn("- /implement", rendered)

    def test_render_prompt_injects_chained_artifacts(self) -> None:
        manifest = self.load()
        item = manifest["items"][0]
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
        rendered = cli.consumed_artifacts(
            self.manifest_path,
            {"id": "1"},
            {"id": "implement", "consumes": ["plan.plan"]},
            [{"id": "plan", "produces": {"plan": {}}}, {"id": "implement"}],
        )
        self.assertIn("will be generated", rendered)

    def test_artifact_helpers_cover_defaults_and_errors(self) -> None:
        manifest = self.load()
        item = manifest["items"][0]
        self.assertEqual(cli.produced_artifacts({}), {})
        self.assertEqual(cli.validate_produces(None, "field"), set())
        self.assertEqual(cli.validate_produces({"plan": None}, "field"), {"plan"})
        self.assertEqual(cli.artifact_relative_path(self.manifest_path, item, "run", "note", "artifacts/$item_id/$run_id/$artifact_id.md"), "artifacts/1/run/note.md")
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


class RunManifestTests(OcmoTestCase):
    def test_dry_run_prints_each_sequential_run_without_state_or_subprocess(self) -> None:
        impl_prompt = self.root / "impl.md"
        review_prompt = self.root / "review.md"
        impl_prompt.write_text("Implement $run_id $run_index", encoding="utf-8")
        review_prompt.write_text("Review $run_id $run_index", encoding="utf-8")
        manifest = self.load()
        manifest["items"][0]["runs"] = {
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
        self.assertIn("# item 1 / run implement", output)
        self.assertIn("# item 1 / run review", output)
        self.assertIn("# produces: plan -> artifacts/1/implement/plan.md", output)
        self.assertIn("# consumes: implement.plan", output)
        self.assertIn("Implement implement 1", output)
        self.assertIn("Review review 2", output)
        run.assert_not_called()
        self.assertFalse((self.root / "state.json").exists())

    def test_run_manifest_executes_runs_in_order_and_writes_nested_state(self) -> None:
        manifest = self.load()
        manifest["items"] = [manifest["items"][0]]
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "two", "agent": "build"}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=f"agent output for {command[-1]}\n")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("one", calls[0][-1])
        self.assertIn("two", calls[1][-1])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["1"]["status"], "completed")
        self.assertEqual(state["items"]["1"]["runs"]["one"]["status"], "completed")
        self.assertEqual(state["items"]["1"]["runs"]["two"]["status"], "completed")
        self.assertEqual(state["items"]["1"]["runs"]["one"]["outputPath"], "outputs/1__one.txt")
        self.assertEqual(state["items"]["1"]["runs"]["two"]["outputPath"], "outputs/1__two.txt")
        self.assertIn("agent output for", (self.root / "outputs" / "1__one.txt").read_text(encoding="utf-8"))
        self.assertIn("[ocmo] exit code: 0", (self.root / "outputs" / "1__two.txt").read_text(encoding="utf-8"))

    def test_run_manifest_chains_required_artifacts(self) -> None:
        manifest = self.load()
        manifest["items"] = [manifest["items"][0]]
        manifest["items"][0]["runs"] = {
            "mode": "sequential",
            "steps": [
                {"id": "plan", "agent": "build", "produces": {"plan": {}}},
                {"id": "implement", "agent": "build", "consumes": ["plan.plan"]},
            ],
        }
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        plan_artifact = self.root / "artifacts" / "1" / "plan" / "plan.md"
        prompts: list[str] = []

        def fake_run(command: list[str], **kwargs):
            prompts.append(command[-1])
            if "Required Artifacts" in command[-1]:
                plan_artifact.parent.mkdir(parents=True)
                plan_artifact.write_text("artifact plan", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="ok")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        self.assertIn("artifact plan", prompts[1])
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["1"]["runs"]["plan"]["artifacts"], {"plan": "artifacts/1/plan/plan.md"})

    def test_run_manifest_fails_when_required_artifact_missing(self) -> None:
        manifest = self.load()
        manifest["items"] = [manifest["items"][0]]
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "plan", "agent": "build", "produces": {"plan": {}}}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0, stdout="ok")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["1"]["runs"]["plan"]["status"], "failed")
        self.assertIn("required artifact", state["items"]["1"]["runs"]["plan"]["error"])

    def test_run_manifest_writes_clean_utf8_output_files(self) -> None:
        manifest = self.load()
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        def fake_run(command: list[str], **kwargs):
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["errors"], "replace")
            self.assertEqual(kwargs["env"]["NO_COLOR"], "1")
            self.assertEqual(kwargs["env"]["FORCE_COLOR"], "0")
            self.assertEqual(kwargs["env"]["TERM"], "dumb")
            return subprocess.CompletedProcess(command, 0, stdout="\x1b[0mhello\x1b[31m red\x1b[0m\n")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 0)
        output = (self.root / "outputs" / "1__default.txt").read_text(encoding="utf-8")
        self.assertIn("hello red", output)
        self.assertNotIn("\x1b", output)

    def test_run_manifest_stops_later_runs_after_failure(self) -> None:
        manifest = self.load()
        manifest["items"] = [manifest["items"][0]]
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "one"}, {"id": "two"}]}
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 7)), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["1"]["status"], "failed")
        self.assertEqual(state["items"]["1"]["runs"]["one"]["status"], "failed")
        self.assertNotIn("two", state["items"]["1"].get("runs", {}))

    def test_timeout_marks_item_and_run_timed_out(self) -> None:
        manifest = self.load()
        manifest["items"] = [manifest["items"][0]]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        with mock.patch("ocmo.cli.subprocess.run", side_effect=subprocess.TimeoutExpired(["opencode"], 1)), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, 1, False, True))

        self.assertEqual(code, 1)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["1"]["status"], "timed_out")
        self.assertEqual(state["items"]["1"]["runs"]["default"]["status"], "timed_out")
        self.assertEqual(state["items"]["1"]["runs"]["default"]["outputPath"], "outputs/1__default.txt")
        self.assertIn("[ocmo] timed out after 1 seconds", (self.root / "outputs" / "1__default.txt").read_text(encoding="utf-8"))

    def test_no_selected_items_returns_zero(self) -> None:
        manifest = self.load()
        manifest["items"][0]["status"] = "completed"
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, None, None, None, False, True))

        self.assertEqual(code, 0)
        self.assertIn("No items selected.", stdout.getvalue())

    def test_run_manifest_rejects_invalid_cli_overrides(self) -> None:
        self.write_manifest()

        with self.assertRaisesRegex(cli.OcmoError, "concurrency must be a positive integer"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", 0, None, True, False))
        with self.assertRaisesRegex(cli.OcmoError, "timeout must be a positive integer"):
            cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, 0, True, False))

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
        self.assertIn("# item 1 / run default", stdout.getvalue())

        manifest["queue"]["concurrency"] = 2
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")
        with self.assertRaisesRegex(cli.OcmoError, "policy.worktree=single requires queue.concurrency=1"):
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
        self.assertEqual(state["items"]["1"]["status"], "failed")
        self.assertIn("unexpected worker error", state["items"]["1"]["error"])

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
        self.assertEqual(data["items"]["1"]["status"], "failed")
        self.assertEqual(data["items"]["1"]["runs"]["default"]["status"], "failed")

    def test_run_item_oserror_from_subprocess_records_failed_run(self) -> None:
        manifest = self.load()
        state = cli.StateStore(self.root / "state.json")
        state.ensure_operation(manifest)

        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("cannot start")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_item(manifest, self.manifest_path, manifest["items"][0], state, None, {"enabled": False})

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(data["items"]["1"]["status"], "failed")
        self.assertEqual(data["items"]["1"]["runs"]["default"]["status"], "failed")

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

        with mock.patch("ocmo.cli.ensure_git_repository"), mock.patch("ocmo.cli.subprocess.run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.run_manifest(cli.RunOptions(self.manifest_path, "1", None, None, False, True))

        data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(data["items"]["1"]["status"], "cleanup_failed")


class WorktreeTests(OcmoTestCase):
    def test_worktree_execution_slugifies_paths_and_branch(self) -> None:
        manifest = self.load()
        manifest["operation"]["id"] = "My Operation"
        manifest["queue"]["autoWorktrees"] = {"enabled": True, "root": "worktrees", "baseBranch": "main", "branchPattern": "ocmo/{operation_id}/{item_slug}"}
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
        self.assertEqual(data["items"]["1"]["status"], "worktree_failed")

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
        self.assertEqual(data["items"]["1"]["worktreeStatus"], "removed")

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
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            validate_code = cli.main(["validate", str(self.manifest_path)])
        with contextlib.redirect_stdout(stdout):
            render_code = cli.main(["render", str(self.manifest_path), "--select", "1"])

        self.assertEqual(validate_code, 0)
        self.assertEqual(render_code, 0)
        output = stdout.getvalue()
        self.assertIn("valid:", output)
        self.assertIn("# item 1 / run default", output)

    def test_main_render_compacts_many_prompts_unless_all_is_set(self) -> None:
        manifest = self.load()
        manifest["items"] = [
            {"id": f"ITEM-{index}", "status": "pending", "payload": {"name": f"Item {index}"}}
            for index in range(1, 6)
        ]
        self.manifest_path.write_text(yaml_dump(manifest), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["render", str(self.manifest_path), "--select", "all"])

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("# item ITEM-1 / run default", output)
        self.assertIn("# item ITEM-2 / run default", output)
        self.assertIn("# item ITEM-5 / run default", output)
        self.assertIn("# ... 2 prompt(s) omitted ...", output)
        self.assertNotIn("# item ITEM-3 / run default", output)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["render", str(self.manifest_path), "--select", "all", "--all"])

        self.assertEqual(code, 0)
        self.assertIn("# item ITEM-3 / run default", stdout.getvalue())

    def test_main_run_defaults_manifest_and_accepts_directory(self) -> None:
        manifest_dir = self.root / "operation"
        manifest_dir.mkdir()

        with mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["run", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].manifest_path, Path("manifest.yaml"))

        with mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["run", str(manifest_dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].manifest_path, manifest_dir / "manifest.yaml")

        with mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["run", "--allow-shared-worktree-concurrency", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.args[0].allow_shared_worktree_concurrency)

        with mock.patch("ocmo.cli.run_manifest", return_value=0) as run:
            code = cli.main(["run", "--dry-run", "--all"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.args[0].preview_all)

        self.assertEqual(cli.run_manifest_path(self.manifest_path), self.manifest_path)

    def test_main_returns_two_for_ocmo_errors(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = cli.main(["validate", str(self.root / "missing.yaml")])

        self.assertEqual(code, 2)
        self.assertIn("manifest not found", stderr.getvalue())

    def test_plan_dry_run_prints_prompt_without_subprocess(self) -> None:
        request = self.root / "request.txt"
        request.write_text("Rewrite the reports", encoding="utf-8")
        stdout = io.StringIO()

        with mock.patch("ocmo.cli.subprocess.run") as run, contextlib.redirect_stdout(stdout):
            code = cli.main(["plan", "--from", str(request), "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("Convert this mass-operation request", stdout.getvalue())
        self.assertIn("Rewrite the reports", stdout.getvalue())
        run.assert_not_called()


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

    def test_validation_rejects_invalid_timeout_concurrency_prompt_and_items(self) -> None:
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
        manifest["items"] = []
        with self.assertRaisesRegex(cli.OcmoError, "items must be a non-empty list"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"] = ["bad"]
        with self.assertRaisesRegex(cli.OcmoError, r"items\[1\] must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        del manifest["items"][0]["id"]
        with self.assertRaisesRegex(cli.OcmoError, r"items\[1\].id is required"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][1]["id"] = "1"
        with self.assertRaisesRegex(cli.OcmoError, "duplicate item id"):
            cli.validate_manifest(manifest, self.manifest_path)

    def test_validation_rejects_invalid_runs_shape_and_prompt(self) -> None:
        manifest = self.load()
        manifest["items"][0]["runs"] = []
        with self.assertRaisesRegex(cli.OcmoError, "runs must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": []}
        with self.assertRaisesRegex(cli.OcmoError, "runs.steps must be a non-empty list"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": ["bad"]}
        with self.assertRaisesRegex(cli.OcmoError, r"runs.steps\[1\] must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": ""}]}
        with self.assertRaisesRegex(cli.OcmoError, "id is required"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "x", "prompt": []}]}
        with self.assertRaisesRegex(cli.OcmoError, "prompt must be a mapping"):
            cli.validate_manifest(manifest, self.manifest_path)

        manifest = self.load()
        manifest["items"][0]["runs"] = {"mode": "sequential", "steps": [{"id": "x", "prompt": {"template": str(self.root / "missing.md")}}]}
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
            code = cli.main(["run", str(self.manifest_path), "--select", "1"])
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
        manifest["items"] = [manifest["items"][0]]
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

        with mock.patch("ocmo.cli.subprocess.run", side_effect=OSError("cannot start")):
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

        with mock.patch("ocmo.cli.prepare_worktree", return_value=0), mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0)), mock.patch("ocmo.cli.cleanup_worktree", return_value=8):
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
        state_path.write_text(json.dumps({"items": {"1": {"old": True}}}), encoding="utf-8")
        store = cli.StateStore(state_path)
        store.mark("1", "running", {"new": True})
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(data["items"]["1"]["old"])
        self.assertTrue(data["items"]["1"]["new"])

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
            code = cli.main(["plan", "--from", str(prompt), "--workspace", str(self.workspace), "--max-attempts", "1"])

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
        with self.assertRaisesRegex(cli.OcmoError, "policy.worktree=single requires queue.concurrency=1"):
            cli.validate_manifest_schema(cli.load_manifest(out), out)

    def test_plan_manifest_writes_generated_prompt_template_files(self) -> None:
        prompt = self.root / "request.txt"
        prompt.write_text("Request", encoding="utf-8")
        out = self.root / "planned" / "manifest.yaml"
        manifest_text = self.planned_manifest_text("prompts/generated.md")
        output = f"""{cli.MANIFEST_START}
{manifest_text}{cli.MANIFEST_END}
{cli.FILE_START} prompts/generated.md
Generated template for $item_id
{cli.FILE_END}
"""

        with mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0, stdout=output, stderr="")), contextlib.redirect_stdout(io.StringIO()):
            code = cli.plan_manifest(mock.Mock(from_file=prompt, read_files=[], out=out, model=None, agent="build", dry_run=False, max_attempts=1, workspace=self.workspace, interactive=False))

        self.assertEqual(code, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), manifest_text)
        self.assertEqual((out.parent / "prompts" / "generated.md").read_text(encoding="utf-8"), "Generated template for $item_id\n")

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
            + "    status: pending\n"
            + "    payload: {}\n"
            + "    runs:\n"
            + "      mode: sequential\n"
            + "      steps:\n"
            + "        - id: review\n"
            + "          prompt:\n"
            + "            template: prompts/review.md\n"
        )
        self.assertEqual(cli.plan_template_paths(multi_run), ["prompts/default.md", "prompts/review.md"])
        multi_run["items"][1]["runs"]["steps"].append({"id": "no-prompt"})
        multi_run["items"].append({"id": "bad-runs", "runs": {"steps": "not-a-list"}})
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
        invalid = "schema: ocmo/v1\noperation:\n  id: op\n  workspace: .\nrunner:\n  command: opencode\nqueue:\n  concurrency: 1\nprompt:\n  template: |\n    inline text\nitems:\n  - id: one\n"

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
        with mock.patch("builtins.input", return_value="yes"), mock.patch("ocmo.cli.subprocess.run", return_value=subprocess.CompletedProcess(["opencode"], 0)), contextlib.redirect_stdout(io.StringIO()):
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
