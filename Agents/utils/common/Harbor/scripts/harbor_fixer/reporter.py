"""Stage 4 Fix Report orchestration for Harbor Fixer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_invocation import AgentInvoker
from .analyzer_inputs import resolve_analyzer_paths
from .artifact_io import read_json, write_json, write_text
from .prompts import REPORT_MAIN_AGENT_PROMPT, build_validation_retry_prompt
from .report_markdown import render_human_report
from .validation import (
    TASK_COMPLETE_STATUSES,
    ValidationError,
    parse_strict_json_object,
    task_key,
    validate_env_infra_tasks,
    validate_fix_report,
    validate_report_input,
    validate_report_summary,
    validate_verification_result,
)
from .verification.run_state import (
    collect_task_results,
    generate_monitor_snapshot,
    read_monitor_snapshot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_optional_artifact(path_value: Any) -> tuple[dict[str, Any], str]:
    value = str(path_value or "")
    if not value:
        return {}, "artifact path is empty"
    path = Path(value)
    if not path.is_file():
        return {}, f"artifact is missing or unreadable: {path}"
    try:
        return read_json(path), ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"artifact could not be read: {path}: {exc.__class__.__name__}: {exc}"


def _task_identity(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def _explicit_status(env_task: dict[str, Any]) -> tuple[str, str]:
    for source, value in (
        ("env_infra_task.task_complete_status", env_task.get("task_complete_status")),
        ("env_infra_task.complete_status", env_task.get("complete_status")),
        ("env_infra_task.status", env_task.get("status")),
    ):
        if isinstance(value, str) and value in TASK_COMPLETE_STATUSES:
            return value, source
    return "", "unavailable"


def _monitor_snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    return {
        "benchmark_status": str(snapshot.get("benchmark_status") or ""),
        "status_reason": str(snapshot.get("status_reason") or ""),
        "task_summary": snapshot.get("task_summary")
        if isinstance(snapshot.get("task_summary"), dict)
        else {},
    }


def _old_run_monitor_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "old_run_monitor_status": str(record.get("task_complete_status") or "")
        if record
        else "",
        "old_run_monitor": record or {},
    }


def _old_run_status_fields(old_task: dict[str, Any]) -> dict[str, Any]:
    return {
        "old_run_status": str(old_task.get("old_run_status") or ""),
        "old_run_status_source": str(old_task.get("old_run_status_source") or ""),
        "old_run_monitor_status": str(old_task.get("old_run_monitor_status") or ""),
        "old_run_monitor": old_task.get("old_run_monitor")
        if isinstance(old_task.get("old_run_monitor"), dict)
        else {},
    }


def _analyzer_facts(env_task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(env_task.get(key) or "")
        for key in (
            "final_class",
            "failure_stage",
            "scope",
            "root_cause_code",
            "root_cause_summary",
        )
    } | {"confidence": env_task.get("confidence")}


def _collect_baseline_monitor(
    baseline_run_dir: Path | None,
    output_dir: Path,
    baseline_monitor_policy: str,
    agent: str,
) -> tuple[
    dict[str, Any] | None, str, dict[str, dict[str, Any]], dict[str, Any] | None
]:
    if baseline_run_dir is None:
        return None, "", {}, None
    snapshot = None
    output_path = ""
    if baseline_monitor_policy != "off":
        snapshot, output_path = read_monitor_snapshot(baseline_run_dir)
        if snapshot is None:
            snapshot, output_path = generate_monitor_snapshot(
                baseline_run_dir,
                output_dir / "baseline-monitor",
                agent,
            )
    records, summary = collect_task_results(baseline_run_dir, agent)
    return snapshot, output_path, records, summary


def _build_old_run(
    analyzer_output_path: Path,
    output_dir: Path,
    *,
    baseline_run_dir: Path | None,
    baseline_monitor_policy: str,
    agent: str,
) -> tuple[dict[str, Any], dict[tuple[str, str, str], dict[str, Any]], str]:
    paths = resolve_analyzer_paths(analyzer_output_path)

    snapshot, monitor_output_path, monitor_records, monitor_summary = (
        _collect_baseline_monitor(
            baseline_run_dir,
            output_dir,
            baseline_monitor_policy,
            agent,
        )
    )
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    handover_ids: list[str] = []
    for publication in paths["publications"]:
        env_infra = read_json(Path(publication["env_infra_tasks_path"]))
        validate_env_infra_tasks(env_infra)
        handover_ids.append(str(env_infra.get("handover_id") or ""))
        for item in env_infra.get("tasks", []):
            if not isinstance(item, dict) or not isinstance(item.get("task"), dict):
                continue
            task = item["task"]
            task_index = str(task.get("task_index") or "")
            status, source = _explicit_status(item)
            by_key[task_key(task)] = {
                "task": _task_identity(task),
                "old_run_status": status,
                "old_run_status_source": source,
                **_old_run_monitor_fields(monitor_records.get(task_index)),
                "analyzer": _analyzer_facts(item),
            }
    old_tasks = list(by_key.values())

    old_run = {
        "run_id": str(paths.get("run_id") or ""),
        "handover_id": handover_ids[-1] if handover_ids else "",
        "handover_ids": handover_ids,
        "analyzer_summary": {
            "task_count": len(old_tasks),
            "publication_count": len(paths["publications"]),
        },
        "env_infra_task_count": len(old_tasks),
        "tasks": old_tasks,
        "monitor_available": monitor_summary is not None and bool(monitor_records),
        "monitor_summary": monitor_summary or {},
        "monitor_snapshot": _monitor_snapshot_summary(snapshot),
    }
    return old_run, by_key, monitor_output_path


def _with_old_status(
    record: dict[str, Any],
    old_task_by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    return {**record, **_old_run_status_fields(old_task_by_key.get(task_key(task), {}))}


def _summary_task_result(item: dict[str, Any]) -> dict[str, Any]:
    new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
    return {
        "task": item.get("task", {}),
        "plan_id": item.get("plan_id", ""),
        "sampled": item.get("sampled"),
        "old_run_status": item.get("old_run_status", ""),
        "old_run_monitor_status": item.get("old_run_monitor_status", ""),
        "exec_status": item.get("exec_status", ""),
        "exec_failure_reason": item.get("exec_failure_reason"),
        "new_run_status": new_run.get("task_complete_status", ""),
        "verification_status": item.get("verification_status", ""),
    }


def _summary_input(
    status: str,
    old_run: dict[str, Any],
    verification_result: dict[str, Any],
    task_results: list[dict[str, Any]],
    *,
    baseline_monitor_required: bool,
    baseline_monitor_available: bool,
) -> dict[str, Any]:
    caveats: list[str] = []
    if not baseline_monitor_available:
        caveats.append(
            "baseline monitor data unavailable; before/after comparison omitted"
        )
        if baseline_monitor_required:
            caveats.append("baseline monitor data was required by policy")
    if verification_result.get("status") in {"inconclusive", "exec_failed"}:
        caveats.append(f"verification status is {verification_result.get('status')}")
    if verification_result.get("verification_mode") == "smoke_test":
        sampling = (
            verification_result.get("sampling")
            if isinstance(verification_result.get("sampling"), dict)
            else {}
        )
        caveats.append(
            "verification used smoke sampling: "
            f"{sampling.get('sampled_task_count', 0)} sampled task(s), "
            f"{sampling.get('unsampled_task_count', 0)} unsampled task(s)"
        )
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary_input",
        "status": status,
        "agent": verification_result.get("agent", ""),
        "execution": verification_result.get("execution", {}),
        "reason_codes": verification_result.get("reason_codes", []),
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
            "verification_mode": verification_result.get(
                "verification_mode", "full_run"
            ),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result.get("plan_results", []),
        "task_results": [_summary_task_result(item) for item in task_results],
        "caveats": caveats,
    }


def _fallback_summary(
    summary_input: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    new_run = (
        summary_input.get("new_run")
        if isinstance(summary_input.get("new_run"), dict)
        else {}
    )
    sampling = (
        new_run.get("sampling") if isinstance(new_run.get("sampling"), dict) else {}
    )
    task_results = [
        item for item in summary_input.get("task_results", []) if isinstance(item, dict)
    ]
    sampled = [item for item in task_results if item.get("sampled")]
    plan_task_count = int(sampling.get("plan_task_count") or len(task_results))
    sampled_task_count = int(sampling.get("sampled_task_count") or len(sampled))
    unsampled_task_count = int(
        sampling.get("unsampled_task_count")
        or max(0, plan_task_count - sampled_task_count)
    )
    fixed_count = sum(item.get("verification_status") == "fixed" for item in sampled)
    not_fixed_count = sum(
        item.get("verification_status") == "not_fixed" for item in sampled
    )
    other_count = len(sampled) - fixed_count - not_fixed_count
    mode = str(new_run.get("verification_mode") or "verification run")
    text = (
        "Deterministic fallback summary: report-main-agent was unavailable. "
        f"Fixer recorded {mode} results for {sampled_task_count} of {plan_task_count} planned task(s). "
        f"Among sampled tasks, verifier labels were: {fixed_count} fixed, "
        f"{not_fixed_count} not_fixed, and {other_count} other or inconclusive. "
        f"{unsampled_task_count} task(s) were not sampled and have no rerun result."
    )
    if not summary_input.get("old_run", {}).get("monitor_available"):
        text += " Baseline task results were unavailable, so no before/after comparison is claimed."
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary",
        "status": "failed",
        "text": text,
        "highlights": [
            f"sampled task labels: {fixed_count} fixed, {not_fixed_count} not_fixed, {other_count} other or inconclusive",
            f"unsampled tasks: {unsampled_task_count}",
        ],
        "caveats": [
            "summary generated without report-main-agent due to summary generation failure",
            *summary_input.get("caveats", []),
        ],
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
            raw = invoker.invoke(
                prompt, summary_input, attempt=attempt, label="report-main-agent"
            )
            raw_path = (
                output_dir / "raw-report-main-agent-output" / f"attempt-{attempt}.txt"
            )
            write_text(raw_path, raw)
            raw_paths.append(str(raw_path))
            payload = parse_strict_json_object(raw)
            payload.setdefault("generation_errors", [])
            validate_report_summary(payload)
            return payload, raw_paths
        except (RuntimeError, ValidationError) as exc:
            error = str(exc)
            errors.append(
                {"stage": "report_main_agent", "attempt": attempt, "error": error}
            )
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
        "analyzer_manifest_path": str(analyzer_paths["manifest_path"]),
        "env_infra_tasks_paths": [
            str(item["env_infra_tasks_path"]) for item in analyzer_paths["publications"]
        ],
        "baseline_run_dir": str(baseline_run_dir)
        if baseline_run_dir is not None
        else "",
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
    old_run, old_task_by_key, monitor_path = _build_old_run(
        analyzer_output_path,
        output_dir,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
        agent=str(verification_result["agent"]),
    )
    task_results = [
        _with_old_status(item, old_task_by_key)
        for item in verification_result.get("task_results", [])
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
        "source": _report_source(
            verification_result_path,
            analyzer_output_path,
            baseline_run_dir,
            monitor_path,
        ),
        "baseline_monitor_policy": baseline_monitor_policy,
        "verification_result": verification_result,
        "old_run": old_run,
        "task_results": task_results,
        "unexpected_run_task_results": unexpected_run_task_results,
        "summary_input": _summary_input(
            str(verification_result.get("status") or "inconclusive"),
            old_run,
            verification_result,
            task_results,
            baseline_monitor_required=baseline_monitor_policy == "on",
            baseline_monitor_available=bool(old_run.get("monitor_available")),
        ),
    }
    validate_report_input(payload)
    return payload


def _apply_summary_caveats(
    summary: dict[str, Any], report_input: dict[str, Any]
) -> dict[str, Any]:
    caveats = report_input["summary_input"].get("caveats") or []
    if caveats:
        summary = {
            **summary,
            "caveats": list(dict.fromkeys([*summary.get("caveats", []), *caveats])),
        }
    if report_input["baseline_monitor_policy"] == "on" and not report_input[
        "old_run"
    ].get("monitor_available"):
        summary = {
            **summary,
            "status": "failed",
            "generation_errors": [
                *summary.get("generation_errors", []),
                {
                    "stage": "baseline_monitor",
                    "error": "baseline monitor data required by policy but unavailable",
                },
            ],
        }
    return summary


def run_report(
    report_input: dict[str, Any], output_dir: Path, invoker: AgentInvoker
) -> dict[str, Any]:
    validate_report_input(report_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "report-input.json", report_input)

    verification_result = report_input["verification_result"]
    summary, raw_paths = generate_report_summary(
        invoker, report_input["summary_input"], output_dir
    )
    target_environment_path = output_dir / "target-environment.json"
    target_context_path = output_dir / "target-context.json"
    if not target_environment_path.is_file() or not target_context_path.is_file():
        fix_plan_value = str(verification_result["source"].get("fix_plan_path") or "")
        if fix_plan_value:
            fix_plan_path = Path(fix_plan_value)
            if not fix_plan_path.is_absolute():
                verification_path = Path(
                    report_input["source"]["verification_result_path"]
                )
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
        "agent": verification_result["agent"],
        "status": verification_result["status"],
        "execution": verification_result["execution"],
        "reason_codes": verification_result["reason_codes"],
        "source": report_input["source"],
        "old_run": report_input["old_run"],
        "new_run": {
            "summary": verification_result["new_run_summary"],
            "rerun": verification_result["rerun"],
            "monitor_output_path": verification_result["source"].get(
                "monitor_output_path", ""
            ),
            "verification_mode": verification_result.get(
                "verification_mode", "full_run"
            ),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result["plan_results"],
        "task_results": report_input["task_results"],
        "unexpected_run_task_results": report_input.get(
            "unexpected_run_task_results", []
        ),
        "artifacts": {
            "fix_plan_path": verification_result["source"].get("fix_plan_path", ""),
            "exec_result_path": verification_result["source"].get(
                "exec_result_path", ""
            ),
            "verification_result_path": report_input["source"][
                "verification_result_path"
            ],
            "report_input_path": str(output_dir / "report-input.json"),
            "target_environment_path": (
                str(target_environment_path)
                if target_environment_path.is_file()
                else ""
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
    write_json(output_dir / "fix-report-latest.json", result_payload)
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
