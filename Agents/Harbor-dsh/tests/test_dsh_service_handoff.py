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
            ready = root / "ready"
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

            def snapshot(_: Path) -> set[ProcessIdentity]:
                self.assertFalse(ready.exists())
                return set()

            with (
                patch.object(
                    dsh_service_handoff, "snapshot_processes", side_effect=snapshot
                ),
                patch.object(
                    dsh_service_handoff,
                    "_new_listener_records",
                    return_value=[record],
                ),
                patch.object(
                    dsh_service_handoff.time, "sleep", side_effect=stop_watcher
                ),
            ):
                records = watch_new_listeners(control, manifest, ready_path=ready)

            self.assertEqual(records, [record])
            self.assertTrue(ready.exists())
            self.assertEqual(
                json.loads(manifest.read_text())["processes"][0]["pid"], 42
            )

    def test_watcher_uses_exact_pre_teardown_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            control = root / "active"
            manifest = root / "services.json"
            request = root / "snapshot-request"
            snapshot_ready = root / "snapshot-ready"
            control.touch()
            stopped = ProcessRecord(
                pid=41,
                started="122",
                parent_pid=1,
                argv=["python", "temporary-server.py"],
                cwd=str(root),
                env={},
                ports=[8079],
            )
            live = ProcessRecord(
                pid=42,
                started="123",
                parent_pid=1,
                argv=["python", "persistent-server.py"],
                cwd=str(root),
                env={},
                ports=[8080],
            )
            sleeps = 0

            def advance(_: float) -> None:
                nonlocal sleeps
                sleeps += 1
                if sleeps == 1:
                    request.touch()
                else:
                    self.assertTrue(snapshot_ready.exists())
                    control.unlink()

            with (
                patch.object(
                    dsh_service_handoff, "snapshot_processes", return_value=set()
                ),
                patch.object(
                    dsh_service_handoff,
                    "_new_listener_records",
                    side_effect=[[stopped], [live]],
                ),
                patch.object(dsh_service_handoff.time, "sleep", side_effect=advance),
            ):
                records = watch_new_listeners(
                    control,
                    manifest,
                    snapshot_request_path=request,
                    snapshot_ready_path=snapshot_ready,
                )

            self.assertEqual(records, [live])
            self.assertEqual(
                json.loads(manifest.read_text())["processes"][0]["pid"], 42
            )

    def test_listener_selection_keeps_master_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            proc_root = root / "proc"
            (proc_root / "net").mkdir(parents=True)
            (proc_root / "net" / "tcp").write_text(
                "header\n0: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 4242\n",
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
            records = dsh_service_handoff._new_listener_records(
                {ProcessIdentity(pid=100, started="1000")},
                proc_root=proc_root,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].pid, 200)
            self.assertEqual(records[0].ports, [8080])
            self.assertEqual(records[0].env["APP_MODE"], "test")
            self.assertNotIn("API_KEY", records[0].env)

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
                patch.object(dsh_service_handoff, "_identity_alive", return_value=True),
                patch.object(
                    dsh_service_handoff,
                    "_process_listening_ports",
                    return_value=set(),
                ),
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

    def test_restore_stops_waiting_when_relaunched_process_exits_zero(self) -> None:
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
                                "argv": ["python", "temporary-server.py"],
                                "cwd": str(root),
                                "env": {},
                                "ports": [8080],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            process = Mock(pid=321)
            process.poll.return_value = 0

            with (
                patch.object(
                    dsh_service_handoff, "_identity_alive", return_value=False
                ),
                patch.object(
                    dsh_service_handoff, "_listening_ports", return_value=set()
                ),
                patch.object(
                    dsh_service_handoff.subprocess, "Popen", return_value=process
                ),
                patch.object(dsh_service_handoff.time, "sleep") as sleep,
            ):
                outcomes = restore_services(manifest, receipt, proc_root=root / "proc")

            self.assertEqual(outcomes[0]["status"], "exited-before-ready")
            self.assertEqual(outcomes[0]["exit_code"], 0)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
