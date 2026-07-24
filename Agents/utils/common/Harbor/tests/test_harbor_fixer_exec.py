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


class HarborFixerExecTest(FixerTestCase):
    def test_exec_preserves_order_logs_and_failure_boundaries(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        output_dir = self.root / "fixer"
        plan = make_fix_plan()
        def command(command_id: str, value: str) -> dict:
            return {
                "command_id": command_id,
                "cwd": ".",
                "command": value,
                "purpose": "fixture",
                "expected_effect": "fixture",
            }
        plan["plans"] = [
            {
                **plan["plans"][0],
                "commands": [
                    command("cmd-001", "printf 1 >> order.txt"),
                    command("cmd-fail", "printf 2 >> order.txt; exit 7"),
                    command("cmd-skip", "printf X >> order.txt"),
                ],
            },
            {
                **plan["plans"][0],
                "plan_id": "fix-002",
                "commands": [command("cmd-003", "printf 3 >> order.txt")],
            },
        ]
        plan_path = self.root / "fix-plan-latest.json"
        write_json(plan_path, plan)
        result = run_fix_exec(build_exec_input(plan_path, workspace), output_dir)

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual((workspace / "order.txt").read_text(encoding="utf-8"), "123")
        self.assertEqual(result["plans"][0]["commands"][2]["status"], "skipped")
        self.assertTrue(
            (output_dir / result["plans"][0]["commands"][0]["stdout_path"]).exists()
        )

    def test_exec_missing_or_inaccessible_cwd_records_failed_command(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        plan_path = self.root / "fix-plan-latest.json"
        plan = make_fix_plan()
        plan["plans"][0]["commands"][0]["cwd"] = "missing"
        write_json(plan_path, plan)
        result = run_fix_exec(build_exec_input(plan_path, workspace), self.root / "fixer")
        self.assertEqual(result["status"], "failed")

        plan["plans"][0]["commands"][0]["cwd"] = "."
        write_json(plan_path, plan)
        original_is_dir = Path.is_dir

        def fake_is_dir(path: Path) -> bool:
            if path == workspace.resolve():
                raise OSError("no access")
            return original_is_dir(path)

        with mock.patch("harbor_fixer.executor.Path.is_dir", fake_is_dir):
            result = run_fix_exec(
                build_exec_input(plan_path, workspace),
                self.root / "fixer-inaccessible",
            )
        self.assertEqual(result["plans"][0]["commands"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
