# Harbor DeepSeek Harness

`dsh_harbor.py` runs the pinned `@deepseek-ai/dsh` runtime through its
one-shot `headless` profile. Node and DSH are prepared once on the Agent Fleet
runner and mounted into each Docker/OpenSandbox task, so agent setup performs
no package-manager network access.

The adapter supports two explicit routes. `DSH_PROVIDER=deepseek` selects
DSH's native `deepseek-official` wire. `DSH_PROVIDER=harbor` declares a pi-ai
`openai-completions` provider for private gateways and pins
`compat.thinkingFormat=deepseek`. The latter is required for YiCloud MaaS,
which exposes `reasoning_content` through an OpenAI-compatible API rather than
native DSML. The generic profile also freezes its model context/output limits,
HTTP request timeout, stream-idle timeout, and provider retry budget instead of
inheriting DSH defaults. It records the resolved Cordis tree, uncompressed
session JSONL, stdout/stderr, and aggregate token counts.

DSH `0.1.1-rc.2` exposes `temperature`, `maxTokens`, `stop`, and native
`reasoningEffort`, but its request vocabulary does not expose `top_p`. The
adapter rejects a non-empty `top_p` instead of silently claiming that it was
forwarded. The generic route also does not claim to forward native
`reasoningEffort`; its DeepSeek reasoning dialect is configured explicitly and
must be audited from the resulting MaaS trajectory.

## Official minimal SDK agent

Set `AGENT=dsh-minimal` to run the separate official two-tool composition from
the public Harbor `Add DeepSeek Harness minimal SDK agent` implementation. It
uses only persistent Bash and `str_replace_editor`; it does not enable skills,
web tools, subagents, workflow tools, or context compaction.

Agent Fleet pins `deepseek-harness-sdk==0.1.0-rc.6`. The development host
prepares the SDK and its native runtime together with the portable Python 3.12
`python-build-standalone` runtime, then mounts the two archives read-only into
each Docker/OpenSandbox
task. The task environment performs no `curl`, `pip`, or package-manager
installation during agent setup.

```bash
export AGENT=dsh-minimal
export DSH_PROVIDER=deepseek
export DSH_MINIMAL_SDK_VERSION=0.1.0-rc.6
export DSH_BASE_URL=https://gateway.example.test/v1
export DSH_API_KEY=replace-me
export DSH_PROVIDER_RETRY_MAX=5
export DSH_PROCESS_RETRY_MAX=2
```

The minimal SDK route intentionally supports only DSH's native
`deepseek-official` provider with an optional custom `DEEPSEEK_BASE_URL`.
`DSH_PROVIDER=harbor` remains available only to the headless `dsh` control
agent. The minimal composition pins the native DeepSeek provider to
`thinking: enabled` and `reasoningEffort: max`. A loopback-only relay between
the SDK and MaaS overwrites every completion request with
`reasoning_effort=max`, `temperature=1.0`, and `top_p=0.95`. It writes a
redacted per-request receipt to `/logs/agent/sampling-relay.jsonl`; prompts,
API keys, reasoning, and tool arguments are never recorded there.
`DSH_PROVIDER_RETRY_MAX` opts into DSH's bounded `normal` request retry policy
for empty responses, rate limits, server errors, timeouts, transport failures,
and YiCloud's observed intermittent `HTTP_405`. Retries stay inside the failed
DSH step and do not add a user turn or modify the system prompt.

`DSH_PROCESS_RETRY_MAX` is a second, agent-fleet-only recovery layer and also
defaults to `0`. After a non-zero DSH exit it starts a fresh DSH process in the
same task sandbox, with the exact same benchmark instruction and existing
filesystem state. It does not add or modify any prompt, persona, tool, or DSH
agent-loop setting. Each process writes a separate DSH session JSONL so the
restart remains auditable.

## Quick start

Keep credentials and deployment-specific values in ignored `config.local.env`.
From the repository root, a local Docker smoke run can be launched with:

```bash
set -a
source config.local.env
set +a

export AGENT=dsh
export DSH_PROVIDER=harbor
export DSH_VERSION=0.1.1-rc.2
export MODEL="$DS_MODEL_YICLOUD"
export DATASET_PATH=/absolute/path/to/terminal-bench-2.1
export HARBOR_ENVIRONMENT_TYPE=docker
export HARBOR_RUNS=1
export HARBOR_N_CONCURRENT=1
export OUTPUT_PATH="$PWD/runs/dsh-smoke"
bash Agents/utils/common/Harbor/harboropik.sh
```

`DSH_PROVIDER=harbor` expects `DSH_BASE_URL` and `DSH_API_KEY` (the launcher
also accepts the repository's model gateway aliases). Use
`DSH_PROVIDER=deepseek` only with DeepSeek's native API. Increase
`HARBOR_N_CONCURRENT` after a one-task trajectory and verifier result have been
confirmed. For YiCloud Sandbox, change `HARBOR_ENVIRONMENT_TYPE` to
`opensandbox` and configure the documented `YICLOUD_SANDBOX_*` variables in
`config.local.env`.
