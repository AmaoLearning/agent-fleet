#!/usr/bin/env python3
"""Lightweight tests for Harbor Fixer MVP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from harbor_fixer.artifacts import build_task_inputs  # noqa: E402
from harbor_fixer.batch import run_batch_plan_from_manifest  # noqa: E402
from harbor_fixer.environment import collect_target_environment  # noqa: E402
from harbor_fixer.environment import _path_state as collect_environment_path_state  # noqa: E402
from harbor_fixer.executor import build_exec_input, run_fix_exec  # noqa: E402
from harbor_fixer.orchestrator import collect_task_summaries, generate_fix_plan, run_stage1  # noqa: E402
from harbor_fixer.prompts import MAIN_AGENT_PROMPT, REPORT_MAIN_AGENT_PROMPT, TASK_SUBAGENT_PROMPT  # noqa: E402
from harbor_fixer.reporter import generate_report_summary, run_report_from_paths  # noqa: E402
from harbor_fixer.runner import PiAgentConfig, PiAgentRunner  # noqa: E402
from harbor_fixer.target_context import collect_target_context  # noqa: E402
from harbor_fixer.target_context import _path_state as collect_context_path_state  # noqa: E402
from harbor_fixer.validation import ValidationError, validate_fix_plan_set, validate_fix_report, validate_task_summary, validate_verification_result  # noqa: E402
from harbor_fixer.verifier import run_verification_from_paths  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def make_task(index: int, *, status: str | None = None) -> dict:
    payload = {
        "task": {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None},
        "analysis_status": "analysis_complete",
        "final_class": "env_fail",
        "failure_stage": "environment_setup",
        "scope": "benchmark",
        "confidence": 0.91,
        "root_cause_code": "docker_registry_unavailable",
        "root_cause_summary": "Docker registry is unreachable.",
        "reasoning_summary": "Docker pull failed before agent work started.",
        "fix_references": [
            {
                "path": f"/logs/task-{index}.log",
                "line_start": 10,
                "line_end": 12,
                "fact": "docker pull cannot reach registry",
                "reason": "The environment setup fails before task execution.",
                "snippet": "cannot reach registry",
            }
        ],
    }
    if status:
        payload["task_complete_status"] = status
    return payload


def write_analyzer_fixture(root: Path, count: int = 1) -> Path:
    analyzer_dir = root / "analyzer"
    tasks = [make_task(index) for index in range(1, count + 1)]
    write_json(
        analyzer_dir / "analyzer-report-latest.json",
        {
            "schema_version": 2,
            "kind": "harbor_benchmark_root_cause_report",
            "handover_id": "handover-1",
            "run_id": "run-1",
            "generated_at": "2026-07-16T00:00:00Z",
            "summary": {"task_count": count},
            "tasks": tasks,
            "analyzer_metadata": {},
        },
    )
    write_json(
        analyzer_dir / "env-infra-tasks-latest.json",
        {
            "schema_version": 2,
            "kind": "harbor_env_infra_task_list",
            "handover_id": "handover-1",
            "generated_at": "2026-07-16T00:00:00Z",
            "task_count": count,
            "tasks": [
                {
                    "task": task["task"],
                    "final_class": task["final_class"],
                    "failure_stage": task["failure_stage"],
                    "scope": task["scope"],
                    "confidence": task["confidence"],
                    "root_cause_code": task["root_cause_code"],
                    "root_cause_summary": task["root_cause_summary"],
                    **({"task_complete_status": task["task_complete_status"]} if "task_complete_status" in task else {}),
                }
                for task in tasks
            ],
        },
    )
    with (analyzer_dir / "fix-line-index-latest.jsonl").open("w", encoding="utf-8") as handle:
        for offset, task in enumerate(tasks):
            ref = dict(task["fix_references"][0])
            ref.update(
                {
                    "schema_version": 2,
                    "kind": "harbor_fix_line_reference",
                    "task": task["task"],
                    "root_cause_code": task["root_cause_code"],
                    "analysis_report_pointer": f"/tasks/{offset}",
                    "task_analysis_path": f"tasks/{task['task']['task_index']}.json",
                }
            )
            handle.write(json.dumps(ref) + "\n")
    return analyzer_dir


def task_summary_for(task_input: dict) -> dict:
    analyzer = task_input["analyzer_result"]
    evidence = task_input["evidence"][0] if task_input["evidence"] else {}
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_task_summary",
        "task": task_input["task"],
        "analyzer_alignment": {
            "final_class": analyzer["final_class"],
            "analyzer_scope": analyzer["scope"],
            "root_cause_code": analyzer["root_cause_code"],
            "scope_agreement": "agree",
        },
        "root_cause_summary": analyzer["root_cause_summary"],
        "reasoning_summary": analyzer["reasoning_summary"],
        "strongest_evidence": [
            {
                "path": evidence.get("path", "/logs/unknown.log"),
                "line_start": evidence.get("line_start", 1),
                "line_end": evidence.get("line_end", 1),
                "summary": evidence.get("fact", "evidence"),
            }
        ],
        "fix_direction": {
            "suggested_scope": "benchmark",
            "summary": "Configure benchmark registry mirror.",
            "why_this_should_fix_it": "The failure happens during Docker pull.",
        },
        "grouping_key_hint": analyzer["root_cause_code"],
        "confidence": "high",
        "unknowns": [],
    }


class SequenceRunner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.records: list[tuple[str, dict, int, str]] = []

    def run(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        self.records.append((prompt, payload, attempt, label))
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


class SummaryRunner:
    def run(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        return json.dumps(task_summary_for(payload))


class MainRunner:
    def run(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        summaries = payload["task_summaries"]
        plan = make_fix_plan()
        plan["source"] = payload["source"]
        plan["plans"][0]["task_list"] = [
            {
                "task_index": summary["task"]["task_index"],
                "task_name": summary["task"]["task_name"],
                "attempt_id": summary["task"]["attempt_id"],
                "root_cause_code": summary["analyzer_alignment"]["root_cause_code"],
                "final_class": summary["analyzer_alignment"]["final_class"],
            }
            for summary in summaries
        ]
        return json.dumps(plan)


class ReportRunner:
    def __init__(self, output: str | None = None) -> None:
        self.output = output
        self.calls: list[tuple[str, dict, int, str]] = []

    def run(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        self.calls.append((prompt, payload, attempt, label))
        return self.output or json.dumps(
            {
                "schema_version": 1,
                "kind": "harbor_fixer_report_summary",
                "status": "success",
                "text": "Fixture fix report summary.",
                "highlights": ["1 planned task fixed"],
                "caveats": [],
                "generation_errors": [],
            }
        )


def make_fix_plan() -> dict:
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_fix_plan_set",
        "source": {"fixture": True},
        "plans": [
            {
                "plan_id": "fix-001",
                "fix_scope": "benchmark",
                "analyzer_scope_comparison": {
                    "analyzer_scopes": ["benchmark"],
                    "relation": "same",
                    "reason": "Fixture plan.",
                },
                "task_list": [
                    {
                        "task_index": "1",
                        "task_name": "task-1",
                        "attempt_id": None,
                        "root_cause_code": "fixture",
                        "final_class": "env_fail",
                    }
                ],
                "commands": [
                    {
                        "command_id": "cmd-001",
                        "cwd": ".",
                        "command": "printf '%s\\n' hello",
                        "purpose": "Emit a harmless test line.",
                        "expected_effect": "stdout contains hello.",
                    }
                ],
                "fix_reason": {"summary": "Fixture fix.", "evidence": [], "reasoning": "Fixture reasoning."},
                "verification_hint": {"expected_original_failure_absent": "fixture failure", "target_task_indexes": ["1"]},
            }
        ],
        "unplanned_tasks": [],
        "generation_errors": [],
    }


def make_exec_result(plan_status: str = "success") -> dict:
    command_status = "success" if plan_status == "success" else "failed"
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_exec_result",
        "source": {"fix_plan_path": "fix-plan-latest.json", "workspace_root": "/workspace"},
        "status": "success" if plan_status == "success" else "failed",
        "plans": [
            {
                "plan_id": "fix-001",
                "status": plan_status,
                "commands": [
                    {
                        "command_id": "cmd-001",
                        "cwd": "/workspace",
                        "command": "true" if command_status == "success" else "false",
                        "purpose": "Fixture command.",
                        "expected_effect": "Fixture effect.",
                        "status": command_status,
                        "exit_code": 0 if command_status == "success" else 1,
                    }
                ],
            }
        ],
    }


def write_harbor_run_fixture(root: Path, task_names: list[str], done_rows: list[tuple[str, str, str, str, str]], failed_rows: list[tuple[str, str, str, str]]) -> Path:
    run_dir = root / "verification-run"
    queue_dir = run_dir / "queue" / "claude-code"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "tasks.txt").write_text("\n".join(task_names) + "\n", encoding="utf-8")
    (queue_dir / "done.txt").write_text("".join("\t".join(row) + "\n" for row in done_rows), encoding="utf-8")
    (queue_dir / "failed.txt").write_text("".join("\t".join(row) + "\n" for row in failed_rows), encoding="utf-8")
    return run_dir


def write_smoke_rerun_script(path: Path, statuses: dict[str, str]) -> Path:
    path.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, pathlib, shutil",
                f"statuses = {json.dumps(statuses, sort_keys=True)}",
                "for inherited_name in ('QUEUE_DIR', 'RUNTIME_DIR', 'JOBS_ROOT', 'HARBOR_MONITOR_DIR', 'NEXT_INDEX_FILE', 'RL_QUEUE_DIR'):",
                "    if inherited_name in os.environ:",
                "        raise SystemExit(f'inherited run path was not cleared: {inherited_name}')",
                "run_dir = pathlib.Path(os.environ['OUTPUT_PATH'])",
                "task_file = pathlib.Path(os.environ['TASK_FILE'])",
                "source_file = pathlib.Path(os.environ['TASK_SOURCE_FILE'])",
                "selection = json.loads(pathlib.Path(os.environ['HARBOR_FIXER_SMOKE_SELECTION']).read_text(encoding='utf-8'))",
                "task_file.parent.mkdir(parents=True, exist_ok=True)",
                "shutil.copyfile(source_file, task_file)",
                "queue_dir = run_dir / 'queue' / 'fixture-agent'",
                "queue_dir.mkdir(parents=True, exist_ok=True)",
                "done_rows = []",
                "failed_rows = []",
                "task_names = task_file.read_text(encoding='utf-8').splitlines()",
                "for task in selection.get('tasks', []):",
                "    smoke_index = str(task.get('smoke_task_index'))",
                "    original_index = str(task.get('original_task_index'))",
                "    task_name = task_names[int(smoke_index) - 1]",
                "    status = statuses.get(original_index, 'success')",
                "    if status == 'success':",
                "        done_rows.append((smoke_index, task_name, '1.0', '', ''))",
                "    elif status == 'failed':",
                "        done_rows.append((smoke_index, task_name, '0.0', '', ''))",
                "    elif status == 'unknown':",
                "        done_rows.append((smoke_index, task_name, '', '', ''))",
                "    elif status == 'hard_failed':",
                "        failed_rows.append((smoke_index, task_name, '1', 'fixture failure'))",
                "    elif status == 'not_complete':",
                "        pass",
                "    else:",
                "        raise SystemExit(f'unknown fixture status: {status}')",
                "(queue_dir / 'done.txt').write_text(''.join('\\t'.join(row) + '\\n' for row in done_rows), encoding='utf-8')",
                "(queue_dir / 'failed.txt').write_text(''.join('\\t'.join(row) + '\\n' for row in failed_rows), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_verification_fixture(root_path: Path) -> tuple[Path, Path, Path]:
    analyzer_dir = write_analyzer_fixture(root_path, count=1)
    output_dir = root_path / "fixer"
    plan_path = root_path / "fix-plan-latest.json"
    exec_path = root_path / "exec-result-latest.json"
    write_json(plan_path, make_fix_plan())
    write_json(exec_path, make_exec_result())
    run_dir = write_harbor_run_fixture(root_path, ["task-1"], [("1", "task-1", "1.0", "", "")], [])
    result = run_verification_from_paths(plan_path, exec_path, analyzer_dir, run_dir, output_dir, monitor_policy="off")
    validate_verification_result(result)
    return analyzer_dir, output_dir, output_dir / "verification-result-latest.json"


def write_fixture_pi(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "if '--version' in sys.argv:",
                "    print('fixture-pi 1.0')",
                "    raise SystemExit(0)",
                "raw = sys.stdin.read()",
                "marker = 'HARBOR_FIXER_INPUT_JSON:'",
                "payload = json.loads(raw.split(marker, 1)[1].strip())",
                "if payload['kind'] == 'harbor_fixer_task_input':",
                "    analyzer = payload['analyzer_result']; evidence = (payload.get('evidence') or [{}])[0]",
                "    result = {",
                "      'schema_version': 1, 'kind': 'harbor_fixer_task_summary', 'task': payload['task'],",
                "      'analyzer_alignment': {'final_class': analyzer['final_class'], 'analyzer_scope': analyzer['scope'], 'root_cause_code': analyzer['root_cause_code'], 'scope_agreement': 'agree'},",
                "      'root_cause_summary': analyzer['root_cause_summary'], 'reasoning_summary': analyzer.get('reasoning_summary', ''),",
                "      'strongest_evidence': [{'path': evidence.get('path', '/tmp/evidence.log'), 'line_start': evidence.get('line_start', 1), 'line_end': evidence.get('line_end', 1), 'summary': evidence.get('fact', 'fixture evidence')}],",
                "      'fix_direction': {'suggested_scope': analyzer['scope'], 'summary': analyzer['root_cause_summary'], 'why_this_should_fix_it': 'fixture smoke test'},",
                "      'grouping_key_hint': analyzer['root_cause_code'], 'confidence': 'high', 'unknowns': []}",
                "elif payload['kind'] == 'harbor_fixer_report_summary_input':",
                "    result = {'schema_version': 1, 'kind': 'harbor_fixer_report_summary', 'status': 'success', 'text': 'cli report summary', 'highlights': [], 'caveats': [], 'generation_errors': []}",
                "else:",
                "    summaries = payload['task_summaries']",
                "    result = {",
                "      'schema_version': 1, 'kind': 'harbor_fixer_fix_plan_set', 'source': payload['source'],",
                "      'plans': [{'plan_id': 'fix-001', 'fix_scope': 'benchmark', 'analyzer_scope_comparison': {'analyzer_scopes': ['benchmark'], 'relation': 'same', 'reason': 'fixture'},",
                "        'task_list': [{'task_index': s['task']['task_index'], 'task_name': s['task']['task_name'], 'attempt_id': s['task']['attempt_id'], 'root_cause_code': s['analyzer_alignment']['root_cause_code'], 'final_class': s['analyzer_alignment']['final_class']} for s in summaries],",
                "        'commands': [{'command_id': 'cmd-001', 'cwd': '.', 'command': \"printf '%s\\\\n' fixture-fix\", 'purpose': 'fixture command', 'expected_effect': 'fixture command runs'}],",
                "        'fix_reason': {'summary': 'fixture shared fix', 'evidence': [], 'reasoning': 'fixture'},",
                "        'verification_hint': {'expected_original_failure_absent': 'fixture failure', 'target_task_indexes': [s['task']['task_index'] for s in summaries]}}],",
                "      'unplanned_tasks': [], 'generation_errors': payload.get('generation_errors', [])}",
                "text = '```json\\n' + json.dumps(result) + '\\n```'",
                "events = [",
                "  {'type': 'session', 'id': 'fixture-session'},",
                "  {'type': 'agent_start'},",
                "  {'type': 'turn_start'},",
                "  {'type': 'message_update', 'message': {'role': 'assistant', 'content': 'intermediate-only'}},",
                "  {'type': 'message_end', 'message': {'role': 'assistant', 'content': text, 'stopReason': 'stop'}},",
                "  {'type': 'turn_end', 'message': {'role': 'assistant', 'content': text, 'stopReason': 'stop'}},",
                "  {'type': 'agent_end'},",
                "]",
                "for event in events:",
                "    print(json.dumps(event), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class HarborFixerTest(unittest.TestCase):
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

    def test_stage1_builds_inputs_and_retries_task_agent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            analyzer_dir = write_analyzer_fixture(Path(root), count=1)
            task_input = build_task_inputs(analyzer_dir)[0][0]
            self.assertEqual(task_input["task"]["task_index"], "1")
            self.assertEqual(task_input["evidence"][0]["analysis_report_pointer"], "/tasks/0")

            good = json.dumps(task_summary_for(task_input))
            runner = SequenceRunner(["not-json", good])
            summaries, errors = collect_task_summaries([task_input], runner, Path(root) / "out")
            self.assertEqual(errors, [])
            self.assertEqual(len(summaries), 1)
            retry_prompt = runner.records[1][0]
            self.assertIn("Validation retry:", retry_prompt)
            self.assertIn("invalid JSON:", retry_prompt)
            self.assertIn("<previous-output>\nnot-json\n</previous-output>", retry_prompt)

            bad_summary = json.loads(json.dumps(task_summary_for(task_input)))
            bad_summary["task"]["task_index"] = "other"
            with self.assertRaises(ValidationError):
                validate_task_summary(bad_summary, expected_task=task_input["task"])

    def test_fix_plan_and_report_retries_include_schema_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            invalid_plan = json.dumps(
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_fix_plan_set",
                    "fix_plans": [],
                    "unplanned_tasks": [],
                }
            )
            plan_runner = SequenceRunner([invalid_plan, json.dumps(make_fix_plan())])
            plan = generate_fix_plan(
                plan_runner,
                {"source": {"fixture": True}, "task_summaries": [], "generation_errors": []},
                root_path / "plan",
            )
            validate_fix_plan_set(plan)
            self.assertIn("source must be an object", plan_runner.records[1][0])
            self.assertIn(invalid_plan, plan_runner.records[1][0])

            invalid_summary = json.dumps(
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_report_summary",
                    "status": "inconclusive",
                    "text": "Verification was inconclusive.",
                    "highlights": [],
                    "caveats": [],
                    "generation_errors": [],
                }
            )
            valid_summary = json.dumps(
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_report_summary",
                    "status": "success",
                    "text": "Verification was inconclusive.",
                    "highlights": [],
                    "caveats": ["verification status is inconclusive"],
                    "generation_errors": [],
                }
            )
            report_runner = SequenceRunner([invalid_summary, valid_summary])
            summary, _ = generate_report_summary(
                report_runner,
                {"schema_version": 1, "kind": "harbor_fixer_report_summary_input"},
                root_path / "report",
            )
            self.assertEqual(summary["status"], "success")
            self.assertIn("report summary status must be one of: failed, success", report_runner.records[1][0])
            self.assertIn(invalid_summary, report_runner.records[1][0])

            class FailingReportRunner:
                def __init__(self) -> None:
                    self.calls = 0

                def run(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
                    self.calls += 1
                    raise RuntimeError("pi_provider_request_failed:connection_error")

            fallback_runner = FailingReportRunner()
            fallback_summary, _ = generate_report_summary(
                fallback_runner,
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_report_summary_input",
                    "status": "not_fixed",
                    "old_run": {
                        "analyzer_summary": {"task_count": 5},
                        "env_infra_task_count": 5,
                    },
                    "new_run": {
                        "summary": {
                            "sampled_task_count": 5,
                            "unsampled_task_count": 0,
                            "complete_success": 0,
                            "complete_failed": 5,
                            "complete_unknown": 0,
                            "not_complete": 0,
                        }
                    },
                },
                root_path / "fallback-report",
            )
            self.assertEqual(fallback_summary["status"], "success")
            self.assertIn("Deterministic fallback summary", fallback_summary["text"])
            self.assertIn(
                "summary generated without report-main-agent due to summary generation failure",
                fallback_summary["caveats"],
            )
            self.assertEqual(len(fallback_summary["generation_errors"]), 2)
            self.assertIn("connection_error", fallback_summary["generation_errors"][0]["error"])

    def test_stage1_run_and_cli_smoke_write_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=2)
            output_dir = root_path / "fixer"

            plan = run_stage1(analyzer_dir, output_dir, SummaryRunner(), MainRunner(), max_concurrency=2)
            validate_fix_plan_set(plan)
            self.assertTrue((output_dir / "fix-plan-latest.json").exists())
            environment = json.loads(
                (output_dir / "target-environment.json").read_text(encoding="utf-8")
            )
            main_input = json.loads(
                (output_dir / "main-agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(environment["kind"], "harbor_fixer_target_environment")
            self.assertEqual(main_input["target_environment"], environment)
            self.assertEqual(len(main_input["target_environment_artifact"]["sha256"]), 64)
            self.assertEqual(
                main_input["target_context"]["kind"],
                "harbor_fixer_target_context",
            )
            self.assertEqual(len(main_input["target_context_artifact"]["sha256"]), 64)
            self.assertTrue((output_dir / "target-context.json").exists())
            self.assertFalse((output_dir / "inspection-agent-input.json").exists())

            cli_out = root_path / "cli-fixer"
            agent_script = write_fixture_pi(root_path / "fixture_pi.py")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "fixer.py"),
                    "--analyzer-output",
                    str(analyzer_dir),
                    "--output-dir",
                    str(cli_out),
                    "--pi-bin",
                    str(agent_script),
                    "--pi-base-url",
                    "https://example.test/v1",
                    "--pi-model",
                    "fixture-model",
                    "--pi-api-key-env",
                    "FIXTURE_PI_API_KEY",
                    "--max-concurrency",
                    "2",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={"PATH": "/usr/bin:/bin", "FIXTURE_PI_API_KEY": "fixture"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cli_out / "fix-plan-latest.json").exists())
            task_provenance_paths = sorted(
                (cli_out / "pi-agent-provenance").glob("task-*/attempt-1.json")
            )
            self.assertEqual(len(task_provenance_paths), 2)
            for provenance_path in task_provenance_paths:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                self.assertEqual(provenance["thinking_level"], "off")
                self.assertEqual(
                    provenance["discarded_event_counts"]["message_update"],
                    1,
                )
            provenance = json.loads(
                (
                    cli_out
                    / "pi-agent-provenance"
                    / "main-agent"
                    / "attempt-1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["thinking_level"], "default")
            self.assertEqual(provenance["discarded_event_counts"]["message_update"], 1)
            main_events = (
                cli_out
                / "pi-agent-events"
                / "main-agent"
                / "attempt-1.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn("message_update", main_events)
            self.assertNotIn("intermediate-only", main_events)
            self.assertFalse((cli_out / "pi-agent-provenance" / "inspection-agent").exists())

    def test_exec_preserves_order_logs_and_failure_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            output_dir = root_path / "fixer"
            plan = make_fix_plan()
            plan["plans"] = [
                {
                    **plan["plans"][0],
                    "commands": [
                        {"command_id": "cmd-001", "cwd": ".", "command": "printf 1 >> order.txt", "purpose": "x", "expected_effect": "x"},
                        {"command_id": "cmd-fail", "cwd": ".", "command": "printf 2 >> order.txt; exit 7", "purpose": "x", "expected_effect": "x"},
                        {"command_id": "cmd-skip", "cwd": ".", "command": "printf X >> order.txt", "purpose": "x", "expected_effect": "x"},
                    ],
                },
                {
                    **plan["plans"][0],
                    "plan_id": "fix-002",
                    "commands": [{"command_id": "cmd-003", "cwd": ".", "command": "printf 3 >> order.txt", "purpose": "x", "expected_effect": "x"}],
                },
            ]
            plan_path = root_path / "fix-plan-latest.json"
            write_json(plan_path, plan)

            result = run_fix_exec(build_exec_input(plan_path, workspace), output_dir)

            self.assertEqual(result["status"], "partial_failed")
            self.assertEqual((workspace / "order.txt").read_text(encoding="utf-8"), "123")
            self.assertEqual(result["plans"][0]["commands"][2]["status"], "skipped")
            self.assertIn("previous command", result["plans"][0]["commands"][2]["skip_reason"])
            self.assertTrue((output_dir / result["plans"][0]["commands"][0]["stdout_path"]).exists())

    def test_exec_missing_or_inaccessible_cwd_records_failed_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["commands"][0]["cwd"] = "missing"
            write_json(plan_path, plan)
            self.assertEqual(run_fix_exec(build_exec_input(plan_path, workspace), output_dir)["status"], "failed")

            plan["plans"][0]["commands"][0]["cwd"] = "."
            write_json(plan_path, plan)
            original_is_dir = Path.is_dir

            def fake_is_dir(path: Path) -> bool:
                if path == workspace.resolve():
                    raise OSError("no access")
                return original_is_dir(path)

            with mock.patch("harbor_fixer.executor.Path.is_dir", fake_is_dir):
                result = run_fix_exec(build_exec_input(plan_path, workspace), root_path / "fixer-inaccessible")
            self.assertEqual(result["plans"][0]["commands"][0]["status"], "failed")

    def test_verification_smoke_samples_two_and_marks_unsampled(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=4)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 5)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = root_path / "verification-run"
            rerun_script = write_smoke_rerun_script(root_path / "smoke_rerun.py", {})

            with mock.patch.dict(
                "os.environ",
                {
                    "QUEUE_DIR": str(root_path / "old-run" / "queue"),
                    "RUNTIME_DIR": str(root_path / "old-run" / "runtime"),
                    "JOBS_ROOT": str(root_path / "old-run" / "jobs"),
                    "HARBOR_MONITOR_DIR": str(root_path / "old-run" / "monitor"),
                    "NEXT_INDEX_FILE": str(root_path / "old-run" / "queue" / "next_index"),
                    "RL_QUEUE_DIR": str(root_path / "old-run" / "runtime" / "rl-queue"),
                },
            ):
                result = run_verification_from_paths(
                    plan_path,
                    exec_path,
                    analyzer_dir,
                    run_dir,
                    output_dir,
                    rerun_command=f"{sys.executable} {rerun_script}",
                    monitor_policy="off",
                )

            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["verification_mode"], "smoke_test")
            self.assertEqual(result["status"], "fixed")
            self.assertEqual(result["sampling"]["limit_per_plan"], 2)
            self.assertEqual(result["sampling"]["sampled_task_count"], 2)
            self.assertEqual(result["new_run_summary"]["scope"], "smoke_sample")
            self.assertEqual(result["new_run_summary"]["total"], 2)
            self.assertTrue((output_dir / "verification-smoke-tasks.txt").exists())
            self.assertTrue((output_dir / "verification-smoke-selection.json").exists())
            by_index = {item["task"]["task_index"]: item["verification_status"] for item in result["task_results"]}
            self.assertEqual(sum(1 for status in by_index.values() if status == "fixed"), 2)
            self.assertEqual(sum(1 for status in by_index.values() if status == "not_sampled"), 2)
            self.assertTrue(all(item["new_run"] is None for item in result["task_results"] if item["verification_status"] == "not_sampled"))

    def test_verification_classifies_sampled_plan_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=4)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 5)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = root_path / "verification-run"
            rerun_script = write_smoke_rerun_script(
                root_path / "smoke_rerun.py",
                {"1": "success", "2": "failed", "3": "unknown", "4": "not_complete"},
            )

            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} {rerun_script}",
                monitor_policy="off",
                verification_task_limit_per_plan=4,
            )

            by_index = {item["task"]["task_index"]: item["verification_status"] for item in result["task_results"]}
            self.assertEqual(by_index, {"1": "fixed", "2": "not_fixed", "3": "unknown", "4": "not_complete"})
            self.assertEqual(result["new_run_summary"]["total"], 4)

    def test_verification_smoke_mismatch_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=2)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            plan = make_fix_plan()
            plan["plans"][0]["task_list"] = [
                {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None, "root_cause_code": "fixture", "final_class": "env_fail"}
                for index in range(1, 3)
            ]
            write_json(plan_path, plan)
            write_json(exec_path, make_exec_result())
            run_dir = write_harbor_run_fixture(root_path, ["wrong-task"], [("1", "wrong-task", "1.0", "", "")], [])

            result = run_verification_from_paths(plan_path, exec_path, analyzer_dir, run_dir, output_dir, monitor_policy="off", verification_task_limit_per_plan=1)

            self.assertEqual(result["status"], "inconclusive")
            self.assertTrue(result["sampling"]["mapping_errors"])

    def test_verification_handles_exec_and_rerun_failures(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path, count=1)
            output_dir = root_path / "fixer"
            plan_path = root_path / "fix-plan-latest.json"
            exec_path = root_path / "exec-result-latest.json"
            write_json(plan_path, make_fix_plan())
            run_dir = write_harbor_run_fixture(root_path, ["task-1"], [("1", "task-1", "1.0", "", "")], [])

            write_json(exec_path, make_exec_result(plan_status="failed"))
            skipped_rerun = write_smoke_rerun_script(root_path / "skipped_rerun.py", {})
            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} {skipped_rerun}",
                monitor_policy="auto",
            )
            self.assertEqual(result["status"], "exec_failed")
            self.assertEqual(result["rerun"]["skipped_reason"], "no_sampled_tasks")
            self.assertFalse(result["rerun"]["monitor_available"])
            self.assertEqual(result["new_run_summary"]["total"], 0)
            self.assertEqual(result["new_run_summary"]["success_rate"], 0.0)

            write_json(exec_path, make_exec_result())
            result = run_verification_from_paths(
                plan_path,
                exec_path,
                analyzer_dir,
                run_dir,
                output_dir,
                rerun_command=f"{sys.executable} -c 'import sys; sys.exit(9)'",
                monitor_policy="off",
            )
            self.assertEqual(result["status"], "inconclusive")

    def test_verification_rerun_supports_relative_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = write_analyzer_fixture(root_path / "analyzer", count=1)
            write_json(root_path / "fix-plan.json", make_fix_plan())
            write_json(root_path / "exec-result.json", make_exec_result())
            rerun_script = write_smoke_rerun_script(root_path / "smoke_rerun.py", {})

            previous_cwd = Path.cwd()
            try:
                os.chdir(root_path)
                result = run_verification_from_paths(
                    Path("fix-plan.json"),
                    Path("exec-result.json"),
                    analyzer_dir.relative_to(root_path),
                    Path("verification-run"),
                    Path("fixer-output"),
                    rerun_command=f"{sys.executable} {rerun_script}",
                    monitor_policy="off",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["status"], "fixed")
            self.assertEqual(result["rerun"]["exit_code"], 0)

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
            runner = ReportRunner()

            result = run_report_from_paths(verification_path, analyzer_dir, output_dir, runner, baseline_monitor_policy="off")

            validate_fix_report(result)
            self.assertTrue((output_dir / "fix-report-latest.json").read_text(encoding="utf-8").startswith('{\n  "summary"'))
            self.assertEqual(result["summary"]["text"], "Fixture fix report summary.")
            self.assertEqual(result["task_results"][0]["verification_status"], "fixed")
            self.assertEqual(runner.calls[0][3], "report-main-agent")
            self.assertEqual(runner.calls[0][1]["kind"], "harbor_fixer_report_summary_input")
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

            result = run_report_from_paths(verification_path, analyzer_dir, output_dir, ReportRunner("not json"), baseline_run_dir=baseline_run_dir)

            self.assertEqual(result["summary"]["status"], "success")
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
                ReportRunner(),
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

    def test_batch_plan_success_and_failure_continue(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_good = write_analyzer_fixture(root_path / "bench-good", count=1)
            output_good = root_path / "fixer-good"
            output_bad = root_path / "fixer-bad"
            manifest = root_path / "batch-manifest.json"
            write_json(
                manifest,
                {
                    "schema_version": 1,
                    "kind": "harbor_fixer_batch_manifest",
                    "benchmarks": [
                        {"benchmark_id": "bad", "analyzer_output": str(root_path / "missing"), "output_dir": str(output_bad)},
                        {"benchmark_id": "good", "analyzer_output": str(analyzer_good), "output_dir": str(output_good)},
                    ],
                },
            )
            agent_script = write_fixture_pi(root_path / "fixture_pi.py")

            with mock.patch.dict("os.environ", {"FIXTURE_PI_API_KEY": "fixture"}):
                result = run_batch_plan_from_manifest(
                    manifest,
                    root_path / "batch-output",
                    pi_config=PiAgentConfig(
                        pi_bin=str(agent_script),
                        base_url="https://example.test/v1",
                        model="fixture-model",
                        api_key_env="FIXTURE_PI_API_KEY",
                    ),
                    max_concurrency=2,
                    benchmark_concurrency=2,
                )

            self.assertEqual(result["status"], "partial_failed")
            self.assertEqual({item["benchmark_id"]: item["status"] for item in result["results"]}, {"bad": "failed", "good": "success"})
            self.assertTrue((output_good / "fix-plan-latest.json").exists())
            task_provenance = next(
                (output_good / "pi-agent-provenance").glob("task-*/attempt-1.json")
            )
            self.assertEqual(
                json.loads(task_provenance.read_text(encoding="utf-8"))["thinking_level"],
                "off",
            )

    def test_pi_runner_requires_explicit_model(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            fixture_pi = write_fixture_pi(root_path / "fixture_pi.py")
            runner = PiAgentRunner(
                root_path / "out",
                PiAgentConfig(
                    pi_bin=str(fixture_pi),
                    base_url="https://example.test/v1",
                    api_key_env="FIXTURE_PI_API_KEY",
                ),
            )

            with mock.patch.dict("os.environ", {"FIXTURE_PI_API_KEY": "fixture"}):
                with self.assertRaisesRegex(RuntimeError, "pi_model_not_configured"):
                    runner.run("Return JSON only.", {"kind": "fixture"}, attempt=1, label="fixture")

    def test_target_environment_rejects_missing_workspace_and_records_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            evidence = root_path / "evidence.log"
            evidence.write_text("fixture\n", encoding="utf-8")
            task_inputs = [{"evidence": [{"path": str(evidence)}]}]

            snapshot = collect_target_environment(
                root_path,
                root_path,
                task_inputs,
                pi_bin=sys.executable,
            )

            self.assertEqual(snapshot["kind"], "harbor_fixer_target_environment")
            self.assertTrue(snapshot["repository_paths"]["workspace_root"]["readable"])
            self.assertEqual(snapshot["evidence_files"]["total_count"], 1)
            self.assertEqual(snapshot["evidence_files"]["paths"][0]["type"], "file")
            self.assertEqual(snapshot["commands"]["pi"]["path"], sys.executable)
            serialized = json.dumps(snapshot)
            self.assertNotIn("API_KEY", serialized)
            self.assertNotIn("BASE_URL", serialized)

            with self.assertRaisesRegex(ValidationError, "workspace root"):
                collect_target_environment(root_path / "missing", root_path, [])

    def test_path_probes_degrade_permission_errors_to_unavailable(self) -> None:
        inaccessible = Path("/home/other-user/private/evidence.log")
        with mock.patch.object(
            Path,
            "exists",
            side_effect=PermissionError(13, "Permission denied", str(inaccessible)),
        ):
            environment_state = collect_environment_path_state(inaccessible)
            context_state = collect_context_path_state(inaccessible)

        for state in (environment_state, context_state):
            self.assertEqual(state["path"], str(inaccessible))
            self.assertEqual(state["status"], "unavailable")
            self.assertEqual(state["reason"], "path_unavailable:PermissionError")
            self.assertFalse(state["readable"])

    def test_target_context_is_deterministic_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            analyzer = root_path / "analyzer"
            workspace.mkdir()
            analyzer.mkdir()
            (workspace / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\npassword = "manifest-secret"\n',
                encoding="utf-8",
            )
            evidence = root_path / "evidence.log"
            evidence.write_text(
                "before\nAPI_KEY=super-secret-value\nfailure line\nafter\n",
                encoding="utf-8",
            )
            secret_evidence = workspace / ".env"
            secret_evidence.write_text("TOKEN=hidden\n", encoding="utf-8")
            task_inputs = [
                {
                    "task": {
                        "task_index": "1",
                        "task_name": "fixture",
                        "attempt_id": None,
                    },
                    "evidence": [
                        {
                            "path": str(evidence),
                            "line_start": 3,
                            "line_end": 3,
                        },
                        {
                            "path": str(secret_evidence),
                            "line_start": 1,
                            "line_end": 1,
                        },
                    ],
                }
            ]

            first = collect_target_context(workspace, analyzer, task_inputs)
            second = collect_target_context(workspace, analyzer, task_inputs)

            self.assertEqual(first, second)
            self.assertEqual(first["kind"], "harbor_fixer_target_context")
            self.assertEqual(
                first["workspace"]["project_manifests"][0]["path"],
                str(workspace / "pyproject.toml"),
            )
            serialized = json.dumps(first)
            self.assertNotIn("manifest-secret", serialized)
            self.assertNotIn("super-secret-value", serialized)
            self.assertIn("<REDACTED>", serialized)
            self.assertEqual(first["evidence_excerpts"][0]["status"], "success")
            self.assertIn("failure line", first["evidence_excerpts"][0]["excerpt"])
            self.assertEqual(first["evidence_excerpts"][1]["reason"], "sensitive_path")

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
