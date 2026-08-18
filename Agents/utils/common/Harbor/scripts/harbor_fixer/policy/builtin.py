"""Built-in T1 allow and deny rules."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_DENY_EXECUTABLES = {"rm", "rmdir", "shred", "unlink", "wipefs"}
_READ_ONLY_COMMANDS = {
    "echo",
    "id",
    "printf",
    "pwd",
    "true",
    "uname",
    "which",
}
_DOCKERFILE_ROOT_FIND_RE = re.compile(
    r"(?im)^\s*RUN\s+(?:.*?[\s;&|])?(?:command\s+)?(?:/usr/bin/)?find\s+/(?:\s|$)"
)


def unsafe_file_edit_reason(action: dict[str, Any]) -> tuple[str, str] | None:
    """Reject newly introduced, deterministically unsafe file-edit content."""

    if action.get("action_type") != "file_edit":
        return None
    if Path(str(action.get("path") or "")).name.lower() != "dockerfile":
        return None
    edit = action.get("edit") if isinstance(action.get("edit"), dict) else {}
    old_text = str(edit.get("old_text") or "").replace("\\\n", " ")
    new_text = str(edit.get("new_text") or "").replace("\\\n", " ")
    if _DOCKERFILE_ROOT_FIND_RE.search(
        new_text
    ) and not _DOCKERFILE_ROOT_FIND_RE.search(old_text):
        return (
            "dockerfile_unbounded_root_scan",
            "Dockerfile edit introduces an unbounded find traversal from filesystem root",
        )
    return None


def destructive_reason(tokens: Sequence[str]) -> tuple[str, str] | None:
    """Return the built-in denial reason for one unambiguous executable."""

    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable in _DENY_EXECUTABLES or executable.startswith("mkfs"):
        return "builtin_destructive_command", executable
    return None


def read_only_reason(command_tokens: Sequence[str]) -> tuple[str, str] | None:
    """Return the built-in allow reason for one static bare executable."""

    if not command_tokens:
        return None
    executable = command_tokens[0]
    if Path(executable).name != executable or executable not in _READ_ONLY_COMMANDS:
        return None
    return (
        f"builtin_read_only_{executable}",
        f"built-in read-only command: {executable}",
    )
