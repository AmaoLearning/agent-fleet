"""Literal file-edit path analysis for Policy Agent routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def analyze_paths(
    action: dict[str, Any],
    cwd: Path,
    writable_roots: list[Path],
    *,
    path_resolution_stable: bool = True,
) -> dict[str, Any]:
    """Route only a stable, inside-root file_edit to T2."""

    if action.get("action_type") != "file_edit":
        return {
            "classification": "not_file_edit",
            "write_targets": [],
            "analysis_complete": False,
            "path_resolution_stable": path_resolution_stable,
            "reason": "command actions require command policy evaluation",
        }

    raw = action.get("path")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return {
            "classification": "unknown_or_outside",
            "write_targets": [],
            "analysis_complete": False,
            "path_resolution_stable": path_resolution_stable,
            "reason": "file_edit path is invalid",
        }
    path = Path(raw)
    resolved = (path if path.is_absolute() else cwd / path).resolve()
    inside = any(resolved.is_relative_to(root) for root in writable_roots)
    target = {
        "raw": raw,
        "resolved": str(resolved),
        "inside_writable_roots": inside,
    }
    contained = path_resolution_stable and inside
    return {
        "classification": (
            "inside_writable_roots" if contained else "unknown_or_outside"
        ),
        "write_targets": [target],
        "analysis_complete": True,
        "path_resolution_stable": path_resolution_stable,
        "reason": (
            "stable file_edit target resolves inside writable roots"
            if contained
            else "file_edit target is outside the authorized boundary or unstable"
        ),
    }
