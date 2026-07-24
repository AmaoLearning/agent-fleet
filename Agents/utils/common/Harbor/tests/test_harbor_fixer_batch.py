#!/usr/bin/env python3
"""Tests for Harbor Fixer batch."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerBatchTest(unittest.TestCase):
    def test_batch_plan_success_and_failure_continue(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_good = write_analyzer_fixture(root_path / "bench-good", count=1)
            output_good = root_path / "fixer-good"
            output_bad = root_path / "fixer-bad"
            manifest = root_path / "batch-manifest.json"
            write_json(
                manifest,
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_batch_manifest",
                    "benchmarks": [
                        {"benchmark_id": "bad", "analyzer_output": str(root_path / "missing"), "output_dir": str(output_bad)},
                        {"benchmark_id": "good", "analyzer_output": str(analyzer_good), "output_dir": str(output_good)},
                    ],
                },
            )
            agent_script = write_fixture_pi(root_path / "fixture_pi.py")

            with mock.patch.dict("os.environ", {"FIXTURE_PI_API_KEY": "fixture"}):
                result = run_batch_plan_from_manifest(
                    manifest,
                    root_path / "batch-output",
                    pi_config=PiInvocationConfig(
                        pi_bin=str(agent_script),
                        base_url="https://example.test/v1",
                        model="fixture-model",
                        api_key_env="FIXTURE_PI_API_KEY",
                    ),
                    max_concurrency=2,
                    benchmark_concurrency=2,
                )

            self.assertEqual(result["status"], "partial_failed")
            self.assertEqual({item["benchmark_id"]: item["status"] for item in result["results"]}, {"bad": "failed", "good": "success"})
            self.assertTrue((output_good / "fix-plan-latest.json").exists())
            task_provenance = next(
                (output_good / "pi-agent-provenance").glob("task-*/attempt-1.json")
            )
            self.assertEqual(
                json.loads(task_provenance.read_text(encoding="utf-8"))["thinking_level"],
                "off",
            )


if __name__ == "__main__":
    unittest.main()
