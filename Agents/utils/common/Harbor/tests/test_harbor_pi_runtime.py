#!/usr/bin/env python3
"""Tests for the small shared Harbor Pi subprocess helper."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harbor_analyzer.pi import dispatch_to_child
from harbor_pi_runtime import run_pi_json_process
from harbor_pi_runtime.process import models_config


def write_fixture_pi(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('fixture-pi 1.0')",
                "    raise SystemExit(0)",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read() if args[-1] == 'system' else args[-1]",
                "payload = {'ok': True, 'prompt': prompt}",
                "events = [",
                "    {'type': 'session', 'id': 'fixture-session'},",
                "    {'type': 'agent_start'},",
                "    {'type': 'turn_start'},",
                "    {'type': 'message_update', 'message': {'role': 'assistant', 'content': 'partial'}},",
                "    {'type': 'message_end', 'message': {'role': 'assistant', 'content': json.dumps(payload), 'stopReason': 'stop'}},",
                "    {'type': 'turn_end'},",
                "    {'type': 'agent_end'},",
                "]",
                "for event in events:",
                "    print(json.dumps(event), flush=True)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HarborPiRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.pi_bin = write_fixture_pi(self.root / "fixture-pi")

    def run_runtime(self, prompt: str, **overrides: object):
        options = {
            "prompt": prompt,
            "events_path": self.root / "events.jsonl",
            "stderr_path": self.root / "stderr.txt",
            "runtime_home": self.root / "home",
            "runtime_workdir": self.root / "work",
            "pi_bin": str(self.pi_bin),
            "provider": "fixture",
            "model": "fixture-model",
            "base_url": "https://example.test/v1",
            "api_key_env": "FIXTURE_API_KEY",
            "agent_name": "fixture-agent",
            "display_name": "Fixture",
            "timeout_seconds": 5,
            "launch_mode": "fixture",
            "system_prompt": "system",
        }
        options.update(overrides)
        with mock.patch.dict(os.environ, {"FIXTURE_API_KEY": "fake"}, clear=True):
            return run_pi_json_process(**options)

    def test_models_config_preserves_explicit_and_omitted_auth_header(self) -> None:
        explicit = models_config(
            provider="analyzer",
            model="fixture",
            base_url="https://example.test/v1",
            api_key_env="FIXTURE_API_KEY",
            display_name="Analyzer",
            auth_header=True,
        )
        omitted = models_config(
            provider="fixer",
            model="fixture",
            base_url="https://example.test/v1",
            api_key_env="FIXTURE_API_KEY",
            display_name="Fixer",
        )

        self.assertTrue(explicit["providers"]["analyzer"]["authHeader"])
        self.assertNotIn("authHeader", omitted["providers"]["fixer"])

    def test_compact_events_and_stdin_prompt(self) -> None:
        result = self.run_runtime(
            "stdin-prompt",
            base_url="https://example.test",
            prompt_in_stdin=True,
            no_tools=True,
            thinking_level="off",
        )

        self.assertIsNone(result.block_reason)
        self.assertEqual(result.output_json, {"ok": True, "prompt": "stdin-prompt"})
        self.assertEqual(result.provenance["thinking_level"], "off")
        self.assertEqual(result.provenance["discarded_event_counts"], {"message_update": 1})
        self.assertNotIn(
            "message_update",
            (self.root / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_raw_events_and_argv_prompt(self) -> None:
        result = self.run_runtime(
            "argv-prompt",
            event_retention="raw",
            auth_header=True,
        )

        self.assertIsNone(result.block_reason)
        self.assertEqual(result.output_json, {"ok": True, "prompt": "argv-prompt"})
        self.assertIn(
            "message_update",
            (self.root / "events.jsonl").read_text(encoding="utf-8"),
        )
        model = json.loads((self.root / "home" / "models.json").read_text(encoding="utf-8"))
        self.assertTrue(model["providers"]["fixture"]["authHeader"])
        self.assertNotIn("discarded_event_counts", result.provenance)

    def test_analyzer_adapter_keeps_raw_events_and_domain_provenance(self) -> None:
        evidence = self.root / "evidence.txt"
        evidence.write_text("fixture\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HARBOR_ANALYZER_API_KEY": "fake"}, clear=True):
            result = dispatch_to_child(
                prompt="analyzer-prompt",
                analysis_id="analysis-1",
                output_dir=self.root / "out",
                pi_bin=str(self.pi_bin),
                provider="harbor-analyzer",
                model="fixture-model",
                base_url="https://example.test/v1",
                api_key_env="HARBOR_ANALYZER_API_KEY",
                agent_name="harbor_analyzer_pi_subagent",
                timeout_seconds=5,
                allowed_paths=[evidence],
            )

        self.assertIsNone(result.block_reason)
        self.assertEqual(result.report, {"ok": True, "prompt": "analyzer-prompt"})
        self.assertNotIn("thinking_level", result.provenance)
        self.assertEqual(result.provenance["tools_allowlist"], ["read", "grep", "find", "ls"])
        self.assertIn(str(evidence.resolve()), result.provenance["allowed_paths"])
        events_path = self.root / "out" / "analyzer-subagent-events" / "analysis-1.jsonl"
        self.assertIn("message_update", events_path.read_text(encoding="utf-8"))

    def test_analyzer_adapter_maps_shared_configuration_errors(self) -> None:
        common = {
            "prompt": "fixture",
            "analysis_id": "analysis-1",
            "output_dir": self.root / "out",
            "pi_bin": sys.executable,
            "provider": "harbor-analyzer",
            "model": "fixture-model",
            "agent_name": "harbor_analyzer_pi_subagent",
            "timeout_seconds": 5,
        }
        invalid_env = dispatch_to_child(
            **common,
            base_url="https://example.test/v1",
            api_key_env="INVALID-NAME",
        )
        with mock.patch.dict(os.environ, {"FIXTURE_API_KEY": "fake"}, clear=True):
            invalid_url = dispatch_to_child(
                **common,
                base_url="not-a-url",
                api_key_env="FIXTURE_API_KEY",
            )

        self.assertEqual(invalid_env.block_reason, "analyzer_api_key_env_invalid")
        self.assertEqual(invalid_url.block_reason, "analyzer_base_url_invalid")


if __name__ == "__main__":
    unittest.main()
