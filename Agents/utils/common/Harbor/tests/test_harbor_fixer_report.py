#!/usr/bin/env python3
"""Tests for Harbor Fixer report."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerReportTest(unittest.TestCase):
    def test_report_preserves_verification_facts_and_uses_summary_agent_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir, output_dir, verification_path = write_verification_fixture(root_path)
            write_json(
                output_dir / "target-environment.json",
                {"schema_version": 1, "kind": "harbor_fixer_target_environment"},
            )
            write_json(
                output_dir / "target-context.json",
                {"schema_version": 1, "kind": "harbor_fixer_target_context"},
            )
            invoker = ReportInvoker()

            result = run_report_from_paths(
                verification_path,
                analyzer_dir,
                output_dir,
                invoker,
                baseline_monitor_policy="off",
            )

            validate_fix_report(result)
            self.assertTrue((output_dir / "fix-report-latest.json").read_text(encoding="utf-8").startswith('{\n  "summary"'))
            self.assertEqual(result["summary"]["text"], "Fixture fix report summary.")
            self.assertEqual(result["task_results"][0]["verification_status"], "fixed")
            self.assertEqual(invoker.calls[0][3], "report-main-agent")
            self.assertEqual(invoker.calls[0][1]["kind"], "harbor_fixer_report_summary_input")
            self.assertEqual(
                result["artifacts"]["target_environment_path"],
                str(output_dir / "target-environment.json"),
            )
            self.assertEqual(
                result["artifacts"]["target_context_path"],
                str(output_dir / "target-context.json"),
            )
            human_report_path = output_dir / "fix-report-latest.md"
            self.assertEqual(result["artifacts"]["human_report_path"], str(human_report_path))
            human_report = human_report_path.read_text(encoding="utf-8")
            for heading in (
                "## Human summary",
                "## Problems and root causes",
                "## Fix approach and suggested commands",
                "## Trial execution",
                "## Verification",
                "### Sampled task results",
                "## Failures and interruptions",
            ):
                self.assertIn(heading, human_report)
            self.assertIn("Docker registry is unreachable.", human_report)
            self.assertIn("printf '%s\\n' hello", human_report)
            self.assertIn("| 1 | task-1 | fix-001 | yes |", human_report)

    def test_report_summary_failure_and_baseline_monitor_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir, output_dir, verification_path = write_verification_fixture(root_path)
            baseline_run_dir = write_harbor_run_fixture(
                root_path / "baseline",
                ["task-1", "task-2"],
                [("1", "task-1", "0.0", "", ""), ("2", "task-2", "1.0", "", "")],
                [],
            )

            result = run_report_from_paths(
                verification_path,
                analyzer_dir,
                output_dir,
                ReportInvoker("not json"),
                baseline_run_dir=baseline_run_dir,
            )

            self.assertEqual(result["summary"]["status"], "failed")
            self.assertIn("Deterministic fallback summary", result["summary"]["text"])
            self.assertTrue(result["old_run"]["monitor_available"])
            self.assertEqual(result["old_run"]["monitor_summary"]["total"], 2)
            self.assertEqual(result["task_results"][0]["old_run_monitor_status"], "complete_failed")
            human_report = (output_dir / "fix-report-latest.md").read_text(encoding="utf-8")
            self.assertIn("Deterministic fallback summary", human_report)
            self.assertIn("summary generated without report-main-agent", human_report)

    def test_human_report_explains_exec_failure_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=1)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["commands"][0]["command"] = (
                "OPENAI_API_KEY=openai-secret "
                "HARBOR_FIXER_API_KEY='fixer-secret' "
                "GITHUB_TOKEN=github-secret "
                "curl --password password-secret "
                "--api-key=option-secret "
                "'https://example.test?access_token=query-secret' "
                "-H 'X-API-Key: header-secret' "
                "-H 'Authorization: Bearer bearer-secret'"
            )
            exec_result = make_exec_result(plan_status="failed")
            exec_result["plans"][0]["commands"][0]["stderr_summary"] = (
                "permission denied while connecting to /var/run/docker.sock"
            )
            write_json(plan_path, plan)
            write_json(exec_path, exec_result)
            run_dir = write_harbor_run_fixture(
                root_path,
                ["task-1"],
                [("1", "task-1", "0.0", "", "")],
                [],
            )
            skipped_rerun = write_smoke_rerun_script(root_path / "skipped_rerun.py", {})
            verification = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} {skipped_rerun}",
                monitor_policy="off",
            )

            result = run_report_from_paths(
                output_dir / "verification-result-latest.json",
                analyzer_dir,
                output_dir,
                ReportInvoker(),
                baseline_monitor_policy="off",
            )

            self.assertEqual(verification["status"], "exec_failed")
            self.assertEqual(result["status"], "exec_failed")
            human_report = (output_dir / "fix-report-latest.md").read_text(encoding="utf-8")
            self.assertIn("rerun skipped: no_sampled_tasks", human_report)
            self.assertIn("permission denied while connecting to /var/run/docker.sock", human_report)
            self.assertIn("OPENAI_API_KEY=<REDACTED>", human_report)
            self.assertIn("HARBOR_FIXER_API_KEY=<REDACTED>", human_report)
            self.assertIn("GITHUB_TOKEN=<REDACTED>", human_report)
            self.assertIn("--password <REDACTED>", human_report)
            self.assertIn("--api-key=<REDACTED>", human_report)
            self.assertIn("access_token=<REDACTED>", human_report)
            self.assertIn("X-API-Key: <REDACTED>", human_report)
            self.assertIn("Authorization: Bearer <REDACTED>", human_report)
            for secret in (
                "openai-secret",
                "fixer-secret",
                "github-secret",
                "password-secret",
                "option-secret",
                "query-secret",
                "header-secret",
                "bearer-secret",
            ):
                self.assertNotIn(secret, human_report)
            self.assertIn("| Monitor available | Monitor timed out |", human_report)
            self.assertIn("| False | False |", human_report)
            self.assertIn("verification_status=exec_failed", human_report)

    def test_cli_verify_and_report_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir, output_dir, verification_path = write_verification_fixture(root_path)
            report_output = root_path / "report-output"
            agent_script = write_fixture_pi(root_path / "fixture_pi.py")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "fixer.py"),
                    "--report-only",
                    "--verification-result",
                    str(verification_path),
                    "--analyzer-output",
                    str(analyzer_dir),
                    "--output-dir",
                    str(report_output),
                    "--pi-bin",
                    str(agent_script),
                    "--pi-base-url",
                    "https://example.test/v1",
                    "--pi-model",
                    "fixture-model",
                    "--pi-api-key-env",
                    "FIXTURE_PI_API_KEY",
                    "--write-prompts",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={"PATH": "/usr/bin:/bin", "FIXTURE_PI_API_KEY": "fixture"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((report_output / "fix-report-latest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["text"], "cli report summary")
            self.assertTrue((report_output / "prompts" / "report-main-agent-prompt.md").exists())
            self.assertFalse((report_output / "prompts" / "main-agent-prompt.md").exists())


if __name__ == "__main__":
    unittest.main()
