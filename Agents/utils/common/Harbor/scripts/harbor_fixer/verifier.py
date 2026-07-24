"""Stage 3 Fix Verification implementation for Harbor Fixer MVP."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import build_task_inputs, read_json, write_json
from .run_artifacts import collect_run_tasks, generate_monitor_snapshot, read_monitor_snapshot
from .validation import (
    validate_exec_result,
    validate_fix_plan_set,
    validate_verification_input,
    validate_verification_result,
)


TASK_VERIFICATION_STATUSES = {"exec_failed", "fixed", "not_complete", "not_fixed", "not_sampled", "unknown"}
RUN_SCOPED_ENV_VARS = {
    "QUEUE_DIR",
    "RUNTIME_DIR",
    "LAYOUT_FILE",
    "JOBS_ROOT",
    "HARBOR_ONLINE_ANALYSIS_DIR",
    "HARBOR_ONLINE_ANALYSIS_PID_FILE",
    "HARBOR_ONLINE_ANALYSIS_LOG_FILE",
    "HARBOR_MONITOR_DIR",
    "HARBOR_MONITOR_PID_FILE",
    "HARBOR_MONITOR_LOG_FILE",
    "HARBOR_BENCHMARK_PID_FILE",
    "HARBOR_BENCHMARK_EXIT_FILE",
    "HARBOR_JOB_DIR_FILE",
    "HARBOR_MONITOR_RESTART_CMD",
    "HARBOR_MONITOR_STOP_CMD",
    "NEXT_INDEX_FILE",
    "LOCK_FILE",
    "WORKERS_READY_FILE",
    "WORKERS_FAILED_FILE",
    "RL_TRACE_LOG",
    "RL_SERVER_LOG",
    "RL_SERVER_PID_FILE",
    "RL_QUEUE_DIR",
    "RL_ACTIVE_DIR",
    "RL_JOB_QUEUE_ROOT",
    "RL_JOB_RUNTIME_ROOT",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _read_monitor_snapshot(run_dir: Path, output_dir: Path) -> tuple[dict[str, Any] | None, str]:
    return read_monitor_snapshot(run_dir, output_dir, "verification-monitor")


def _generate_monitor_snapshot(run_dir: Path, output_dir: Path) -> tuple[dict[str, Any] | None, str]:
    return generate_monitor_snapshot(run_dir, output_dir, "verification-monitor")


def _monitor_is_terminal(snapshot: dict[str, Any]) -> bool:
    benchmark_status = str(snapshot.get("benchmark_status") or "")
    decision = str(snapshot.get("monitor_follow_decision") or "")
    if benchmark_status == "completed":
        return True
    if decision in {"stop_completed", "stop_action_required"}:
        return True
    return False


def _wait_for_monitor_terminal(
    run_dir: Path,
    output_dir: Path,
    *,
    timeout_seconds: int,
    poll_interval: float,
) -> tuple[dict[str, Any] | None, str, bool]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    last_snapshot: dict[str, Any] | None = None
    last_path = ""
    while True:
        snapshot, path = _generate_monitor_snapshot(run_dir, output_dir)
        if snapshot is not None:
            last_snapshot = snapshot
            last_path = path
            if _monitor_is_terminal(snapshot):
                return snapshot, path, False
        if time.monotonic() >= deadline:
            return last_snapshot, last_path, True
        time.sleep(max(0.1, poll_interval))


def _collect_run_tasks(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    return collect_run_tasks(run_dir)


def _analyzer_task_context(analyzer_output_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    task_inputs, source = build_task_inputs(analyzer_output_path)
    return {str(item["task"]["task_index"]): item for item in task_inputs}, source


def _plan_exec_map(exec_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(plan.get("plan_id")): plan for plan in exec_result.get("plans", []) if isinstance(plan, dict)}


def _plan_task_indexes(fix_plan: dict[str, Any]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for plan in fix_plan.get("plans", []):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "")
        indexes: list[str] = []
        for task in plan.get("task_list", []):
            if isinstance(task, dict):
                indexes.append(str(task.get("task_index") or ""))
        mapping[plan_id] = [index for index in indexes if index]
    return mapping


def _plan_task_identities(fix_plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for plan in fix_plan.get("plans", []):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "")
        tasks: list[dict[str, Any]] = []
        for task in plan.get("task_list", []):
            if not isinstance(task, dict):
                continue
            task_index = str(task.get("task_index") or "")
            if not task_index:
                continue
            tasks.append(
                {
                    "task_index": task_index,
                    "task_name": str(task.get("task_name") or ""),
                    "attempt_id": task.get("attempt_id"),
                }
            )
        mapping[plan_id] = tasks
    return mapping


def _task_to_plan_ids(fix_plan: dict[str, Any]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for plan_id, task_indexes in _plan_task_indexes(fix_plan).items():
        for task_index in task_indexes:
            mapping.setdefault(task_index, []).append(plan_id)
    return mapping


def _exec_status_for_task(plan_ids: list[str], exec_plans: dict[str, dict[str, Any]]) -> str:
    if not plan_ids:
        return "unknown"
    for plan_id in plan_ids:
        if exec_plans.get(plan_id, {}).get("status") != "success":
            return "failed"
    return "success"


def _task_sort_key(value: str) -> tuple[int, int | str]:
    try:
        return 0, int(value)
    except ValueError:
        return 1, value


def _selection_hash(source: dict[str, Any], plan_id: str, task: dict[str, Any]) -> str:
    payload = {
        "handover_id": source.get("handover_id", ""),
        "run_id": source.get("run_id", ""),
        "plan_id": plan_id,
        "task": {
            "task_index": str(task.get("task_index") or ""),
            "task_name": str(task.get("task_name") or ""),
            "attempt_id": task.get("attempt_id"),
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_smoke_selection(
    fix_plan: dict[str, Any],
    exec_plans: dict[str, dict[str, Any]],
    analyzer_tasks: dict[str, dict[str, Any]],
    analyzer_source: dict[str, Any],
    *,
    limit_per_plan: int,
    output_dir: Path,
) -> dict[str, Any]:
    plan_tasks = _plan_task_identities(fix_plan)
    selected_by_task: dict[str, dict[str, Any]] = {}
    plan_records: list[dict[str, Any]] = []
    selection_errors: list[dict[str, Any]] = []

    for plan in fix_plan.get("plans", []):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "")
        exec_status = str(exec_plans.get(plan_id, {}).get("status") or "unknown")
        tasks = plan_tasks.get(plan_id, [])
        selected: list[dict[str, Any]] = []
        if exec_status == "success":
            ranked = sorted(((_selection_hash(analyzer_source, plan_id, task), task) for task in tasks), key=lambda item: item[0])
            selected = [task for _hash, task in ranked[:limit_per_plan]]
            for selected_hash, task in ranked[:limit_per_plan]:
                task_index = str(task.get("task_index") or "")
                analyzer_input = analyzer_tasks.get(task_index, {})
                analyzer_task = analyzer_input.get("task", {}) if isinstance(analyzer_input, dict) else {}
                task_name = str(analyzer_task.get("task_name") or task.get("task_name") or "")
                if not task_name:
                    selection_errors.append({"task_index": task_index, "plan_id": plan_id, "error": "task_name_missing"})
                    task_name = task_index
                record = selected_by_task.setdefault(
                    task_index,
                    {
                        "original_task_index": task_index,
                        "task_name": task_name,
                        "attempt_id": analyzer_task.get("attempt_id", task.get("attempt_id")),
                        "sampled_by_plan_ids": [],
                        "selection_hashes": {},
                    },
                )
                if plan_id not in record["sampled_by_plan_ids"]:
                    record["sampled_by_plan_ids"].append(plan_id)
                record["selection_hashes"][plan_id] = selected_hash
        selected_indexes = {str(task.get("task_index") or "") for task in selected}
        plan_records.append(
            {
                "plan_id": plan_id,
                "exec_status": exec_status,
                "limit_per_plan": limit_per_plan,
                "total_task_count": len(tasks),
                "sampled_task_indexes": sorted(selected_indexes, key=_task_sort_key),
                "unsampled_task_indexes": sorted(
                    [str(task.get("task_index") or "") for task in tasks if str(task.get("task_index") or "") not in selected_indexes],
                    key=_task_sort_key,
                ),
            }
        )

    selected_tasks = sorted(
        selected_by_task.values(),
        key=lambda item: (min(item["selection_hashes"].values()), _task_sort_key(str(item["original_task_index"]))),
    )
    for smoke_index, task in enumerate(selected_tasks, start=1):
        task["smoke_task_index"] = str(smoke_index)
        task["sampled_by_plan_ids"] = sorted(task["sampled_by_plan_ids"])

    task_source_path = output_dir / "verification-smoke-tasks.txt"
    task_source_path.parent.mkdir(parents=True, exist_ok=True)
    task_source_path.write_text("".join(f"{task['task_name']}\n" for task in selected_tasks), encoding="utf-8")

    selection_path = output_dir / "verification-smoke-selection.json"
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_verification_smoke_selection",
        "verification_mode": "smoke_test",
        "selection_policy": "stable_hash",
        "limit_per_plan": limit_per_plan,
        "source": {
            "handover_id": str(analyzer_source.get("handover_id") or ""),
            "run_id": str(analyzer_source.get("run_id") or ""),
            "task_source_path": str(task_source_path),
            "selection_path": str(selection_path),
        },
        "plans": plan_records,
        "tasks": selected_tasks,
        "sampled_task_count": len(selected_tasks),
        "selection_errors": selection_errors,
    }
    write_json(selection_path, payload)
    return payload


def _verification_status(exec_status: str, analyzer_result: dict[str, Any], run_record: dict[str, Any] | None) -> str:
    if exec_status != "success":
        return "exec_failed"
    if analyzer_result.get("final_class") not in {"env_fail", "infra_fail"}:
        return "unknown"
    if run_record is None:
        return "unknown"
    status = run_record.get("task_complete_status")
    if status == "complete_success":
        return "fixed"
    if status == "complete_failed":
        return "not_fixed"
    if status == "complete_unknown":
        return "unknown"
    if status == "not_complete":
        return "not_complete"
    return "unknown"


def _aggregate_status(task_results: list[dict[str, Any]], rerun_exit_code: int | None, *, rerun_required: bool) -> str:
    if rerun_exit_code not in (None, 0):
        return "inconclusive"
    if rerun_required and rerun_exit_code is None:
        return "inconclusive"
    statuses = [
        str(item.get("verification_status") or "unknown")
        for item in task_results
        if str(item.get("verification_status") or "") != "not_sampled"
    ]
    if not statuses:
        all_statuses = [str(item.get("verification_status") or "unknown") for item in task_results]
        return "exec_failed" if all_statuses and all(status == "exec_failed" for status in all_statuses) else "inconclusive"
    if all(status == "exec_failed" for status in statuses):
        return "exec_failed"
    if any(status == "exec_failed" for status in statuses):
        return "exec_failed"
    if all(status == "fixed" for status in statuses):
        return "fixed"
    if any(status == "fixed" for status in statuses) and any(status in {"not_fixed", "unknown", "not_complete"} for status in statuses):
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in statuses):
        return "inconclusive"
    return "not_fixed"


def _plan_status(task_statuses: list[str]) -> str:
    task_statuses = [status for status in task_statuses if status != "not_sampled"]
    if not task_statuses:
        return "inconclusive"
    if all(status == "fixed" for status in task_statuses):
        return "fixed"
    if any(status == "exec_failed" for status in task_statuses):
        return "exec_failed"
    if any(status == "fixed" for status in task_statuses):
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in task_statuses):
        return "inconclusive"
    return "not_fixed"


def _run_rerun_command(
    command: str | None,
    verification_run_dir: Path,
    *,
    task_source_path: str,
    selection_path: str,
    should_run: bool,
) -> dict[str, Any]:
    if not command:
        return {
            "command": "",
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "stdout_summary": "",
            "stderr_summary": "",
            "skipped_reason": "",
        }
    if not should_run:
        return {
            "command": command,
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "stdout_summary": "",
            "stderr_summary": "",
            "skipped_reason": "no_sampled_tasks",
        }
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--rerun-command must not be empty")
    verification_run_dir = verification_run_dir.resolve()
    verification_run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in RUN_SCOPED_ENV_VARS:
        env.pop(name, None)
    env.update(
        {
            "TASK_SOURCE_FILE": str(Path(task_source_path).resolve()),
            "TASK_FILE": str(verification_run_dir / "tasks.txt"),
            "OUTPUT_PATH": str(verification_run_dir),
            "RESET_RUN": "1",
            "HARBOR_FIXER_SMOKE_SELECTION": str(Path(selection_path).resolve()),
        }
    )
    started_at = _utc_now()
    start = time.monotonic()
    result = subprocess.run(
        argv,
        cwd=verification_run_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    return {
        "command": command,
        "exit_code": result.returncode,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": _duration_ms(start),
        "stdout_summary": result.stdout[-4000:],
        "stderr_summary": result.stderr[-4000:],
        "skipped_reason": "",
    }


def _missing_run_record(task_index: str, task_name: str, smoke_task_index: str) -> dict[str, Any]:
    return {
        "task_index": task_index,
        "task_name": task_name,
        "smoke_task_index": smoke_task_index,
        "task_complete_status": "complete_unknown",
        "task_result_signals": ["result_missing"],
        "evidence": {},
        "result_path": "",
    }


def _map_smoke_run_records(
    run_records: dict[str, dict[str, Any]],
    selection: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    selected = {str(task.get("smoke_task_index") or ""): task for task in selection.get("tasks", []) if isinstance(task, dict)}
    mapped: dict[str, dict[str, Any]] = {}
    unexpected: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not selected:
        return mapped, unexpected, errors

    actual_indexes = set(run_records)
    expected_indexes = set(selected)
    if actual_indexes != expected_indexes:
        errors.append(
            {
                "error": "smoke_task_index_set_mismatch",
                "expected": sorted(expected_indexes, key=_task_sort_key),
                "actual": sorted(actual_indexes, key=_task_sort_key),
            }
        )

    for smoke_index, selected_task in selected.items():
        original_index = str(selected_task.get("original_task_index") or "")
        expected_name = str(selected_task.get("task_name") or "")
        raw_record = run_records.get(smoke_index)
        if raw_record is None:
            mapped[original_index] = _missing_run_record(original_index, expected_name, smoke_index)
            continue
        actual_name = str(raw_record.get("task_name") or "")
        if expected_name and actual_name and actual_name != expected_name:
            errors.append(
                {
                    "error": "smoke_task_name_mismatch",
                    "smoke_task_index": smoke_index,
                    "original_task_index": original_index,
                    "expected_task_name": expected_name,
                    "actual_task_name": actual_name,
                }
            )
        mapped[original_index] = {
            **raw_record,
            "task_index": original_index,
            "task_name": expected_name or actual_name,
            "smoke_task_index": smoke_index,
            "smoke_task_name": actual_name,
        }

    for smoke_index, raw_record in sorted(run_records.items(), key=lambda item: _task_sort_key(item[0])):
        if smoke_index not in selected:
            unexpected.append(raw_record)
    return mapped, unexpected, errors


def build_verification_input(
    fix_plan_path: Path,
    exec_result_path: Path,
    analyzer_output_path: Path,
    verification_run_dir: Path,
    *,
    rerun_command: str | None,
    monitor_policy: str,
    output_dir: Path,
    monitor_wait_timeout: int = 3600,
    monitor_poll_interval: float = 30.0,
    verification_task_limit_per_plan: int = 2,
) -> dict[str, Any]:
    fix_plan = read_json(fix_plan_path)
    exec_result = read_json(exec_result_path)
    validate_fix_plan_set(fix_plan)
    validate_exec_result(exec_result)
    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_input",
        "fix_plan_path": str(fix_plan_path),
        "exec_result_path": str(exec_result_path),
        "analyzer_output_path": str(analyzer_output_path),
        "verification_run_dir": str(verification_run_dir),
        "output_dir": str(output_dir),
        "rerun_command": rerun_command or "",
        "monitor_policy": monitor_policy,
        "monitor_wait_timeout": monitor_wait_timeout,
        "monitor_poll_interval": monitor_poll_interval,
        "verification_mode": "smoke_test",
        "verification_task_limit_per_plan": verification_task_limit_per_plan,
        "fix_plan": fix_plan,
        "exec_result": exec_result,
    }
    validate_verification_input(payload)
    return payload


def run_verification(verification_input: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    validate_verification_input(verification_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "verification-input.json", verification_input)

    fix_plan = verification_input["fix_plan"]
    exec_result = verification_input["exec_result"]
    analyzer_output_path = Path(verification_input["analyzer_output_path"])
    verification_run_dir = Path(verification_input["verification_run_dir"])
    monitor_policy = verification_input["monitor_policy"]
    task_limit = int(verification_input.get("verification_task_limit_per_plan") or 2)

    analyzer_tasks, analyzer_source = _analyzer_task_context(analyzer_output_path)
    exec_plans = _plan_exec_map(exec_result)
    selection = _build_smoke_selection(
        fix_plan,
        exec_plans,
        analyzer_tasks,
        analyzer_source,
        limit_per_plan=task_limit,
        output_dir=output_dir,
    )
    selected_tasks = selection.get("tasks", []) if isinstance(selection.get("tasks"), list) else []
    rerun = _run_rerun_command(
        verification_input.get("rerun_command") or None,
        verification_run_dir,
        task_source_path=str(selection["source"].get("task_source_path") or ""),
        selection_path=str(selection["source"].get("selection_path") or ""),
        should_run=bool(selected_tasks),
    )
    monitor_snapshot: dict[str, Any] | None = None
    monitor_output_path = ""
    monitor_timed_out = False
    if selected_tasks:
        monitor_snapshot, monitor_output_path = _read_monitor_snapshot(verification_run_dir, output_dir)
        if monitor_policy in {"auto", "on"} and verification_input.get("rerun_command"):
            monitor_snapshot, monitor_output_path, monitor_timed_out = _wait_for_monitor_terminal(
                verification_run_dir,
                output_dir,
                timeout_seconds=int(verification_input.get("monitor_wait_timeout") or 0),
                poll_interval=float(verification_input.get("monitor_poll_interval") or 30.0),
            )
        elif monitor_snapshot is None and monitor_policy in {"auto", "on"}:
            monitor_snapshot, monitor_output_path = _generate_monitor_snapshot(verification_run_dir, output_dir)
    monitor_required = monitor_policy == "on"
    monitor_available = monitor_snapshot is not None

    if selected_tasks:
        raw_run_records, new_run_summary = _collect_run_tasks(verification_run_dir)
    else:
        raw_run_records = {}
        new_run_summary = {
            "total": 0,
            "complete_success": 0,
            "complete_failed": 0,
            "complete_unknown": 0,
            "not_complete": 0,
            "finished": 0,
            "success_rate": 0.0,
        }
    run_records, unexpected_run_task_results, smoke_mapping_errors = _map_smoke_run_records(raw_run_records, selection)
    task_to_plan = _task_to_plan_ids(fix_plan)
    new_run_summary = {
        **new_run_summary,
        "scope": "smoke_sample",
        "verification_mode": "smoke_test",
        "sampled_task_count": len(selected_tasks),
        "plan_task_count": len(task_to_plan),
        "unsampled_task_count": max(0, len(task_to_plan) - len(selected_tasks)),
    }
    plan_tasks = _plan_task_indexes(fix_plan)
    sampled_by_task = {
        str(task.get("original_task_index") or ""): [str(plan_id) for plan_id in task.get("sampled_by_plan_ids", [])]
        for task in selected_tasks
        if isinstance(task, dict)
    }
    smoke_index_by_task = {
        str(task.get("original_task_index") or ""): str(task.get("smoke_task_index") or "")
        for task in selected_tasks
        if isinstance(task, dict)
    }

    task_results: list[dict[str, Any]] = []
    for task_index in sorted(task_to_plan):
        plan_ids = task_to_plan[task_index]
        analyzer_input = analyzer_tasks.get(task_index, {})
        analyzer_result = analyzer_input.get("analyzer_result", {}) if isinstance(analyzer_input, dict) else {}
        task_identity = analyzer_input.get("task", {}) if isinstance(analyzer_input, dict) else {"task_index": task_index}
        exec_status = _exec_status_for_task(plan_ids, exec_plans)
        sampled_by_plan_ids = sampled_by_task.get(task_index, [])
        sampled = bool(sampled_by_plan_ids)
        run_record = run_records.get(task_index) if sampled else None
        verification_status = (
            _verification_status(exec_status, analyzer_result, run_record)
            if sampled or exec_status != "success"
            else "not_sampled"
        )
        task_results.append(
            {
                "task": {
                    "task_index": str(task_identity.get("task_index") or task_index),
                    "task_name": str(task_identity.get("task_name") or ""),
                    "attempt_id": task_identity.get("attempt_id"),
                },
                "plan_ids": plan_ids,
                "sampled": sampled,
                "sampled_by_plan_ids": sampled_by_plan_ids,
                "smoke_task_index": smoke_index_by_task.get(task_index, ""),
                "old_analyzer": {
                    "final_class": str(analyzer_result.get("final_class") or ""),
                    "scope": str(analyzer_result.get("scope") or ""),
                    "root_cause_code": str(analyzer_result.get("root_cause_code") or ""),
                },
                "exec_status": exec_status,
                "new_run": run_record if sampled else None,
                "verification_status": verification_status,
            }
        )

    plan_results: list[dict[str, Any]] = []
    for plan in fix_plan.get("plans", []):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "")
        indexes = plan_tasks.get(plan_id, [])
        statuses = [
            str(task.get("verification_status"))
            for task in task_results
            if plan_id in task.get("sampled_by_plan_ids", []) or (plan_id in task.get("plan_ids", []) and task.get("exec_status") != "success")
        ]
        sampled_indexes = [
            str(task.get("task", {}).get("task_index") or "")
            for task in task_results
            if plan_id in task.get("sampled_by_plan_ids", [])
        ]
        unsampled_indexes = [index for index in indexes if index not in set(sampled_indexes)]
        plan_results.append(
            {
                "plan_id": plan_id,
                "exec_status": str(exec_plans.get(plan_id, {}).get("status") or "unknown"),
                "task_indexes": indexes,
                "sampled_task_indexes": sampled_indexes,
                "unsampled_task_indexes": unsampled_indexes,
                "sampled_task_count": len(sampled_indexes),
                "unsampled_task_count": len(unsampled_indexes),
                "status": _plan_status(statuses),
                "verification_status_counts": {status: statuses.count(status) for status in sorted(TASK_VERIFICATION_STATUSES)},
            }
        )

    plan_task_set = set(task_to_plan)
    non_plan_task_results = [
        record
        for task_index, record in sorted(run_records.items())
        if task_index not in plan_task_set and record.get("task_complete_status") in {"complete_failed", "complete_unknown", "not_complete"}
    ]

    status = _aggregate_status(
        task_results,
        rerun.get("exit_code"),
        rerun_required=bool(verification_input.get("rerun_command") and selected_tasks),
    )
    if selected_tasks and monitor_required and not monitor_available:
        status = "inconclusive"
    if monitor_timed_out:
        status = "inconclusive"
    if selection.get("selection_errors") or smoke_mapping_errors:
        status = "inconclusive"

    result_payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "verification_mode": "smoke_test",
        "source": {
            "fix_plan_path": verification_input["fix_plan_path"],
            "exec_result_path": verification_input["exec_result_path"],
            "analyzer_output_path": verification_input["analyzer_output_path"],
            "verification_run_dir": verification_input["verification_run_dir"],
            "monitor_output_path": monitor_output_path,
            "smoke_task_source_path": selection["source"].get("task_source_path", ""),
            "smoke_selection_path": selection["source"].get("selection_path", ""),
        },
        "status": status,
        "rerun": {
            **rerun,
            "monitor_policy": monitor_policy,
            "monitor_available": monitor_available,
            "monitor_timed_out": monitor_timed_out,
        },
        "sampling": {
            "mode": "smoke_test",
            "selection_policy": "stable_hash",
            "limit_per_plan": task_limit,
            "sampled_task_count": len(selected_tasks),
            "plan_task_count": len(task_to_plan),
            "unsampled_task_count": max(0, len(task_to_plan) - len(selected_tasks)),
            "sampled_task_indexes": [str(task.get("original_task_index") or "") for task in selected_tasks if isinstance(task, dict)],
            "selection_errors": selection.get("selection_errors", []),
            "mapping_errors": smoke_mapping_errors,
        },
        "new_run_summary": new_run_summary,
        "plan_results": plan_results,
        "task_results": task_results,
        "non_plan_task_results": non_plan_task_results,
        "unexpected_run_task_results": unexpected_run_task_results,
    }
    validate_verification_result(result_payload)
    write_json(output_dir / "verification-result-latest.json", result_payload)
    return result_payload


def run_verification_from_paths(
    fix_plan_path: Path,
    exec_result_path: Path,
    analyzer_output_path: Path,
    verification_run_dir: Path,
    output_dir: Path,
    *,
    rerun_command: str | None = None,
    monitor_policy: str = "auto",
    monitor_wait_timeout: int = 3600,
    monitor_poll_interval: float = 30.0,
    verification_task_limit_per_plan: int = 2,
) -> dict[str, Any]:
    verification_input = build_verification_input(
        fix_plan_path,
        exec_result_path,
        analyzer_output_path,
        verification_run_dir,
        rerun_command=rerun_command,
        monitor_policy=monitor_policy,
        output_dir=output_dir,
        monitor_wait_timeout=monitor_wait_timeout,
        monitor_poll_interval=monitor_poll_interval,
        verification_task_limit_per_plan=verification_task_limit_per_plan,
    )
    return run_verification(verification_input, output_dir)
