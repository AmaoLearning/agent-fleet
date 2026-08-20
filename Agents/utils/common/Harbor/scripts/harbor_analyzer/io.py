"""Small, dependency-free I/O helpers for analyzer artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor_runtime import (
    json_sha256,
    read_json_object,
)
from harbor_runtime import (
    utc_now as _runtime_utc_now,
)
from harbor_runtime import (
    write_json_atomic as _write_json_atomic,
)
from harbor_runtime import (
    write_text_atomic as _write_text_atomic,
)


def utc_now() -> str:
    return _runtime_utc_now(zulu=False)


def stable_hash(value: Any) -> str:
    return json_sha256(value)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = read_json_object(path)
    except FileNotFoundError as exc:
        raise ValueError(f"JSON file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON file {path}: {exc}") from exc
    except TypeError:
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def write_text_atomic(path: Path, content: str) -> None:
    _write_text_atomic(path, content)
