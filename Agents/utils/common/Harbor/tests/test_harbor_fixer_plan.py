#!/usr/bin/env python3
"""Tests for Harbor Fixer plan."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerPlanTest(unittest.TestCase):
    def test_plan_generation_builds_inputs_and_retries_task_agent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            analyzer_dir = write_analyzer_fixture(Path(root), count=1)
            task_input = build_task_inputs(analyzer_dir)[0][0]
            self.assertEqual(task_input["task"]["task_index"], "1")
            self.assertEqual(task_input["evidence"][0]["analysis_report_pointer"], "/tasks/0")

            good = json.dumps(task_summary_for(task_input))
            invoker = SequenceInvoker(["not-json", good])
            summaries, errors = collect_task_summaries([task_input], invoker, Path(root) / "out")
            self.assertEqual(errors, [])
            self.assertEqual(len(summaries), 1)
            retry_prompt = invoker.records[1][0]
            self.assertIn("Validation retry:", retry_prompt)
            self.assertIn("invalid JSON:", retry_prompt)
            self.assertIn("<previous-output>\nnot-json\n</previous-output>", retry_prompt)

            bad_summary = json.loads(json.dumps(task_summary_for(task_input)))
            bad_summary["task"]["task_index"] = "other"
            with self.assertRaises(ValidationError):
                validate_task_summary(bad_summary, expected_task=task_input["task"])

    def test_plan_generation_and_cli_smoke_write_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=2)
            output_dir = root_path / "fixer"

            plan = run_plan_generation(
                analyzer_dir,
                output_dir,
                SummaryInvoker(),
                MainInvoker(),
                max_concurrency=2,
            )
            validate_fix_plan_set(plan)
            self.assertTrue((output_dir / "fix-plan-latest.json").exists())
            environment = json.loads(
                (output_dir / "target-environment.json").read_text(encoding="utf-8")
            )
            main_input = json.loads(
                (output_dir / "main-agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(environment["kind"], "harbor_fixer_target_environment")
            self.assertEqual(main_input["target_environment"], environment)
            self.assertEqual(len(main_input["target_environment_artifact"]["sha256"]), 64)
            self.assertEqual(
                main_input["target_context"]["kind"],
                "harbor_fixer_target_context",
            )
            self.assertEqual(len(main_input["target_context_artifact"]["sha256"]), 64)
            self.assertTrue((output_dir / "target-context.json").exists())
            self.assertFalse((output_dir / "inspection-agent-input.json").exists())

            cli_out = root_path / "cli-fixer"
            agent_script = write_fixture_pi(root_path / "fixture_pi.py")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "fixer.py"),
                    "--analyzer-output",
                    str(analyzer_dir),
                    "--output-dir",
                    str(cli_out),
                    "--pi-bin",
                    str(agent_script),
                    "--pi-base-url",
                    "https://example.test/v1",
                    "--pi-model",
                    "fixture-model",
                    "--pi-api-key-env",
                    "FIXTURE_PI_API_KEY",
                    "--max-concurrency",
                    "2",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={"PATH": "/usr/bin:/bin", "FIXTURE_PI_API_KEY": "fixture"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cli_out / "fix-plan-latest.json").exists())
            task_provenance_paths = sorted(
                (cli_out / "pi-agent-provenance").glob("task-*/attempt-1.json")
            )
            self.assertEqual(len(task_provenance_paths), 2)
            for provenance_path in task_provenance_paths:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                self.assertEqual(provenance["thinking_level"], "off")
                self.assertEqual(
                    provenance["discarded_event_counts"]["message_update"],
                    1,
                )
            provenance = json.loads(
                (
                    cli_out
                    / "pi-agent-provenance"
                    / "main-agent"
                    / "attempt-1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["thinking_level"], "default")
            self.assertEqual(provenance["discarded_event_counts"]["message_update"], 1)
            main_events = (
                cli_out
                / "pi-agent-events"
                / "main-agent"
                / "attempt-1.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn("message_update", main_events)
            self.assertNotIn("intermediate-only", main_events)
            self.assertFalse((cli_out / "pi-agent-provenance" / "inspection-agent").exists())


if __name__ == "__main__":
    unittest.main()
