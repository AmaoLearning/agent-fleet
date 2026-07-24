"""Stage 2 Fix Exec implementation for Harbor Fixer MVP."""

from __future__ import annotations

import hashlib
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import read_json, write_json
from .validation import validate_exec_input, validate_exec_result, validate_fix_plan_set


SUMMARY_LIMIT = 4000


def _safe_label(value: str, prefix: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value).strip(".-")
    safe = safe[:60] or prefix
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tail_summary(value: str, *, limit: int = SUMMARY_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _resolve_cwd(workspace_root: Path, cwd: str) -> Path:
    path = Path(cwd)
    if path.is_absolute():
        return path
    return workspace_root / path


def _command_log_paths(
    output_dir: Path,
    plan_id: str,
    command_id: str,
    command_index: int,
) -> tuple[Path, Path]:
    command_label = f"{command_index + 1:04d}-{_safe_label(command_id, 'command')}"
    log_dir = output_dir / "command-logs" / _safe_label(plan_id, "plan") / command_label
    return log_dir / "stdout.txt", log_dir / "stderr.txt"


def _relative_log_paths(output_dir: Path, stdout_path: Path, stderr_path: Path) -> tuple[str, str]:
    return stdout_path.relative_to(output_dir).as_posix(), stderr_path.relative_to(output_dir).as_posix()


def _read_tail(path: Path, *, limit: int = SUMMARY_LIMIT) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except FileNotFoundError:
        return ""
    return data.decode("utf-8", errors="replace")


def _write_command_logs(
    output_dir: Path,
    plan_id: str,
    command_id: str,
    command_index: int,
    stdout: str,
    stderr: str,
) -> tuple[str, str]:
    stdout_path, stderr_path = _command_log_paths(output_dir, plan_id, command_id, command_index)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return _relative_log_paths(output_dir, stdout_path, stderr_path)


def build_exec_input(fix_plan_path: Path, workspace_root: Path) -> dict[str, Any]:
    fix_plan = read_json(fix_plan_path)
    validate_fix_plan_set(fix_plan)
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_exec_input",
        "fix_plan_path": str(fix_plan_path),
        "workspace_root": str(workspace_root.resolve()),
        "fix_plan": fix_plan,
    }
    validate_exec_input(payload)
    return payload


def _skipped_command_record(
    output_dir: Path,
    plan_id: str,
    command: dict[str, Any],
    command_index: int,
    cwd: Path,
    skip_reason: str,
) -> dict[str, Any]:
    now = _utc_now()
    stderr = skip_reason + "\n"
    stdout_path, stderr_path = _write_command_logs(output_dir, plan_id, command["command_id"], command_index, "", stderr)
    return {
        "command_id": command["command_id"],
        "cwd": str(cwd),
        "command": command["command"],
        "purpose": command["purpose"],
        "expected_effect": command["expected_effect"],
        "status": "skipped",
        "exit_code": None,
        "started_at": now,
        "finished_at": now,
        "duration_ms": 0,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_summary": "",
        "stderr_summary": _tail_summary(stderr),
        "skip_reason": skip_reason,
    }


def _failed_without_execution_record(
    output_dir: Path,
    plan_id: str,
    command: dict[str, Any],
    command_index: int,
    cwd: Path,
    error: str,
) -> dict[str, Any]:
    now = _utc_now()
    stderr = error + "\n"
    stdout_path, stderr_path = _write_command_logs(output_dir, plan_id, command["command_id"], command_index, "", stderr)
    return {
        "command_id": command["command_id"],
        "cwd": str(cwd),
        "command": command["command"],
        "purpose": command["purpose"],
        "expected_effect": command["expected_effect"],
        "status": "failed",
        "exit_code": None,
        "started_at": now,
        "finished_at": now,
        "duration_ms": 0,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_summary": "",
        "stderr_summary": _tail_summary(stderr),
        "skip_reason": "",
    }


def _run_command(
    output_dir: Path,
    workspace_root: Path,
    plan_id: str,
    command: dict[str, Any],
    command_index: int,
) -> dict[str, Any]:
    cwd = _resolve_cwd(workspace_root, command["cwd"]).resolve()
    try:
        cwd_exists = cwd.is_dir()
    except OSError as exc:
        return _failed_without_execution_record(
            output_dir,
            plan_id,
            command,
            command_index,
            cwd,
            f"command cwd is not accessible: {cwd}: {exc}",
        )
    if not cwd_exists:
        return _failed_without_execution_record(
            output_dir,
            plan_id,
            command,
            command_index,
            cwd,
            f"command cwd does not exist or is not a directory: {cwd}",
        )

    started_at = _utc_now()
    start = time.monotonic()
    stdout_file, stderr_file = _command_log_paths(output_dir, plan_id, command["command_id"], command_index)
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    with stdout_file.open("w", encoding="utf-8") as stdout_handle, stderr_file.open("w", encoding="utf-8") as stderr_handle:
        result = subprocess.run(
            ["bash", "-lc", command["command"]],
            cwd=cwd,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )
    finished_at = _utc_now()
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_path, stderr_path = _relative_log_paths(output_dir, stdout_file, stderr_file)
    return {
        "command_id": command["command_id"],
        "cwd": str(cwd),
        "command": command["command"],
        "purpose": command["purpose"],
        "expected_effect": command["expected_effect"],
        "status": "success" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_summary": _read_tail(stdout_file),
        "stderr_summary": _read_tail(stderr_file),
        "skip_reason": "",
    }


def run_fix_exec(exec_input: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    validate_exec_input(exec_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "exec-input.json", exec_input)

    workspace_root = Path(exec_input["workspace_root"])
    fix_plan = exec_input["fix_plan"]
    plan_results: list[dict[str, Any]] = []

    for plan in fix_plan["plans"]:
        plan_id = plan["plan_id"]
        command_results: list[dict[str, Any]] = []
        plan_failed = False
        for command_index, command in enumerate(plan["commands"]):
            try:
                cwd = _resolve_cwd(workspace_root, command["cwd"]).resolve()
            except OSError:
                cwd = _resolve_cwd(workspace_root, command["cwd"])
            if plan_failed:
                command_results.append(
                    _skipped_command_record(
                        output_dir,
                        plan_id,
                        command,
                        command_index,
                        cwd,
                        "previous command in this plan failed",
                    )
                )
                continue
            command_result = _run_command(output_dir, workspace_root, plan_id, command, command_index)
            command_results.append(command_result)
            if command_result["status"] == "failed":
                plan_failed = True
        plan_results.append(
            {
                "plan_id": plan_id,
                "status": "failed" if plan_failed else "success",
                "commands": command_results,
            }
        )

    failed_count = sum(1 for plan_result in plan_results if plan_result["status"] == "failed")
    if failed_count == 0:
        status = "success"
    elif failed_count == len(plan_results):
        status = "failed"
    else:
        status = "partial_failed"

    result_payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_exec_result",
        "source": {
            "fix_plan_path": exec_input["fix_plan_path"],
            "workspace_root": exec_input["workspace_root"],
        },
        "status": status,
        "plans": plan_results,
    }
    validate_exec_result(result_payload)
    write_json(output_dir / "exec-result-latest.json", result_payload)
    return result_payload


def run_fix_exec_from_plan(fix_plan_path: Path, output_dir: Path, workspace_root: Path) -> dict[str, Any]:
    exec_input = build_exec_input(fix_plan_path, workspace_root)
    return run_fix_exec(exec_input, output_dir)
