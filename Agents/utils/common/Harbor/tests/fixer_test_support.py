#!/usr/bin/env python3
"""Shared fixtures and test doubles for Harbor Fixer stage tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from harbor_fixer.analyzer_inputs import build_task_inputs  # noqa: E402
from harbor_fixer.planning_context.runtime_inventory import collect_runtime_inventory  # noqa: E402
from harbor_fixer.planning_context.runtime_inventory import _path_state as inspect_runtime_path  # noqa: E402
from harbor_fixer.planning_context.workspace_evidence import collect_workspace_evidence  # noqa: E402
from harbor_fixer.planning_context.workspace_evidence import _path_state as inspect_workspace_path  # noqa: E402
from harbor_fixer.validation import ValidationError  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class FixerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)


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
