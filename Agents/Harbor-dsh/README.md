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
