"""Lightweight validation helpers for Harbor Fixer MVP artifacts."""

from __future__ import annotations

import json
from typing import Any


class ValidationError(ValueError):
    """Raised when an external artifact or agent output is unusable."""


ENV_INFRA_CLASSES = {"env_fail", "infra_fail"}
ANALYZER_SCOPES = {"task", "benchmark", "host"}
FIX_SCOPES = {"task", "benchmark", "host"}
SUMMARY_SCOPES = {"task", "benchmark", "host", "unknown"}
SCOPE_AGREEMENTS = {"agree", "unclear", "disagree"}
CONFIDENCE_LABELS = {"high", "medium", "low"}
def require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    return value


def require_string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{name} must be non-empty")
    return value


def require_enum(value: Any, name: str, allowed: set[str]) -> str:
    text = require_string(value, name)
    if text not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return text


def task_key(task: dict[str, Any]) -> tuple[str, str, Any]:
    return (
        str(task.get("task_index", "")),
        str(task.get("task_name", "")),
        task.get("attempt_id"),
    )


def parse_strict_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc
    return require_dict(payload, "model output")


def _check_kind(payload: dict[str, Any], *, version: int, kind: str, name: str) -> None:
    require_dict(payload, name)
    if payload.get("schema_version") != version:
        raise ValidationError(f"{name} schema_version must be {version}")
    if payload.get("kind") != kind:
        raise ValidationError(f"{name} kind must be {kind}")


def _check_kind_versions(payload: dict[str, Any], *, versions: set[int], kind: str, name: str) -> int:
    require_dict(payload, name)
    version = payload.get("schema_version")
    if version not in versions:
        raise ValidationError(f"{name} schema_version must be one of: {', '.join(str(item) for item in sorted(versions))}")
    if payload.get("kind") != kind:
        raise ValidationError(f"{name} kind must be {kind}")
    return int(version)


def validate_analyzer_report(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=2, kind="harbor_benchmark_root_cause_report", name="analyzer report")
    require_list(payload.get("tasks"), "analyzer report tasks")


def validate_env_infra_tasks(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=2, kind="harbor_env_infra_task_list", name="env/infra task list")
    for index, item in enumerate(require_list(payload.get("tasks"), "env/infra tasks")):
        item_obj = require_dict(item, f"env/infra tasks[{index}]")
        task = require_dict(item_obj.get("task"), f"env/infra tasks[{index}].task")
        require_string(task.get("task_index"), f"env/infra tasks[{index}].task.task_index")
        require_enum(item_obj.get("final_class"), f"env/infra tasks[{index}].final_class", ENV_INFRA_CLASSES)
        require_enum(item_obj.get("scope"), f"env/infra tasks[{index}].scope", ANALYZER_SCOPES)


def validate_task_input(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_task_input", name="task input")
    require_string(require_dict(payload.get("task"), "task input task").get("task_index"), "task.task_index")
    analyzer = require_dict(payload.get("analyzer_result"), "analyzer_result")
    require_enum(analyzer.get("final_class"), "analyzer_result.final_class", ENV_INFRA_CLASSES)
    require_enum(analyzer.get("scope"), "analyzer_result.scope", ANALYZER_SCOPES)
    require_string(analyzer.get("root_cause_code"), "analyzer_result.root_cause_code")
    require_string(analyzer.get("root_cause_summary"), "analyzer_result.root_cause_summary")
    require_list(payload.get("evidence"), "evidence")


def validate_task_summary(payload: dict[str, Any], expected_task: dict[str, Any] | None = None) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_task_summary", name="task summary")
    task = require_dict(payload.get("task"), "task summary task")
    require_string(task.get("task_index"), "task.task_index")
    if expected_task is not None and task_key(task) != task_key(expected_task):
        raise ValidationError("task summary identity does not match task input")
    alignment = require_dict(payload.get("analyzer_alignment"), "analyzer_alignment")
    require_enum(alignment.get("final_class"), "analyzer_alignment.final_class", ENV_INFRA_CLASSES)
    require_enum(alignment.get("analyzer_scope"), "analyzer_alignment.analyzer_scope", ANALYZER_SCOPES)
    require_string(alignment.get("root_cause_code"), "analyzer_alignment.root_cause_code")
    require_enum(alignment.get("scope_agreement"), "analyzer_alignment.scope_agreement", SCOPE_AGREEMENTS)
    require_string(payload.get("root_cause_summary"), "root_cause_summary")
    require_list(payload.get("strongest_evidence"), "strongest_evidence")
    fix_direction = require_dict(payload.get("fix_direction"), "fix_direction")
    require_enum(fix_direction.get("suggested_scope"), "fix_direction.suggested_scope", SUMMARY_SCOPES)
    require_string(fix_direction.get("summary"), "fix_direction.summary")
    require_enum(payload.get("confidence"), "confidence", CONFIDENCE_LABELS)
    require_list(payload.get("unknowns"), "unknowns")


def validate_fix_plan_set(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_fix_plan_set", name="fix plan set")
    require_dict(payload.get("source"), "source")
    seen_plan_ids: set[str] = set()
    for index, plan in enumerate(require_list(payload.get("plans"), "plans")):
        plan_obj = require_dict(plan, f"plans[{index}]")
        plan_id = require_string(plan_obj.get("plan_id"), f"plans[{index}].plan_id")
        if plan_id in seen_plan_ids:
            raise ValidationError(f"duplicate plan_id: {plan_id}")
        seen_plan_ids.add(plan_id)
        require_enum(plan_obj.get("fix_scope"), f"plans[{index}].fix_scope", FIX_SCOPES)
        require_dict(plan_obj.get("analyzer_scope_comparison"), f"plans[{index}].analyzer_scope_comparison")
        if not require_list(plan_obj.get("task_list"), f"plans[{index}].task_list"):
            raise ValidationError(f"plans[{index}].task_list must be non-empty")
        commands = require_list(plan_obj.get("commands"), f"plans[{index}].commands")
        if not commands:
            raise ValidationError(f"plans[{index}].commands must be non-empty")
        for command_index, command in enumerate(commands):
            command_obj = require_dict(command, f"plans[{index}].commands[{command_index}]")
            require_string(command_obj.get("command_id"), f"plans[{index}].commands[{command_index}].command_id")
            require_string(command_obj.get("cwd"), f"plans[{index}].commands[{command_index}].cwd")
            require_string(command_obj.get("command"), f"plans[{index}].commands[{command_index}].command")
        require_dict(plan_obj.get("fix_reason"), f"plans[{index}].fix_reason")
        require_dict(plan_obj.get("verification_hint"), f"plans[{index}].verification_hint")
    require_list(payload.get("unplanned_tasks"), "unplanned_tasks")
    require_list(payload.get("generation_errors"), "generation_errors")
