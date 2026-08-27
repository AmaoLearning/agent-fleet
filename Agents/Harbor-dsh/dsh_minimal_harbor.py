"""Offline Harbor adapter for the official DeepSeek Harness minimal SDK."""

from __future__ import annotations

import base64
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class AgentFleetDshMinimal(BaseInstalledAgent):
    """Run the official two-tool minimal composition without network installs."""

    _PYTHON_ROOT = "/opt/dsh-minimal-python3.12-runtime"
    _PYTHON = f"{_PYTHON_ROOT}/bin/python3.12"
    _RUNTIME_ROOT = "/opt/dsh-minimal-runtime"
    _SITE_PACKAGES = f"{_RUNTIME_ROOT}/site-packages"
    _REMOTE_RUNNER = "/installed-agent/minimal.py"
    _REMOTE_CONFIG = "/installed-agent/minimal.cordis.yml"
    _REMOTE_RELAY = "/installed-agent/dsh_sampling_relay.py"
    _RELAY_PORT = 18100
    _SESSION_ROOT = "/logs/agent/dsh-sessions"
    _OUTPUT_FILENAME = "dsh-minimal.txt"

    @staticmethod
    @override
    def name() -> str:
        return "dsh-minimal"

    def __init__(
        self,
        *args: Any,
        permission_mode: str = "danger-full-access",
        provider_route: str = "deepseek",
        context_window: str | int = "1000000",
        max_tokens: str | int | None = None,
        provider_retry_max: str | int = "0",
        process_retry_max: str | int = "0",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if permission_mode != "danger-full-access":
            raise ValueError(
                "dsh-minimal uses the official danger-full-access composition"
            )
        if provider_route != "deepseek":
            raise ValueError(
                "dsh-minimal's official composition supports the native "
                "DeepSeek provider only"
            )
        if self.skills_dir or self.mcp_servers:
            raise ValueError("dsh-minimal does not support Skills or MCP servers")
        self._context_window = self._positive_int("context_window", context_window)
        self._max_tokens = (
            None
            if max_tokens in (None, "")
            else self._positive_int("max_tokens", max_tokens)
        )
        self._provider_retry_max = self._nonnegative_int(
            "provider_retry_max", provider_retry_max
        )
        self._process_retry_max = self._nonnegative_int(
            "process_retry_max", process_retry_max
        )

    @staticmethod
    def _positive_int(name: str, value: str | int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed <= 0 or str(parsed) != str(value).strip():
            raise ValueError(f"{name} must be positive")
        return parsed

    @staticmethod
    def _nonnegative_int(name: str, value: str | int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < 0 or str(parsed) != str(value).strip():
            raise ValueError(f"{name} must be nonnegative")
        return parsed

    def _model_id(self) -> str:
        if not self.model_name or not self.model_name.startswith("deepseek/"):
            raise ValueError("dsh-minimal model name must be deepseek/<wire-model-id>")
        model_id = self.model_name.split("/", 1)[1]
        if not model_id:
            raise ValueError("dsh-minimal model ID cannot be empty")
        return model_id

    def _base_url(self) -> str:
        value = self._get_env("DSH_BASE_URL")
        if not value:
            raise ValueError("DSH_BASE_URL is required")
        normalized = value.rstrip("/")
        for endpoint in ("/chat/completions", "/responses"):
            if normalized.endswith(endpoint):
                normalized = normalized[: -len(endpoint)]
                break
        return normalized

    def _runtime_env(self, *, placeholder_key: bool = False) -> dict[str, str]:
        api_key = self._get_env("DSH_API_KEY")
        if not api_key and not placeholder_key:
            raise ValueError("DSH_API_KEY is required")
        return {
            "DEEPSEEK_API_KEY": api_key or "config-dump-placeholder",
            "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{self._RELAY_PORT}/v1",
            "DSH_CONTEXT_WINDOW": str(self._context_window),
            "DSH_MODEL": self._model_id(),
            "DSH_PROVIDER_RETRY_MAX": str(self._provider_retry_max),
            "DSH_SESSION_ROOT": self._SESSION_ROOT,
            "DSH_TELEMETRY_DISABLED": "1",
            "DSH_SAMPLING_RELAY_PORT": str(self._RELAY_PORT),
            "DSH_SAMPLING_RECEIPT_PATH": "/logs/agent/sampling-relay.jsonl",
            "DSH_SAMPLING_UPSTREAM_BASE_URL": self._base_url(),
            "PYTHONPATH": self._SITE_PACKAGES,
        }

    @staticmethod
    def _encoded(content: str) -> str:
        return base64.b64encode(content.encode()).decode("ascii")

    async def _upload_text(
        self,
        environment: BaseEnvironment,
        *,
        content: str,
        path: str,
    ) -> None:
        await self.exec_as_agent(
            environment,
            command=(
                f"printf %s {shlex.quote(self._encoded(content))} | "
                f"base64 -d > {shlex.quote(path)}"
            ),
        )

    @override
    def get_version_command(self) -> str:
        return (
            f"PYTHONPATH={shlex.quote(self._SITE_PACKAGES)} {self._PYTHON} "
            "-c \"from importlib.metadata import version; "
            "print(version('deepseek-harness-sdk'))\""
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        version = str(self.version() or os.environ.get("DSH_MINIMAL_SDK_VERSION", "0.1.0-rc.6"))
        owner = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                "command -v bash >/dev/null && command -v tar >/dev/null && "
                "command -v base64 >/dev/null && "
                "mkdir -p /installed-agent /logs/agent /opt && "
                f"chown -R {shlex.quote(owner)} /installed-agent /logs/agent && "
                f"rm -rf {shlex.quote(self._PYTHON_ROOT)} "
                f"{shlex.quote(self._RUNTIME_ROOT)} && "
                'test -f "${DSH_PYTHON_RUNTIME_PATH}" && '
                'test -f "${DSH_MINIMAL_RUNTIME_TAR_PATH}" && '
                'tar -xzf "${DSH_PYTHON_RUNTIME_PATH}" -C /opt && '
                'tar -xzf "${DSH_MINIMAL_RUNTIME_TAR_PATH}" -C /opt'
            ),
            env={
                "DSH_PYTHON_RUNTIME_PATH": self._get_env(
                    "DSH_PYTHON_RUNTIME_PATH"
                )
                or (
                    "/opt/tb-opik/python-wheels/"
                    "dsh-minimal-python3.12-runtime.tar.gz"
                ),
                "DSH_MINIMAL_RUNTIME_TAR_PATH": self._get_env(
                    "DSH_MINIMAL_RUNTIME_TAR_PATH"
                )
                or (
                    "/opt/tb-opik/python-wheels/"
                    f"dsh-minimal-runtime-{version}.tar.gz"
                ),
            },
        )

        cordis_filename = (
            "dsh_minimal_recovery.cordis.yml"
            if self._provider_retry_max
            else "dsh_minimal.cordis.yml"
        )
        for filename, remote in (
            ("dsh_minimal_runner.py", self._REMOTE_RUNNER),
            (cordis_filename, self._REMOTE_CONFIG),
            ("dsh_sampling_relay.py", self._REMOTE_RELAY),
        ):
            await self._upload_text(
                environment,
                content=Path(__file__).with_name(filename).read_text(encoding="utf-8"),
                path=remote,
            )
        await self.exec_as_agent(
            environment,
            command=f"{self.get_version_command()} > /logs/agent/dsh-minimal-version.txt",
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        session_id = f"harbor-{uuid.uuid4().hex}"
        runner_parts = [
            shlex.quote(self._PYTHON),
            shlex.quote(self._REMOTE_RUNNER),
            '--workspace "$PWD"',
            "--session-root",
            shlex.quote(self._SESSION_ROOT),
            "--provider",
            "deepseek-official",
            "--model",
            shlex.quote(self._model_id()),
        ]
        if self._max_tokens is not None:
            runner_parts.extend(("--max-tokens", str(self._max_tokens)))
        runner_parts.extend(
            (
                '--session-id "$1"',
                shlex.quote(instruction),
            )
        )
        runner = " ".join(runner_parts)
        output = f"/logs/agent/{self._OUTPUT_FILENAME}"
        script = f"""\
set -o pipefail
relay_log=/logs/agent/sampling-relay.log
dsh_session_id={shlex.quote(session_id)}
printf '%s\n' "$PWD" > /logs/agent/dsh-workspace.txt
printf '%s\n' "$dsh_session_id" > /logs/agent/dsh-session-id.txt
{self._PYTHON} {self._REMOTE_RELAY} >>"$relay_log" 2>&1 &
relay_pid=$!
cleanup_relay() {{
  kill "$relay_pid" 2>/dev/null || true
  wait "$relay_pid" 2>/dev/null || true
}}
trap cleanup_relay EXIT INT TERM
relay_ready=0
for attempt in {{1..50}}; do
  if {self._PYTHON} -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:{self._RELAY_PORT}/healthz", timeout=1).read()'; then
    relay_ready=1
    break
  fi
  sleep 0.1
done
if (( relay_ready == 0 )); then
  echo "sampling relay failed to become ready" >&2
  exit 70
fi
run_dsh() {{
  {runner}
}}
retry=0
while true; do
  attempt_session_id="$dsh_session_id"
  if (( retry > 0 )); then
    attempt_session_id="$dsh_session_id-retry-$retry"
  fi
  if (( retry == 0 )); then
    run_dsh "$attempt_session_id" 2>&1 | stdbuf -oL tee {shlex.quote(output)}
  else
    run_dsh "$attempt_session_id" 2>&1 | stdbuf -oL tee -a {shlex.quote(output)}
  fi
  status=${{PIPESTATUS[0]}}
  if (( status == 0 || retry >= {self._process_retry_max} )); then
    exit "$status"
  fi
  retry=$((retry + 1))
  delay=$((retry * 5))
  printf 'agent-fleet: restarting dsh-minimal process after exit %s (retry %s/%s, delay %ss)\n' \
    "$status" "$retry" "{self._process_retry_max}" "$delay" >&2
  sleep "$delay"
done
"""
        await self.exec_as_agent(
            environment,
            command=f"bash -lc {shlex.quote(script)}",
            env=self._runtime_env(),
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        root = self.logs_dir / "dsh-sessions"
        if not root.is_dir():
            return

        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0
        for path in root.rglob("session.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant/message":
                    continue
                data = event.get("data")
                usage = data.get("usage") if isinstance(data, dict) else None
                if not isinstance(usage, dict):
                    continue
                cache_read = int(usage.get("cacheReadTokens") or 0)
                cache_write = int(usage.get("cacheWriteTokens") or 0)
                input_tokens += int(usage.get("inputTokens") or 0)
                input_tokens += cache_read + cache_write
                cache_tokens += cache_read
                output_tokens += int(usage.get("outputTokens") or 0)

        context.n_input_tokens = input_tokens or None
        context.n_output_tokens = output_tokens or None
        context.n_cache_tokens = cache_tokens or None
