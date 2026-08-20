"""Canonical serialization and atomic artifact writes for Harbor workflows."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now(*, zulu: bool = True) -> str:
    value = datetime.now(timezone.utc).isoformat()
    return value.replace("+00:00", "Z") if zulu else value


def read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("expected a JSON object")
    return payload


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(8)}.tmp")
        try:
            fd = os.open(
                temp_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(path)
        except Exception:
            with suppress(FileNotFoundError):
                temp_path.unlink()
            raise
        return
    raise FileExistsError("could not allocate a temporary artifact file")


def write_text_atomic(path: Path, content: str, *, private: bool = False) -> None:
    if private:
        _write_private(path, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def write_json_atomic(
    path: Path, payload: dict[str, Any], *, private: bool = False
) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        private=private,
    )
