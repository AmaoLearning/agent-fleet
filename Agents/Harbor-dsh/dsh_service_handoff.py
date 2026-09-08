#!/usr/bin/env python3
"""Hand listening task services from the DSH PTY to the Harbor trial.

The official SDK deliberately tears down its complete managed process tree on
shutdown.  That is the right default for DSH, but a shared-environment Harbor
verifier may need a server that the agent was explicitly asked to leave
running.  This module snapshots new listening processes before SDK shutdown
and restarts only the ones shutdown actually removed.  The restarted process
is owned by the task container, whose teardown remains the final cleanup
boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_SENSITIVE_ENV = re.compile(
    r"(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|"
    r"authorization|cookie|session)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus Linux start time, which fences PID reuse."""

    pid: int
    started: str


@dataclass(frozen=True)
class ProcessRecord:
    """Minimum process state required to restore one listening service."""

    pid: int
    started: str
    parent_pid: int
    argv: list[str]
    cwd: str
    env: dict[str, str]
    ports: list[int]


def _process_stat(pid: int, proc_root: Path) -> tuple[int, str] | None:
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(") ")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    # The suffix starts at field 3 (state); PPID is field 4 and starttime 22.
    if len(fields) <= 19:
        return None
    return int(fields[1]), fields[19]


def snapshot_processes(proc_root: Path = Path("/proc")) -> set[ProcessIdentity]:
    """Return all currently observable process identities."""
    identities: set[ProcessIdentity] = set()
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return identities
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _process_stat(pid, proc_root)
        if stat is not None:
            identities.add(ProcessIdentity(pid=pid, started=stat[1]))
    return identities


def _tcp_listeners(proc_root: Path) -> dict[str, int]:
    listeners: dict[str, int] = {}
    for table in ("tcp", "tcp6"):
        path = proc_root / "net" / table
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            listeners[fields[9]] = port
    return listeners


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _process_socket_inodes(pid: int, proc_root: Path) -> set[str]:
    inodes: set[str] = set()
    try:
        descriptors = tuple((proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return inodes
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        match = re.fullmatch(r"socket:\[(\d+)\]", target)
        if match is not None:
            inodes.add(match.group(1))
    return inodes


def _safe_environment(pid: int, proc_root: Path) -> dict[str, str]:
    raw = _read_bytes(proc_root / str(pid) / "environ")
    if raw is None:
        return {}
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key_bytes, value_bytes = item.split(b"=", 1)
        key = key_bytes.decode("utf-8", errors="surrogateescape")
        environment[key] = value_bytes.decode("utf-8", errors="surrogateescape")
    return _redact_environment(environment)


def _redact_environment(environment: dict[str, str]) -> dict[str, str]:
    """Drop credential-bearing variables before persistence or relaunch."""
    return {
        key: value
        for key, value in environment.items()
        if _SENSITIVE_ENV.search(key) is None
    }


def _process_record(
    pid: int,
    ports: set[int],
    proc_root: Path,
) -> ProcessRecord | None:
    stat = _process_stat(pid, proc_root)
    argv_raw = _read_bytes(proc_root / str(pid) / "cmdline")
    if stat is None or not argv_raw:
        return None
    argv = [
        item.decode("utf-8", errors="surrogateescape")
        for item in argv_raw.split(b"\0")
        if item
    ]
    if not argv:
        return None
    try:
        cwd = os.readlink(proc_root / str(pid) / "cwd")
    except OSError:
        return None
    return ProcessRecord(
        pid=pid,
        started=stat[1],
        parent_pid=stat[0],
        argv=argv,
        cwd=cwd,
        env=_safe_environment(pid, proc_root),
        ports=sorted(ports),
    )


def _new_listener_records(
    baseline: set[ProcessIdentity],
    *,
    proc_root: Path,
) -> list[ProcessRecord]:
    listener_inodes = _tcp_listeners(proc_root)
    baseline_pairs = {(item.pid, item.started) for item in baseline}
    by_pid: dict[int, set[int]] = {}
    for identity in snapshot_processes(proc_root):
        if (identity.pid, identity.started) in baseline_pairs:
            continue
        for inode in _process_socket_inodes(identity.pid, proc_root):
            port = listener_inodes.get(inode)
            if port is not None:
                by_pid.setdefault(identity.pid, set()).add(port)

    candidates = {
        pid: record
        for pid, ports in by_pid.items()
        if (record := _process_record(pid, ports, proc_root)) is not None
    }
    # A prefork server can expose the same socket from master and workers. Keep
    # only the highest candidate ancestor so one service is never relaunched
    # once per worker.
    selected: list[ProcessRecord] = []
    for pid, record in sorted(candidates.items()):
        ancestor = record.parent_pid
        seen: set[int] = set()
        while ancestor in candidates and ancestor not in seen:
            seen.add(ancestor)
            ancestor = candidates[ancestor].parent_pid
        if not seen:
            selected.append(record)

    return selected


def _write_manifest(manifest_path: Path, records: list[ProcessRecord]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"version": 1, "processes": [asdict(item) for item in records]}
    temporary = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, manifest_path)


def watch_new_listeners(
    control_path: Path,
    manifest_path: Path,
    *,
    ready_path: Path | None = None,
    snapshot_request_path: Path | None = None,
    snapshot_ready_path: Path | None = None,
    proc_root: Path = Path("/proc"),
    poll_interval: float = 0.1,
    freshness_window: float = 15.0,
) -> list[ProcessRecord]:
    """Observe DSH externally and retain listeners alive at its termination.

    A Harbor timeout can terminate the DSH runner before its Python ``finally``
    block executes.  This watcher therefore runs in an independent session.
    Normal completion requests an exact snapshot before harness teardown.
    Per-process last-seen times remain a fallback when timeout or interruption
    prevents that handshake.
    """
    baseline = snapshot_processes(proc_root)
    if ready_path is not None:
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        ready_path.touch()
    observed: dict[tuple[int, str], tuple[ProcessRecord, float]] = {}
    snapshot: list[ProcessRecord] | None = None
    while control_path.exists():
        snapshot_requested = (
            snapshot_request_path is not None and snapshot_request_path.exists()
        )
        now = time.monotonic()
        current = _new_listener_records(
            baseline,
            proc_root=proc_root,
        )
        for record in current:
            observed[(record.pid, record.started)] = (record, now)
        if snapshot_requested and snapshot is None:
            snapshot = current
            if snapshot_ready_path is not None:
                snapshot_ready_path.parent.mkdir(parents=True, exist_ok=True)
                snapshot_ready_path.touch()
        time.sleep(poll_interval)

    stopped_at = time.monotonic()
    selected = snapshot
    if selected is None:
        selected = [
            record
            for record, last_seen in observed.values()
            if stopped_at - last_seen <= freshness_window
        ]
    selected.sort(key=lambda item: item.pid)
    _write_manifest(manifest_path, selected)
    return selected


def _identity_alive(record: ProcessRecord, proc_root: Path) -> bool:
    stat = _process_stat(record.pid, proc_root)
    return stat is not None and stat[1] == record.started


def _process_listening_ports(pid: int, proc_root: Path) -> set[int]:
    listeners = _tcp_listeners(proc_root)
    return {
        listeners[inode]
        for inode in _process_socket_inodes(pid, proc_root)
        if inode in listeners
    }


def _listening_ports(proc_root: Path) -> set[int]:
    return set(_tcp_listeners(proc_root).values())


def _load_manifest(path: Path) -> list[ProcessRecord]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("processes"), list):
        raise ValueError("unsupported DSH service handoff manifest")
    return [ProcessRecord(**item) for item in value["processes"]]


