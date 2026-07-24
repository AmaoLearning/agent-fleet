"""Inspect Harbor run task state and monitor snapshots for Fixer stages."""

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

from .artifact_io import read_json


def locate_queue_files(run_dir: Path) -> tuple[Path, Path]:
    queue_root = run_dir / "queue"
    if queue_root.is_dir():
        for queue_dir in sorted(queue_root.iterdir()):
            if not queue_dir.is_dir():
                continue
            done = queue_dir / "done.txt"
            failed = queue_dir / "failed.txt"
            if done.exists() or failed.exists():
                return done, failed
    return run_dir / "done.txt", run_dir / "failed.txt"


def load_task_manifest(run_dir: Path) -> dict[str, str]:
    candidates = [
        run_dir / "task-manifest.tsv",
        run_dir / "task_manifest.tsv",
        run_dir / "manifest.tsv",
    ]
    for candidate in candidates:
        manifest = load_manifest(candidate)
        if manifest:
            return manifest
    return load_task_file_manifest(run_dir / "tasks.txt")


def collect_task_results(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    done_path, failed_path = locate_queue_files(run_dir)
    tasks: dict[str, TaskInput] = load_task_records(done_path, failed_path)
    for task_index, task_name in load_task_manifest(run_dir).items():
        tasks.setdefault(task_index, TaskInput(task_index=task_index, task_name=task_name))

    records: dict[str, dict[str, Any]] = {}
    summary = {
        "total": len(tasks),
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }
    for task_index, task in sorted(tasks.items(), key=lambda item: item[0]):
        status, signals, evidence = classify_task_status(task, [run_dir, done_path.parent])
        summary[status] += 1
        if status != "not_complete":
            summary["finished"] += 1
        records[task_index] = {
            "task_index": task_index,
            "task_name": task.task_name,
            "task_complete_status": status,
            "task_result_signals": sorted(set(signals)),
            "evidence": evidence,
            "result_path": task.result_path or "",
        }
    finished = int(summary["finished"])
    summary["success_rate"] = (float(summary["complete_success"]) / finished * 100.0) if finished else 0.0
    return records, summary


def read_monitor_snapshot(run_dir: Path, output_dir: Path, artifact_dir_name: str) -> tuple[dict[str, Any] | None, str]:
    candidates = [
        run_dir / "monitor" / "monitor-latest.json",
        run_dir / "monitor-latest.json",
        output_dir / artifact_dir_name / "monitor-latest.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            payload = read_json(candidate)
            if not _monitor_snapshot_matches_run_dir(payload, run_dir):
                continue
            return payload, str(candidate)
    return None, ""


def _monitor_snapshot_matches_run_dir(payload: dict[str, Any], run_dir: Path) -> bool:
    user_notify = payload.get("user_notify")
    if not isinstance(user_notify, dict):
        return True
    paths = user_notify.get("paths")
    if not isinstance(paths, dict):
        return True
    recorded = paths.get("run_dir")
    if not isinstance(recorded, str) or not recorded:
        return True
    try:
        return Path(recorded).resolve() == run_dir.resolve()
    except OSError:
        return recorded == str(run_dir)


def generate_monitor_snapshot(
    run_dir: Path,
    output_dir: Path,
    artifact_dir_name: str,
) -> tuple[dict[str, Any] | None, str]:
    done_path, failed_path = locate_queue_files(run_dir)
    queue_dir = done_path.parent if done_path.parent.exists() else None
    monitor_dir = output_dir / artifact_dir_name
    monitor_output = monitor_dir / "monitor-latest.json"
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            run_loop(
                run_dir=run_dir,
                done_path=done_path,
                failed_path=failed_path,
                queue_dir=queue_dir,
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
    except Exception:
        return None, ""
    if monitor_output.is_file():
        return read_json(monitor_output), str(monitor_output)
    return None, ""
