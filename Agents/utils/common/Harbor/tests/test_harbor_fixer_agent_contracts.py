#!/usr/bin/env python3
"""Tests for Harbor Fixer agent contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerAgentContractsTest(FixerTestCase):
    def test_agent_prompts_define_complete_output_contracts(self) -> None:
        for field in ("task", "analyzer_alignment", "root_cause_summary", "reasoning_summary", "strongest_evidence", "fix_direction", "grouping_key_hint", "confidence", "unknowns"):
            self.assertIn(f'"{field}"', TASK_SUBAGENT_PROMPT)
        self.assertIn('"env_fail" | "infra_fail"', TASK_SUBAGENT_PROMPT)
        self.assertIn('"high" | "medium" | "low"', TASK_SUBAGENT_PROMPT)

        for field in ("source", "plans", "unplanned_tasks", "generation_errors", "analyzer_scope_comparison", "task_list", "commands", "fix_reason", "verification_hint"):
            self.assertIn(f'"{field}"', MAIN_AGENT_PROMPT)
        self.assertIn('"same" | "narrower" | "broader" | "mixed"', MAIN_AGENT_PROMPT)
        self.assertIn("The output field is named plans, never fix_plans", MAIN_AGENT_PROMPT)
        self.assertIn("input.target_environment", MAIN_AGENT_PROMPT)
        self.assertIn("input.target_context", MAIN_AGENT_PROMPT)
        self.assertIn("already satisfied", MAIN_AGENT_PROMPT)

        for field in ("status", "text", "highlights", "caveats", "generation_errors"):
            self.assertIn(f'"{field}"', REPORT_MAIN_AGENT_PROMPT)
        self.assertIn('status describes summary generation, not the verification outcome', REPORT_MAIN_AGENT_PROMPT)
        self.assertIn('"success | failed"', REPORT_MAIN_AGENT_PROMPT)

    def test_fix_plan_and_report_retries_include_schema_feedback(self) -> None:
        invalid_plan = json.dumps(
            {
                "schema_version": 1,
                "kind": "harbor_fixer_fix_plan_set",
                "fix_plans": [],
                "unplanned_tasks": [],
            }
        )
        plan_invoker = SequenceInvoker([invalid_plan, json.dumps(make_fix_plan())])
        validate_fix_plan_set(
            request_fix_plan(
                plan_invoker,
                {"source": {}, "task_summaries": [], "generation_errors": []},
                self.root / "plan",
            )
        )
        self.assertIn("source must be an object", plan_invoker.records[1][0])

        invalid = make_report_summary("inconclusive")
        valid = make_report_summary()
        report_invoker = SequenceInvoker([json.dumps(invalid), json.dumps(valid)])
        summary, _ = generate_report_summary(
            report_invoker,
            {"schema_version": 1, "kind": "harbor_fixer_report_summary_input"},
            self.root / "report",
        )
        self.assertEqual(summary["status"], "success")
        self.assertIn(
            "report summary status must be one of: failed, success",
            report_invoker.records[1][0],
        )

        class FailingReportInvoker:
            def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
                raise RuntimeError("pi_provider_request_failed:connection_error")

        fallback, _ = generate_report_summary(
            FailingReportInvoker(),
            {
                "schema_version": 1,
                "kind": "harbor_fixer_report_summary_input",
                "status": "not_fixed",
                "old_run": {"analyzer_summary": {"task_count": 5}},
                "new_run": {"summary": {"complete_failed": 5}},
            },
            self.root / "fallback-report",
        )
        self.assertEqual(fallback["status"], "failed")
        self.assertIn("Deterministic fallback summary", fallback["text"])
        self.assertEqual(len(fallback["generation_errors"]), 2)


if __name__ == "__main__":
    unittest.main()
