from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.modules.setdefault(
    "deepseek_harness",
    SimpleNamespace(DeepSeekHarness=object),
)

import dsh_sdk_minimal_runner


class DshSdkMinimalRunnerTests(unittest.TestCase):
    def test_requests_service_snapshot_before_harness_teardown(self) -> None:
        events: list[str] = []

        class Harness:
            def __init__(self, **_: object) -> None:
                pass

            def __enter__(self) -> Self:
                events.append("enter")
                return self

            def run(self, *_: object, **__: object) -> SimpleNamespace:
                events.append("run")
                return SimpleNamespace(
                    final_response="done",
                    finish_reason="stop",
                    session_id="session",
                )

            def __exit__(self, *_: object) -> None:
                events.append("exit")

        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            argv = [
                "sdk_minimal.py",
                "--dsh-home",
                str(root),
                "--dsh-bin",
                str(root / "dsh"),
                "--trace-path",
                str(root / "trace.jsonl"),
                "--service-snapshot-request",
                str(root / "snapshot-request"),
                "--service-snapshot-ready",
                str(root / "snapshot-ready"),
                "task",
            ]
            with (
                patch.object(dsh_sdk_minimal_runner, "DeepSeekHarness", Harness),
                patch.object(
                    dsh_sdk_minimal_runner,
                    "request_service_snapshot",
                    side_effect=lambda *_: events.append("snapshot"),
                ),
                patch.object(sys, "argv", argv),
            ):
                dsh_sdk_minimal_runner.main()

        self.assertEqual(events, ["enter", "run", "snapshot", "exit"])


if __name__ == "__main__":
    unittest.main()
