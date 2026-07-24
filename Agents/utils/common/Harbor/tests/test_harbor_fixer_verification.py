#!/usr/bin/env python3
"""Tests for Harbor Fixer verification."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerVerificationTest(FixerTestCase):
    def run_smoke(self, statuses: dict[str, str], *, count: int = 4, limit: int = 2):
        analyzer, output, plan, execution = write_verification_inputs(
            self.root,
            count=count,
        )
        rerun = write_smoke_rerun_script(self.root / "smoke_rerun.py", statuses)
        return run_verification_from_paths(
            plan,
            execution,
            analyzer,
            self.root / "verification-run",
            output,
            rerun_command=f"{sys.executable} {rerun}",
            monitor_policy="off",
            verification_task_limit_per_plan=limit,
        )

    def test_verification_smoke_samples_two_and_marks_unsampled(self) -> None:
        inherited_paths = {
            name: str(self.root / "old-run" / name.lower())
            for name in (
                "QUEUE_DIR",
                "RUNTIME_DIR",
                "JOBS_ROOT",
                "HARBOR_MONITOR_DIR",
                "NEXT_INDEX_FILE",
                "RL_QUEUE_DIR",
            )
        }
        with mock.patch.dict("os.environ", inherited_paths):
            result = self.run_smoke({})

        statuses = [
            item["verification_status"] for item in result["task_results"]
        ]
        self.assertEqual(result["status"], "fixed")
        self.assertEqual(result["sampling"]["sampled_task_count"], 2)
        self.assertEqual(statuses.count("fixed"), 2)
        self.assertEqual(statuses.count("not_sampled"), 2)
        self.assertTrue((self.root / "fixer" / "verification-smoke-selection.json").exists())

    def test_verification_classifies_sampled_plan_tasks(self) -> None:
        result = self.run_smoke(
            {"1": "success", "2": "failed", "3": "unknown", "4": "not_complete"},
            limit=4,
        )
        by_index = {
            item["task"]["task_index"]: item["verification_status"]
            for item in result["task_results"]
        }
        self.assertEqual(
            by_index,
            {"1": "fixed", "2": "not_fixed", "3": "unknown", "4": "not_complete"},
        )

    def test_verification_smoke_mismatch_is_inconclusive(self) -> None:
        analyzer, output, plan, execution = write_verification_inputs(
            self.root,
            count=2,
        )
        run_dir = write_harbor_run_fixture(
            self.root,
            ["wrong-task"],
            [("1", "wrong-task", "1.0", "", "")],
            [],
        )
        result = run_verification_from_paths(
            plan,
            execution,
            analyzer,
            run_dir,
            output,
            monitor_policy="off",
            verification_task_limit_per_plan=1,
        )
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(result["sampling"]["mapping_errors"])

    def test_verification_handles_exec_and_rerun_failures(self) -> None:
        analyzer, output, plan, execution = write_verification_inputs(
            self.root,
            exec_status="failed",
        )
        run_dir = write_harbor_run_fixture(
            self.root,
            ["task-1"],
            [("1", "task-1", "1.0", "", "")],
            [],
        )
        skipped = write_smoke_rerun_script(self.root / "skipped.py", {})
        result = run_verification_from_paths(
            plan,
            execution,
            analyzer,
            run_dir,
            output,
            rerun_command=f"{sys.executable} {skipped}",
            monitor_policy="auto",
        )
        self.assertEqual(result["status"], "exec_failed")
        self.assertEqual(result["rerun"]["skipped_reason"], "no_sampled_tasks")

        write_json(execution, make_exec_result())
        result = run_verification_from_paths(
            plan,
            execution,
            analyzer,
            run_dir,
            output,
            rerun_command=f"{sys.executable} -c 'raise SystemExit(9)'",
            monitor_policy="off",
        )
        self.assertEqual(result["status"], "inconclusive")

    def test_verification_rerun_supports_relative_artifact_paths(self) -> None:
        analyzer = write_analyzer_fixture(self.root / "analyzer")
        write_json(self.root / "fix-plan.json", make_fix_plan())
        write_json(self.root / "exec-result.json", make_exec_result())
        rerun = write_smoke_rerun_script(self.root / "smoke_rerun.py", {})

        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            result = run_verification_from_paths(
                Path("fix-plan.json"),
                Path("exec-result.json"),
                analyzer.relative_to(self.root),
                Path("verification-run"),
                Path("fixer-output"),
                rerun_command=f"{sys.executable} {rerun}",
                monitor_policy="off",
            )
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(result["rerun"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
