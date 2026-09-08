from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from dsh_sdk_minimal_harbor import AgentFleetDshSdkMinimal


class AgentFleetDshSdkMinimalTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, root: Path, **kwargs: Any) -> AgentFleetDshSdkMinimal:
        return AgentFleetDshSdkMinimal(
            logs_dir=root,
            version="0.1.3-alpha.1",
            model_name="deepseek/private/deepseek-v4-flash-0731",
            extra_env={
                "API_KEY": "fake-key",
                "BASE_URL": "https://gateway.example.test/v1",
                "DSH_PYTHON_RUNTIME_PATH": "/cache/python.tar.gz",
                "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH": "/cache/sdk.tar.gz",
                "DSH_CLI_RUNTIME_PATH": "/cache/dsh.tar.gz",
            },
            **kwargs,
        )

    def test_runtime_env_selects_profile_home_and_sampling_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            env = self.make_agent(Path(temporary_name))._runtime_env()

        self.assertEqual(env["DSH_HOME"], "/logs/agent/dsh-home")
        self.assertEqual(env["DSH_MODEL"], "private/deepseek-v4-flash-0731")
        self.assertEqual(env["DSH_CONTEXT_WINDOW"], "200000")
        self.assertEqual(env["DEEPSEEK_BASE_URL"], "https://gateway.example.test/v1")
        self.assertEqual(env["DSH_TEMPERATURE"], "1.0")
        self.assertEqual(env["DSH_TOP_P"], "0.95")
        self.assertEqual(env["GIT_PAGER"], "cat")
        self.assertEqual(env["GIT_EDITOR"], "true")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_base_url_normalizes_common_openai_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            self.assertEqual(
                self.make_agent(root)._base_url(), "https://gateway.example.test/v1"
            )
            agent = self.make_agent(root)
            agent.extra_env["BASE_URL"] = (
                "https://gateway.example.test/v1/chat/completions"
            )
            self.assertEqual(agent._base_url(), "https://gateway.example.test/v1")

    def test_sampling_values_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            with self.assertRaisesRegex(ValueError, "temperature"):
                self.make_agent(root, temperature="nan")
            with self.assertRaisesRegex(ValueError, "top_p"):
                self.make_agent(root, top_p="0")

    async def test_install_uses_three_offline_archives_and_checks_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            environment = AsyncMock()
            environment.default_user = "agent"
            agent.exec_as_root = AsyncMock()
            agent.exec_as_agent = AsyncMock()

            await agent.install(environment)

        root_command = agent.exec_as_root.await_args.kwargs["command"]
        for variable in (
            "DSH_PYTHON_RUNTIME_PATH",
            "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH",
            "DSH_CLI_RUNTIME_PATH",
        ):
            self.assertIn(variable, root_command)
        commands = "\n".join(
            call.kwargs["command"] for call in agent.exec_as_agent.await_args_list
        )
        self.assertIn("dsh --profile sdk-minimal --patch", commands)
        self.assertIn("dsh_sdk_minimal.cordis.yml", commands)
        self.assertIn("dsh_sampling_plugin.mjs", commands)
        self.assertNotIn("pip install", commands)
        self.assertNotIn("curl", commands)

    async def test_run_uses_new_sdk_profile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name), max_tokens="49152")
            environment = AsyncMock()
            agent.exec_as_agent = AsyncMock()

            await agent.run("fix the tests", environment, AsyncMock())

        self.assertEqual(agent.exec_as_agent.await_count, 3)
        watcher = agent.exec_as_agent.await_args_list[0].kwargs["command"]
        self.assertIn("dsh_service_handoff.py --watch", watcher)
        self.assertIn("setsid", watcher)
        self.assertIn("-services.ready", watcher)
        self.assertIn("--snapshot-request", watcher)
        self.assertIn("--snapshot-ready", watcher)
        self.assertIn('kill -0 "$watcher_pid"', watcher)
        call = agent.exec_as_agent.await_args_list[1]
        command = call.kwargs["command"]
        self.assertIn("/installed-agent/sdk_minimal.py", command)
        self.assertIn("--profile sdk-minimal", command)
        self.assertIn("--patch /installed-agent/dsh_sdk_minimal.cordis.yml", command)
        self.assertIn("--dsh-home /logs/agent/dsh-home", command)
        self.assertIn('--dsh-bin "$HOME/.local/bin/dsh"', command)
        self.assertIn("--reasoning-effort max", command)
        self.assertIn("--max-tokens 49152", command)
        self.assertIn("--trace-path /logs/agent/dsh-sdk-minimal-trace.jsonl", command)
        self.assertIn("--service-snapshot-request", command)
        self.assertIn("--service-snapshot-ready", command)
        self.assertNotIn("--service-handoff", command)
        self.assertNotIn("dsh_sampling_relay.py", command)
        self.assertEqual(call.kwargs["env"]["DSH_TEMPERATURE"], "1.0")
        self.assertEqual(call.kwargs["env"]["DSH_TOP_P"], "0.95")
        self.assertIn("command -v stdbuf", command)
        self.assertIn('else\n    tee "$@"', command)
        self.assertEqual(call.kwargs["env"]["DSH_HOME"], "/logs/agent/dsh-home")
        restore = agent.exec_as_agent.await_args_list[2].kwargs["command"]
        self.assertIn("dsh_service_handoff.py", restore)
        self.assertIn("--receipt /logs/agent/dsh-service-handoff.json", restore)


if __name__ == "__main__":
    unittest.main()
