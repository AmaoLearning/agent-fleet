#!/usr/bin/env python3
"""Tests for Harbor Fixer runtime context."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from fixer_test_support import *  # noqa: E402,F403


class HarborFixerRuntimeContextTest(FixerTestCase):
    def test_pi_invoker_requires_explicit_model(self) -> None:
        invoker = PiAgentInvoker(
            self.root / "out",
            PiInvocationConfig(
                pi_bin=str(write_fixture_pi(self.root / "fixture_pi.py")),
                base_url="https://example.test/v1",
                api_key_env="FIXTURE_PI_API_KEY",
            ),
        )
        with mock.patch.dict("os.environ", {"FIXTURE_PI_API_KEY": "fixture"}):
            with self.assertRaisesRegex(RuntimeError, "pi_model_not_configured"):
                invoker.invoke(
                    "Return JSON only.",
                    {"kind": "fixture"},
                    attempt=1,
                    label="fixture",
                )

    def test_target_environment_rejects_missing_workspace_and_records_file_state(self) -> None:
        evidence = self.root / "evidence.log"
        evidence.write_text("fixture\n", encoding="utf-8")
        snapshot = collect_runtime_inventory(
            self.root,
            self.root,
            [{"evidence": [{"path": str(evidence)}]}],
            pi_bin=sys.executable,
        )

        self.assertEqual(snapshot["kind"], "harbor_fixer_target_environment")
        self.assertTrue(snapshot["repository_paths"]["workspace_root"]["readable"])
        self.assertEqual(snapshot["evidence_files"]["paths"][0]["type"], "file")
        self.assertEqual(snapshot["commands"]["pi"]["path"], sys.executable)
        serialized = json.dumps(snapshot)
        self.assertNotIn("API_KEY", serialized)
        self.assertNotIn("BASE_URL", serialized)

        with self.assertRaisesRegex(ValidationError, "workspace root"):
            collect_runtime_inventory(self.root / "missing", self.root, [])

    def test_path_probes_degrade_permission_errors_to_unavailable(self) -> None:
        inaccessible = Path("/home/other-user/private/evidence.log")
        with mock.patch.object(
            Path,
            "exists",
            side_effect=PermissionError(13, "Permission denied", str(inaccessible)),
        ):
            environment_state = inspect_runtime_path(inaccessible)
            context_state = inspect_workspace_path(inaccessible)

        for state in (environment_state, context_state):
            self.assertEqual(state["path"], str(inaccessible))
            self.assertEqual(state["status"], "unavailable")
            self.assertEqual(state["reason"], "path_unavailable:PermissionError")
            self.assertFalse(state["readable"])

    def test_target_context_is_deterministic_bounded_and_redacted(self) -> None:
        workspace = self.root / "workspace"
        analyzer = self.root / "analyzer"
        workspace.mkdir()
        analyzer.mkdir()
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\npassword = "manifest-secret"\n',
            encoding="utf-8",
        )
        evidence = self.root / "evidence.log"
        evidence.write_text(
            "before\nAPI_KEY=super-secret-value\nfailure line\nafter\n",
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
            }
        ]

        first = collect_workspace_evidence(workspace, analyzer, task_inputs)
        self.assertEqual(first, collect_workspace_evidence(workspace, analyzer, task_inputs))
        serialized = json.dumps(first)
        self.assertNotIn("manifest-secret", serialized)
        self.assertNotIn("super-secret-value", serialized)
        self.assertIn("<REDACTED>", serialized)
        self.assertIn("failure line", first["evidence_excerpts"][0]["excerpt"])
        self.assertEqual(first["evidence_excerpts"][1]["reason"], "sensitive_path")


if __name__ == "__main__":
    unittest.main()
