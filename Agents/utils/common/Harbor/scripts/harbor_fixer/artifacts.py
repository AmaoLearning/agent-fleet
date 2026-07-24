"""Read Analyzer outputs and build Harbor Fixer task inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .validation import (
    ValidationError,
    task_key,
    validate_analyzer_report,
    validate_env_infra_tasks,
    validate_task_input,
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"expected JSON object: {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValidationError(f"missing JSONL file: {path}") from exc
    for line_number, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSONL record {path}:{line_number}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValidationError(f"expected JSON object at {path}:{line_number}")
        records.append(payload)
    return records


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def resolve_analyzer_paths(analyzer_output_path: Path) -> dict[str, Path]:
    return {
        "analyzer_report_path": analyzer_output_path / "analyzer-report-latest.json",
        "env_infra_tasks_path": analyzer_output_path / "env-infra-tasks-latest.json",
        "fix_line_index_path": analyzer_output_path / "fix-line-index-latest.jsonl",
    }


def _index_report_tasks(report: dict[str, Any]) -> dict[tuple[str, str, Any], dict[str, Any]]:
    indexed: dict[tuple[str, str, Any], dict[str, Any]] = {}
    for task_analysis in report.get("tasks", []):
        if not isinstance(task_analysis, dict):
            continue
        task = task_analysis.get("task")
        if isinstance(task, dict):
            indexed[task_key(task)] = task_analysis
    return indexed


def _index_fix_lines(records: list[dict[str, Any]]) -> dict[tuple[str, str, Any], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str, Any], list[dict[str, Any]]] = {}
    for record in records:
        task = record.get("task")
        if not isinstance(task, dict):
            continue
        indexed.setdefault(task_key(task), []).append(record)
    return indexed


def _identity_from_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": str(task.get("task_index", "")),
        "task_name": str(task.get("task_name", "")),
        "attempt_id": task.get("attempt_id"),
    }


def _evidence_int(record: dict[str, Any], key: str) -> int:
    try:
        return int(record.get(key) or 0)
    except (TypeError, ValueError) as exc:
        task = record.get("task") if isinstance(record.get("task"), dict) else {}
        task_index = task.get("task_index", "<unknown>") if isinstance(task, dict) else "<unknown>"
        raise ValidationError(f"invalid fix-line-index {key} for task_index={task_index}") from exc


def build_task_inputs(analyzer_output_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = resolve_analyzer_paths(analyzer_output_path)
    report = read_json(paths["analyzer_report_path"])
    env_infra = read_json(paths["env_infra_tasks_path"])
    fix_lines = read_jsonl(paths["fix_line_index_path"])
    validate_analyzer_report(report)
    validate_env_infra_tasks(env_infra)

    report_tasks = _index_report_tasks(report)
    fix_line_index = _index_fix_lines(fix_lines)
    source = {
        "analyzer_report_path": str(paths["analyzer_report_path"]),
        "env_infra_tasks_path": str(paths["env_infra_tasks_path"]),
        "fix_line_index_path": str(paths["fix_line_index_path"]),
        "handover_id": str(env_infra.get("handover_id") or report.get("handover_id") or ""),
        "run_id": str(report.get("run_id") or ""),
    }

    task_inputs: list[dict[str, Any]] = []
    for item in env_infra.get("tasks", []):
        task = item.get("task") if isinstance(item, dict) else None
        if not isinstance(task, dict):
            continue
        key = task_key(task)
        report_task = report_tasks.get(key, {})
        evidence_records = fix_line_index.get(key, [])
        evidence = [
            {
                "path": str(record.get("path") or ""),
                "line_start": _evidence_int(record, "line_start"),
                "line_end": _evidence_int(record, "line_end"),
                "fact": str(record.get("fact") or ""),
                "reason": str(record.get("reason") or ""),
                "snippet": str(record.get("snippet") or ""),
                "analysis_report_pointer": str(record.get("analysis_report_pointer") or ""),
                "task_analysis_path": str(record.get("task_analysis_path") or ""),
            }
            for record in evidence_records
        ]
        payload = {
            "schema_version": 1,
            "kind": "harbor_fixer_task_input",
            "source": source,
            "task": _identity_from_task(task),
            "analyzer_result": {
                "final_class": str(item.get("final_class") or ""),
                "failure_stage": str(item.get("failure_stage") or report_task.get("failure_stage") or ""),
                "scope": str(item.get("scope") or report_task.get("scope") or ""),
                "confidence": float(item.get("confidence") if item.get("confidence") is not None else report_task.get("confidence") or 0.0),
                "root_cause_code": str(item.get("root_cause_code") or report_task.get("root_cause_code") or ""),
                "root_cause_summary": str(item.get("root_cause_summary") or report_task.get("root_cause_summary") or ""),
                "reasoning_summary": str(report_task.get("reasoning_summary") or ""),
            },
            "evidence": evidence,
        }
        validate_task_input(payload)
        task_inputs.append(payload)
    return task_inputs, source
