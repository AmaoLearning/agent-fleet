"""Stage 4 Fix Report implementation for Harbor Fixer MVP."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_invocation import AgentInvoker
from .analyzer_inputs import resolve_analyzer_paths
from .artifact_io import read_json, write_json, write_text
from .harbor_run_state import collect_task_results, generate_monitor_snapshot, read_monitor_snapshot
from .prompts import REPORT_MAIN_AGENT_PROMPT, build_validation_retry_prompt
from .validation import (
    TASK_COMPLETE_STATUSES,
    ValidationError,
    parse_strict_json_object,
    task_key,
    validate_analyzer_report,
    validate_env_infra_tasks,
    validate_fix_report,
    validate_report_input,
    validate_report_summary,
    validate_verification_result,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_ordered_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


SECRET_NAME_PATTERN = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|password|client[_-]?secret|"
    r"secret[_-]?access[_-]?key|secret|private[_-]?key)"
)
SECRET_VALUE_PATTERN = r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"\\]+)"
SECRET_ASSIGNMENT_RE = re.compile(
    rf"((?<![a-z0-9_-])(?:--|['\"])?{SECRET_NAME_PATTERN}['\"]?\s*[=:]\s*)"
    rf"({SECRET_VALUE_PATTERN})",
    re.IGNORECASE,
)
SECRET_OPTION_RE = re.compile(
    rf"((?<![a-z0-9_-])--{SECRET_NAME_PATTERN}\s+)({SECRET_VALUE_PATTERN})",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+)[^\s'\"\\]+")


def _redact_human_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", text)
    text = SECRET_OPTION_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", text)
    return BEARER_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", text)


def _bounded_human_text(value: Any, limit: int = 4000) -> str:
    text = _redact_human_text(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n<TRUNCATED>"


def _markdown_cell(value: Any) -> str:
    text = _bounded_human_text(value, 2000)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _markdown_code(value: Any, language: str = "") -> str:
    text = _bounded_human_text(value, 12000)
    fence = "````" if "```" in text else "```"
    return f"{fence}{language}\n{text}\n{fence}"


def _markdown_quote(value: Any) -> str:
    text = _bounded_human_text(value)
    if not text:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def _read_optional_artifact(path_value: Any) -> tuple[dict[str, Any], str]:
    value = str(path_value or "")
    if not value:
        return {}, "artifact path is empty"
    path = Path(value)
    if not path.is_file():
        return {}, f"artifact is missing or unreadable: {path}"
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"artifact could not be read: {path}: {exc.__class__.__name__}: {exc}"
    return payload, ""


def _plan_by_id(fix_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("plan_id") or ""): item
        for item in fix_plan.get("plans", [])
        if isinstance(item, dict) and item.get("plan_id")
    }


def _exec_by_plan_id(exec_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("plan_id") or ""): item
        for item in exec_result.get("plans", [])
        if isinstance(item, dict) and item.get("plan_id")
    }


def _run_summary_rows(report: dict[str, Any]) -> list[list[Any]]:
    old_summary = report.get("old_run", {}).get("monitor_summary", {})
    new_summary = report.get("new_run", {}).get("summary", {})
    rows: list[list[Any]] = []
    for label, key in (
        ("Total", "total"),
        ("Complete success", "complete_success"),
        ("Complete failed", "complete_failed"),
        ("Complete unknown", "complete_unknown"),
        ("Not complete", "not_complete"),
        ("Success rate", "success_rate"),
    ):
        rows.append([label, old_summary.get(key, 0), new_summary.get(key, 0)])
    return rows


def _problem_rows(report: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in report.get("old_run", {}).get("tasks", []):
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        analyzer = item.get("analyzer") if isinstance(item.get("analyzer"), dict) else {}
        rows.append(
            [
                task.get("task_index", ""),
                task.get("task_name", ""),
                item.get("old_run_status", ""),
                analyzer.get("final_class", ""),
                analyzer.get("failure_stage", ""),
                analyzer.get("scope", ""),
                analyzer.get("root_cause_code", ""),
                analyzer.get("root_cause_summary", ""),
            ]
        )
    return rows


def _task_result_rows(report: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in report.get("task_results", []):
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
        evidence = new_run.get("evidence") if isinstance(new_run.get("evidence"), dict) else {}
        rows.append(
            [
                task.get("task_index", ""),
                task.get("task_name", ""),
                ", ".join(str(value) for value in item.get("plan_ids", [])),
                "yes" if item.get("sampled") else "no",
                item.get("old_run_status", ""),
                item.get("exec_status", ""),
                new_run.get("task_complete_status", "not run"),
                evidence.get("reward_raw", ""),
                evidence.get("exception_type", ""),
                item.get("verification_status", ""),
            ]
        )
    return rows


def _failure_rows(
    report: dict[str, Any],
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
    artifact_errors: list[str],
) -> list[list[Any]]:
    rows: list[list[Any]] = [["artifact", "-", error] for error in artifact_errors if error]
    for error in fix_plan.get("generation_errors", []):
        rows.append(["plan", "-", json.dumps(error, ensure_ascii=False)])
    for item in fix_plan.get("unplanned_tasks", []):
        if isinstance(item, dict):
            rows.append(
                [
                    "plan",
                    str(item.get("task_index") or item.get("task_name") or "-"),
                    item.get("reason", "task was not assigned to a fix plan"),
                ]
            )
    for plan in exec_result.get("plans", []):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "-")
        for command in plan.get("commands", []):
            if not isinstance(command, dict) or command.get("status") == "success":
                continue
            reason = (
                command.get("stderr_summary")
                or command.get("stdout_summary")
                or command.get("skip_reason")
                or f"command exited with {command.get('exit_code')}"
            )
            rows.append(["exec", f"{plan_id}/{command.get('command_id', '-')}", reason])
    rerun = report.get("new_run", {}).get("rerun", {})
    if rerun.get("skipped_reason"):
        rows.append(["verification", "rerun", f"rerun skipped: {rerun['skipped_reason']}"])
    elif rerun.get("exit_code") not in {None, 0}:
        rows.append(
            [
                "verification",
                "rerun",
                rerun.get("stderr_summary")
                or rerun.get("stdout_summary")
                or f"rerun exited with {rerun.get('exit_code')}",
            ]
        )
    if rerun.get("monitor_timed_out"):
        rows.append(["verification", "monitor", "monitor wait timed out"])
    for item in report.get("task_results", []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("verification_status") or "")
        if status in {"fixed", "not_sampled"}:
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
        evidence = new_run.get("evidence") if isinstance(new_run.get("evidence"), dict) else {}
        signals = new_run.get("task_result_signals")
        details = [
            f"verification_status={status or 'unknown'}",
            f"task_complete_status={new_run.get('task_complete_status') or 'not run'}",
        ]
        if evidence.get("rc") not in {None, ""}:
            details.append(f"rc={evidence['rc']}")
        if evidence.get("exception_type"):
            details.append(f"exception={evidence['exception_type']}")
        if evidence.get("early_stop_reason"):
            details.append(f"early_stop={evidence['early_stop_reason']}")
        if isinstance(signals, list) and signals:
            details.append("signals=" + ",".join(str(value) for value in signals))
        rows.append(
            [
                "verification",
                str(task.get("task_index") or task.get("task_name") or "-"),
                "; ".join(details),
            ]
        )
    sampling = report.get("new_run", {}).get("sampling", {})
    for error in sampling.get("selection_errors", []):
        rows.append(["verification", "sampling", json.dumps(error, ensure_ascii=False)])
    for error in sampling.get("mapping_errors", []):
        rows.append(["verification", "mapping", json.dumps(error, ensure_ascii=False)])
    for error in report.get("summary", {}).get("generation_errors", []):
        rows.append(["report", "summary", json.dumps(error, ensure_ascii=False)])
    return rows


def render_human_report(
    report: dict[str, Any],
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
    *,
    artifact_errors: list[str] | None = None,
) -> str:
    """Render the code-owned Fixer facts as a human-oriented Markdown report."""

    artifact_errors = artifact_errors or []
    old_run = report.get("old_run", {})
    summary = report.get("summary", {})
    rerun = report.get("new_run", {}).get("rerun", {})
    plans = _plan_by_id(fix_plan)
    exec_plans = _exec_by_plan_id(exec_result)
    new_run = report.get("new_run", {})
    new_summary = new_run.get("summary", {})
    sampling = new_run.get("sampling", {})
    lines = [
        f"# Harbor Fixer Report: {_markdown_cell(old_run.get('run_id') or 'unknown run')}",
        "",
        _markdown_table(
            ["Field", "Value"],
            [
                ["Overall status", report.get("status", "")],
                ["Generated at", report.get("generated_at", "")],
                ["Verification mode", new_run.get("verification_mode", "")],
                ["Planned tasks", new_summary.get("plan_task_count", sampling.get("plan_task_count", 0))],
                ["Sampled tasks", new_summary.get("sampled_task_count", sampling.get("sampled_task_count", 0))],
                ["Unsampled tasks", new_summary.get("unsampled_task_count", sampling.get("unsampled_task_count", 0))],
            ],
        ),
        "",
        "## Human summary",
        "",
        _bounded_human_text(summary.get("text")) or "No model-generated summary was available.",
    ]
    if summary.get("highlights"):
        lines.extend(
            [
                "",
                "### Highlights",
                "",
                _markdown_table(
                    ["#","Result"],
                    [[index, value] for index, value in enumerate(summary["highlights"], 1)],
                ),
            ]
        )
    if summary.get("caveats"):
        lines.extend(
            [
                "",
                "### Caveats",
                "",
                _markdown_table(
                    ["#","Caveat"],
                    [[index, value] for index, value in enumerate(summary["caveats"], 1)],
                ),
            ]
        )

    lines.extend(["", "## Problems and root causes", ""])
    problem_rows = _problem_rows(report)
    lines.append(
        _markdown_table(
            ["Task", "Name", "Original status", "Class", "Stage", "Scope", "Root cause", "Details"],
            problem_rows,
        )
        if problem_rows
        else "No Analyzer env/infra task details were available."
    )

    lines.extend(["", "## Fix approach and suggested commands", ""])
    if not plans:
        lines.append(_markdown_quote("No readable fix plan was available."))
    for plan_id, plan in plans.items():
        fix_reason = plan.get("fix_reason") if isinstance(plan.get("fix_reason"), dict) else {}
        scope_comparison = (
            plan.get("analyzer_scope_comparison")
            if isinstance(plan.get("analyzer_scope_comparison"), dict)
            else {}
        )
        target_tasks = ", ".join(
            str(item.get("task_index") or item.get("task_name") or "")
            for item in plan.get("task_list", [])
            if isinstance(item, dict)
        )
        lines.extend(
            [
                f"### {plan_id}",
                "",
                _markdown_table(
                    ["Field", "Value"],
                    [
                        ["Fix scope", plan.get("fix_scope", "")],
                        ["Target tasks", target_tasks],
                        ["Analyzer-scope relation", scope_comparison.get("relation", "")],
                        ["Execution status", exec_plans.get(plan_id, {}).get("status", "not recorded")],
                    ],
                ),
                "",
                "**Approach**",
                "",
                _bounded_human_text(fix_reason.get("summary")) or "No approach summary was recorded.",
                "",
                "**Root-cause reasoning**",
                "",
                _bounded_human_text(fix_reason.get("reasoning"))
                or _bounded_human_text(scope_comparison.get("reason"))
                or "No plan reasoning was recorded.",
            ]
        )
        for command in plan.get("commands", []):
            if not isinstance(command, dict):
                continue
            lines.extend(
                [
                    "",
                    f"#### {command.get('command_id', 'command')}",
                    "",
                    _markdown_table(
                        ["Working directory", "Purpose", "Expected effect"],
                        [[command.get("cwd", ""), command.get("purpose", ""), command.get("expected_effect", "")]],
                    ),
                    "",
                    _markdown_code(command.get("command", ""), "bash"),
                ]
            )

    lines.extend(["", "## Trial execution", ""])
    exec_rows: list[list[Any]] = []
    for plan in exec_result.get("plans", []):
        if not isinstance(plan, dict):
            continue
        for command in plan.get("commands", []):
            if not isinstance(command, dict):
                continue
            exec_rows.append(
                [
                    plan.get("plan_id", ""),
                    command.get("command_id", ""),
                    command.get("status", ""),
                    command.get("exit_code", ""),
                    command.get("duration_ms", ""),
                    command.get("purpose", ""),
                ]
            )
    lines.append(
        _markdown_table(
            ["Plan", "Command", "Status", "Exit code", "Duration ms", "Purpose"],
            exec_rows,
        )
        if exec_rows
        else "No command execution results were available."
    )
    for plan in exec_result.get("plans", []):
        if not isinstance(plan, dict):
            continue
        for command in plan.get("commands", []):
            if not isinstance(command, dict) or command.get("status") == "success":
                continue
            evidence = command.get("stderr_summary") or command.get("stdout_summary") or command.get("skip_reason")
            if evidence:
                lines.extend(
                    [
                        "",
                        f"**{plan.get('plan_id', '-')}/{command.get('command_id', '-')} failure evidence**",
                        "",
                        _markdown_quote(evidence),
                    ]
                )

    lines.extend(
        [
            "",
            "## Verification",
            "",
            "### Before and after",
            "",
            _markdown_table(["Metric", "Before", "After"], _run_summary_rows(report)),
            "",
            "### Rerun",
            "",
            _markdown_table(
                ["Command", "Exit code", "Skipped reason", "Monitor available", "Monitor timed out", "Duration ms"],
                [[
                    rerun.get("command", ""),
                    rerun.get("exit_code", ""),
                    rerun.get("skipped_reason", ""),
                    rerun.get("monitor_available", False),
                    rerun.get("monitor_timed_out", False),
                    rerun.get("duration_ms", 0),
                ]],
            ),
            "",
            "### Sampled task results",
            "",
        ]
    )
    task_rows = _task_result_rows(report)
    lines.append(
        _markdown_table(
            [
                "Task",
                "Name",
                "Plans",
                "Sampled",
                "Before",
                "Exec",
                "After",
                "Reward",
                "Exception",
                "Verification",
            ],
            task_rows,
        )
        if task_rows
        else "No planned task results were available."
    )
    rerun_evidence = rerun.get("stderr_summary") or rerun.get("stdout_summary")
    if rerun_evidence:
        lines.extend(["", "### Rerun evidence", "", _markdown_quote(rerun_evidence)])

    lines.extend(["", "## Failures and interruptions", ""])
    failure_rows = _failure_rows(report, fix_plan, exec_result, artifact_errors)
    lines.append(
        _markdown_table(["Stage", "Item", "Cause"], failure_rows)
        if failure_rows
        else "No plan-generation, execution, rerun, mapping, monitor, or report-generation error was recorded."
    )

    lines.extend(["", "## Artifacts", ""])
    artifact_rows = [
        [name, value]
        for name, value in report.get("artifacts", {}).items()
        if value and name != "raw_summary_output_paths"
    ]
    lines.append(_markdown_table(["Artifact", "Path"], artifact_rows))
    return "\n".join(lines).rstrip() + "\n"


def _task_identity(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def _index_report_tasks(report: dict[str, Any]) -> dict[tuple[str, str, Any], dict[str, Any]]:
    return {
        task_key(task): item
        for item in report.get("tasks", [])
        if isinstance(item, dict) and isinstance((task := item.get("task")), dict)
    }


def _empty_run_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }


def _explicit_status(env_task: dict[str, Any], report_task: dict[str, Any]) -> tuple[str, str]:
    for source, value in (
        ("env_infra_task.task_complete_status", env_task.get("task_complete_status")),
        ("env_infra_task.complete_status", env_task.get("complete_status")),
        ("env_infra_task.status", env_task.get("status")),
        ("analyzer_report_task.task_complete_status", report_task.get("task_complete_status")),
        ("analyzer_report_task.complete_status", report_task.get("complete_status")),
        ("analyzer_report_task.status", report_task.get("status")),
    ):
        if isinstance(value, str) and value in TASK_COMPLETE_STATUSES:
            return value, source
    return "complete_failed", "analyzer_env_infra_task"


def _monitor_snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    return {
        "benchmark_status": str(snapshot.get("benchmark_status") or ""),
        "status_reason": str(snapshot.get("status_reason") or ""),
        "task_summary": snapshot.get("task_summary") if isinstance(snapshot.get("task_summary"), dict) else {},
    }


def _old_run_monitor_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "old_run_monitor_status": str(record.get("task_complete_status") or "") if record else "",
        "old_run_monitor": record or {},
    }


def _old_run_status_fields(old_task: dict[str, Any]) -> dict[str, Any]:
    return {
        "old_run_status": str(old_task.get("old_run_status") or ""),
        "old_run_status_source": str(old_task.get("old_run_status_source") or ""),
        "old_run_monitor_status": str(old_task.get("old_run_monitor_status") or ""),
        "old_run_monitor": old_task.get("old_run_monitor") if isinstance(old_task.get("old_run_monitor"), dict) else {},
    }


def _analyzer_facts(env_task: dict[str, Any], report_task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(env_task.get(key) or report_task.get(key) or "")
        for key in ("final_class", "failure_stage", "scope", "root_cause_code", "root_cause_summary")
    } | {"confidence": env_task.get("confidence") if env_task.get("confidence") is not None else report_task.get("confidence")}


def _collect_baseline_monitor(
    baseline_run_dir: Path | None,
    output_dir: Path,
    baseline_monitor_policy: str,
) -> tuple[dict[str, Any] | None, str, dict[str, dict[str, Any]], dict[str, Any] | None]:
    if baseline_run_dir is None:
        return None, "", {}, None
    snapshot = None
    output_path = ""
    if baseline_monitor_policy != "off":
        snapshot, output_path = read_monitor_snapshot(baseline_run_dir, output_dir, "baseline-monitor")
        if snapshot is None:
            snapshot, output_path = generate_monitor_snapshot(baseline_run_dir, output_dir, "baseline-monitor")
    records, summary = collect_task_results(baseline_run_dir)
    return snapshot, output_path, records, summary


def _build_old_run(
    analyzer_output_path: Path,
    output_dir: Path,
    *,
    baseline_run_dir: Path | None,
    baseline_monitor_policy: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    paths = resolve_analyzer_paths(analyzer_output_path)
    analyzer_report = read_json(paths["analyzer_report_path"])
    env_infra = read_json(paths["env_infra_tasks_path"])
    validate_analyzer_report(analyzer_report)
    validate_env_infra_tasks(env_infra)

    snapshot, monitor_output_path, monitor_records, monitor_summary = _collect_baseline_monitor(
        baseline_run_dir,
        output_dir,
        baseline_monitor_policy,
    )
    report_tasks = _index_report_tasks(analyzer_report)
    old_tasks: list[dict[str, Any]] = []
    by_index: dict[str, dict[str, Any]] = {}
    for item in env_infra.get("tasks", []):
        if not isinstance(item, dict) or not isinstance(item.get("task"), dict):
            continue
        task = item["task"]
        task_index = str(task.get("task_index") or "")
        report_task = report_tasks.get(task_key(task), {})
        status, source = _explicit_status(item, report_task)
        old_task = {
            "task": _task_identity(task),
            "old_run_status": status,
            "old_run_status_source": source,
            **_old_run_monitor_fields(monitor_records.get(task_index)),
            "analyzer": _analyzer_facts(item, report_task),
        }
        old_tasks.append(old_task)
        by_index[task_index] = old_task

    old_run = {
        "run_id": str(analyzer_report.get("run_id") or ""),
        "handover_id": str(env_infra.get("handover_id") or analyzer_report.get("handover_id") or ""),
        "analyzer_summary": analyzer_report.get("summary") if isinstance(analyzer_report.get("summary"), dict) else {},
        "env_infra_task_count": len(old_tasks),
        "tasks": old_tasks,
        "monitor_available": monitor_summary is not None and bool(monitor_records),
        "monitor_summary": monitor_summary or _empty_run_summary(),
        "monitor_snapshot": _monitor_snapshot_summary(snapshot),
    }
    return old_run, by_index, monitor_output_path


def _with_old_status(record: dict[str, Any], old_task_by_index: dict[str, dict[str, Any]], task_index: str) -> dict[str, Any]:
    return {**record, **_old_run_status_fields(old_task_by_index.get(task_index, {}))}


def _summary_task_result(item: dict[str, Any]) -> dict[str, Any]:
    new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
    return {
        "task": item.get("task", {}),
        "plan_ids": item.get("plan_ids", []),
        "sampled": bool(item.get("sampled", True)),
        "sampled_by_plan_ids": item.get("sampled_by_plan_ids", []),
        "old_run_status": item.get("old_run_status", ""),
        "old_run_monitor_status": item.get("old_run_monitor_status", ""),
        "old_analyzer": item.get("old_analyzer", {}),
        "exec_status": item.get("exec_status", ""),
        "new_run_status": new_run.get("task_complete_status", ""),
        "verification_status": item.get("verification_status", ""),
    }


def _summary_input(
    status: str,
    old_run: dict[str, Any],
    verification_result: dict[str, Any],
    task_results: list[dict[str, Any]],
    non_plan_task_results: list[dict[str, Any]],
    *,
    baseline_monitor_required: bool,
    baseline_monitor_available: bool,
) -> dict[str, Any]:
    caveats: list[str] = []
    if baseline_monitor_required and not baseline_monitor_available:
        caveats.append("baseline monitor data required by policy but unavailable")
    if verification_result.get("status") in {"inconclusive", "exec_failed"}:
        caveats.append(f"verification status is {verification_result.get('status')}")
    if verification_result.get("verification_mode") == "smoke_test":
        sampling = verification_result.get("sampling") if isinstance(verification_result.get("sampling"), dict) else {}
        caveats.append(
            "verification used smoke sampling: "
            f"{sampling.get('sampled_task_count', 0)} sampled task(s), "
            f"{sampling.get('unsampled_task_count', 0)} unsampled task(s)"
        )
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary_input",
        "status": status,
        "old_run": {
            "run_id": old_run.get("run_id", ""),
            "handover_id": old_run.get("handover_id", ""),
            "analyzer_summary": old_run.get("analyzer_summary", {}),
            "env_infra_task_count": old_run.get("env_infra_task_count", 0),
            "monitor_available": old_run.get("monitor_available", False),
            "monitor_summary": old_run.get("monitor_summary", {}),
        },
        "new_run": {
            "summary": verification_result.get("new_run_summary", {}),
            "rerun": verification_result.get("rerun", {}),
            "verification_mode": verification_result.get("verification_mode", "full_run"),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result.get("plan_results", []),
        "task_results": [_summary_task_result(item) for item in task_results],
        "non_plan_task_result_count": len(non_plan_task_results),
        "caveats": caveats,
    }


def _fallback_summary(
    summary_input: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    verification_status = str(summary_input.get("status") or "inconclusive")
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary",
        "status": "failed",
        "text": (
            "Deterministic fallback summary: report-main-agent failed; "
            f"verification status is `{verification_status}`."
        ),
        "highlights": [f"verification status: {verification_status}"],
        "caveats": ["summary generated without report-main-agent due to summary generation failure"],
        "generation_errors": errors,
    }


def generate_report_summary(
    invoker: AgentInvoker,
    summary_input: dict[str, Any],
    output_dir: Path,
    *,
    max_attempts: int = 2,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[dict[str, Any]] = []
    raw_paths: list[str] = []
    prompt = REPORT_MAIN_AGENT_PROMPT
    for attempt in range(1, max_attempts + 1):
        raw = ""
        try:
            raw = invoker.invoke(prompt, summary_input, attempt=attempt, label="report-main-agent")
            raw_path = output_dir / "raw-report-main-agent-output" / f"attempt-{attempt}.txt"
            write_text(raw_path, raw)
            raw_paths.append(str(raw_path))
            payload = parse_strict_json_object(raw)
            payload.setdefault("generation_errors", [])
            validate_report_summary(payload)
            return payload, raw_paths
        except (RuntimeError, ValidationError) as exc:
            error = str(exc)
            errors.append({"stage": "report_main_agent", "attempt": attempt, "error": error})
            if raw and attempt < max_attempts:
                prompt = build_validation_retry_prompt(
                    base_prompt=REPORT_MAIN_AGENT_PROMPT,
                    previous_output=raw,
                    validation_error=error,
                )
    return _fallback_summary(summary_input, errors), raw_paths


def _report_source(
    verification_result_path: Path,
    analyzer_output_path: Path,
    baseline_run_dir: Path | None,
    baseline_monitor_output_path: str,
) -> dict[str, Any]:
    analyzer_paths = resolve_analyzer_paths(analyzer_output_path)
    return {
        "verification_result_path": str(verification_result_path),
        "analyzer_output_path": str(analyzer_output_path),
        "analyzer_report_path": str(analyzer_paths["analyzer_report_path"]),
        "env_infra_tasks_path": str(analyzer_paths["env_infra_tasks_path"]),
        "baseline_run_dir": str(baseline_run_dir) if baseline_run_dir is not None else "",
        "baseline_monitor_output_path": baseline_monitor_output_path,
    }


def build_report_input(
    verification_result_path: Path,
    analyzer_output_path: Path,
    output_dir: Path,
    *,
    baseline_run_dir: Path | None,
    baseline_monitor_policy: str,
) -> dict[str, Any]:
    verification_result = read_json(verification_result_path)
    validate_verification_result(verification_result)
    old_run, old_task_by_index, monitor_path = _build_old_run(
        analyzer_output_path,
        output_dir,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
    )
    task_results = [
        _with_old_status(item, old_task_by_index, str(item.get("task", {}).get("task_index") or ""))
        for item in verification_result.get("task_results", [])
        if isinstance(item, dict)
    ]
    non_plan_task_results = [
        _with_old_status(item, old_task_by_index, str(item.get("task_index") or ""))
        for item in verification_result.get("non_plan_task_results", [])
        if isinstance(item, dict)
    ]
    unexpected_run_task_results = [
        item
        for item in verification_result.get("unexpected_run_task_results", [])
        if isinstance(item, dict)
    ]
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_report_input",
        "source": _report_source(verification_result_path, analyzer_output_path, baseline_run_dir, monitor_path),
        "baseline_monitor_policy": baseline_monitor_policy,
        "verification_result": verification_result,
        "old_run": old_run,
        "task_results": task_results,
        "non_plan_task_results": non_plan_task_results,
        "unexpected_run_task_results": unexpected_run_task_results,
        "summary_input": _summary_input(
            str(verification_result.get("status") or "inconclusive"),
            old_run,
            verification_result,
            task_results,
            non_plan_task_results,
            baseline_monitor_required=baseline_monitor_policy == "on",
            baseline_monitor_available=bool(monitor_path),
        ),
    }
    validate_report_input(payload)
    return payload


def _apply_summary_caveats(summary: dict[str, Any], report_input: dict[str, Any]) -> dict[str, Any]:
    caveats = report_input["summary_input"].get("caveats") or []
    if caveats and summary.get("status") == "success":
        summary = {**summary, "caveats": [*summary.get("caveats", []), *caveats]}
    if report_input["baseline_monitor_policy"] == "on" and not report_input["source"].get("baseline_monitor_output_path"):
        summary = {
            **summary,
            "status": "failed",
            "generation_errors": [
                *summary.get("generation_errors", []),
                {"stage": "baseline_monitor", "error": "baseline monitor data required by policy but unavailable"},
            ],
        }
    return summary


def run_report(report_input: dict[str, Any], output_dir: Path, invoker: AgentInvoker) -> dict[str, Any]:
    validate_report_input(report_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "report-input.json", report_input)

    verification_result = report_input["verification_result"]
    summary, raw_paths = generate_report_summary(invoker, report_input["summary_input"], output_dir)
    target_environment_path = output_dir / "target-environment.json"
    target_context_path = output_dir / "target-context.json"
    if not target_environment_path.is_file() or not target_context_path.is_file():
        fix_plan_value = str(verification_result["source"].get("fix_plan_path") or "")
        if fix_plan_value:
            fix_plan_path = Path(fix_plan_value)
            if not fix_plan_path.is_absolute():
                verification_path = Path(report_input["source"]["verification_result_path"])
                fix_plan_path = verification_path.parent / fix_plan_path
            candidate = fix_plan_path.parent / "target-environment.json"
            if not target_environment_path.is_file() and candidate.is_file():
                target_environment_path = candidate
            context_candidate = fix_plan_path.parent / "target-context.json"
            if not target_context_path.is_file() and context_candidate.is_file():
                target_context_path = context_candidate
    human_report_path = output_dir / "fix-report-latest.md"
    result_payload = {
        "summary": _apply_summary_caveats(summary, report_input),
        "schema_version": 1,
        "kind": "harbor_fixer_report",
        "generated_at": _utc_now(),
        "status": verification_result["status"],
        "source": report_input["source"],
        "old_run": report_input["old_run"],
        "new_run": {
            "summary": verification_result["new_run_summary"],
            "rerun": verification_result["rerun"],
            "monitor_output_path": verification_result["source"].get("monitor_output_path", ""),
            "verification_mode": verification_result.get("verification_mode", "full_run"),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result["plan_results"],
        "task_results": report_input["task_results"],
        "non_plan_task_results": report_input["non_plan_task_results"],
        "unexpected_run_task_results": report_input.get("unexpected_run_task_results", []),
        "artifacts": {
            "fix_plan_path": verification_result["source"].get("fix_plan_path", ""),
            "exec_result_path": verification_result["source"].get("exec_result_path", ""),
            "verification_result_path": report_input["source"]["verification_result_path"],
            "report_input_path": str(output_dir / "report-input.json"),
            "target_environment_path": (
                str(target_environment_path) if target_environment_path.is_file() else ""
            ),
            "target_context_path": (
                str(target_context_path) if target_context_path.is_file() else ""
            ),
            "human_report_path": str(human_report_path),
            "raw_summary_output_paths": raw_paths,
        },
    }
    validate_fix_report(result_payload, verification_result=verification_result)
    fix_plan, fix_plan_error = _read_optional_artifact(
        result_payload["artifacts"]["fix_plan_path"]
    )
    exec_result, exec_result_error = _read_optional_artifact(
        result_payload["artifacts"]["exec_result_path"]
    )
    human_report = render_human_report(
        result_payload,
        fix_plan,
        exec_result,
        artifact_errors=[fix_plan_error, exec_result_error],
    )
    write_text(human_report_path, human_report)
    _write_ordered_json(output_dir / "fix-report-latest.json", result_payload)
    return result_payload


def run_report_from_paths(
    verification_result_path: Path,
    analyzer_output_path: Path,
    output_dir: Path,
    invoker: AgentInvoker,
    *,
    baseline_run_dir: Path | None = None,
    baseline_monitor_policy: str = "auto",
) -> dict[str, Any]:
    if baseline_monitor_policy not in {"auto", "on", "off"}:
        raise ValidationError("baseline_monitor_policy must be one of: auto, off, on")
    report_input = build_report_input(
        verification_result_path,
        analyzer_output_path,
        output_dir,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
    )
    return run_report(report_input, output_dir, invoker)
