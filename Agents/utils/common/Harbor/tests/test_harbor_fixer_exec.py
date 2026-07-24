"""Tests for Harbor Fixer execution."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, PolicyInvoker, make_fix_plan, write_json
from harbor_fixer import executor
from harbor_fixer.executor import build_exec_input, run_fix_exec


def _command(action_id: str, script: str) -> dict:
    return {
        "action_id": action_id,
        "action_type": "command",
        "cwd": ".",
        "executable": sys.executable,
        "arguments": ["-c", script],
        "purpose": "fixture",
        "expected_effect": "fixture",
    }


def _file_edit(path: str) -> dict:
    return {
        "action_id": "edit-001",
        "action_type": "file_edit",
        "cwd": ".",
        "path": path,
        "edit": {
            "kind": "replace_text",
            "old_text": "false",
            "new_text": "true",
            "expected_replacements": 1,
        },
        "purpose": "enable fixture",
        "expected_effect": "fixture is enabled",
    }


class HarborFixerExecTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _run(
        self,
        actions: list[dict],
        name: str,
        *,
        plan: dict | None = None,
        roots: list[Path] | None = None,
        timeout: float = 300,
    ) -> dict:
        plan = plan or make_fix_plan()
        plan["plans"][0]["actions"] = actions
        plan_path = self.root / f"{name}-plan.json"
        write_json(plan_path, plan)
        return run_fix_exec(
            build_exec_input(plan_path, self.workspace),
            self.root / name,
            policy_invoker=PolicyInvoker(),
            policy_write_roots=roots,
            execution_timeout_seconds=timeout,
        )

    def test_order_logs_and_failure_boundaries(self) -> None:
        actions = [
            _command("one", "open('order.txt', 'a').write('1')"),
            _command("fail", "import sys; open('order.txt', 'a').write('2'); sys.exit(7)"),
            _command("skip", "open('order.txt', 'a').write('X')"),
        ]
        plan = make_fix_plan()
        later = copy.deepcopy(plan["plans"][0])
        later["plan_id"] = "fix-002"
        later["task_list"][0].update({"task_index": "2", "task_name": "task-2"})
        later["actions"] = [_command("three", "open('order.txt', 'a').write('3')")]
        later["verification_hint"]["target_task_indexes"] = ["2"]
        plan["plans"].append(later)

        result = self._run(actions, "order", plan=plan)

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual((self.workspace / "order.txt").read_text(), "123")
        self.assertEqual(result["plans"][0]["actions"][2]["status"], "skipped")
        log = self.root / "order" / result["plans"][0]["actions"][0]["stdout_path"]
        self.assertTrue(log.exists())

    def test_exact_file_edit_and_write_boundaries(self) -> None:
        target = self.workspace / "daemon.json"
        target.write_text("false\n")
        result = self._run([_file_edit("daemon.json")], "edit", roots=[self.workspace])
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_text(), "true\n")

        denied = self._run([_file_edit("daemon.json")], "denied")
        self.assertEqual(denied["policy_status"], "denied")

    def test_symlink_swap_cannot_escape_authorized_root(self) -> None:
        allowed = self.workspace / "config"
        outside = self.root / "outside"
        allowed.mkdir()
        outside.mkdir()
        (allowed / "daemon.json").write_text("false\n")
        outside_target = outside / "daemon.json"
        outside_target.write_text("false\n")
        original_open = executor._open_authorized_parent

        def swap_then_open(target: Path, roots: list[Path]) -> tuple[int, str]:
            allowed.rename(self.workspace / "config-original")
            allowed.symlink_to(outside, target_is_directory=True)
            return original_open(target, roots)

        with mock.patch(
            "harbor_fixer.executor._open_authorized_parent",
            side_effect=swap_then_open,
        ):
            result = self._run(
                [_file_edit("config/daemon.json")],
                "race",
                roots=[self.workspace],
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(outside_target.read_text(), "false\n")

    def test_timeout_and_missing_cwd_fail(self) -> None:
        timed_out = self._run(
            [_command("timeout", "import time; time.sleep(10)")],
            "timeout",
            timeout=0.05,
        )["plans"][0]["actions"][0]
        self.assertIsNone(timed_out["exit_code"])
        self.assertIn("timed out", timed_out["stderr_summary"])

        missing = _command("missing", "pass")
        missing["cwd"] = "missing"
        result = self._run([missing], "missing-cwd")
        self.assertEqual(result["plans"][0]["actions"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
