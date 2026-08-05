"""Deterministic analysis of structured Harbor Fixer command actions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandAnalysis:
    """Facts copied or derived from a command action without a policy verdict."""

    classification: str
    argv: tuple[str, ...]
    executable_token: str
    interpreter: str
    has_embedded_script: bool
    action_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_version": 2,
            "classification": self.classification,
            "argv": list(self.argv),
            "executable_token": self.executable_token,
            "interpreter": self.interpreter,
            "has_embedded_script": self.has_embedded_script,
            "action_sha256": self.action_sha256,
        }


def _action_sha256(action: Any) -> str:
    serialized = json.dumps(
        action,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _interpreter_details(argv: tuple[str, ...]) -> tuple[str, bool]:
    executable = Path(argv[0]).name
    arguments = argv[1:]
    if executable.startswith(("python", "pypy")):
        return "python", "-c" in arguments
    if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
        return "shell", any(
            "c" in option[1:] for option in arguments if option.startswith("-")
        )
    if executable == "node":
        return "javascript", any(option in arguments for option in ("-e", "--eval"))
    if executable in {"perl", "ruby"}:
        return executable, "-e" in arguments
    return "", False


def analyze_command(action: dict[str, Any]) -> CommandAnalysis:
    """Copy an exact argv from a command action and identify interpreter payloads."""

    digest = _action_sha256(action)
    executable = action.get("executable")
    arguments = action.get("arguments")
    valid = (
        action.get("action_type") == "command"
        and isinstance(executable, str)
        and bool(executable)
        and "\x00" not in executable
        and not any(character.isspace() for character in executable)
        and isinstance(arguments, list)
        and all(isinstance(argument, str) and "\x00" not in argument for argument in arguments)
    )
    if not valid:
        return CommandAnalysis(
            classification="invalid",
            argv=(),
            executable_token="",
            interpreter="",
            has_embedded_script=False,
            action_sha256=digest,
        )
    argv = (executable, *arguments)
    interpreter, embedded_script = _interpreter_details(argv)
    return CommandAnalysis(
        classification="static_argv",
        argv=argv,
        executable_token=executable,
        interpreter=interpreter,
        has_embedded_script=embedded_script,
        action_sha256=digest,
    )
