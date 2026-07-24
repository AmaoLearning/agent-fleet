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


class HarborFixerPlanTest(FixerTestCase):
    def test_plan_generation_builds_inputs_and_retries_task_agent(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root)
        task_input = build_task_inputs(analyzer_dir)[0][0]
        self.assertEqual(task_input["evidence"][0]["analysis_report_pointer"], "/tasks/0")

        invoker = SequenceInvoker(["not-json", json.dumps(task_summary_for(task_input))])
        summaries, errors = collect_task_summaries([task_input], invoker, self.root / "out")
        self.assertEqual((len(summaries), errors), (1, []))
        retry_prompt = invoker.records[1][0]
        self.assertIn("invalid JSON:", retry_prompt)
        self.assertIn("<previous-output>\nnot-json\n</previous-output>", retry_prompt)

        bad_summary = json.loads(json.dumps(task_summary_for(task_input)))
        bad_summary["task"]["task_index"] = "other"
        with self.assertRaises(ValidationError):
            validate_task_summary(bad_summary, expected_task=task_input["task"])
        with self.assertRaisesRegex(ValidationError, "after 2 attempts"):
            request_fix_plan(
                SequenceInvoker(["not-json", "still-not-json"]),
                {"source": {}, "generation_errors": []},
                self.root / "main-out",
            )

    def test_plan_generation_and_cli_smoke_write_fix_plan(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root, count=2)
        output_dir = self.root / "fixer"
        plan = run_plan_generation(
            analyzer_dir,
            output_dir,
            SummaryInvoker(),
            MainInvoker(),
            max_concurrency=2,
        )
        validate_fix_plan_set(plan)
        main_input = json.loads(
            (output_dir / "main-agent-input.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            main_input["target_context"]["kind"],
            "harbor_fixer_target_context",
        )
        self.assertEqual(len(main_input["target_context_artifact"]["sha256"]), 64)

        cli_out = self.root / "cli-fixer"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--analyzer-output",
                str(analyzer_dir),
                "--output-dir",
                str(cli_out),
                "--pi-bin",
                str(write_fixture_pi(self.root / "fixture_pi.py")),
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
        provenance_paths = sorted(
            (cli_out / "pi-agent-provenance").glob("task-*/attempt-1.json")
        )
        self.assertEqual(len(provenance_paths), 2)
        self.assertTrue(
            all(
                json.loads(path.read_text(encoding="utf-8"))["thinking_level"] == "off"
                for path in provenance_paths
            )
        )
        main_events = (
            cli_out / "pi-agent-events" / "main-agent" / "attempt-1.jsonl"
        ).read_text(encoding="utf-8")
        self.assertNotIn("message_update", main_events)


if __name__ == "__main__":
    unittest.main()
