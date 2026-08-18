"""Minimal integration tests for Harbor Fixer reporting."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import (  # noqa: E402
    ReportInvoker,
    make_fix_plan,
    write_json,
    write_verification_fixture,
)
from harbor_fixer.reporter import (  # noqa: E402
    render_human_report,
    run_report_from_paths,
)
from harbor_fixer.validation import validate_fix_report  # noqa: E402


class HarborFixerReportTest(unittest.TestCase):
    def test_report_preserves_facts_and_orders_attributed_analysis_last(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir, output_dir, verification_path = write_verification_fixture(
                root_path
            )

            result = run_report_from_paths(
                verification_path,
                analyzer_dir,
                output_dir,
                ReportInvoker(),
                baseline_monitor_policy="off",
            )

            validate_fix_report(result)
            self.assertEqual(result["summary"]["text"], "Fixture fix report summary.")
            self.assertEqual(result["task_results"][0]["verification_status"], "fixed")
            report = (output_dir / "fix-report-latest.md").read_text(encoding="utf-8")
            self.assertLess(
                report.index("## Observed Fixer results"),
                report.index("## Attributed analysis"),
            )
            self.assertLess(
                report.index("## Unavailable or missing information"),
                report.index("## Attributed analysis"),
            )
            self.assertIn(
                "| 1 | task-1 | fix-001 | Sampled — fixed | Unavailable | success | complete_success |",
                report,
            )
            self.assertIn("Docker registry is unreachable.", report)
            self.assertIn("| 0.91 |", report)
            self.assertIn("printf '%s\\n' hello", report)
            self.assertIn("Original status for task 1", report)
            self.assertNotIn("Overall status", report)

    def test_markdown_distinguishes_sampling_outcomes_and_redacts_secrets(self) -> None:
        report = {
            "summary": {"text": "Fixture summary.", "highlights": [], "caveats": []},
            "generated_at": "2026-08-18T00:00:00Z",
            "old_run": {
                "run_id": "run-1",
                "monitor_available": False,
                "monitor_summary": {},
                "tasks": [],
            },
            "new_run": {
                "verification_mode": "smoke_test",
                "sampling": {
                    "plan_task_count": 3,
                    "sampled_task_count": 2,
                    "unsampled_task_count": 1,
                },
                "summary": {
                    "total": 2,
                    "complete_success": 1,
                    "complete_failed": 1,
                    "plan_task_count": 3,
                    "sampled_task_count": 2,
                    "unsampled_task_count": 1,
                },
                "rerun": {
                    "monitor_available": False,
                    "monitor_timed_out": False,
                },
            },
            "task_results": [
                {
                    "task": {"task_index": str(index), "task_name": f"task-{index}"},
                    "plan_id": "fix-001",
                    "sampled": sampled,
                    "exec_status": "success",
                    "new_run": new_run,
                    "verification_status": status,
                }
                for index, sampled, status, new_run in (
                    (
                        1,
                        True,
                        "fixed",
                        {"task_complete_status": "complete_success", "evidence": {}},
                    ),
                    (
                        2,
                        True,
                        "not_fixed",
                        {"task_complete_status": "complete_failed", "evidence": {}},
                    ),
                    (3, False, "not_sampled", None),
                )
            ],
            "artifacts": {},
        }
        plan = make_fix_plan()
        plan["plans"][0]["actions"][0]["arguments"] = ["API_KEY=secret-value"]

        markdown = render_human_report(report, plan, {})

        for expected in (
            "Sampled — fixed",
            "Sampled — not fixed",
            "Not sampled",
            "Baseline run metrics are unavailable",
            "API_KEY=<REDACTED>",
        ):
            self.assertIn(expected, markdown)
        self.assertNotIn("secret-value", markdown)
        self.assertNotIn("Verifier aggregate status", markdown)

    def test_cli_reports_fallback_and_validation_failures_without_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir, _output_dir, verification_path = write_verification_fixture(
                root_path
            )
            output_dir = root_path / "report-output"
            base_command = [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--report-only",
                "--analyzer-output",
                str(analyzer_dir),
                "--output-dir",
                str(output_dir),
            ]
            environment = {
                "PATH": "/usr/bin:/bin",
                "HARBOR_FIXER_API_KEY": "fixture",
                "HARBOR_AGENT_RETRY_INITIAL_SECONDS": "0",
            }

            fallback = subprocess.run(
                [
                    *base_command,
                    "--verification-result",
                    str(verification_path),
                    "--pi-bin",
                    "/bin/false",
                    "--pi-model",
                    "fixture-model",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(fallback.returncode, 1, fallback.stderr)
            payload = json.loads(
                (output_dir / "fix-report-latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["summary"]["status"], "failed")

            invalid_path = root_path / "invalid-verification.json"
            write_json(invalid_path, {})
            invalid = subprocess.run(
                [*base_command, "--verification-result", str(invalid_path)],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("verification result schema_version must be 2", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
