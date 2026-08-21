"""Apply QZ mapping instruction handoffs before Harbor starts an agent."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from qz_template_resolver import (
    MAPPING_ENV_VAR,
    QzTemplateResolutionError,
    load_mapping,
    task_instruction_prefix,
)


@lru_cache(maxsize=4)
def _cached_mapping(path: str) -> dict[str, Any]:
    return load_mapping(Path(path))


def mapped_agent_instruction(
    mapping_path: Path,
    task_name: str,
    instruction: str,
) -> str:
    """Remove one exact setup prefix declared by the selected mapping task."""
    payload = _cached_mapping(str(mapping_path.expanduser().resolve()))
    prefix = task_instruction_prefix(payload, task_name)
    if not prefix:
        return instruction
    if not instruction.startswith(prefix):
        raise QzTemplateResolutionError(
            f"task {task_name!r} instruction does not match its mapped setup prefix"
        )
    agent_instruction = instruction[len(prefix) :]
    if not agent_instruction.strip():
        raise QzTemplateResolutionError(
            f"task {task_name!r} has no agent instruction after its mapped setup prefix"
        )
    return agent_instruction


def patch_harbor_task_instruction() -> None:
    """Patch Harbor Task construction when a QZ Template map is configured."""
    try:
        from harbor.models.task.task import Task
    except ImportError:
        return

    if getattr(Task, "_qz_instruction_patch_applied", False):
        return

    original_init = Task.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        mapping_path = os.environ.get(MAPPING_ENV_VAR, "").strip()
        if not mapping_path:
            return
        try:
            self.instruction = mapped_agent_instruction(
                Path(mapping_path),
                self.name,
                self.instruction,
            )
        except (OSError, QzTemplateResolutionError) as exc:
            raise RuntimeError(
                f"failed to prepare QZ agent instruction for {self.name!r}: {exc}"
            ) from exc

    Task.__init__ = patched_init
    Task._qz_instruction_patch_applied = True
