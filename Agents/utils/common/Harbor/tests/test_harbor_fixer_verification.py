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


class HarborFixerVerificationTest(unittest.TestCase):
    def test_verification_smoke_samples_two_and_marks_unsampled(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=4)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 5)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = root_path / "verification-run"
            rerun_script = write_smoke_rerun_script(root_path / "smoke_rerun.py", {})

            with mock.patch.dict(
                "os.environ",
                {
                    "QUEUE_DIR": str(root_path / "old-run" / "queue"),
                    "RUNTIME_DIR": str(root_path / "old-run" / "runtime"),
                    "JOBS_ROOT": str(root_path / "old-run" / "jobs"),
                    "HARBOR_MONITOR_DIR": str(root_path / "old-run" / "monitor"),
                    "NEXT_INDEX_FILE": str(root_path / "old-run" / "queue" / "next_index"),
                    "RL_QUEUE_DIR": str(root_path / "old-run" / "runtime" / "rl-queue"),
                },
            ):
                result = run_verification_from_paths(
                    plan_path,
                    exec_path,
                    analyzer_dir,
                    run_dir,
                    output_dir,
                    rerun_command=f"{sys.executable} {rerun_script}",
                    monitor_policy="off",
                )

            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["verification_mode"], "smoke_test")
            self.assertEqual(result["status"], "fixed")
            self.assertEqual(result["sampling"]["limit_per_plan"], 2)
            self.assertEqual(result["sampling"]["sampled_task_count"], 2)
            self.assertEqual(result["new_run_summary"]["scope"], "smoke_sample")
            self.assertEqual(result["new_run_summary"]["total"], 2)
            self.assertTrue((output_dir / "verification-smoke-tasks.txt").exists())
            self.assertTrue((output_dir / "verification-smoke-selection.json").exists())
            by_index = {item["task"]["task_index"]: item["verification_status"] for item in result["task_results"]}
            self.assertEqual(sum(1 for status in by_index.values() if status == "fixed"), 2)
            self.assertEqual(sum(1 for status in by_index.values() if status == "not_sampled"), 2)
            self.assertTrue(all(item["new_run"] is None for item in result["task_results"] if item["verification_status"] == "not_sampled"))

    def test_verification_classifies_sampled_plan_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=4)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 5)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = root_path / "verification-run"
            rerun_script = write_smoke_rerun_script(
                root_path / "smoke_rerun.py",
                {"1": "success", "2": "failed", "3": "unknown", "4": "not_complete"},
            )

            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} {rerun_script}",
                monitor_policy="off",
                verification_task_limit_per_plan=4,
            )

            by_index = {item["task"]["task_index"]: item["verification_status"] for item in result["task_results"]}
            self.assertEqual(by_index, {"1": "fixed", "2": "not_fixed", "3": "unknown", "4": "not_complete"})
            self.assertEqual(result["new_run_summary"]["total"], 4)

    def test_verification_smoke_mismatch_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=2)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 3)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = write_harbor_run_fixture(root_path, ["wrong-task"], [("1", "wrong-task", "1.0", "", "")], [])

            result = run_verification_from_paths(plan_path, exec_path, analyzer_dir, run_dir, output_dir, monitor_policy="off", verification_task_limit_per_plan=1)

            self.assertEqual(result["status"], "inconclusive")
            self.assertTrue(result["sampling"]["mapping_errors"])

    def test_verification_handles_exec_and_rerun_failures(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=1)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            write_json(plan_path, make_fix_plan())
            run_dir = write_harbor_run_fixture(root_path, ["task-1"], [("1", "task-1", "1.0", "", "")], [])

            write_json(exec_path, make_exec_result(plan_status="failed"))
            skipped_rerun = write_smoke_rerun_script(root_path / "skipped_rerun.py", {})
            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} {skipped_rerun}",
                monitor_policy="auto",
            )
            self.assertEqual(result["status"], "exec_failed")
            self.assertEqual(result["rerun"]["skipped_reason"], "no_sampled_tasks")
            self.assertFalse(result["rerun"]["monitor_available"])
            self.assertEqual(result["new_run_summary"]["total"], 0)
            self.assertEqual(result["new_run_summary"]["success_rate"], 0.0)

            write_json(exec_path, make_exec_result())
            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} -c 'import sys; sys.exit(9)'",
                monitor_policy="off",
            )
            self.assertEqual(result["status"], "inconclusive")

    def test_verification_rerun_supports_relative_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path / "analyzer", count=1)
            write_json(root_path / "fix-plan.json", make_fix_plan())
            write_json(root_path / "exec-result.json", make_exec_result())
            rerun_script = write_smoke_rerun_script(root_path / "smoke_rerun.py", {})

            previous_cwd = Path.cwd()
            try:
                os.chdir(root_path)
                result = run_verification_from_paths(
                    Path("fix-plan.json"),
                    Path("exec-result.json"),
                    analyzer_dir.relative_to(root_path),
                    Path("verification-run"),
                    Path("fixer-output"),
                    rerun_command=f"{sys.executable} {rerun_script}",
                    monitor_policy="off",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["status"], "fixed")
            self.assertEqual(result["rerun"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
