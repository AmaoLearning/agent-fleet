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
EXEC_STATUSES = {"success", "partial_failed", "failed"}
EXEC_COMMAND_STATUSES = {"success", "failed", "skipped"}
MONITOR_POLICIES = {"auto", "on", "off"}
VERIFICATION_STATUSES = {"fixed", "partially_fixed", "not_fixed", "inconclusive", "exec_failed"}
TASK_VERIFICATION_STATUSES = {"fixed", "not_fixed", "unknown", "not_complete", "not_sampled", "exec_failed"}
TASK_COMPLETE_STATUSES = {"complete_success", "complete_failed", "complete_unknown", "not_complete"}
REPORT_SUMMARY_STATUSES = {"success", "failed"}


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


def validate_exec_input(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_exec_input", name="exec input")
    require_string(payload.get("fix_plan_path"), "fix_plan_path")
    require_string(payload.get("workspace_root"), "workspace_root")
    validate_fix_plan_set(require_dict(payload.get("fix_plan"), "fix_plan"))


def validate_exec_result(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_exec_result", name="exec result")
    status = require_enum(payload.get("status"), "status", EXEC_STATUSES)
    plans = require_list(payload.get("plans"), "plans")
    failed_plan_count = 0
    for plan_index, plan in enumerate(plans):
        plan_obj = require_dict(plan, f"plans[{plan_index}]")
        plan_status = require_enum(plan_obj.get("status"), f"plans[{plan_index}].status", {"success", "failed"})
        if plan_status == "failed":
            failed_plan_count += 1
        for command_index, command in enumerate(require_list(plan_obj.get("commands"), f"plans[{plan_index}].commands")):
            command_obj = require_dict(command, f"plans[{plan_index}].commands[{command_index}]")
            command_status = require_enum(command_obj.get("status"), "command.status", EXEC_COMMAND_STATUSES)
            exit_code = command_obj.get("exit_code")
            if command_status == "success" and exit_code != 0:
                raise ValidationError("successful command exit_code must be 0")
            if command_status == "skipped" and exit_code is not None:
                raise ValidationError("skipped command exit_code must be null")
    expected = "success" if failed_plan_count == 0 else "failed" if failed_plan_count == len(plans) else "partial_failed"
    if status != expected:
        raise ValidationError(f"status must be {expected}")


def validate_verification_input(payload: dict[str, Any]) -> None:
    version = _check_kind_versions(payload, versions={1, 2}, kind="harbor_fixer_verification_input", name="verification input")
    for key in ("fix_plan_path", "exec_result_path", "analyzer_output_path", "verification_run_dir", "output_dir"):
        require_string(payload.get(key), key)
    require_enum(payload.get("monitor_policy"), "monitor_policy", MONITOR_POLICIES)
    if version >= 2:
        mode = payload.get("verification_mode")
        if mode != "smoke_test":
            raise ValidationError("verification_mode must be smoke_test")
        try:
            limit = int(payload.get("verification_task_limit_per_plan"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("verification_task_limit_per_plan must be a positive integer") from exc
        if limit <= 0:
            raise ValidationError("verification_task_limit_per_plan must be a positive integer")
    validate_fix_plan_set(require_dict(payload.get("fix_plan"), "fix_plan"))
    validate_exec_result(require_dict(payload.get("exec_result"), "exec_result"))


def _validate_run_record(payload: dict[str, Any], name: str) -> None:
    require_string(payload.get("task_index"), f"{name}.task_index")
    require_enum(payload.get("task_complete_status"), f"{name}.task_complete_status", TASK_COMPLETE_STATUSES)


def validate_verification_result(payload: dict[str, Any]) -> None:
    version = _check_kind_versions(payload, versions={1, 2}, kind="harbor_fixer_verification_result", name="verification result")
    require_enum(payload.get("status"), "status", VERIFICATION_STATUSES)
    require_dict(payload.get("source"), "source")
    require_dict(payload.get("rerun"), "rerun")
    require_dict(payload.get("new_run_summary"), "new_run_summary")
    if version >= 2:
        if payload.get("verification_mode") != "smoke_test":
            raise ValidationError("verification_mode must be smoke_test")
        require_dict(payload.get("sampling"), "sampling")
    for index, plan in enumerate(require_list(payload.get("plan_results"), "plan_results")):
        plan_obj = require_dict(plan, f"plan_results[{index}]")
        require_string(plan_obj.get("plan_id"), f"plan_results[{index}].plan_id")
        require_enum(plan_obj.get("status"), f"plan_results[{index}].status", VERIFICATION_STATUSES)
    for index, task in enumerate(require_list(payload.get("task_results"), "task_results")):
        task_obj = require_dict(task, f"task_results[{index}]")
        require_string(require_dict(task_obj.get("task"), f"task_results[{index}].task").get("task_index"), f"task_results[{index}].task.task_index")
        verification_status = require_enum(task_obj.get("verification_status"), f"task_results[{index}].verification_status", TASK_VERIFICATION_STATUSES)
        new_run = task_obj.get("new_run")
        if verification_status in {"not_sampled", "exec_failed"}:
            if new_run is not None:
                raise ValidationError(f"task_results[{index}].new_run must be null when verification_status is {verification_status}")
        else:
            _validate_run_record(require_dict(new_run, f"task_results[{index}].new_run"), f"task_results[{index}].new_run")
    for index, record in enumerate(require_list(payload.get("non_plan_task_results"), "non_plan_task_results")):
        _validate_run_record(require_dict(record, f"non_plan_task_results[{index}]"), f"non_plan_task_results[{index}]")
    for index, record in enumerate(require_list(payload.get("unexpected_run_task_results", []), "unexpected_run_task_results")):
        _validate_run_record(require_dict(record, f"unexpected_run_task_results[{index}]"), f"unexpected_run_task_results[{index}]")


def validate_report_summary(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_report_summary", name="report summary")
    require_enum(payload.get("status"), "report summary status", REPORT_SUMMARY_STATUSES)
    require_string(payload.get("text"), "report summary text", allow_empty=True)
    require_list(payload.get("highlights"), "report summary highlights")
    require_list(payload.get("caveats"), "report summary caveats")
    require_list(payload.get("generation_errors"), "report summary generation_errors")


def validate_report_input(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_report_input", name="report input")
    require_dict(payload.get("source"), "report input source")
    require_enum(payload.get("baseline_monitor_policy"), "baseline_monitor_policy", MONITOR_POLICIES)
    validate_verification_result(require_dict(payload.get("verification_result"), "verification_result"))
    require_dict(payload.get("old_run"), "old_run")
    require_dict(payload.get("summary_input"), "summary_input")


def _core_task_result(task_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": task_result.get("task"),
        "plan_ids": task_result.get("plan_ids"),
        "sampled": task_result.get("sampled"),
        "sampled_by_plan_ids": task_result.get("sampled_by_plan_ids"),
        "smoke_task_index": task_result.get("smoke_task_index"),
        "old_analyzer": task_result.get("old_analyzer"),
        "exec_status": task_result.get("exec_status"),
        "new_run": task_result.get("new_run"),
        "verification_status": task_result.get("verification_status"),
    }


def _core_run_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": record.get("task_index"),
        "task_name": record.get("task_name"),
        "task_complete_status": record.get("task_complete_status"),
        "task_result_signals": record.get("task_result_signals"),
        "evidence": record.get("evidence"),
        "result_path": record.get("result_path"),
    }


def _require_equal(actual: Any, expected: Any, name: str) -> None:
    if actual != expected:
        raise ValidationError(f"{name} must match verification result")


def _validate_report_matches_verification(payload: dict[str, Any], verification_result: dict[str, Any]) -> None:
    validate_verification_result(verification_result)
    _require_equal(payload.get("status"), verification_result.get("status"), "status")
    new_run = require_dict(payload.get("new_run"), "new_run")
    _require_equal(new_run.get("summary"), verification_result.get("new_run_summary"), "new_run.summary")
    _require_equal(new_run.get("rerun"), verification_result.get("rerun"), "new_run.rerun")
    if "sampling" in verification_result:
        _require_equal(new_run.get("sampling"), verification_result.get("sampling"), "new_run.sampling")
    _require_equal(payload.get("plan_results"), verification_result.get("plan_results"), "plan_results")
    _require_equal(
        [_core_task_result(require_dict(item, "task_result")) for item in require_list(payload.get("task_results"), "task_results")],
        [_core_task_result(require_dict(item, "verification_task_result")) for item in require_list(verification_result.get("task_results"), "verification task_results")],
        "task_results",
    )
    _require_equal(
        [_core_run_record(require_dict(item, "non_plan_result")) for item in require_list(payload.get("non_plan_task_results"), "non_plan_task_results")],
        [_core_run_record(require_dict(item, "verification_non_plan_result")) for item in require_list(verification_result.get("non_plan_task_results"), "verification non_plan_task_results")],
        "non_plan_task_results",
    )
    if "unexpected_run_task_results" in verification_result:
        _require_equal(
            [_core_run_record(require_dict(item, "unexpected_result")) for item in require_list(payload.get("unexpected_run_task_results", []), "unexpected_run_task_results")],
            [_core_run_record(require_dict(item, "verification_unexpected_result")) for item in require_list(verification_result.get("unexpected_run_task_results", []), "verification unexpected_run_task_results")],
            "unexpected_run_task_results",
        )


def validate_fix_report(payload: dict[str, Any], verification_result: dict[str, Any] | None = None) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_report", name="fix report")
    validate_report_summary(require_dict(payload.get("summary"), "summary"))
    require_enum(payload.get("status"), "status", VERIFICATION_STATUSES)
    for key in ("source", "old_run", "new_run", "artifacts"):
        require_dict(payload.get(key), key)
    for key in ("plan_results", "task_results", "non_plan_task_results"):
        require_list(payload.get(key), key)
    if verification_result is not None:
        _validate_report_matches_verification(payload, verification_result)
