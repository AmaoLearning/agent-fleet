"""Tests for Harbor Fixer runtime context."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import (  # noqa: E402
    FixerTestCase,
    write_analyzer_fixture,
    write_json,
)
from harbor_fixer.analyzer_inputs import build_task_inputs  # noqa: E402
from harbor_fixer.planning_context import (
    collect_planning_context,  # noqa: E402
    workspace_evidence,  # noqa: E402
)
from harbor_fixer.planning_context.runtime_inventory import (  # noqa: E402
    collect_runtime_inventory,
)
from harbor_fixer.planning_context.safe_paths import inspect_path  # noqa: E402
from harbor_fixer.planning_context.workspace_evidence import (  # noqa: E402
    collect_workspace_evidence,
)
from harbor_fixer.validation import ValidationError, task_key  # noqa: E402


class HarborFixerRuntimeContextTest(FixerTestCase):
    def test_analyzer_artifacts_build_task_inputs(self) -> None:
        analyzer = write_analyzer_fixture(self.root)
        write_json(
            analyzer / "env-infra-tasks" / "handover-1" / "publication-zzzz.json",
            {"stale": True},
        )
        inputs, source = build_task_inputs(analyzer)

        self.assertEqual(source["run_id"], "run-1")
        self.assertEqual(source["publications"][0]["publication_id"], "publication-current")
        self.assertEqual(inputs[0]["task"]["task_index"], "1")
        self.assertEqual(inputs[0]["source"]["handover_id"], "handover-1")
        self.assertEqual(inputs[0]["source"]["publication_id"], "publication-current")
        self.assertEqual(inputs[0]["evidence"][0]["path"], "/logs/task-1.log")
        self.assertNotIn("reasoning_summary", inputs[0]["analyzer_result"])
        self.assertNotIn("analyzer_report_path", inputs[0]["source"])
        self.assertNotIn("/stale-copy/", json.dumps(inputs))

        selected_env = Path(source["publications"][0]["env_infra_tasks_path"])
        selected_inputs, selected_source = build_task_inputs(selected_env)
        self.assertEqual(selected_inputs, inputs)
        self.assertEqual(selected_source, source)

    def test_empty_analyzer_output_is_valid_planning_context(self) -> None:
        analyzer = write_analyzer_fixture(self.root, count=0)

        inputs, source = build_task_inputs(analyzer)
        runtime, context = collect_planning_context(
            self.root,
            analyzer,
            inputs,
            pi_bin=sys.executable,
        )

        self.assertEqual(inputs, [])
        self.assertEqual(len(source["publications"]), 1)
        self.assertEqual(runtime["evidence_files"]["paths"], [])
        self.assertEqual(context["evidence_excerpts"], [])
        self.assertTrue(context["analyzer_artifacts"]["manifest"]["readable"])
        self.assertEqual(len(context["analyzer_artifacts"]["publications"]), 1)

    def test_analyzer_task_requires_matching_evidence(self) -> None:
        analyzer = write_analyzer_fixture(self.root)
        fix_line_index = (
            analyzer / "fix-line-index" / "handover-1" / "publication-current.jsonl"
        )
        fix_line_index.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "evidence must be non-empty"):
            build_task_inputs(analyzer)

    def test_target_environment_rejects_missing_workspace_and_records_file_state(self) -> None:
        evidence = self.root / "evidence.log"
        dependency_cache = self.root / "configured-harbor-deps"
        evidence.write_text("fixture\n", encoding="utf-8")
        dependency_cache.mkdir()
        with mock.patch.dict(
            os.environ,
            {"LOCAL_WHEEL_DIR": str(dependency_cache)},
        ):
            snapshot = collect_runtime_inventory(
                self.root,
                [{"evidence": [{"path": str(evidence)}]}],
                pi_bin=sys.executable,
            )

        self.assertEqual(snapshot["kind"], "harbor_fixer_target_environment")
        self.assertTrue(snapshot["repository_paths"]["workspace_root"]["readable"])
        self.assertEqual(
            snapshot["repository_paths"]["local_dependency_cache"]["path"],
            str(dependency_cache),
        )
        self.assertTrue(
            snapshot["repository_paths"]["opik_plugin"]["path"].endswith(
                "third_party/agent-opik-plugin"
            )
        )
        self.assertEqual(snapshot["evidence_files"]["paths"][0]["type"], "file")
        self.assertEqual(snapshot["commands"]["pi"]["path"], sys.executable)
        self.assertNotIn("docker", snapshot["commands"])
        self.assertNotIn("available", snapshot["docker"])

        with self.assertRaisesRegex(ValidationError, "workspace root"):
            collect_runtime_inventory(self.root / "missing", [])

    def test_path_probe_degrades_permission_error_to_unavailable(self) -> None:
        inaccessible = Path("/home/other-user/private/evidence.log")
        with mock.patch.object(
            Path,
            "exists",
            side_effect=PermissionError(13, "Permission denied", str(inaccessible)),
        ):
            state = inspect_path(
                inaccessible,
                expand_user=False,
                include_writable=False,
                include_executable=False,
                include_mode=False,
            )

        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["reason"], "path_unavailable:PermissionError")

    def test_target_context_is_deterministic_bounded_and_redacted(self) -> None:
        workspace = self.root / "workspace"
        analyzer = write_analyzer_fixture(self.root, count=0)
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\npassword = "manifest-secret"\n',
            encoding="utf-8",
        )
        evidence = self.root / "evidence.log"
        evidence.write_text(
            "before\nAPI_KEY=super-secret-value\nfailure line\n"
            "registry=https://user:password@registry.example\n"
            "sk-proj-fixture0123456789abcdefghijklmnop\nafter\n",
            encoding="utf-8",
        )
        secret_evidence = workspace / ".env"
        secret_evidence.write_text("TOKEN=hidden\n", encoding="utf-8")
        task_inputs = [
            {
                "task": {
                    "task_index": "1",
                    "task_name": "fixture",
                    "attempt_id": None,
                },
                "evidence": [
                    {"path": str(evidence), "line_start": 3, "line_end": 3},
                    {"path": str(secret_evidence), "line_start": 1, "line_end": 1},
                ],
            },
            {
                "task": {
                    "task_index": "2",
                    "task_name": "fixture-2",
                    "attempt_id": None,
                },
                "evidence": [
                    {"path": str(evidence), "line_start": 3, "line_end": 3},
                ],
            },
        ]

        first = collect_workspace_evidence(workspace, analyzer, task_inputs)
        self.assertEqual(first, collect_workspace_evidence(workspace, analyzer, task_inputs))
        serialized = json.dumps(first)
        self.assertNotIn("manifest-secret", serialized)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("user:password", serialized)
        self.assertNotIn("sk-proj-fixture0123456789abcdefghijklmnop", serialized)
        self.assertIn("<REDACTED>", serialized)
        self.assertIn("failure line", first["evidence_excerpts"][0]["excerpt"])
        self.assertEqual(first["evidence_excerpts"][1]["reason"], "sensitive_path")
        self.assertEqual(
            [item["task"]["task_index"] for item in first["evidence_excerpts"]],
            ["1", "1", "2"],
        )

    def test_workspace_scan_counts_directories_toward_limit(self) -> None:
        analyzer = write_analyzer_fixture(self.root, count=0)
        workspace = self.root / "workspace"
        (workspace / "one").mkdir(parents=True)
        (workspace / "two").mkdir()

        with mock.patch.object(workspace_evidence, "MAX_SCAN_ENTRIES", 1):
            context = collect_workspace_evidence(workspace, analyzer, [])

        self.assertTrue(context["workspace"]["project_manifests_truncated"])

    def test_task_key_matches_analyzer_normalization(self) -> None:
        base = {"task_index": "1", "task_name": "task"}

        self.assertEqual(
            task_key({**base, "attempt_id": 0}),
            task_key({**base, "attempt_id": "0"}),
        )
        self.assertEqual(
            task_key({**base, "attempt_id": None}),
            task_key({**base, "attempt_id": ""}),
        )


if __name__ == "__main__":
    unittest.main()
