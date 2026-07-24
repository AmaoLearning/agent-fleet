"""Stage 3 Fix Exec implementation for Harbor Fixer."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_invocation import AgentInvoker
from .artifact_io import read_json, write_json, write_json_atomic
from .command_analysis import analyze_command
from .policy import run_policy_preflight
from .validation import validate_exec_input, validate_exec_result, validate_fix_plan_set

SUMMARY_LIMIT = 4000
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_GRACE_SECONDS = 5


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _safe_label(value: str, prefix: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "._-" else "-" for char in value
    ).strip(".-")
    safe = safe[:60] or prefix
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tail_summary(value: str, *, limit: int = SUMMARY_LIMIT) -> str:
    return value if len(value) <= limit else value[-limit:]


def _resolve_cwd(workspace_root: Path, cwd: str) -> Path:
    path = Path(cwd)
    return (path if path.is_absolute() else workspace_root / path).resolve()


def _action_log_paths(
    output_dir: Path,
    plan_id: str,
    action_id: str,
    action_index: int,
) -> tuple[Path, Path]:
    label = f"{action_index + 1:04d}-{_safe_label(action_id, 'action')}"
    log_dir = output_dir / "action-logs" / _safe_label(plan_id, "plan") / label
    return log_dir / "stdout.txt", log_dir / "stderr.txt"


def _relative_log_paths(
    output_dir: Path, stdout_path: Path, stderr_path: Path
) -> tuple[str, str]:
    return stdout_path.relative_to(output_dir).as_posix(), stderr_path.relative_to(
        output_dir
    ).as_posix()


def _read_tail(path: Path, *, limit: int = SUMMARY_LIMIT) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - limit))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _write_action_logs(
    output_dir: Path,
    plan_id: str,
    action_id: str,
    action_index: int,
    stdout: str,
    stderr: str,
) -> tuple[str, str]:
    stdout_path, stderr_path = _action_log_paths(
        output_dir, plan_id, action_id, action_index
    )
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


def _action_record(
    output_dir: Path,
    plan_id: str,
    action: dict[str, Any],
    action_index: int,
    cwd: Path,
    *,
    status: str,
    exit_code: int | None,
    started_at: str,
    duration_ms: int,
    stdout: str = "",
    stderr: str = "",
    skip_reason: str = "",
) -> dict[str, Any]:
    stdout_path, stderr_path = _write_action_logs(
        output_dir,
        plan_id,
        action["action_id"],
        action_index,
        stdout,
        stderr,
    )
    return {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "cwd": str(cwd),
        "purpose": action["purpose"],
        "expected_effect": action["expected_effect"],
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": duration_ms,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_summary": _tail_summary(stdout),
        "stderr_summary": _tail_summary(stderr),
        "skip_reason": skip_reason,
    }


def _unexecuted_action_record(
    output_dir: Path,
    plan_id: str,
    action: dict[str, Any],
    action_index: int,
    cwd: Path,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    now = _utc_now()
    return _action_record(
        output_dir,
        plan_id,
        action,
        action_index,
        cwd,
        status=status,
        exit_code=None,
        started_at=now,
        duration_ms=0,
        stderr=reason + "\n",
        skip_reason=reason if status == "skipped" else "",
    )


def _binding_error(
    action: dict[str, Any], cwd: Path, decision: dict[str, Any]
) -> str | None:
    if _json_sha256(action) != decision["action_sha256"]:
        return "action no longer matches the policy decision"
    if str(cwd) != decision["resolved_cwd"]:
        return "action cwd no longer matches the policy decision"
    return None


def _terminate_process_group(process: subprocess.Popen[str]) -> int:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.wait()
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return process.wait()


def _run_command_action(
    output_dir: Path,
    plan_id: str,
    action: dict[str, Any],
    action_index: int,
    cwd: Path,
    decision: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    analysis = analyze_command(action).as_dict()
    if analysis != decision["command_analysis"]:
        return _unexecuted_action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            reason="command no longer matches the policy analysis",
        )
    resolved_executable = decision["resolved_executable"]
    if not resolved_executable:
        return _unexecuted_action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            reason="policy did not bind the command to an executable",
        )

    started_at = _utc_now()
    start = time.monotonic()
    stdout_file, stderr_file = _action_log_paths(
        output_dir, plan_id, action["action_id"], action_index
    )
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            stdout_file.open("w", encoding="utf-8") as stdout_handle,
            stderr_file.open("w", encoding="utf-8") as stderr_handle,
        ):
            process = subprocess.Popen(
                [resolved_executable, *action["arguments"]],
                cwd=cwd,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            timed_out = False
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                return_code = _terminate_process_group(process)
                stderr_handle.write(
                    f"command timed out after {timeout_seconds:g} seconds\n"
                )
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return _action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            exit_code=None,
            started_at=started_at,
            duration_ms=duration_ms,
            stderr=f"command execution failed: {exc}\n",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_path, stderr_path = _relative_log_paths(output_dir, stdout_file, stderr_file)
    return {
        "action_id": action["action_id"],
        "action_type": "command",
        "cwd": str(cwd),
        "purpose": action["purpose"],
        "expected_effect": action["expected_effect"],
        "status": "success" if return_code == 0 and not timed_out else "failed",
        "exit_code": None if timed_out else return_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": duration_ms,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_summary": _read_tail(stdout_file),
        "stderr_summary": _read_tail(stderr_file),
        "skip_reason": "",
    }


def _open_directory_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd


def _open_authorized_parent(target: Path, writable_roots: list[Path]) -> tuple[int, str]:
    containing_roots = [root for root in writable_roots if target.is_relative_to(root)]
    if not containing_roots:
        raise PermissionError("file_edit target is outside the authorized roots")
    root = max(containing_roots, key=lambda value: len(value.parts))
    relative = target.relative_to(root)
    if not relative.parts:
        raise PermissionError("file_edit target must be below an authorized root")
    directory_fd = _open_directory_nofollow(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd, relative.parts[-1]


def _create_temp_file(parent_fd: int, target_name: str, mode: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    for index in range(100):
        name = f".{target_name}.{os.getpid()}.{index}.tmp"
        try:
            return os.open(name, flags, mode, dir_fd=parent_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a file_edit temporary file")


def _run_file_edit_action(
    output_dir: Path,
    plan_id: str,
    action: dict[str, Any],
    action_index: int,
    cwd: Path,
    decision: dict[str, Any],
    writable_roots: list[Path],
) -> dict[str, Any]:
    raw_target = Path(action["path"])
    target = (raw_target if raw_target.is_absolute() else cwd / raw_target).resolve()
    targets = decision["path_analysis"].get("write_targets", [])
    if (
        decision["tier"] != "T2"
        or len(targets) != 1
        or str(target) != targets[0].get("resolved")
    ):
        return _unexecuted_action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            reason="file_edit target no longer matches the policy analysis",
        )

    started_at = _utc_now()
    start = time.monotonic()
    parent_fd: int | None = None
    temp_name = ""
    try:
        parent_fd, target_name = _open_authorized_parent(target, writable_roots)
        target_fd = os.open(
            target_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        with os.fdopen(target_fd, encoding="utf-8") as handle:
            target_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(target_stat.st_mode):
                raise ValueError("file_edit target must be a regular file")
            original = handle.read()
        edit = action["edit"]
        actual_replacements = original.count(edit["old_text"])
        if actual_replacements != edit["expected_replacements"]:
            raise ValueError(
                "file_edit expected "
                f"{edit['expected_replacements']} replacements but found "
                f"{actual_replacements}"
            )
        updated = original.replace(edit["old_text"], edit["new_text"])
        temp_fd, temp_name = _create_temp_file(
            parent_fd, target_name, stat.S_IMODE(target_stat.st_mode)
        )
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(target_stat.st_mode))
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        current_fd = os.open(
            target_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        with os.fdopen(current_fd, "rb") as handle:
            current_stat = os.fstat(handle.fileno())
        if (current_stat.st_dev, current_stat.st_ino) != (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            raise OSError("file_edit target changed during execution")
        os.replace(
            temp_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_name = ""
    except (OSError, UnicodeError, ValueError) as exc:
        if parent_fd is not None and temp_name:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        duration_ms = int((time.monotonic() - start) * 1000)
        return _action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            exit_code=None,
            started_at=started_at,
            duration_ms=duration_ms,
            stderr=f"file_edit failed: {exc}\n",
        )
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    duration_ms = int((time.monotonic() - start) * 1000)
    return _action_record(
        output_dir,
        plan_id,
        action,
        action_index,
        cwd,
        status="success",
        exit_code=0,
        started_at=started_at,
        duration_ms=duration_ms,
        stdout=f"updated {target}\n",
    )


def _run_action(
    output_dir: Path,
    workspace_root: Path,
    plan_id: str,
    action: dict[str, Any],
    action_index: int,
    decision: dict[str, Any],
    writable_roots: list[Path],
    timeout_seconds: float,
) -> dict[str, Any]:
    cwd = _resolve_cwd(workspace_root, action["cwd"])
    error = _binding_error(action, cwd, decision)
    if error is not None:
        return _unexecuted_action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            reason=error,
        )
    try:
        cwd_exists = cwd.is_dir()
    except OSError as exc:
        error = f"action cwd is not accessible: {cwd}: {exc}"
    else:
        error = None if cwd_exists else f"action cwd does not exist: {cwd}"
    if error is not None:
        return _unexecuted_action_record(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            status="failed",
            reason=error,
        )
    if action["action_type"] == "command":
        return _run_command_action(
            output_dir,
            plan_id,
            action,
            action_index,
            cwd,
            decision,
            timeout_seconds,
        )
    return _run_file_edit_action(
        output_dir,
        plan_id,
        action,
        action_index,
        cwd,
        decision,
        writable_roots,
    )


def _policy_fields(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        key: decision[key]
        for key in (
            "tier",
            "decision",
            "risk_level",
            "source",
            "rule_id",
            "reason_code",
            "reason",
        )
    }


def _policy_blocked_result(
    exec_input: dict[str, Any], output_dir: Path, policy_result: dict[str, Any]
) -> dict[str, Any]:
    workspace_root = Path(exec_input["workspace_root"])
    decisions = {
        (decision["plan_id"], decision["action_id"]): decision
        for decision in policy_result["decisions"]
    }
    plan_results = []
    for plan in exec_input["fix_plan"]["plans"]:
        action_results = []
        for action_index, action in enumerate(plan["actions"]):
            decision = decisions[(plan["plan_id"], action["action_id"])]
            denied = decision["decision"] == "deny"
            reason = (
                f"execution policy denied action: {decision['reason']}"
                if denied
                else "execution blocked because policy preflight denied another action"
            )
            record = _unexecuted_action_record(
                output_dir,
                plan["plan_id"],
                action,
                action_index,
                _resolve_cwd(workspace_root, action["cwd"]),
                status="failed" if denied else "skipped",
                reason=reason,
            )
            record["policy"] = _policy_fields(decision)
            action_results.append(record)
        plan_results.append(
            {"plan_id": plan["plan_id"], "status": "failed", "actions": action_results}
        )
    return _publish_result(exec_input, output_dir, "failed", "denied", plan_results)


def _publish_result(
    exec_input: dict[str, Any],
    output_dir: Path,
    status: str,
    policy_status: str,
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_exec_result",
        "source": {
            "fix_plan_path": exec_input["fix_plan_path"],
            "workspace_root": exec_input["workspace_root"],
            "policy_decision_path": str(output_dir / "execution-policy-decision.json"),
        },
        "status": status,
        "policy_status": policy_status,
        "plans": plans,
    }
    validate_exec_result(payload)
    write_json_atomic(output_dir / "exec-result-latest.json", payload)
    return payload


def run_fix_exec(
    exec_input: dict[str, Any],
    output_dir: Path,
    *,
    policy_invoker: AgentInvoker | None = None,
    policy_rules_path: Path | None = None,
    policy_write_roots: list[Path] | None = None,
    execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    validate_exec_input(exec_input)
    if execution_timeout_seconds <= 0:
        raise ValueError("execution_timeout_seconds must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "exec-input.json", exec_input)

    workspace_root = Path(exec_input["workspace_root"])
    fix_plan = exec_input["fix_plan"]
    policy_result = run_policy_preflight(
        fix_plan,
        workspace_root,
        output_dir,
        policy_invoker,
        user_rules_path=policy_rules_path,
        writable_roots=policy_write_roots,
    )
    if policy_result["fix_plan_sha256"] != _json_sha256(fix_plan):
        raise RuntimeError("policy decision does not match the execution fix plan")
    if policy_result["status"] == "denied":
        return _policy_blocked_result(exec_input, output_dir, policy_result)
    decisions = {
        (decision["plan_id"], decision["action_id"]): decision
        for decision in policy_result["decisions"]
    }
    writable_roots = [Path(value) for value in policy_result["writable_roots"]]
    plan_results = []
    for plan in fix_plan["plans"]:
        action_results = []
        plan_failed = False
        for action_index, action in enumerate(plan["actions"]):
            if plan_failed:
                result = _unexecuted_action_record(
                    output_dir,
                    plan["plan_id"],
                    action,
                    action_index,
                    _resolve_cwd(workspace_root, action["cwd"]),
                    status="skipped",
                    reason="previous action in this plan failed",
                )
            else:
                result = _run_action(
                    output_dir,
                    workspace_root,
                    plan["plan_id"],
                    action,
                    action_index,
                    decisions[(plan["plan_id"], action["action_id"])],
                    writable_roots,
                    execution_timeout_seconds,
                )
                plan_failed = result["status"] == "failed"
            action_results.append(result)
        plan_results.append(
            {
                "plan_id": plan["plan_id"],
                "status": "failed" if plan_failed else "success",
                "actions": action_results,
            }
        )

    failed_count = sum(plan["status"] == "failed" for plan in plan_results)
    status = (
        "success"
        if failed_count == 0
        else "failed"
        if failed_count == len(plan_results)
        else "partial_failed"
    )
    return _publish_result(exec_input, output_dir, status, "allowed", plan_results)


def run_fix_exec_from_plan(
    fix_plan_path: Path,
    output_dir: Path,
    workspace_root: Path,
    *,
    policy_invoker: AgentInvoker | None = None,
    policy_rules_path: Path | None = None,
    policy_write_roots: list[Path] | None = None,
    execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return run_fix_exec(
        build_exec_input(fix_plan_path, workspace_root),
        output_dir,
        policy_invoker=policy_invoker,
        policy_rules_path=policy_rules_path,
        policy_write_roots=policy_write_roots,
        execution_timeout_seconds=execution_timeout_seconds,
    )
