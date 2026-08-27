from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from dsh_minimal_harbor import AgentFleetDshMinimal  # noqa: E402


class AgentFleetDshMinimalTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, root: Path, **kwargs: Any) -> AgentFleetDshMinimal:
        return AgentFleetDshMinimal(
            logs_dir=root,
            version="0.1.0-rc.6",
            model_name="deepseek/private/deepseek-v4-flash-0731",
            extra_env={
                "DSH_API_KEY": "fake-key",
                "DSH_BASE_URL": "https://gateway.example.test/v1",
                "DSH_PYTHON_RUNTIME_PATH": (
                    "/cache/dsh-minimal-python3.12-runtime.tar.gz"
                ),
                "DSH_MINIMAL_RUNTIME_TAR_PATH": "/cache/dsh-minimal.tar.gz",
            },
            **kwargs,
        )

    def test_runtime_env_preserves_native_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            env = agent._runtime_env()

        self.assertEqual(env["DEEPSEEK_API_KEY"], "fake-key")
        self.assertEqual(
            env["DEEPSEEK_BASE_URL"], "https://gateway.example.test/v1"
        )
        self.assertEqual(env["DSH_MODEL"], "private/deepseek-v4-flash-0731")
        self.assertEqual(env["DSH_CONTEXT_WINDOW"], "1000000")
        self.assertEqual(env["DSH_PROVIDER_RETRY_MAX"], "0")
        self.assertEqual(env["PYTHONPATH"], "/opt/dsh-minimal-runtime/site-packages")

    def test_runtime_env_configures_bounded_provider_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(
                Path(temporary_name), provider_retry_max="5"
            )
            env = agent._runtime_env()

        self.assertEqual(env["DSH_PROVIDER_RETRY_MAX"], "5")

    def test_rejects_invalid_provider_retry_max(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_name,
            self.assertRaisesRegex(ValueError, "must be nonnegative"),
        ):
            self.make_agent(Path(temporary_name), provider_retry_max="-1")

    def test_rejects_invalid_process_retry_max(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_name,
            self.assertRaisesRegex(ValueError, "must be nonnegative"),
        ):
            self.make_agent(Path(temporary_name), process_retry_max="-1")

    def test_rejects_non_official_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            with self.assertRaisesRegex(ValueError, "danger-full-access"):
                self.make_agent(
                    Path(temporary_name), permission_mode="workspace-write"
                )
            with self.assertRaisesRegex(ValueError, "native DeepSeek"):
                self.make_agent(Path(temporary_name), provider_route="harbor")

    def test_base_url_normalizes_completion_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            agent._extra_env["DSH_BASE_URL"] = (
                "https://gateway.example.test/v1/chat/completions"
            )
            self.assertEqual(
                agent._base_url(), "https://gateway.example.test/v1"
            )

    async def test_install_uses_only_mounted_offline_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            environment = AsyncMock()
            environment.default_user = "agent"
            root_exec = AsyncMock()
            agent_exec = AsyncMock()
            agent.exec_as_root = root_exec
            agent.exec_as_agent = agent_exec

            await agent.install(environment)

        root_call = root_exec.await_args
        self.assertIsNotNone(root_call)
        assert root_call is not None
        command = root_call.kwargs["command"]
        self.assertIn('tar -xzf "${DSH_PYTHON_RUNTIME_PATH}"', command)
        self.assertIn('tar -xzf "${DSH_MINIMAL_RUNTIME_TAR_PATH}"', command)
        self.assertNotIn("curl", command)
        self.assertNotIn("pip install", command)
        self.assertEqual(agent_exec.await_count, 3)

    async def test_run_invokes_sdk_runner_in_task_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            environment = AsyncMock()
            agent_exec = AsyncMock()
            agent.exec_as_agent = agent_exec

            await agent.run("fix the tests", environment, AsyncMock())

        call = agent_exec.await_args
        self.assertIsNotNone(call)
        assert call is not None
        self.assertIn("dsh_minimal_runner.py", call.kwargs["command"])
        self.assertIn("fix the tests", call.kwargs["command"])
        self.assertIn("retry >= 0", call.kwargs["command"])
        self.assertEqual(
            call.kwargs["env"]["DSH_MODEL"],
            "private/deepseek-v4-flash-0731",
        )

    async def test_run_configures_same_prompt_process_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name), process_retry_max="2")
            environment = AsyncMock()
            agent_exec = AsyncMock()
            agent.exec_as_agent = agent_exec

            await agent.run("fix the tests", environment, AsyncMock())

        call = agent_exec.await_args
        self.assertIsNotNone(call)
        assert call is not None
        command = call.kwargs["command"]
        self.assertIn("retry >= 2", command)
        self.assertIn("restarting dsh-minimal process", command)
        self.assertEqual(command.count("fix the tests"), 1)

    def test_cordis_exposes_only_official_minimal_tools(self) -> None:
        content = (MODULE_DIR / "dsh_minimal.cordis.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("@deepseek-ai/dsh-tool-bash-persistent", content)
        self.assertIn("@deepseek-ai/dsh-tool-str-replace-editor", content)
        self.assertNotIn("dsh-tool-web", content)
        self.assertNotIn("dsh-tool-jobs", content)
        self.assertIn("skills:\n      enabled: false", content)
        self.assertIn("mode: normal", content)
        self.assertIn("HTTP_405", content)
        self.assertIn("TRANSPORT", content)


if __name__ == "__main__":
    unittest.main()
