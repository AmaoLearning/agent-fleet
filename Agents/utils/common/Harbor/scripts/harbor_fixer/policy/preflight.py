"""Execution-policy routing and artifact publication."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ..agent_invocation import AgentInvoker
from ..artifact_io import write_json_atomic
from ..command_analysis import CommandAnalysis, analyze_command
from ..validation import ValidationError
from .agent import evaluate_agent
from .builtin import unsafe_file_edit_reason
from .paths import analyze_paths
from .rules import evaluate_t1, load_user_rules

POLICY_VERSION = "fixer-policy-v2"
TRUSTED_T1_EXECUTABLE_DIRS = {
    Path("/bin").resolve(),
    Path("/usr/bin").resolve(),
    Path("/usr/sbin").resolve(),
}
PATH_STABLE_T3_READ_COMMANDS = {
    "cat",
    "du",
    "grep",
    "head",
    "ls",
    "stat",
    "test",
    "wc",
}


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _resolved_cwd(action: dict[str, Any], workspace_root: Path) -> Path:
    value = Path(action["cwd"])
    return (value if value.is_absolute() else workspace_root / value).resolve()


def _resolved_executable(executable_token: str, cwd: Path) -> str:
    if not executable_token:
        return ""
    if "/" in executable_token:
        path = Path(executable_token)
        return str((path if path.is_absolute() else cwd / path).resolve())
    match = shutil.which(executable_token)
    return str(Path(match).resolve()) if match else ""


def _is_trusted_t1_executable(resolved_executable: str) -> bool:
    return bool(resolved_executable) and Path(resolved_executable).parent in (
        TRUSTED_T1_EXECUTABLE_DIRS
    )


def _is_path_stable_t3_read(resolved_executable: str) -> bool:
    return _is_trusted_t1_executable(resolved_executable) and Path(
        resolved_executable
    ).name in PATH_STABLE_T3_READ_COMMANDS


def _command_analysis(action: dict[str, Any]) -> CommandAnalysis | None:
    return analyze_command(action) if action.get("action_type") == "command" else None


def _configuration_denials(
    fix_plan: dict[str, Any],
    workspace_root: Path,
    error: Exception,
) -> list[dict[str, Any]]:
    decisions = []
    for plan in fix_plan["plans"]:
        for action in plan["actions"]:
            analysis = _command_analysis(action)
            decisions.append(
                {
                    "plan_id": plan["plan_id"],
                    "action_id": action["action_id"],
                    "action_sha256": _json_sha256(action),
                    "tier": "T1",
                    "decision": "deny",
                    "risk_level": "high",
                    "source": "policy_configuration",
                    "rule_id": "",
                    "reason_code": "policy_configuration_error",
                    "reason": f"{error.__class__.__name__}: {error}",
                    "path_analysis": {},
                    "command_analysis": analysis.as_dict() if analysis else None,
                    "resolved_executable": "",
                    "resolved_cwd": str(_resolved_cwd(action, workspace_root)),
                }
            )
    return decisions


def run_policy_preflight(
    fix_plan: dict[str, Any],
    workspace_root: Path,
    output_dir: Path,
    invoker: AgentInvoker | None,
    *,
    user_rules_path: Path | None = None,
    writable_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Evaluate a complete Fix Plan without executing its actions."""

    resolved_roots = list(
        dict.fromkeys(root.resolve() for root in (writable_roots or []))
    )
    fix_plan_sha256 = _json_sha256(fix_plan)
    try:
        user_rules, rules_source = load_user_rules(user_rules_path)
    except (OSError, ValidationError) as exc:
        decisions = _configuration_denials(fix_plan, workspace_root, exc)
        rules_source = str(user_rules_path or "")
        serialized_rules: list[dict[str, Any]] = []
    else:
        serialized_rules = [
            {
                "rule_id": rule.rule_id,
                "pattern": list(rule.pattern),
                "match": rule.match,
                "decision": rule.decision,
            }
            for rule in user_rules
        ]
        decisions = []
        path_resolution_stable = True
        for plan in fix_plan["plans"]:
            for action in plan["actions"]:
                action_sha256 = _json_sha256(action)
                cwd = _resolved_cwd(action, workspace_root)
                path_analysis = analyze_paths(
                    action,
                    cwd,
                    resolved_roots,
                    path_resolution_stable=path_resolution_stable,
                )
                command_analysis = _command_analysis(action)
                resolved_executable = (
                    _resolved_executable(command_analysis.executable_token, cwd)
                    if command_analysis is not None
                    and command_analysis.classification == "static_argv"
                    else ""
                )
                decision = None
                if command_analysis is not None:
                    if command_analysis.classification == "invalid":
                        decision = {
                            "tier": "T1",
                            "decision": "deny",
                            "risk_level": "high",
                            "source": "builtin_rule",
                            "rule_id": "invalid_command_action",
                            "reason_code": "invalid_command_action",
                            "reason": "command action does not contain a valid argv",
                        }
                    else:
                        decision = evaluate_t1(
                            action,
                            user_rules,
                            analysis=command_analysis,
                            executable_verified=_is_trusted_t1_executable(
                                resolved_executable
                            ),
                        )
                elif unsafe_reason := unsafe_file_edit_reason(action):
                    decision = {
                        "tier": "T1",
                        "decision": "deny",
                        "risk_level": "high",
                        "source": "builtin_rule",
                        "rule_id": unsafe_reason[0],
                        "reason_code": unsafe_reason[0],
                        "reason": unsafe_reason[1],
                    }
                elif path_analysis["classification"] != "inside_writable_roots":
                    decision = {
                        "tier": "T3",
                        "decision": "deny",
                        "risk_level": "high",
                        "source": "builtin_rule",
                        "rule_id": "file_edit_outside_writable_roots",
                        "reason_code": "file_edit_outside_writable_roots",
                        "reason": "file_edit target is outside the authorized roots",
                    }
                if decision is None:
                    tier = (
                        "T2"
                        if path_analysis["classification"]
                        == "inside_writable_roots"
                        else "T3"
                    )
                    decision = evaluate_agent(
                        invoker,
                        {
                            "schema_version": 2,
                            "kind": "harbor_fixer_policy_agent_input",
                            "policy_version": POLICY_VERSION,
                            "tier": tier,
                            "plan_id": plan["plan_id"],
                            "action": action,
                            "fix_plan_sha256": fix_plan_sha256,
                            "action_sha256": action_sha256,
                            "plan_context": {
                                "fix_scope": plan["fix_scope"],
                                "analyzer_scope_comparison": plan[
                                    "analyzer_scope_comparison"
                                ],
                                "task_list": plan["task_list"],
                                "fix_reason": plan["fix_reason"],
                            },
                            "workspace_root": str(workspace_root),
                            "writable_roots": [str(root) for root in resolved_roots],
                            "resolved_cwd": str(cwd),
                            "command_analysis": (
                                command_analysis.as_dict()
                                if command_analysis is not None
                                else None
                            ),
                            "resolved_executable": resolved_executable,
                            "user_rules": serialized_rules,
                            "path_analysis": path_analysis,
                        },
                    )
                decisions.append(
                    {
                        "plan_id": plan["plan_id"],
                        "action_id": action["action_id"],
                        "action_sha256": action_sha256,
                        **decision,
                        "path_analysis": path_analysis,
                        "command_analysis": (
                            command_analysis.as_dict()
                            if command_analysis is not None
                            else None
                        ),
                        "resolved_cwd": str(cwd),
                        "resolved_executable": resolved_executable,
                    }
                )
                if (
                    decision["tier"] == "T3"
                    and decision["decision"] == "allow"
                    and not _is_path_stable_t3_read(resolved_executable)
                ):
                    path_resolution_stable = False
    result = {
        "schema_version": 2,
        "kind": "harbor_fixer_execution_policy_decision",
        "policy_version": POLICY_VERSION,
        "fix_plan_sha256": fix_plan_sha256,
        "status": (
            "allowed"
            if all(decision["decision"] == "allow" for decision in decisions)
            else "denied"
        ),
        "workspace_root": str(workspace_root.resolve()),
        "writable_roots": [str(root) for root in resolved_roots],
        "user_rules_path": rules_source,
        "user_rules": serialized_rules,
        "decisions": decisions,
    }
    write_json_atomic(output_dir / "execution-policy-decision.json", result)
    return result
