"""Shared fixtures and test doubles for Harbor Fixer stage tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class FixerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)


def make_task(index: int) -> dict:
    return {
        "task": {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None},
        "final_class": "env_fail",
        "failure_stage": "environment_setup",
        "scope": "benchmark",
        "confidence": 0.91,
        "root_cause_code": "docker_registry_unavailable",
        "root_cause_summary": "Docker registry is unreachable.",
        "evidence": {
            "path": f"/logs/task-{index}.log",
            "line_start": 10,
            "line_end": 12,
            "fact": "docker pull cannot reach registry",
            "reason": "The environment setup fails before task execution.",
        },
    }


def write_analyzer_fixture(root: Path, count: int = 1) -> Path:
    analyzer_dir = root / "analyzer"
    handover_id = "handover-1"
    publication_id = "publication-current"
    tasks = [make_task(index) for index in range(1, count + 1)]
    env_infra_path = (
        analyzer_dir / "env-infra-tasks" / handover_id / f"{publication_id}.json"
    )
    fix_line_index_path = (
        analyzer_dir / "fix-line-index" / handover_id / f"{publication_id}.jsonl"
    )
    write_json(
        analyzer_dir / "analyzer-artifacts-latest.json",
        {
            "schema_version": 2,
            "kind": "harbor_analyzer_latest_artifacts",
            "handover_id": handover_id,
            "publication_id": publication_id,
            "run_id": "run-1",
            "generated_at": "2026-07-16T00:00:00Z",
            "artifacts": {
                "env_infra_tasks_path": "/stale-copy/env-infra-tasks.json",
                "fix_line_index_path": "/stale-copy/fix-line-index.jsonl",
            },
            "publications": [
                {
                    "handover_id": handover_id,
                    "publication_id": publication_id,
                    "generated_at": "2026-07-16T00:00:00Z",
                    "artifacts": {
                        "env_infra_tasks_path": "/stale-copy/env-infra-tasks.json",
                        "fix_line_index_path": "/stale-copy/fix-line-index.jsonl",
                    },
                }
            ],
        },
    )
    write_json(
        env_infra_path,
        {
            "schema_version": 2,
            "kind": "harbor_env_infra_task_list",
            "handover_id": handover_id,
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
                }
                for task in tasks
            ],
        },
    )
    fix_line_index_path.parent.mkdir(parents=True, exist_ok=True)
    with fix_line_index_path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            ref = dict(task["evidence"])
            ref.update(
                {
                    "schema_version": 2,
                    "kind": "harbor_fix_line_reference",
                    "task": task["task"],
                    "root_cause_code": task["root_cause_code"],
                }
            )
            handle.write(json.dumps(ref) + "\n")
    return analyzer_dir
