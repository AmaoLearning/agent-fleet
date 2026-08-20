"""Focused tests for shared Harbor workflow runtime helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harbor_runtime import (
    ProcessIdentity,
    json_sha256,
    read_json_object,
    task_identity,
    task_key,
    utc_now,
    write_json_atomic,
)


class HarborRuntimeTest(unittest.TestCase):
    def test_canonical_hash_and_timestamp_formats(self) -> None:
        self.assertEqual(
            json_sha256({"b": 2, "a": 1}),
            json_sha256({"a": 1, "b": 2}),
        )
        self.assertTrue(utc_now().endswith("Z"))
        self.assertTrue(utc_now(zulu=False).endswith("+00:00"))

    def test_atomic_json_preserves_legacy_and_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "legacy.json"
            private = root / "private.json"
            old_umask = os.umask(0o022)
            try:
                write_json_atomic(legacy, {"ok": True})
                write_json_atomic(private, {"ok": True}, private=True)
            finally:
                os.umask(old_umask)

            self.assertEqual(read_json_object(legacy), {"ok": True})
            self.assertEqual(legacy.stat().st_mode & 0o777, 0o644)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

    def test_process_identity_round_trips_and_checks_liveness(self) -> None:
        identity = ProcessIdentity.capture(os.getpid())
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(ProcessIdentity.from_mapping(identity.as_dict()), identity)
        self.assertTrue(identity.is_live())
        self.assertIsNone(ProcessIdentity.capture(999_999_999))

    def test_task_identity_normalizes_the_cross_stage_key(self) -> None:
        task = {"task_index": 7, "task_name": "fixture", "attempt_id": None}
        self.assertEqual(
            task_identity(task),
            {"task_index": "7", "task_name": "fixture", "attempt_id": None},
        )
        self.assertEqual(task_key(task), ("7", "fixture", ""))


if __name__ == "__main__":
    unittest.main()
