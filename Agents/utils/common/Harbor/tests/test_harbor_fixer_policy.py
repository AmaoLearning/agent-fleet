"""Tests for Harbor Fixer Policy Agent routing."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import (
    FixerTestCase,
    PolicyInvoker,
    SequenceInvoker,
    make_fix_plan,
)
from harbor_fixer.policy import run_policy_preflight


def _command(template: dict, action_id: str, executable: str, *arguments: str) -> dict:
    return {
        **template,
        "action_id": action_id,
        "executable": executable,
        "arguments": list(arguments),
    }


def _file_edit(action_id: str, cwd: str, path: str) -> dict:
    return {
        "action_id": action_id,
        "action_type": "file_edit",
        "cwd": cwd,
        "path": path,
        "edit": {
            "kind": "replace_text",
            "old_text": "false",
            "new_text": "true",
            "expected_replacements": 1,
        },
        "purpose": "Enable the fixture.",
        "expected_effect": "The fixture is enabled.",
    }


class HarborFixerPolicyTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _run(
        self,
        plan: dict,
        invoker: PolicyInvoker | SequenceInvoker | None,
        *,
        user_rules_path: Path | None = None,
        writable_roots: list[Path] | None = None,
    ) -> dict:
        return run_policy_preflight(
            plan,
            self.workspace,
            self.root / "policy-output",
            invoker,
            user_rules_path=user_rules_path,
            writable_roots=writable_roots,
        )

    def test_routes_file_edits_to_t2_and_commands_to_t3(self) -> None:
        allowed_root = self.root / "config"
        allowed_root.mkdir()
        plan = make_fix_plan()
        template = plan["plans"][0]["actions"][0]
        plan["plans"][0]["actions"] = [
            _file_edit("authorized-edit", ".", str(allowed_root / "daemon.json")),
            _file_edit("workspace-edit", ".", "workspace.json"),
            _command(template, "docker-build", "docker", "build", "-t", "fixture", "."),
            _command(template, "explicit-shell", "bash", "-c", "echo ok && docker ps"),
        ]
        invoker = PolicyInvoker()

        result = self._run(plan, invoker, writable_roots=[allowed_root])

        self.assertEqual(
            [decision["tier"] for decision in result["decisions"]],
            ["T2", "T3", "T3", "T3"],
        )
        self.assertEqual(len(result["fix_plan_sha256"]), 64)
        self.assertEqual(len(result["decisions"][0]["action_sha256"]), 64)
        self.assertIsNone(result["decisions"][0]["command_analysis"])
        shell_input = invoker.records[-1][1]
        self.assertEqual(shell_input["action"]["action_id"], "explicit-shell")
        self.assertTrue(shell_input["command_analysis"]["has_embedded_script"])
        self.assertEqual(
            shell_input["action_sha256"],
            shell_input["command_analysis"]["action_sha256"],
        )

        previous_plan_digest = result["fix_plan_sha256"]
        previous_digest = result["decisions"][0]["action_sha256"]
        plan["plans"][0]["actions"][0]["edit"]["new_text"] = "enabled"
        changed = self._run(plan, invoker, writable_roots=[allowed_root])
        self.assertNotEqual(changed["fix_plan_sha256"], previous_plan_digest)
        self.assertNotEqual(changed["decisions"][0]["action_sha256"], previous_digest)

    def test_t3_action_makes_later_file_edit_path_unstable(self) -> None:
        allowed_root = self.root / "config"
        allowed_root.mkdir()
        plan = make_fix_plan()
        template = plan["plans"][0]["actions"][0]
        first_plan = plan["plans"][0]
        second_plan = json.loads(json.dumps(first_plan))
        first_plan["actions"] = [
            _command(template, "link-change", "ln", "-s", "/etc", str(allowed_root / "link")),
        ]
        second_plan["plan_id"] = "fix-002"
        second_plan["task_list"][0]["task_index"] = "2"
        second_plan["task_list"][0]["task_name"] = "task-2"
        second_plan["verification_hint"]["target_task_indexes"] = ["2"]
        second_plan["actions"] = [
            _file_edit("later-edit", ".", str(allowed_root / "link/config"))
        ]
        plan["plans"] = [first_plan, second_plan]
        result = self._run(plan, PolicyInvoker(), writable_roots=[allowed_root])

        self.assertEqual([item["tier"] for item in result["decisions"]], ["T3", "T3"])
        self.assertFalse(
            result["decisions"][1]["path_analysis"]["path_resolution_stable"]
        )

    def test_t1_requires_trusted_executable_and_invalid_argv_is_denied(self) -> None:
        plan = make_fix_plan()
        action = plan["plans"][0]["actions"][0]
        action.update({"executable": "ls", "arguments": []})
        with mock.patch(
            "harbor_fixer.policy.preflight.shutil.which", return_value="/tmp/ls"
        ):
            result = self._run(plan, PolicyInvoker())
        self.assertEqual(result["decisions"][0]["tier"], "T3")

        action["executable"] = "invalid executable"
        result = self._run(plan, None)
        self.assertEqual(
            result["decisions"][0]["reason_code"], "invalid_command_action"
        )

    def test_agent_and_configuration_fail_closed(self) -> None:
        plan = make_fix_plan()
        plan["plans"][0]["actions"][0].update(
            {"executable": "docker", "arguments": ["build", "."]}
        )
        result = self._run(
            plan,
            SequenceInvoker(["not-json", json.dumps({"decision": "allow"})]),
        )
        self.assertEqual(
            result["decisions"][0]["reason_code"], "policy_agent_failed_closed"
        )

        result = self._run(
            make_fix_plan(),
            None,
            user_rules_path=self.root / "missing-rules.json",
        )
        self.assertEqual(
            result["decisions"][0]["reason_code"], "policy_configuration_error"
        )


if __name__ == "__main__":
    unittest.main()
