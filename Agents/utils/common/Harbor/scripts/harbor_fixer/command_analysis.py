"""Conservative shell-shape analysis for Harbor Fixer commands."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHELL_PUNCTUATION = frozenset("();&|<>")
SHELL_RESERVED_WORDS = {
    "!",
    "case",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "select",
    "then",
    "time",
    "until",
    "while",
    "{",
    "}",
}


@dataclass(frozen=True)
class CommandAnalysis:
    """Facts derived from a command without deciding whether to allow it."""

    classification: str
    argv: tuple[str, ...]
    executable_token: str
    interpreter: str
    has_embedded_script: bool
    shell_features: tuple[str, ...]
    command_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_version": 1,
            "classification": self.classification,
            "argv": list(self.argv),
            "executable_token": self.executable_token,
            "interpreter": self.interpreter,
            "has_embedded_script": self.has_embedded_script,
            "shell_features": list(self.shell_features),
            "command_sha256": self.command_sha256,
        }


def _shell_features(command: str) -> set[str]:
    features: set[str] = set()
    quote = ""
    escaped = False
    word_start = True
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            word_start = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if character == quote:
                quote = ""
            elif quote == '"' and character in "$`":
                features.add("expansion")
            index += 1
            continue
        if character in "'\"":
            quote = character
            word_start = False
        elif character in "\r\n":
            features.add("newline")
            word_start = True
        elif character.isspace():
            word_start = True
        elif character in ";&|()":
            features.add("control_operator")
            word_start = True
        elif character in "<>":
            features.add("redirection")
            if command[index : index + 2] == "<<":
                features.add("heredoc")
            word_start = True
        elif character in "$`":
            features.add("expansion")
            word_start = False
        elif character in "*?[":
            features.add("glob")
            word_start = False
        elif character in "{}":
            features.add("brace_expansion")
            word_start = False
        elif character == "~" and word_start:
            features.add("tilde_expansion")
            word_start = False
        elif character == "#" and word_start:
            features.add("comment")
            word_start = False
        else:
            word_start = False
        index += 1
    return features


def _lex_tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def lex_single_command(command: str) -> list[str] | None:
    """Lex one command, including dynamic words, without evaluating it."""

    if not command or "\x00" in command or any(char in command for char in "\r\n"):
        return None
    tokens = _lex_tokens(command)
    if (
        not tokens
        or tokens[0] in SHELL_RESERVED_WORDS
        or any(token and set(token) <= SHELL_PUNCTUATION for token in tokens)
    ):
        return None
    return tokens


def _is_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and name.replace("_", "a").isalnum()


def _interpreter_details(argv: list[str]) -> tuple[str, bool]:
    if not argv:
        return "", False
    executable = Path(argv[0]).name
    if executable.startswith(("python", "pypy")):
        return "python", "-c" in argv[1:]
    if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
        return "shell", any(
            "c" in option[1:] for option in argv[1:] if option.startswith("-")
        )
    if executable == "node":
        return "javascript", any(option in argv[1:] for option in ("-e", "--eval"))
    if executable in {"perl", "ruby"}:
        return executable, "-e" in argv[1:]
    return "", False


def analyze_command(command: str) -> CommandAnalysis:
    """Classify a raw command as static argv, shell script, or invalid."""

    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    features = _shell_features(command)
    if not command.strip() or "\x00" in command or _lex_tokens(command) is None:
        return CommandAnalysis(
            classification="invalid",
            argv=(),
            executable_token="",
            interpreter="",
            has_embedded_script=False,
            shell_features=tuple(sorted(features)),
            command_sha256=digest,
        )
    lexical_argv = lex_single_command(command)
    if lexical_argv is None:
        return CommandAnalysis(
            classification="shell_script",
            argv=(),
            executable_token="",
            interpreter="",
            has_embedded_script=False,
            shell_features=tuple(sorted(features)),
            command_sha256=digest,
        )

    if _is_assignment(lexical_argv[0]):
        features.add("environment_assignment")
    is_static = not features and lexical_argv[0] not in SHELL_RESERVED_WORDS
    interpreter, embedded_script = _interpreter_details(lexical_argv)
    return CommandAnalysis(
        classification="static_argv" if is_static else "shell_script",
        argv=tuple(lexical_argv) if is_static else (),
        executable_token=lexical_argv[0],
        interpreter=interpreter,
        has_embedded_script=embedded_script,
        shell_features=tuple(sorted(features)),
        command_sha256=digest,
    )
