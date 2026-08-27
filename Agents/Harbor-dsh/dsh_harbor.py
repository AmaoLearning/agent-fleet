"""DeepSeek Harness adapter backed by Agent Fleet's pinned offline runtime."""

from __future__ import annotations

import base64
import json
import os
import shlex
from pathlib import Path
from typing import Any, override

import yaml
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class AgentFleetDsh(BaseInstalledAgent):
    """Run pinned DSH in headless mode without network installs in the task.

    Agent Fleet prepares Node 22 and ``@deepseek-ai/dsh`` once on the runner,
    then mounts both archives read-only into Docker/OpenSandbox trials.  Each
    trial writes an auditable Cordis patch under ``DSH_HOME`` and executes one
    task through ``dsh --profile headless``.
    """

    _OUTPUT_FILENAME = "dsh.txt"
    _DSH_HOME = "/installed-agent/dsh-home"
    _NODE_HOME = "/installed-agent/dsh-node"
    _PATCH_PATH = f"{_DSH_HOME}/cordis.patch.yml"
    _SETTINGS_PATH = f"{_DSH_HOME}/settings.yaml"
    # Cordis resolves relative plugin entries from the selected profile
    # directory, not from DSH_HOME.  Keep the uploaded module at that exact
    # resolution target so headless startup cannot depend on the caller cwd.
    _HEADLESS_PROFILE_DIR = f"{_DSH_HOME}/profiles/headless"
    _SAMPLING_PLUGIN_PATH = f"{_HEADLESS_PROFILE_DIR}/sampling-plugin.mjs"
    _SESSION_ROOT = "/logs/agent/dsh-sessions"
    _API_KEY_ENV = "DSH_API_KEY"
    _NATIVE_MODEL_PROVIDER = "deepseek"
    _NATIVE_DSH_PROVIDER = "deepseek-official"
    _GENERIC_MODEL_PROVIDER = "harbor"

    @staticmethod
    @override
    def name() -> str:
        return "dsh"

    def __init__(
        self,
        *args: Any,
        permission_mode: str = "danger-full-access",
        thinking: str = "enabled",
        reasoning_effort: str = "max",
        temperature: str | float = "1.0",
        max_tokens: str | int = "256000",
        context_window: str | int = "1000000",
        provider_retry_max: str | int = "0",
        request_timeout_ms: str | int = "300000",
        stream_idle_timeout_ms: str | int = "300000",
        provider_route: str = "deepseek",
        thinking_format: str = "deepseek",
        top_p: str | float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if permission_mode not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ValueError(f"invalid DSH permission_mode: {permission_mode!r}")
        if thinking not in {"enabled", "disabled"}:
            raise ValueError(f"invalid DSH thinking mode: {thinking!r}")
        if reasoning_effort not in {"off", "low", "high", "max"}:
            raise ValueError(
                f"invalid DSH reasoning_effort: {reasoning_effort!r}"
            )
        if thinking == "disabled" and reasoning_effort != "off":
            raise ValueError(
                "DSH thinking=disabled requires reasoning_effort=off"
            )
        if top_p not in (None, ""):
            raise ValueError(
                "DSH 0.1.1-rc.2 does not expose top_p; refusing to ignore it"
            )
        if provider_route not in {
            self._NATIVE_MODEL_PROVIDER,
            self._GENERIC_MODEL_PROVIDER,
        }:
            raise ValueError(
                "DSH provider_route must be deepseek or harbor"
            )
        if provider_route == self._GENERIC_MODEL_PROVIDER and not thinking_format:
            raise ValueError(
                "DSH generic route requires an explicit thinking_format"
            )

        self._provider_route = provider_route
        self._thinking_format = thinking_format
        self._permission_mode = permission_mode
        self._thinking = thinking
        self._reasoning_effort = reasoning_effort
        self._temperature = self._finite_float(
            "temperature", temperature, minimum=0.0, maximum=2.0
        )
        self._max_tokens = self._positive_int("max_tokens", max_tokens)
        self._context_window = self._positive_int(
            "context_window", context_window
        )
        self._provider_retry_max = self._nonnegative_int(
            "provider_retry_max", provider_retry_max
        )
        self._request_timeout_ms = self._positive_int(
            "request_timeout_ms", request_timeout_ms
        )
        self._stream_idle_timeout_ms = self._positive_int(
            "stream_idle_timeout_ms", stream_idle_timeout_ms
        )

    @staticmethod
    def _finite_float(
        name: str,
        value: str | float,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _positive_int(name: str, value: str | int) -> int:
        parsed = AgentFleetDsh._nonnegative_int(name, value)
        if parsed == 0:
            raise ValueError(f"{name} must be positive")
        return parsed

    @staticmethod
    def _nonnegative_int(name: str, value: str | int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < 0 or str(parsed) != str(value).strip():
            raise ValueError(f"{name} must be a non-negative integer")
        return parsed

    def _model_id(self) -> str:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError(
                "DSH model name must be <provider>/<wire-model-id>"
            )
        provider, model_id = self.model_name.split("/", 1)
        if provider != self._provider_route or not model_id:
            raise ValueError(
                "DSH model provider prefix must match provider_route "
                f"{self._provider_route!r}"
            )
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

    def build_eval_patch(self) -> str:
        """Render the highest-priority user Cordis layer for this trial."""
        model_id = self._model_id()
        dsh_provider = (
            self._NATIVE_DSH_PROVIDER
            if self._provider_route == self._NATIVE_MODEL_PROVIDER
            else self._GENERIC_MODEL_PROVIDER
        )
        entries: list[dict[str, Any]] = [
            {
                "id": "agent-default-model",
                "config": {
                    "provider": dsh_provider,
                    "model": model_id,
                },
            },
            {
                "id": "session-persistence-jsonl",
                "config": {
                    "root": self._SESSION_ROOT,
                    "compression": "none",
                    "packChunks": False,
                },
            },
            {"id": "session-title-llm", "disabled": True},
            {
                "insert": [
                    {
                        "id": "agent-fleet-sampling",
                        "name": "./sampling-plugin.mjs",
                        "config": {"temperature": self._temperature},
                    }
                ]
            },
        ]
        if self._provider_route == self._NATIVE_MODEL_PROVIDER:
            entries.insert(
                3,
                {
                    "id": "llm-deepseek",
                    "config": {
                        "apiKeyEnv": self._API_KEY_ENV,
                        "baseURL": self._base_url(),
                        "thinking": self._thinking,
                        "reasoningEffort": self._reasoning_effort,
                        "maxTokens": self._max_tokens,
                        "defaultContextWindow": self._context_window,
                        "models": [
                            {
                                "id": model_id,
                                "name": model_id,
                                "contextWindow": self._context_window,
                                "maxTokens": self._max_tokens,
                            }
                        ],
                        "streamIdleTimeoutMs": self._stream_idle_timeout_ms,
                        "retryPolicy": {
                            "mode": "normal",
                            "maxRetries": self._provider_retry_max,
                        },
                    },
                },
            )
        return yaml.safe_dump(entries, sort_keys=False)

    def build_settings(self) -> str | None:
        """Declare the pi-ai OpenAI-compatible route for private gateways."""
        if self._provider_route == self._NATIVE_MODEL_PROVIDER:
            return None
        model_id = self._model_id()
        return yaml.safe_dump(
            {
                "llm-pi-ai": {
                    "providers": {
                        self._GENERIC_MODEL_PROVIDER: {
                            "apiKeyEnv": self._API_KEY_ENV,
                            "api": "openai-completions",
                            "baseURL": self._base_url(),
                            "models": [
                                {
                                    "id": model_id,
                                    "name": model_id,
                                    "contextWindow": self._context_window,
                                    "maxTokens": self._max_tokens,
                                }
                            ],
                            "timeoutMs": self._request_timeout_ms,
                            "streamIdleTimeoutMs": self._stream_idle_timeout_ms,
                            "retryPolicy": {
                                "mode": "normal",
                                "maxRetries": self._provider_retry_max,
                            },
                            "compat": {
                                "thinkingFormat": self._thinking_format
                            },
                        }
                    }
                }
            },
            sort_keys=False,
        )

    def _runtime_env(self, *, placeholder_key: bool = False) -> dict[str, str]:
        api_key = self._get_env(self._API_KEY_ENV)
        if not api_key and not placeholder_key:
            raise ValueError("DSH_API_KEY is required")
        return {
            "DSH_API_KEY": api_key or "config-dump-placeholder",
            "DSH_BASE_URL": self._base_url(),
            "DSH_HOME": self._DSH_HOME,
            "DSH_PERMISSION_MODE": self._permission_mode,
            "DSH_TELEMETRY_DISABLED": "1",
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
    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; dsh --version'

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        version = str(self.version() or os.environ.get("DSH_VERSION", "0.1.1-rc.2"))
        owner = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {shlex.quote(self._DSH_HOME)} "
                f"{shlex.quote(self._HEADLESS_PROFILE_DIR)} "
                f"{shlex.quote(self._NODE_HOME)} /logs/agent && "
                f"chown -R {shlex.quote(owner)} "
                f"{shlex.quote(self._DSH_HOME)} {shlex.quote(self._NODE_HOME)} "
                "/logs/agent"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                'cache_dir="${DSH_CACHE_DIR:-/opt/tb-opik/python-wheels}"; '
                'node_tar="${DSH_NODE_RUNTIME_PATH:-$cache_dir/node-runtime.tar.gz}"; '
                f'runtime_tar="${{DSH_RUNTIME_TAR_PATH:-$cache_dir/dsh-runtime-{version}.tar.gz}}"; '
                'test -f "$node_tar" || { echo "missing DSH Node runtime: $node_tar" >&2; exit 1; }; '
                'test -f "$runtime_tar" || { echo "missing DSH runtime: $runtime_tar" >&2; exit 1; }; '
                f"rm -rf {shlex.quote(self._NODE_HOME)}/*; "
                f"tar -xzf \"$node_tar\" -C {shlex.quote(self._NODE_HOME)} --strip-components=1; "
                'mkdir -p "$HOME/.local/bin"; '
                f"ln -sf {shlex.quote(self._NODE_HOME)}/bin/node \"$HOME/.local/bin/node\"; "
                f"ln -sf {shlex.quote(self._NODE_HOME)}/bin/npm \"$HOME/.local/bin/npm\"; "
                f"ln -sf {shlex.quote(self._NODE_HOME)}/bin/npx \"$HOME/.local/bin/npx\"; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]')\"; "
                'test "$node_major" -ge 22 || { echo "DSH requires Node 22+" >&2; exit 1; }; '
                'tar -xzf "$runtime_tar" -C "$HOME/.local"; '
                "dsh --version"
            ),
        )

        sampling_path = Path(__file__).with_name("sampling_plugin.mjs")
        await self._upload_text(
            environment,
            content=self.build_eval_patch(),
            path=self._PATCH_PATH,
        )
        settings = self.build_settings()
        if settings is not None:
            await self._upload_text(
                environment,
                content=settings,
                path=self._SETTINGS_PATH,
            )
        await self._upload_text(
            environment,
            content=sampling_path.read_text(encoding="utf-8"),
            path=self._SAMPLING_PLUGIN_PATH,
        )

        # This is also the install-time plugin/profile conformance check.  Do
        # not hide loader failures: a broken profile must fail setup before an
        # evaluation attempt is spent on the agent command.
        await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "dsh --profile headless --dump-config "
                "> /logs/agent/dsh-config-dump.yml 2>&1"
            ),
            env=self._runtime_env(placeholder_key=True),
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._model_id()
        await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                f"dsh --profile headless {shlex.quote(instruction)} "
                "< /dev/null 2>&1 | "
                f"tee /logs/agent/{self._OUTPUT_FILENAME}"
            ),
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
