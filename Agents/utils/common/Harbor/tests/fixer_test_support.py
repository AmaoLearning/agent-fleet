#!/usr/bin/env python3
"""Shared fixtures and test doubles for Harbor Fixer stage tests."""

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

from harbor_fixer.agent_invocation import PiAgentInvoker, PiInvocationConfig  # noqa: E402
from harbor_fixer.analyzer_inputs import build_task_inputs  # noqa: E402
from harbor_fixer.batch import run_batch_plan_from_manifest  # noqa: E402
from harbor_fixer.executor import build_exec_input, run_fix_exec  # noqa: E402
from harbor_fixer.plan_generation import collect_task_summaries, request_fix_plan, run_plan_generation  # noqa: E402
from harbor_fixer.planning_context.runtime_inventory import collect_runtime_inventory  # noqa: E402
from harbor_fixer.planning_context.runtime_inventory import _path_state as inspect_runtime_path  # noqa: E402
from harbor_fixer.planning_context.workspace_evidence import collect_workspace_evidence  # noqa: E402
from harbor_fixer.planning_context.workspace_evidence import _path_state as inspect_workspace_path  # noqa: E402
from harbor_fixer.prompts import MAIN_AGENT_PROMPT, REPORT_MAIN_AGENT_PROMPT, TASK_SUBAGENT_PROMPT  # noqa: E402
from harbor_fixer.reporter import generate_report_summary, run_report_from_paths  # noqa: E402
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


class SequenceInvoker:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.records: list[tuple[str, dict, int, str]] = []

    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        self.records.append((prompt, payload, attempt, label))
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


class SummaryInvoker:
    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        return json.dumps(task_summary_for(payload))


class MainInvoker:
    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
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


class ReportInvoker:
    def __init__(self, output: str | None = None) -> None:
        self.output = output
        self.calls: list[tuple[str, dict, int, str]] = []

    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
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
