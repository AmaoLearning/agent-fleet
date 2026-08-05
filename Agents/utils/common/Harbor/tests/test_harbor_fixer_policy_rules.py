"""Tests for deterministic Harbor Fixer policy rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import FixerTestCase, write_json
from harbor_fixer.policy import analyze_command, evaluate_t1, load_user_rules


def _prefix_rule(rule_id: str, *pattern: str) -> dict:
    return {"rule_id": rule_id, "pattern": list(pattern), "match": "prefix"}


def _command(executable: str, *arguments: str) -> dict:
    return {
        "action_id": "action-001",
        "action_type": "command",
        "cwd": ".",
        "executable": executable,
        "arguments": list(arguments),
        "purpose": "test",
        "expected_effect": "test",
    }


class HarborFixerPolicyRulesTest(FixerTestCase):
    def test_command_analysis_copies_structured_argv(self) -> None:
        cases = [
            (_command("python3", "-c", 'print("x" * 1000)'), ("static_argv", True)),
            (_command("bash", "-lc", "docker ps | grep fixture"), ("static_argv", True)),
            (_command("echo", "$HOME"), ("static_argv", False)),
            ({"action_type": "file_edit"}, ("invalid", False)),
        ]
        for action, expected in cases:
            with self.subTest(action=action):
                analysis = analyze_command(action)
                self.assertEqual(
                    (analysis.classification, analysis.has_embedded_script),
                    expected,
                )
                self.assertEqual(len(analysis.action_sha256), 64)

    def test_builtin_rules(self) -> None:
        cases = [
            (_command("docker", "ps", "--all"), None),
            (_command("git", "status", "--short"), None),
            (_command("find", ".", "-delete"), None),
            (_command("ls", "workspace"), "allow"),
            (_command("rm", "-rf", "build"), "deny"),
            (_command("sudo", "-u", "root", "rm", "-f", "/tmp/item"), "deny"),
            (_command("env", "FOO=bar", "rm", "-rf", "build"), "deny"),
            (_command("bash", "-lc", "rm -rf build"), None),
            (_command("xargs", "rm", "-f"), None),
            (_command("rm", "-rf", "*"), "deny"),
            (_command("ls", "*"), "allow"),
            (_command("echo", "$HOME"), "allow"),
        ]
        for action, expected in cases:
            with self.subTest(action=action):
                decision = evaluate_t1(action, [], executable_verified=True)
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
                "deny": [
                    _prefix_rule("deny-docker-ps", "docker", "ps"),
                ],
                "allow": [
                    _prefix_rule("allow-docker-ps", "docker", "ps"),
                    _prefix_rule("allow-rm", "rm", "-rf"),
                    _prefix_rule("allow-pytest", "pytest", "-q"),
                    _prefix_rule("allow-python", "python3", "-c"),
                ],
            },
        )
        rules, _ = load_user_rules(rules_path)

        self.assertEqual(
            evaluate_t1(_command("docker", "ps", "-a"), rules)["rule_id"],
            "deny-docker-ps",
        )
        self.assertEqual(
            evaluate_t1(_command("rm", "-rf", "build"), rules)["source"],
            "builtin_rule",
        )
        self.assertIsNone(
            evaluate_t1(
                _command("python3", "-c", "print(1)"),
                rules,
                executable_verified=True,
            )
        )
        self.assertEqual(
            evaluate_t1(
                _command("pytest", "-q", "$TESTS"),
                rules,
                executable_verified=True,
            )["decision"],
            "allow",
        )
        self.assertIsNone(
            evaluate_t1(
                _command("curl", "https://example.test"),
                [],
                analysis=analyze_command(_command("ls")),
                executable_verified=True,
            )
        )
        self.assertIsNone(evaluate_t1(_command("ls"), []))


if __name__ == "__main__":
    unittest.main()
