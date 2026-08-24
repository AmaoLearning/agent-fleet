from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Model:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)

    def to_json_dict(self):
        return self.__dict__


class _BaseInstalledAgent:
    def __init__(
        self,
        logs_dir: Path,
        version: str | None = None,
        model_name: str | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs,
    ):
        self.logs_dir = Path(logs_dir)
        self._version = version
        self.model_name = model_name
        self._extra_env = extra_env or {}
        self._resolved_flags = {
            "provider": kwargs.pop("provider", ""),
            "reasoning_effort": kwargs.pop("reasoning_effort", ""),
        }
        self._resolved_env_vars = {
            "ANTE_ENABLE_ATIF": os.environ.get("ANTE_ENABLE_ATIF", "false")
        }
        self.root_commands: list[str] = []
        self.agent_commands: list[str] = []
        self.logger = types.SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        )

    def resolve_env_vars(self):
        return dict(self._resolved_env_vars)

    def version(self):
        return self._version

    async def exec_as_root(self, environment, command, **kwargs):
        self.root_commands.append(command)
        return await environment.exec(command=command, **kwargs)

    async def exec_as_agent(self, environment, command, **kwargs):
        self.agent_commands.append(command)
        return await environment.exec(command=command, **kwargs)


def _install_harbor_stubs() -> None:
    modules = {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.installed": types.ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.base": types.ModuleType("harbor.agents.installed.base"),
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": types.ModuleType("harbor.environments.base"),
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": types.ModuleType("harbor.models.agent.context"),
        "harbor.models.trajectories": types.ModuleType("harbor.models.trajectories"),
        "harbor.utils": types.ModuleType("harbor.utils"),
        "harbor.utils.trajectory_utils": types.ModuleType("harbor.utils.trajectory_utils"),
    }
    base = modules["harbor.agents.installed.base"]
    for name in (
        "AgentAuthenticationError",
        "AgentSafetyRefusalError",
        "ApiConnectionClosedError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiRateLimitError",
        "ApiUsageLimitError",
        "ContextWindowExceededError",
        "NetworkConnectionError",
        "NonZeroAgentExitCodeError",
        "UnknownApiError",
    ):
        setattr(base, name, type(name, (Exception,), {}))
    base.BaseInstalledAgent = _BaseInstalledAgent
    base.CliFlag = _Model
    base.EnvVar = _Model
    base.with_prompt_template = lambda function: function
    modules["harbor.environments.base"].BaseEnvironment = _Model
    modules["harbor.models.agent.context"].AgentContext = _Model
    trajectories = modules["harbor.models.trajectories"]
    for name in (
        "Agent",
        "FinalMetrics",
        "Metrics",
        "Observation",
        "ObservationResult",
        "Step",
        "ToolCall",
        "Trajectory",
    ):
        setattr(trajectories, name, _Model)
    modules["harbor.utils.trajectory_utils"].format_trajectory_json = str
    sys.modules.update(modules)


_install_harbor_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ante_agent = importlib.import_module("ante_harbor")


class _ExecResult:
    def __init__(self, return_code=0, stdout="", stderr=""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _Environment:
    def __init__(self):
        self.uploads: list[tuple[Path, str]] = []
        self.commands: list[str] = []

    async def upload_file(self, source_path, target_path):
        self.uploads.append((Path(source_path), target_path))

    async def exec(self, command, **kwargs):
        self.commands.append(command)
        if command == "ante --version" and len(self.commands) == 1:
            return _ExecResult(return_code=127)
        if command == "ante --version":
            return _ExecResult(stdout="ante 0.preview.71\n")
        return _ExecResult(stdout='{"event":{"TurnEnd":{"status":"Success"}}}\n')


class ShellCommandTests(unittest.TestCase):
    def run_shell(self, command: str, *, path: Path | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if path is not None:
            env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["sh", "-c", command],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_tee_command_preserves_command_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "command.log"
            command = ante_agent._tee_command("printf 'failed output\\n'; exit 23", log, append=False)
            result = self.run_shell(command)

            self.assertEqual(result.returncode, 23)
            self.assertEqual(log.read_text(), "failed output\n")

    def test_tee_command_preserves_tee_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tee = root / "tee"
            tee.write_text("#!/bin/sh\ncat >/dev/null\nexit 31\n")
            tee.chmod(0o755)
            log = root / "command.log"
            command = ante_agent._tee_command("exit 0", log, append=False)
            result = self.run_shell(command, path=root)

            self.assertEqual(result.returncode, 31)

    def test_ante_command_preserves_agent_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ante"
            binary.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'agent output\\n'\nexit 19\n")
            binary.chmod(0o755)
            instruction = root / "instruction.md"
            instruction.write_text("test instruction")
            log = root / "logs" / "ante.txt"

            with (
                patch.object(ante_agent, "_AGENT_LOG", log),
                patch.object(ante_agent, "_INSTRUCTION_PATH", instruction),
            ):
                command = ante_agent.ante_command("test-model", None, None, "")
            result = self.run_shell(command, path=root)

            self.assertEqual(result.returncode, 19)
            self.assertEqual(log.read_text(), "agent output\n")
            self.assertFalse(instruction.exists())


class AdapterTests(unittest.TestCase):
    def test_default_args_match_public_control_run(self):
        self.assertEqual(
            ante_agent.DEFAULT_ANTE_ARGS,
            "--yolo --output-format json --no-session-save --no-skills --check",
        )

    def test_install_uploads_runner_cached_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ante"
            binary.write_text("fake binary")
            binary.chmod(0o755)
            environment = _Environment()
            agent = ante_agent.AnteAgent(
                logs_dir=root / "logs",
                version="0.preview.71",
                model_name="test-model",
                binary_path=str(binary),
            )

            asyncio.run(agent.install(environment))

            self.assertEqual(environment.uploads, [(binary, "/usr/local/bin/ante")])
            self.assertTrue(any("chmod +x /usr/local/bin/ante" in command for command in environment.commands))

    def test_run_forwards_provider_effort_and_public_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = _Environment()
            agent = ante_agent.AnteAgent(
                logs_dir=root / "logs",
                model_name="deployment/model",
                provider="openai-compatible",
                reasoning_effort="max",
            )

            asyncio.run(agent.run("solve this", environment, _Model()))

            command = agent.agent_commands[-1]
            self.assertIn("--model deployment/model", command)
            self.assertIn("--provider openai-compatible", command)
            self.assertIn("--effort max", command)
            self.assertIn("--no-session-save --no-skills --check", command)
            self.assertEqual(environment.uploads[-1][1], "/tmp/instruction.md")

    def test_reserved_flags_are_rejected_in_ante_args(self):
        with self.assertRaisesRegex(ValueError, "must not include"):
            ante_agent.split_extra_ante_args("--model other")


if __name__ == "__main__":
    unittest.main()
