"""Benchmark-level batch orchestration for Harbor Fixer MVP."""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .artifacts import read_json, write_json
from .orchestrator import run_stage1
from .reporter import run_report_from_paths
from .runner import PiAgentConfig, PiAgentRunner
from .validation import MONITOR_POLICIES, ValidationError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_manifest(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if payload.get("schema_version") != 1:
        raise ValidationError("batch manifest schema_version must be 1")
    if payload.get("kind") != "harbor_fixer_batch_manifest":
        raise ValidationError("batch manifest kind must be harbor_fixer_batch_manifest")
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise ValidationError("batch manifest benchmarks must be a non-empty list")
    benchmark_ids: set[str] = set()
    output_dirs: set[str] = set()
    for index, item in enumerate(benchmarks):
        if not isinstance(item, dict):
            raise ValidationError(f"benchmarks[{index}] must be an object")
        benchmark_id = str(item.get("benchmark_id") or "")
        if not benchmark_id:
            raise ValidationError(f"benchmarks[{index}].benchmark_id must be non-empty")
        if benchmark_id in benchmark_ids:
            raise ValidationError(f"duplicate benchmark_id: {benchmark_id}")
        benchmark_ids.add(benchmark_id)
        if not str(item.get("analyzer_output") or ""):
            raise ValidationError(f"benchmarks[{index}].analyzer_output must be non-empty")
        output_dir = str(item.get("output_dir") or "")
        if not output_dir:
            raise ValidationError(f"benchmarks[{index}].output_dir must be non-empty")
        resolved_output_dir = str(Path(output_dir).resolve())
        if resolved_output_dir in output_dirs:
            raise ValidationError(f"duplicate output_dir: {output_dir}")
        output_dirs.add(resolved_output_dir)
    return payload


def _batch_status(results: list[dict[str, Any]]) -> str:
    failed_count = sum(1 for item in results if item.get("status") != "success")
    if failed_count == 0:
        return "success"
    if failed_count == len(results):
        return "failed"
    return "partial_failed"


def _run_items(
    benchmarks: list[dict[str, Any]],
    *,
    benchmark_concurrency: int,
    worker: Callable[[int, dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    results_by_index: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=benchmark_concurrency) as executor:
        future_to_index = {
            executor.submit(worker, index, benchmark): index
            for index, benchmark in enumerate(benchmarks)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            benchmark = benchmarks[index]
            benchmark_id = str(benchmark.get("benchmark_id") or index)
            try:
                results_by_index[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate one benchmark failure.
                results_by_index[index] = {
                    "benchmark_id": benchmark_id,
                    "index": index,
                    "status": "failed",
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                }
    return [results_by_index[index] for index in range(len(benchmarks))]


def _write_batch_result(
    output_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    write_json(output_dir / "batch-result-latest.json", payload)
    return payload


def run_batch_plan_from_manifest(
    manifest_path: Path,
    output_dir: Path,
    *,
    pi_config: PiAgentConfig,
    max_concurrency: int,
    benchmark_concurrency: int,
    workspace_root: Path = Path("."),
) -> dict[str, Any]:
    manifest = _require_manifest(manifest_path)
    benchmarks = manifest["benchmarks"]
    started_at = _utc_now()
    started = time.monotonic()

    def worker(index: int, benchmark: dict[str, Any]) -> dict[str, Any]:
        benchmark_id = str(benchmark["benchmark_id"])
        item_output_dir = Path(str(benchmark["output_dir"]))
        plan = run_stage1(
            Path(str(benchmark["analyzer_output"])),
            item_output_dir,
            PiAgentRunner(
                item_output_dir,
                replace(pi_config, thinking_level="off"),
            ),
            PiAgentRunner(item_output_dir, pi_config),
            max_concurrency=max_concurrency,
            workspace_root=workspace_root,
        )
        return {
            "benchmark_id": benchmark_id,
            "index": index,
            "status": "success",
            "analyzer_output": str(benchmark["analyzer_output"]),
            "output_dir": str(item_output_dir),
            "fix_plan_path": str(item_output_dir / "fix-plan-latest.json"),
            "target_environment_path": str(item_output_dir / "target-environment.json"),
            "target_context_path": str(item_output_dir / "target-context.json"),
            "plan_count": len(plan.get("plans", [])),
            "unplanned_task_count": len(plan.get("unplanned_tasks", [])),
            "generation_error_count": len(plan.get("generation_errors", [])),
        }

    results = _run_items(benchmarks, benchmark_concurrency=benchmark_concurrency, worker=worker)
    finished_at = _utc_now()
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_batch_result",
        "stage": "plan",
        "status": _batch_status(results),
        "source": {
            "manifest_path": str(manifest_path),
            "output_dir": str(output_dir),
        },
        "benchmark_concurrency": benchmark_concurrency,
        "max_concurrency": max_concurrency,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
    }
    return _write_batch_result(output_dir, payload)


def run_batch_report_from_manifest(
    manifest_path: Path,
    output_dir: Path,
    *,
    pi_config: PiAgentConfig,
    benchmark_concurrency: int,
    baseline_monitor_policy: str,
) -> dict[str, Any]:
    if baseline_monitor_policy not in MONITOR_POLICIES:
        raise ValidationError("baseline_monitor_policy must be auto, on, or off")
    manifest = _require_manifest(manifest_path)
    benchmarks = manifest["benchmarks"]
    for index, item in enumerate(benchmarks):
        if not str(item.get("verification_result") or ""):
            raise ValidationError(f"benchmarks[{index}].verification_result must be non-empty")
        item_policy = str(item.get("baseline_monitor_policy") or baseline_monitor_policy)
        if item_policy not in MONITOR_POLICIES:
            raise ValidationError(f"benchmarks[{index}].baseline_monitor_policy must be auto, on, or off")

    started_at = _utc_now()
    started = time.monotonic()

    def worker(index: int, benchmark: dict[str, Any]) -> dict[str, Any]:
        benchmark_id = str(benchmark["benchmark_id"])
        item_output_dir = Path(str(benchmark["output_dir"]))
        baseline_run_dir = (
            Path(str(benchmark["baseline_run_dir"]))
            if str(benchmark.get("baseline_run_dir") or "")
            else None
        )
        item_policy = str(benchmark.get("baseline_monitor_policy") or baseline_monitor_policy)
        report = run_report_from_paths(
            Path(str(benchmark["verification_result"])),
            Path(str(benchmark["analyzer_output"])),
            item_output_dir,
            PiAgentRunner(item_output_dir, pi_config),
            baseline_run_dir=baseline_run_dir,
            baseline_monitor_policy=item_policy,
        )
        return {
            "benchmark_id": benchmark_id,
            "index": index,
            "status": "success",
            "analyzer_output": str(benchmark["analyzer_output"]),
            "verification_result": str(benchmark["verification_result"]),
            "output_dir": str(item_output_dir),
            "fix_report_path": str(item_output_dir / "fix-report-latest.json"),
            "human_report_path": str(item_output_dir / "fix-report-latest.md"),
            "report_status": str(report.get("status") or ""),
            "summary_status": str(report.get("summary", {}).get("status") or ""),
        }

    results = _run_items(benchmarks, benchmark_concurrency=benchmark_concurrency, worker=worker)
    finished_at = _utc_now()
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_batch_result",
        "stage": "report",
        "status": _batch_status(results),
        "source": {
            "manifest_path": str(manifest_path),
            "output_dir": str(output_dir),
        },
        "benchmark_concurrency": benchmark_concurrency,
        "baseline_monitor_policy": baseline_monitor_policy,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
    }
    return _write_batch_result(output_dir, payload)
