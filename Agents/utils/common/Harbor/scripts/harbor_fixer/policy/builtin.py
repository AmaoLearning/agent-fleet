"""Built-in T1 allow and deny rules."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_DENY_EXECUTABLES = {"rm", "rmdir", "shred", "unlink", "wipefs"}
_READ_ONLY_COMMANDS = {
    "cat",
    "du",
    "echo",
    "grep",
    "head",
    "id",
    "ls",
    "printf",
    "pwd",
    "stat",
    "test",
    "true",
    "uname",
    "wc",
    "which",
}


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
