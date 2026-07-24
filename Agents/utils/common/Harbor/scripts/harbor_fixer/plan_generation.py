"""Generate Fix Plans from Analyzer outputs and bounded planning context."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .agent_invocation import AgentInvoker
from .analyzer_inputs import build_task_inputs
from .artifact_io import write_json, write_text
from .planning_context import collect_planning_context
from .prompts import MAIN_AGENT_PROMPT, TASK_SUBAGENT_PROMPT, build_validation_retry_prompt
from .validation import (
    ValidationError,
    parse_strict_json_object,
    task_key,
    validate_fix_plan_set,
    validate_task_summary,
)


def _task_label(task_input: dict[str, Any]) -> str:
    return _task_label_from_identity(task_input["task"])


def _task_label_from_identity(task: dict[str, Any]) -> str:
    task_index = str(task.get("task_index") or "unknown")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_index).strip(".-")[:60] or "task"
    digest = hashlib.sha256(task_index.encode("utf-8")).hexdigest()[:12]
    return f"task-{slug}-{digest}"


def _summarize_task_with_retry(
    invoker: AgentInvoker,
    task_input: dict[str, Any],
    raw_output_dir: Path,
    *,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    label = _task_label(task_input)
    last_error = ""
    prompt = TASK_SUBAGENT_PROMPT
    for attempt in range(1, max_attempts + 1):
        raw = ""
        try:
            raw = invoker.invoke(prompt, task_input, attempt=attempt, label=label)
            write_text(raw_output_dir / f"{label}-attempt-{attempt}.txt", raw)
            payload = parse_strict_json_object(raw)
            validate_task_summary(payload, expected_task=task_input["task"])
            return payload, None
        except (RuntimeError, ValidationError) as exc:
            last_error = str(exc)
            if raw and attempt < max_attempts:
                prompt = build_validation_retry_prompt(
                    base_prompt=TASK_SUBAGENT_PROMPT,
                    previous_output=raw,
                    validation_error=last_error,
                )
    return None, {
        "stage": "task_subagent",
        "task": task_input["task"],
        "error": last_error,
    }


def collect_task_summaries(
    task_inputs: list[dict[str, Any]],
    invoker: AgentInvoker,
    output_dir: Path,
    *,
    max_concurrency: int = 4,
    max_attempts: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_output_dir = output_dir / "raw-task-subagent-outputs"
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        future_to_key = {
            executor.submit(
                _summarize_task_with_retry,
                invoker,
                task_input,
                raw_output_dir,
                max_attempts=max_attempts,
            ): task_key(task_input["task"])
            for task_input in task_inputs
        }
        results: dict[tuple[str, str, Any], tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            results[key] = future.result()
    for task_input in task_inputs:
        summary, error = results[task_key(task_input["task"])]
        if summary is not None:
            summaries.append(summary)
        if error is not None:
            errors.append(error)
    return summaries, errors


def build_plan_agent_input(
    source: dict[str, Any],
    task_summaries: list[dict[str, Any]],
    generation_errors: list[dict[str, Any]],
    runtime_inventory: dict[str, Any],
    runtime_inventory_path: Path,
    workspace_evidence: dict[str, Any],
    workspace_evidence_path: Path,
) -> dict[str, Any]:
    runtime_inventory_sha256 = hashlib.sha256(
        json.dumps(
            runtime_inventory,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    workspace_evidence_sha256 = hashlib.sha256(
        json.dumps(
            workspace_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_main_agent_input",
        "source": source,
        "task_summaries": task_summaries,
        "generation_errors": generation_errors,
        "target_environment": runtime_inventory,
        "target_environment_artifact": {
            "path": str(runtime_inventory_path),
            "sha256": runtime_inventory_sha256,
        },
        "target_context": workspace_evidence,
        "target_context_artifact": {
            "path": str(workspace_evidence_path),
            "sha256": workspace_evidence_sha256,
        },
    }


def request_fix_plan(
    main_invoker: AgentInvoker,
    main_input: dict[str, Any],
    output_dir: Path,
    *,
    max_attempts: int = 2,
) -> dict[str, Any]:
    last_error = ""
    prompt = MAIN_AGENT_PROMPT
    for attempt in range(1, max_attempts + 1):
        raw = ""
        try:
            raw = main_invoker.invoke(prompt, main_input, attempt=attempt, label="main-agent")
            write_text(output_dir / "raw-main-agent-output" / f"attempt-{attempt}.txt", raw)
            payload = parse_strict_json_object(raw)
            payload.setdefault("generation_errors", [])
            if isinstance(payload.get("generation_errors"), list):
                payload["generation_errors"].extend(main_input.get("generation_errors", []))
            validate_fix_plan_set(payload)
            return payload
        except (RuntimeError, ValidationError) as exc:
            last_error = str(exc)
            if raw and attempt < max_attempts:
                prompt = build_validation_retry_prompt(
                    base_prompt=MAIN_AGENT_PROMPT,
                    previous_output=raw,
                    validation_error=last_error,
                )
    raise ValidationError(
        f"main agent failed to emit a valid fix plan after {max_attempts} attempts: {last_error}"
    )


def run_plan_generation(
    analyzer_output_path: Path,
    output_dir: Path,
    task_invoker: AgentInvoker,
    main_invoker: AgentInvoker,
    *,
    max_concurrency: int = 4,
    workspace_root: Path = Path("."),
) -> dict[str, Any]:
    task_inputs, source = build_task_inputs(analyzer_output_path)
    runtime_inventory_path = output_dir / "target-environment.json"
    runtime_inventory, workspace_evidence = collect_planning_context(
        workspace_root,
        analyzer_output_path,
        task_inputs,
        pi_bin=getattr(main_invoker, "configured_pi_binary", None),
    )
    write_json(runtime_inventory_path, runtime_inventory)
    workspace_evidence_path = output_dir / "target-context.json"
    write_json(workspace_evidence_path, workspace_evidence)
    for task_input in task_inputs:
        write_json(output_dir / "task-inputs" / f"{_task_label(task_input)}.json", task_input)
    task_summaries, errors = collect_task_summaries(
        task_inputs,
        task_invoker,
        output_dir,
        max_concurrency=max_concurrency,
    )
    for summary in task_summaries:
        write_json(output_dir / "task-summaries" / f"{_task_label_from_identity(summary['task'])}.json", summary)
    main_input = build_plan_agent_input(
        source,
        task_summaries,
        errors,
        runtime_inventory,
        runtime_inventory_path,
        workspace_evidence,
        workspace_evidence_path,
    )
    write_json(output_dir / "main-agent-input.json", main_input)
    fix_plan = request_fix_plan(main_invoker, main_input, output_dir)
    write_json(output_dir / "fix-plan-latest.json", fix_plan)
    return fix_plan
