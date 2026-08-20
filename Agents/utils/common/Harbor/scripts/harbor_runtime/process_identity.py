"""Stable Linux process identity for Harbor workflow ownership records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int

    @classmethod
    def capture(cls, pid: int) -> ProcessIdentity | None:
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(
                ")", 1
            )[1].split()
            start_ticks = int(stat_fields[19])
        except (IndexError, OSError, ValueError):
            return None
        return cls(pid=pid, start_ticks=start_ticks)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ProcessIdentity:
        return cls(pid=int(value["pid"]), start_ticks=int(value["start_ticks"]))

    def as_dict(self) -> dict[str, int]:
        return {"pid": self.pid, "start_ticks": self.start_ticks}

    def is_live(self) -> bool:
        return self.pid > 0 and self.capture(self.pid) == self
