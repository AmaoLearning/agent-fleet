"""Tests for Harbor Fixer execution policy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

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
    write_json,
)
from harbor_fixer.policy import evaluate_t1, load_user_rules, run_policy_preflight


def _prefix_rule(rule_id: str, *pattern: str) -> dict:
    return {"rule_id": rule_id, "pattern": list(pattern), "match": "prefix"}


class HarborFixerPolicyTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _run_preflight(
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
            self.root / "policy",
            invoker,
            user_rules_path=user_rules_path,
            writable_roots=writable_roots,
        )

    def test_t1_rules(self) -> None:
        cases = {
            "docker ps --all": "allow",
            "git status --short": "allow",
            "cat pyproject.toml | grep dependency": "allow",
            "rm -rf build": "deny",
            "sudo -u root rm -f /tmp/item": "deny",
            "bash -lc 'rm -rf build'": "deny",
            "echo $(rm -f marker)": "deny",
            "printf '%s\\n' marker | xargs rm -f": "deny",
            "docker exec fixture rm -f /tmp/item": "deny",
            "find . -delete": "deny",
            "diff --output=change.patch old new": None,
            "git diff --output=change.patch": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                decision = evaluate_t1(command, [])
                self.assertEqual(
                    None if decision is None else decision["decision"], expected
                )

    def test_user_deny_precedes_allow_and_builtin_deny(self) -> None:
        rules_path = self.root / "rules.json"
        write_json(
            rules_path,
            {
                "schema_version": 1,
                "kind": "harbor_fixer_policy_rules",
                "deny": [_prefix_rule("deny-docker-ps", "docker", "ps")],
                "allow": [
                    _prefix_rule("allow-docker-ps", "docker", "ps"),
                    _prefix_rule("allow-rm", "rm", "-rf"),
                    _prefix_rule("allow-pytest", "pytest", "-q"),
                ],
            },
        )
        rules, _ = load_user_rules(rules_path)

        self.assertEqual(
            evaluate_t1("docker ps -a", rules)["rule_id"], "deny-docker-ps"
        )
        self.assertEqual(evaluate_t1("rm -rf build", rules)["source"], "builtin_rule")
        self.assertEqual(
            evaluate_t1("pytest -q tests/unit", rules)["decision"], "allow"
        )

    def test_preflight_routes_commands_by_write_boundary(self) -> None:
        config_root = self.root / "daemon-config"
        config_root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        plan = make_fix_plan()
        template = plan["plans"][0]["commands"][0]
        commands = [
            ("inside-write", "printf enabled > daemon.json"),
            ("configured-write", f"touch {config_root / 'daemon.json'}"),
            ("docker-build", "docker build -t fixture ."),
            ("symlink-escape", "touch escape/config.json"),
        ]
        plan["plans"][0]["commands"] = [
            {**template, "command_id": command_id, "command": command}
            for command_id, command in commands
        ]
        invoker = PolicyInvoker()

        result = self._run_preflight(plan, invoker, writable_roots=[config_root])

        self.assertEqual(result["status"], "allowed")
        self.assertEqual(result["writable_roots"], [str(config_root.resolve())])
        self.assertEqual(
            [decision["tier"] for decision in result["decisions"]],
            ["T3", "T2", "T3", "T3"],
        )
        self.assertEqual(
            [record[1]["tier"] for record in invoker.records],
            ["T3", "T2", "T3", "T3"],
        )
        self.assertFalse(
            result["decisions"][-1]["path_analysis"]["write_targets"][0][
                "inside_writable_roots"
            ]
        )

    def test_policy_agent_retries_then_fails_closed(self) -> None:
        plan = make_fix_plan()
        plan["plans"][0]["commands"][0]["command"] = "docker build ."
        invoker = SequenceInvoker(["not-json", json.dumps({"decision": "allow"})])

        result = self._run_preflight(plan, invoker)

        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result["decisions"][0]["reason_code"], "policy_agent_failed_closed"
        )
        self.assertEqual(invoker.calls, 2)

    def test_invalid_user_rules_fail_closed(self) -> None:
        result = self._run_preflight(
            make_fix_plan(),
            None,
            user_rules_path=self.root / "missing-rules.json",
        )

        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result["decisions"][0]["reason_code"], "policy_configuration_error"
        )


if __name__ == "__main__":
    unittest.main()
