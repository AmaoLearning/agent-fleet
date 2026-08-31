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

The vendored runner is byte-identical to DeepSeek Harness `minimal.py` at
`663253099d5174eddc0bf09542fa63dcc1171331` (SHA-256
`0ccda1dac75d73f7bf61ee4f0bc072344b0ddcd2c114579d85342e22010d67fb`). The
default Cordis file is byte-identical to `minimal.cordis.yml` at
`3bcc1a0bf791d5d1368640d4453a4418b715f2e1` (SHA-256
`4ddf99b5492fac7b578e3caddb0158815e44d5db176ba0aeab57012d35299fca`).

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
# Leave empty to preserve the provider default; set only when the reproduction
# contract freezes the official SDK max_tokens option.
export DSH_MINIMAL_MAX_TOKENS=
export DSH_PROVIDER_RETRY_MAX=0
export DSH_PROCESS_RETRY_MAX=0
```

The minimal SDK route intentionally supports only DSH's native
`deepseek-official` provider with an optional custom `DEEPSEEK_BASE_URL`.
`DSH_PROVIDER=harbor` remains available only to the headless `dsh` control
agent. Its default Cordis file tracks DeepSeek's checked-in
`examples/jsonrpc-agent/minimal.cordis.yml`: the Harbor wrapper supplies an
explicit task workspace, isolated session root, unique session ID, provider,
model, and optional `max_tokens`, while retaining the official final-response
and process-exit semantics. A loopback-only relay between the SDK and MaaS
overwrites every completion request with
`reasoning_effort=max`, `temperature=1.0`, and `top_p=0.95`. It writes a
redacted per-request receipt to `/logs/agent/sampling-relay.jsonl`; prompts,
API keys, reasoning, and tool arguments are never recorded there.
The explicit workspace and base session ID are recorded in
`/logs/agent/dsh-workspace.txt` and `/logs/agent/dsh-session-id.txt`.

`DSH_PROVIDER_RETRY_MAX=0` uses the unmodified official minimal Cordis behavior.
A positive value explicitly selects a separate recovery Cordis variant with a
bounded `normal` request retry policy for empty responses, rate limits, server
errors, timeouts, transport failures, and YiCloud's observed intermittent
`HTTP_405`. Retries stay inside the failed DSH step and do not add a user turn
or modify the system prompt.

`DSH_PROCESS_RETRY_MAX` is a second, agent-fleet-only recovery layer and also
defaults to `0`. After a non-zero DSH exit it starts a fresh DSH process in the
same task sandbox, with the exact same benchmark instruction and existing
filesystem state. It does not add or modify any prompt, persona, tool, or DSH
agent-loop setting. The baseline session uses a unique `harbor-<uuid>` ID;
restarted processes use explicit `-retry-N` session IDs so each process writes
a separate, auditable DSH session JSONL.

## Version-matched SDK minimal profile

Set `AGENT=dsh-sdk-minimal` to run the official `sdk-minimal` profile through
the newer Python JSON-RPC SDK. This is a parallel control; it does not replace
the frozen `dsh-minimal` implementation above. The default pins both the DSH
CLI package and Python SDK source to `dsh-v0.1.2-alpha.2` at commit
`0a53fb55bea101816fa226bb964ae2bed71c343b`. The source commit is explicit
because an equivalent alpha Python SDK wheel is not published.

Unlike the older adapter, this route supplies `dsh_home`, `dsh_bin`,
`profile=sdk-minimal`, and `reasoning_effort=max` to the SDK and lets the
version-matched profile own its complete Cordis composition. It does not
upload a custom Cordis patch. Agent setup validates the profile with
`dsh --profile sdk-minimal --dump-config`; the resolved config, DSH/SDK source
fingerprints, sessions, stdout/stderr, and sampling-relay receipts remain in
`/logs/agent`.

```bash
export AGENT=dsh-sdk-minimal
export DSH_PROVIDER=deepseek
export DSH_SDK_MINIMAL_DSH_VERSION=0.1.2-alpha.2
export DSH_SDK_MINIMAL_SOURCE_REF=dsh-v0.1.2-alpha.2
export DSH_SDK_MINIMAL_SOURCE_SHA=0a53fb55bea101816fa226bb964ae2bed71c343b
# Optional: use an existing checkout whose HEAD exactly matches SOURCE_SHA.
export DSH_SDK_MINIMAL_SOURCE_DIR=
export DSH_SDK_MINIMAL_MAX_TOKENS=
export DSH_PROCESS_RETRY_MAX=0
```

The same loopback sampling relay fixes `reasoning_effort=max`,
`temperature=1.0`, and `top_p=0.95` at the MaaS request boundary. This route
does not add the older adapter's optional provider-retry Cordis patch.

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
