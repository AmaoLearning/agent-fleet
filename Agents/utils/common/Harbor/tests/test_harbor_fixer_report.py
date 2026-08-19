"""Tests for deterministic and agent-generated Harbor Fixer reports."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, make_exec_result, make_fix_plan
from harbor_fixer.report import (
    generate_report_summary,
    render_fix_report,
    write_fix_report,
)
from harbor_fixer.validation import ValidationError, validate_report_summary


def _verification_result() -> dict:
    return {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "agent": "claude-code",
        "verification_mode": "smoke_test",
        "source": {},
        "execution": {"status": "success", "policy_status": "allowed"},
        "status": "fixed",
        "reason_codes": [],
        "rerun": {},
        "sampling": {"plan_task_count": 1},
        "new_run_summary": {},
        "plan_results": [],
        "task_results": [
            {
                "task": {
                    "task_index": "1",
                    "task_name": "task-1",
                    "attempt_id": None,
                },
                "verification_status": "fixed",
                "exec_status": "success",
                "exec_failure_reason": None,
                "new_run": {
                    "task_index": "1",
                    "task_name": "task-1",
                    "task_complete_status": "complete_success",
                },
            }
        ],
        "unexpected_run_task_results": [],
    }


class _SequenceInvoker:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []

    def invoke(
        self, prompt: str, payload: dict, *, attempt: int, label: str
    ) -> str:
        self.prompts.append(prompt)
        return self.outputs[attempt - 1]


class _FailingInvoker:
    def invoke(
        self, prompt: str, payload: dict, *, attempt: int, label: str
    ) -> str:
        raise TimeoutError("provider timed out")


class HarborFixerReportTest(FixerTestCase):
    def test_report_contains_summary_changes_and_remaining_issues(self) -> None:
        fix_plan = make_fix_plan()
        exec_result = make_exec_result(fix_plan=fix_plan)
        verification = _verification_result()

        report = render_fix_report("run-1", fix_plan, exec_result, verification)

        self.assertIn("# Harbor Fixer Report: run-1", report)
        self.assertIn("## Summary", report)
        self.assertIn("| Verification | fixed |", report)
        self.assertIn("| Reverification | 1 fixed |", report)
        self.assertIn("## Changes Applied", report)
        self.assertIn("Emit a harmless test line.", report)
        self.assertIn("## Remaining Issues", report)
        self.assertIn("No remaining issues were reported.", report)

    def test_write_report_publishes_the_rendered_markdown(self) -> None:
        fix_plan = make_fix_plan()
        output = self.root / "fix-report-latest.md"

        write_fix_report(
            "run-1",
            fix_plan,
            make_exec_result(fix_plan=fix_plan),
            _verification_result(),
            output,
        )

        self.assertEqual(
            output.read_text(encoding="utf-8"),
            render_fix_report(
                "run-1",
                fix_plan,
                make_exec_result(fix_plan=fix_plan),
                _verification_result(),
            ),
        )

    def test_runtime_retries_invalid_summary_and_has_factual_fallback(self) -> None:
        valid = {
            "schema_version": 1,
            "kind": "harbor_fixer_report_summary",
            "status": "success",
            "text": "One sampled task was fixed.",
            "highlights": [],
            "caveats": [],
            "generation_errors": [],
        }
        summary_input = {
            "schema_version": 1,
            "kind": "harbor_fixer_report_summary_input",
            "old_run": {"monitor_available": False},
            "new_run": {
                "verification_mode": "smoke_test",
                "sampling": {
                    "plan_task_count": 2,
                    "sampled_task_count": 1,
                    "unsampled_task_count": 1,
                },
            },
            "task_results": [
                {"sampled": True, "verification_status": "fixed"},
                {"sampled": False, "verification_status": "exec_failed"},
            ],
            "caveats": [],
        }
        with self.assertRaises(ValidationError):
            validate_report_summary(
                {**valid, "highlights": [{"task": "task-1"}]}
            )
        with self.assertRaises(ValidationError):
            validate_report_summary({**valid, "detail": {"task": "task-1"}})
        for invalid_text in ("", " \n\t "):
            with self.subTest(invalid_text=invalid_text), self.assertRaises(
                ValidationError
            ):
                validate_report_summary({**valid, "text": invalid_text})
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"HARBOR_AGENT_RETRY_INITIAL_SECONDS": "0"}
        ):
            invoker = _SequenceInvoker(
                [
                    json.dumps({**valid, "status": "failed", "text": ""}),
                    json.dumps(valid),
                ]
            )
            summary, _ = generate_report_summary(
                invoker, summary_input, Path(root) / "valid"
            )
            fallback, _ = generate_report_summary(
                _FailingInvoker(), summary_input, Path(root) / "fallback"
            )
            blocked_output = Path(root) / "blocked"
            blocked_output.write_text("not a directory", encoding="utf-8")
            write_invoker = _SequenceInvoker([json.dumps(valid)])
            with self.assertRaises(OSError):
                generate_report_summary(
                    write_invoker, summary_input, blocked_output, max_attempts=2
                )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(len(summary["generation_errors"]), 1)
        self.assertIn("status must be success", summary["generation_errors"][0]["error"])
        self.assertIn("Validation retry", invoker.prompts[1])
        self.assertEqual(fallback["status"], "failed")
        self.assertIn("1 of 2 planned task(s)", fallback["text"])
        self.assertIn("1 task(s) were not sampled", fallback["text"])
        self.assertIn("1 unsampled task(s) labeled exec_failed", fallback["text"])
        self.assertIn("Baseline monitor data was unavailable", fallback["text"])
        self.assertEqual(len(write_invoker.prompts), 1)
