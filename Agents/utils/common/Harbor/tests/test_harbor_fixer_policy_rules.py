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


class HarborFixerPolicyRulesTest(FixerTestCase):
    def test_command_analysis_separates_argv_from_shell_scripts(self) -> None:
        cases = {
            "python3 -c 'print(\"x\" * 1000)'": ("static_argv", True),
            "python3 - <<'EOF'\nprint('x')\nEOF": ("shell_script", False),
            "docker ps | grep fixture": ("shell_script", False),
            "echo '$HOME'": ("static_argv", False),
            "echo 'unterminated": ("invalid", False),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                analysis = analyze_command(command)
                self.assertEqual(
                    (analysis.classification, analysis.has_embedded_script),
                    expected,
                )
                self.assertEqual(len(analysis.command_sha256), 64)

    def test_builtin_rules(self) -> None:
        cases = {
            "docker ps --all": None,
            "git status --short": None,
            "find . -delete": None,
            "ls workspace": "allow",
            "rm -rf build": "deny",
            "sudo -u root rm -f /tmp/item": "deny",
            "FOO=bar rm -rf build": "deny",
            "bash -lc 'rm -rf build'": None,
            "echo $(rm -f marker)": None,
            "printf '%s\\n' marker | xargs rm -f": None,
            "PATH=$PWD:$PATH ls": None,
            "rm -rf *": "deny",
            "ls *": None,
            "echo '$HOME'": "allow",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                decision = evaluate_t1(command, [], executable_verified=True)
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
            evaluate_t1("docker ps -a", rules)["rule_id"], "deny-docker-ps"
        )
        self.assertEqual(evaluate_t1("rm -rf build", rules)["source"], "builtin_rule")
        self.assertIsNone(evaluate_t1("pytest -q $TESTS", rules))
        self.assertIsNone(
            evaluate_t1("python3 -c 'print(1)'", rules, executable_verified=True)
        )
        self.assertEqual(
            evaluate_t1("pytest -q tests/unit", rules, executable_verified=True)[
                "decision"
            ],
            "allow",
        )
        self.assertIsNone(
            evaluate_t1(
                "curl https://example.test",
                [],
                analysis=analyze_command("ls"),
                executable_verified=True,
            )
        )
        self.assertIsNone(evaluate_t1("ls", []))


if __name__ == "__main__":
    unittest.main()
