"""Deterministic smoke-verification workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifact_io import read_json, write_json
from ..validation import (
    TASK_VERIFICATION_STATUSES,
    task_key,
    validate_verification_input,
    validate_verification_result,
)
from .rerun import map_run_records, run_command, wait_for_monitor
from .run_state import (
    collect_task_results,
    generate_monitor_snapshot,
    read_monitor_snapshot,
)
from .selection import build_smoke_selection, plan_exec_map, plan_tasks


def verification_status(exec_status: str, run_record: dict[str, Any] | None) -> str:
    if exec_status != "success":
        return "exec_failed"
    if run_record is None:
        return "not_sampled"
    return {
        "complete_success": "fixed",
        "complete_failed": "not_fixed",
        "complete_unknown": "unknown",
        "not_complete": "not_complete",
    }[str(run_record["task_complete_status"])]


def aggregate_status(statuses: list[str], rerun_exit_code: int | None) -> str:
    statuses = [status for status in statuses if status != "not_sampled"]
    if rerun_exit_code not in (None, 0) or not statuses:
        return "inconclusive"
    if "exec_failed" in statuses:
        return "exec_failed"
    if all(status == "fixed" for status in statuses):
        return "fixed"
    if "fixed" in statuses:
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in statuses):
        return "inconclusive"
    return "not_fixed"


def plan_status(statuses: list[str]) -> str:
    statuses = [status for status in statuses if status != "not_sampled"]
    if "exec_failed" in statuses:
        return "exec_failed"
    if statuses and all(status == "fixed" for status in statuses):
        return "fixed"
    if "fixed" in statuses:
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in statuses):
        return "inconclusive"
    return "not_fixed" if statuses else "inconclusive"


def build_verification_input(
    fix_plan_path: Path,
    exec_result_path: Path,
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
    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_input",
        "fix_plan_path": str(fix_plan_path),
        "exec_result_path": str(exec_result_path),
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


def run_verification(
    verification_input: dict[str, Any], output_dir: Path
) -> dict[str, Any]:
    validate_verification_input(verification_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "verification-input.json", verification_input)

    fix_plan = verification_input["fix_plan"]
    exec_plans = plan_exec_map(verification_input["exec_result"])
    tasks_by_plan = plan_tasks(fix_plan)
    run_dir = Path(verification_input["verification_run_dir"])
    monitor_policy = verification_input["monitor_policy"]
    limit = int(verification_input["verification_task_limit_per_plan"])
    selection = build_smoke_selection(
        fix_plan, exec_plans, limit_per_plan=limit, output_dir=output_dir
    )
    selected = selection["tasks"]
    rerun = run_command(
        verification_input["rerun_command"] or None,
        run_dir,
        task_source_path=selection["source"]["task_source_path"],
        selection_path=selection["source"]["selection_path"],
        should_run=bool(selected),
    )

    monitor, monitor_path = read_monitor_snapshot(run_dir)
    monitor_timed_out = False
    if selected and monitor_policy in {"auto", "on"}:
        if verification_input["rerun_command"]:
            monitor, monitor_path, monitor_timed_out = wait_for_monitor(
                run_dir,
                output_dir,
                timeout_seconds=int(verification_input["monitor_wait_timeout"]),
                poll_interval=float(verification_input["monitor_poll_interval"]),
            )
        elif monitor is None:
            monitor, monitor_path = generate_monitor_snapshot(run_dir, output_dir)

    empty_summary = {
        "total": 0,
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }
    records, run_summary = (
        collect_task_results(run_dir) if selected else ({}, empty_summary)
    )
    mapped, unexpected, mapping_errors = map_run_records(records, selection)
    sampled_keys = {
        task_key(
            {
                "task_index": task["original_task_index"],
                "task_name": task["task_name"],
                "attempt_id": task["attempt_id"],
            }
        )
        for task in selected
    }
    smoke_indexes = {
        task_key(
            {
                "task_index": task["original_task_index"],
                "task_name": task["task_name"],
                "attempt_id": task["attempt_id"],
            }
        ): task["smoke_task_index"]
        for task in selected
    }

    task_results: list[dict[str, Any]] = []
    plan_results: list[dict[str, Any]] = []
    for plan_id, tasks in tasks_by_plan.items():
        exec_status = str(exec_plans[plan_id]["status"])
        plan_task_results: list[dict[str, Any]] = []
        for task in tasks:
            key = task_key(task)
            record = mapped.get(key) if key in sampled_keys else None
            result = {
                "task": {
                    "task_index": str(task["task_index"]),
                    "task_name": str(task["task_name"]),
                    "attempt_id": task["attempt_id"],
                },
                "plan_id": plan_id,
                "sampled": key in sampled_keys,
                "smoke_task_index": smoke_indexes.get(key, ""),
                "exec_status": exec_status,
                "new_run": record,
                "verification_status": verification_status(exec_status, record),
            }
            task_results.append(result)
            plan_task_results.append(result)

        sampled_indexes = [
            result["task"]["task_index"]
            for result in plan_task_results
            if result["sampled"]
        ]
        statuses = [result["verification_status"] for result in plan_task_results]
        all_indexes = [str(task["task_index"]) for task in tasks]
        plan_results.append(
            {
                "plan_id": plan_id,
                "exec_status": exec_status,
                "task_indexes": all_indexes,
                "sampled_task_indexes": sampled_indexes,
                "unsampled_task_indexes": [
                    i for i in all_indexes if i not in sampled_indexes
                ],
                "sampled_task_count": len(sampled_indexes),
                "unsampled_task_count": len(tasks) - len(sampled_indexes),
                "status": plan_status(statuses),
                "verification_status_counts": {
                    status: statuses.count(status)
                    for status in sorted(TASK_VERIFICATION_STATUSES)
                },
            }
        )

    plan_task_count = sum(len(tasks) for tasks in tasks_by_plan.values())
    run_summary.update(
        {
            "scope": "smoke_sample",
            "verification_mode": "smoke_test",
            "sampled_task_count": len(selected),
            "plan_task_count": plan_task_count,
            "unsampled_task_count": plan_task_count - len(selected),
        }
    )
    status = aggregate_status(
        [result["verification_status"] for result in task_results],
        rerun["exit_code"],
    )
    if (
        mapping_errors
        or monitor_timed_out
        or (selected and monitor_policy == "on" and monitor is None)
    ):
        status = "inconclusive"

    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "verification_mode": "smoke_test",
        "source": {
            "fix_plan_path": verification_input["fix_plan_path"],
            "exec_result_path": verification_input["exec_result_path"],
            "verification_run_dir": verification_input["verification_run_dir"],
            "monitor_output_path": monitor_path,
            "smoke_task_source_path": selection["source"]["task_source_path"],
            "smoke_selection_path": selection["source"]["selection_path"],
        },
        "status": status,
        "rerun": {
            **rerun,
            "monitor_policy": monitor_policy,
            "monitor_available": monitor is not None,
            "monitor_timed_out": monitor_timed_out,
        },
        "sampling": {
            "mode": "smoke_test",
            "selection_policy": "stable_hash",
            "limit_per_plan": limit,
            "sampled_task_count": len(selected),
            "plan_task_count": plan_task_count,
            "unsampled_task_count": plan_task_count - len(selected),
            "sampled_task_indexes": [task["original_task_index"] for task in selected],
            "mapping_errors": mapping_errors,
        },
        "new_run_summary": run_summary,
        "plan_results": plan_results,
        "task_results": task_results,
        "unexpected_run_task_results": unexpected,
    }
    validate_verification_result(payload)
    write_json(output_dir / "verification-result-latest.json", payload)
    return payload


def run_verification_from_paths(
    fix_plan_path: Path,
    exec_result_path: Path,
    verification_run_dir: Path,
    output_dir: Path,
    *,
    rerun_command: str | None = None,
    monitor_policy: str = "auto",
    monitor_wait_timeout: int = 3600,
    monitor_poll_interval: float = 30.0,
    verification_task_limit_per_plan: int = 2,
) -> dict[str, Any]:
    payload = build_verification_input(
        fix_plan_path,
        exec_result_path,
        verification_run_dir,
        rerun_command=rerun_command,
        monitor_policy=monitor_policy,
        output_dir=output_dir,
        monitor_wait_timeout=monitor_wait_timeout,
        monitor_poll_interval=monitor_poll_interval,
        verification_task_limit_per_plan=verification_task_limit_per_plan,
    )
    return run_verification(payload, output_dir)