def restore_services(
    manifest_path: Path,
    receipt_path: Path,
    *,
    proc_root: Path = Path("/proc"),
    readiness_timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Restore captured listeners after the DSH-owned runtime has exited."""
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_path.is_file():
        receipt_path.write_text(
            json.dumps({"version": 1, "status": "no-manifest", "services": []}),
            encoding="utf-8",
        )
        return []

    records = _load_manifest(manifest_path)
    outcomes: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        summary: dict[str, Any] = {
            "original_pid": record.pid,
            "executable": Path(record.argv[0]).name,
            "ports": record.ports,
        }
        if _identity_alive(record, proc_root) and set(record.ports).issubset(
            _process_listening_ports(record.pid, proc_root)
        ):
            summary["status"] = "preserved"
            outcomes.append(summary)
            continue
        if set(record.ports).issubset(_listening_ports(proc_root)):
            summary["status"] = "listener-already-present"
            outcomes.append(summary)
            continue

        log_path = receipt_path.parent / f"dsh-service-{index}.log"
        environment = _redact_environment(dict(os.environ))
        environment.update(record.env)
        try:
            with log_path.open("ab", buffering=0) as output:
                process = subprocess.Popen(
                    record.argv,
                    cwd=record.cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            summary["restored_pid"] = process.pid
        except (OSError, ValueError) as exc:
            summary.update(status="spawn-failed", error=f"{type(exc).__name__}: {exc}")
            outcomes.append(summary)
            continue

        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            if set(record.ports).issubset(_listening_ports(proc_root)):
                summary["status"] = "restored"
                break
            return_code = process.poll()
            if return_code is not None:
                summary.update(status="exited-before-ready", exit_code=return_code)
                break
            time.sleep(0.1)
        else:
            summary["status"] = "readiness-timeout"
        outcomes.append(summary)

    receipt_path.write_text(
        json.dumps(
            {"version": 1, "status": "complete", "services": outcomes}, indent=2
        ),
        encoding="utf-8",
    )
    manifest_path.unlink(missing_ok=True)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--watch", action="store_true")
    action.add_argument("--restore", type=Path)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--snapshot-request", type=Path)
    parser.add_argument("--snapshot-ready", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--freshness-window", type=float, default=15.0)
    parser.add_argument("--readiness-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if (args.snapshot_request is None) != (args.snapshot_ready is None):
        parser.error("--snapshot-request and --snapshot-ready must be used together")
    if args.watch:
        if args.control is None or args.manifest is None:
            parser.error("--watch requires --control and --manifest")
        watch_new_listeners(
            args.control,
            args.manifest,
            ready_path=args.ready,
            snapshot_request_path=args.snapshot_request,
            snapshot_ready_path=args.snapshot_ready,
            poll_interval=args.poll_interval,
            freshness_window=args.freshness_window,
        )
        return 0
    if args.receipt is None:
        parser.error("--restore requires --receipt")
    outcomes = restore_services(
        args.restore,
        args.receipt,
        readiness_timeout=args.readiness_timeout,
    )
    failures = {
        "spawn-failed",
        "exited-before-ready",
        "readiness-timeout",
    }
    return 1 if any(item.get("status") in failures for item in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
