#!/usr/bin/env python3
"""Tests for Harbor Fixer exec."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerExecTest(unittest.TestCase):
    def test_exec_preserves_order_logs_and_failure_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            output_dir = root_path / "fixer"
            plan = make_fix_plan()
            plan["plans"] = [
                {
                    **plan["plans"][0],
                    "commands": [
                        {"command_id": "cmd-001", "cwd": ".", "command": "printf 1 >> order.txt", "purpose": "x", "expected_effect": "x"},
                        {"command_id": "cmd-fail", "cwd": ".", "command": "printf 2 >> order.txt; exit 7", "purpose": "x", "expected_effect": "x"},
                        {"command_id": "cmd-skip", "cwd": ".", "command": "printf X >> order.txt", "purpose": "x", "expected_effect": "x"},
                    ],
                },
                {
                    **plan["plans"][0],
                    "plan_id": "fix-002",
                    "commands": [{"command_id": "cmd-003", "cwd": ".", "command": "printf 3 >> order.txt", "purpose": "x", "expected_effect": "x"}],
                },
            ]
            plan_path = root_path / "fix-plan-latest.json"
            write_json(plan_path, plan)

            result = run_fix_exec(build_exec_input(plan_path, workspace), output_dir)

            self.assertEqual(result["status"], "partial_failed")
            self.assertEqual((workspace / "order.txt").read_text(encoding="utf-8"), "123")
            self.assertEqual(result["plans"][0]["commands"][2]["status"], "skipped")
            self.assertIn("previous command", result["plans"][0]["commands"][2]["skip_reason"])
            self.assertTrue((output_dir / result["plans"][0]["commands"][0]["stdout_path"]).exists())

    def test_exec_missing_or_inaccessible_cwd_records_failed_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["commands"][0]["cwd"] = "missing"
            write_json(plan_path, plan)
            self.assertEqual(run_fix_exec(build_exec_input(plan_path, workspace), output_dir)["status"], "failed")

            plan["plans"][0]["commands"][0]["cwd"] = "."
            write_json(plan_path, plan)
            original_is_dir = Path.is_dir

            def fake_is_dir(path: Path) -> bool:
                if path == workspace.resolve():
                    raise OSError("no access")
                return original_is_dir(path)

            with mock.patch("harbor_fixer.executor.Path.is_dir", fake_is_dir):
                result = run_fix_exec(build_exec_input(plan_path, workspace), root_path / "fixer-inaccessible")
            self.assertEqual(result["plans"][0]["commands"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
