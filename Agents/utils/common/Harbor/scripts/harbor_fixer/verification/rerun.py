"""Launch and inspect a Harbor verification rerun."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..validation import task_key
from .run_state import generate_monitor_snapshot
from .selection import sort_task_index

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


def monitor_is_terminal(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("benchmark_status") == "completed" or snapshot.get(
        "monitor_follow_decision"
    ) in {"stop_completed", "stop_action_required"}


def wait_for_monitor(
    run_dir: Path,
    output_dir: Path,
    *,
    timeout_seconds: int,
    poll_interval: float,
) -> tuple[dict[str, Any] | None, str, bool]:
    deadline = time.monotonic() + timeout_seconds
    latest: tuple[dict[str, Any] | None, str] = (None, "")
    while time.monotonic() < deadline:
        latest = generate_monitor_snapshot(run_dir, output_dir)
        if latest[0] is not None and monitor_is_terminal(latest[0]):
            return *latest, False
        time.sleep(max(0.1, poll_interval))
    return *latest, True


def run_command(
    command: str | None,
    run_dir: Path,
    *,
    task_source_path: str,
    selection_path: str,
    should_run: bool,
) -> dict[str, Any]:
    skipped_reason = "" if should_run else "no_sampled_tasks"
    if not command or not should_run:
        return {
            "command": command or "",
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "stdout_summary": "",
            "stderr_summary": "",
            "skipped_reason": skipped_reason,
        }
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--rerun-command must not be empty")
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in RUN_SCOPED_ENV_VARS
    }
    env.update(
        {
            "TASK_SOURCE_FILE": str(Path(task_source_path).resolve()),
            "TASK_FILE": str(run_dir / "tasks.txt"),
            "OUTPUT_PATH": str(run_dir),
            "RESET_RUN": "1",
            "HARBOR_FIXER_SMOKE_SELECTION": str(Path(selection_path).resolve()),
        }
    )
    started_at = _utc_now()
    started = time.monotonic()
    result = subprocess.run(
        argv,
        cwd=run_dir,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return {
        "command": command,
        "exit_code": result.returncode,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_summary": result.stdout[-4000:],
        "stderr_summary": result.stderr[-4000:],
        "skipped_reason": "",
    }


def map_run_records(
    records: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> tuple[
    dict[tuple[str, str, str], dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    selected = {str(task["smoke_task_index"]): task for task in selection["tasks"]}
    errors: list[dict[str, Any]] = []
    if set(records) != set(selected):
        errors.append(
            {
                "error": "smoke_task_index_set_mismatch",
                "expected": sorted(selected, key=sort_task_index),
                "actual": sorted(records, key=sort_task_index),
            }
        )

    mapped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for smoke_index, task in selected.items():
        identity = {
            "task_index": task["original_task_index"],
            "task_name": task["task_name"],
            "attempt_id": task["attempt_id"],
        }
        record = records.get(smoke_index)
        if record is None:
            record = {
                "task_index": identity["task_index"],
                "task_name": identity["task_name"],
                "task_complete_status": "complete_unknown",
                "task_result_signals": ["result_missing"],
                "evidence": {},
                "result_path": "",
            }
        elif record["task_name"] != identity["task_name"]:
            errors.append(
                {
                    "error": "smoke_task_name_mismatch",
                    "smoke_task_index": smoke_index,
                    "expected_task_name": identity["task_name"],
                    "actual_task_name": record["task_name"],
                }
            )
        mapped[task_key(identity)] = {
            **record,
            "task_index": identity["task_index"],
            "task_name": identity["task_name"],
            "smoke_task_index": smoke_index,
        }

    unexpected = [
        records[index]
        for index in sorted(set(records) - set(selected), key=sort_task_index)
    ]
    return mapped, unexpected, errors
