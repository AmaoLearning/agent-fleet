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


class HarborFixerBatchTest(FixerTestCase):
    def test_batch_plan_success_and_failure_continue(self) -> None:
        analyzer = write_analyzer_fixture(self.root / "bench-good")
        output = self.root / "fixer-good"
        manifest = self.root / "batch-manifest.json"
        write_json(
            manifest,
            {
                "schema_version": 1,
                "kind": "harbor_fixer_batch_manifest",
                "benchmarks": [
                    {
                        "benchmark_id": "bad",
                        "analyzer_output": str(self.root / "missing"),
                        "output_dir": str(self.root / "fixer-bad"),
                    },
                    {
                        "benchmark_id": "good",
                        "analyzer_output": str(analyzer),
                        "output_dir": str(output),
                    },
                ],
            },
        )
        with mock.patch.dict("os.environ", {"FIXTURE_PI_API_KEY": "fixture"}):
            result = run_batch_plan_from_manifest(
                manifest,
                self.root / "batch-output",
                pi_config=PiInvocationConfig(
                    pi_bin=str(write_fixture_pi(self.root / "fixture_pi.py")),
                    base_url="https://example.test/v1",
                    model="fixture-model",
                    api_key_env="FIXTURE_PI_API_KEY",
                ),
                max_concurrency=2,
                benchmark_concurrency=2,
            )

        statuses = {
            item["benchmark_id"]: item["status"] for item in result["results"]
        }
        self.assertEqual(statuses, {"bad": "failed", "good": "success"})
        self.assertTrue((output / "fix-plan-latest.json").exists())

    def test_batch_report_propagates_summary_failure(self) -> None:
        manifest = self.root / "batch-manifest.json"
        write_json(
            manifest,
            {
                "schema_version": 1,
                "kind": "harbor_fixer_batch_manifest",
                "benchmarks": [
                    {
                        "benchmark_id": "failed-summary",
                        "analyzer_output": str(self.root / "analyzer"),
                        "verification_result": str(self.root / "verification.json"),
                        "output_dir": str(self.root / "report"),
                    }
                ],
            },
        )
        with mock.patch(
            "harbor_fixer.batch.run_report_from_paths",
            return_value={"status": "fixed", "summary": {"status": "failed"}},
        ):
            result = run_batch_report_from_manifest(
                manifest,
                self.root / "batch-output",
                pi_config=PiInvocationConfig(),
                benchmark_concurrency=1,
                baseline_monitor_policy="auto",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["summary_status"], "failed")


if __name__ == "__main__":
    unittest.main()
