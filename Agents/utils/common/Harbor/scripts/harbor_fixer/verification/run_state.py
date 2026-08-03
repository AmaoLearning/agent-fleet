"""Read Harbor task results and monitor state for a verification run."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from harbor_monitor.artifacts import (
    TaskInput,
    load_manifest,
    load_task_file_manifest,
    load_task_records,
)
from harbor_monitor.classification import classify_task_status
from harbor_monitor.runner import run_loop

from ..artifact_io import read_json


def locate_queue_files(run_dir: Path) -> tuple[Path, Path]:
    queue_root = run_dir / "queue"
    if queue_root.is_dir():
        for queue_dir in sorted(queue_root.iterdir()):
            done, failed = queue_dir / "done.txt", queue_dir / "failed.txt"
            if done.exists() or failed.exists():
                return done, failed
    return run_dir / "done.txt", run_dir / "failed.txt"


def load_task_manifest(run_dir: Path) -> dict[str, str]:
    return load_manifest(run_dir / "task-manifest.tsv") or load_task_file_manifest(
        run_dir / "tasks.txt"
    )


def collect_task_results(
    run_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    done_path, failed_path = locate_queue_files(run_dir)
    tasks: dict[str, TaskInput] = load_task_records(done_path, failed_path)
    for index, name in load_task_manifest(run_dir).items():
        tasks.setdefault(index, TaskInput(task_index=index, task_name=name))

    records: dict[str, dict[str, Any]] = {}
    counts = {
        "total": len(tasks),
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }
    for index, task in sorted(tasks.items()):
        status, signals, evidence = classify_task_status(
            task, [run_dir, done_path.parent]
        )
        counts[status] += 1
        counts["finished"] += status != "not_complete"
        records[index] = {
            "task_index": index,
            "task_name": task.task_name,
            "task_complete_status": status,
            "task_result_signals": sorted(set(signals)),
            "evidence": evidence,
            "result_path": task.result_path or "",
        }
    if counts["finished"]:
        counts["success_rate"] = counts["complete_success"] / counts["finished"] * 100.0
    return records, counts


def read_monitor_snapshot(run_dir: Path) -> tuple[dict[str, Any] | None, str]:
    path = run_dir / "monitor" / "monitor-latest.json"
    if path.is_file():
        return read_json(path), str(path)
    return None, ""


def generate_monitor_snapshot(
    run_dir: Path, output_dir: Path
) -> tuple[dict[str, Any] | None, str]:
    done_path, failed_path = locate_queue_files(run_dir)
    monitor_dir = output_dir / "verification-monitor"
    monitor_output = monitor_dir / "monitor-latest.json"
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            run_loop(
                run_dir=run_dir,
                done_path=done_path,
                failed_path=failed_path,
                queue_dir=done_path.parent if done_path.parent.exists() else None,
                task_manifest_path=None,
                task_file_path=run_dir / "tasks.txt",
                restart_cmd=None,
                stop_cmd=None,
                output_path=monitor_output,
                poll_interval=1,
                max_retries=0,
                S_default=1800,
                S_min=900,
                S_max=3600,
                startup_grace=1,
                configured_timeout=None,
                total_override=None,
                running_override=None,
                claimed_override=None,
                remaining_override=None,
                user_report_output=monitor_dir / "user-notify-latest.json",
                analyzer_handover_output=monitor_dir / "analyzer-handover-latest.json",
                runner_action_output=monitor_dir / "runner-action-latest.json",
                loop_once=True,
                include_unknown_not_complete=True,
            )
    except (OSError, ValueError):
        return None, ""
    return (
        (read_json(monitor_output), str(monitor_output))
        if monitor_output.is_file()
        else (None, "")
    )
