from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import dsh_service_handoff
from dsh_service_handoff import (
    ProcessIdentity,
    ProcessRecord,
    capture_new_listeners,
    restore_services,
    watch_new_listeners,
)


def write_process(
    proc_root: Path,
    *,
    pid: int,
    parent_pid: int,
    started: str,
    inode: str,
    argv: tuple[str, ...],
    environment: tuple[str, ...] = (),
) -> None:
    process = proc_root / str(pid)
    (process / "fd").mkdir(parents=True)
    suffix = ["S", str(parent_pid), *("0" for _ in range(17)), started]
    (process / "stat").write_text(
        f"{pid} (test service) {' '.join(suffix)}\n", encoding="utf-8"
    )
    (process / "cmdline").write_bytes(
        b"\0".join(item.encode() for item in argv) + b"\0"
    )
    (process / "environ").write_bytes(
        b"\0".join(item.encode() for item in environment) + b"\0"
    )
    (process / "cwd-target").mkdir()
    os.symlink(process / "cwd-target", process / "cwd")
    os.symlink(f"socket:[{inode}]", process / "fd" / "3")


class ServiceHandoffTests(unittest.TestCase):
    def test_watcher_retains_listener_seen_immediately_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            control = root / "active"
            manifest = root / "services.json"
            control.touch()
            record = ProcessRecord(
                pid=42,
                started="123",
                parent_pid=1,
                argv=["python", "server.py"],
                cwd=str(root),
                env={"APP_MODE": "test"},
                ports=[8080],
            )

            def stop_watcher(_: float) -> None:
                control.unlink()

            with (
                patch.object(dsh_service_handoff, "snapshot_processes", return_value=set()),
                patch.object(
                    dsh_service_handoff,
                    "_new_listener_records",
                    return_value=[record],
                ),
                patch.object(dsh_service_handoff.time, "sleep", side_effect=stop_watcher),
            ):
                records = watch_new_listeners(control, manifest)

            self.assertEqual(records, [record])
            self.assertEqual(json.loads(manifest.read_text())["processes"][0]["pid"], 42)

    def test_capture_selects_new_listener_master_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            proc_root = root / "proc"
            (proc_root / "net").mkdir(parents=True)
            (proc_root / "net" / "tcp").write_text(
                "header\n"
                "0: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 4242\n",
                encoding="ascii",
            )
            (proc_root / "net" / "tcp6").write_text("header\n", encoding="ascii")
            write_process(
                proc_root,
                pid=100,
                parent_pid=1,
                started="1000",
                inode="1111",
                argv=("old-server",),
            )
            write_process(
                proc_root,
                pid=200,
                parent_pid=1,
                started="2000",
                inode="4242",
                argv=("python", "-m", "server"),
                environment=("PATH=/bin", "APP_MODE=test", "API_KEY=secret"),
            )
            write_process(
                proc_root,
                pid=201,
                parent_pid=200,
                started="2001",
                inode="4242",
                argv=("python", "worker"),
            )
            manifest = root / "handoff" / "services.json"

            records = capture_new_listeners(
                {ProcessIdentity(pid=100, started="1000")},
                manifest,
                proc_root=proc_root,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].pid, 200)
            self.assertEqual(records[0].ports, [8080])
            self.assertEqual(records[0].env["APP_MODE"], "test")
            self.assertNotIn("API_KEY", records[0].env)
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_restore_relaunches_missing_listener_outside_dsh_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            manifest = root / "services.json"
            receipt = root / "logs" / "receipt.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "processes": [
                            {
                                "pid": 42,
                                "started": "123",
                                "parent_pid": 1,
                                "argv": ["python", "server.py"],
                                "cwd": str(root),
                                "env": {"APP_MODE": "test"},
                                "ports": [8080],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            process = Mock(pid=321)
            process.poll.return_value = None

            with (
                patch.object(dsh_service_handoff, "_identity_alive", return_value=False),
                patch.object(
                    dsh_service_handoff,
                    "_listening_ports",
                    side_effect=[set(), {8080}],
                ),
                patch.object(
                    dsh_service_handoff.subprocess, "Popen", return_value=process
                ) as popen,
                patch.dict(os.environ, {"API_KEY": "must-not-propagate"}),
            ):
                outcomes = restore_services(manifest, receipt, proc_root=root / "proc")

            self.assertEqual(outcomes[0]["status"], "restored")
            self.assertFalse(manifest.exists())
            kwargs = popen.call_args.kwargs
            self.assertTrue(kwargs["start_new_session"])
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["stdin"], dsh_service_handoff.subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["APP_MODE"], "test")
            self.assertNotIn("API_KEY", kwargs["env"])
            self.assertEqual(
                json.loads(receipt.read_text())["services"][0]["ports"], [8080]
            )


if __name__ == "__main__":
    unittest.main()
