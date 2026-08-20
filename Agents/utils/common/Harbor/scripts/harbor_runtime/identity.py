"""Canonical task identity shared across Harbor workflow stages."""

from __future__ import annotations

from typing import Any


def task_identity(task: dict[str, Any] | None) -> dict[str, Any]:
    task = task or {}
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def task_key(task: dict[str, Any] | None) -> tuple[str, str, str]:
    identity = task_identity(task)
    attempt_id = identity["attempt_id"]
    return (
        identity["task_index"],
        identity["task_name"],
        "" if attempt_id is None else str(attempt_id),
    )
