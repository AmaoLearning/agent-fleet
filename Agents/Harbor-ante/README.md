# Harbor Ante

This integration runs the pinned Ante binary through Agent Fleet's shared
Harbor, Zellij, and YiCloud OpenSandbox path. It is based on the Apache-2.0
`ante-harbor` adapter published in AntigmaLabs/ante at tag `v0.preview.71`,
with compatibility for the repository's pinned `harbor==0.18.0`.

The runner downloads the exact release manifest once, verifies the published
size and SHA-256 through Ante's installer, caches the Linux binary on the
runner, and uploads it into each Sandbox. Task Sandboxes do not install Ante
from the internet.

```bash
AGENT=ante \
ANTE_VERSION=0.preview.71 \
ANTE_PROVIDER=openai-compatible \
ANTE_REASONING_EFFORT=max \
HARBOR_TEMPERATURE=1.0 \
HARBOR_TOP_P=0.95 \
HARBOR_MAX_RETRIES=0 \
N_ATTEMPTS=1 \
OPIK_URL= \
HARBOR_ANALYZER_ENABLED=0 \
HARBOR_ENVIRONMENT_TYPE=opensandbox \
bash Agents/utils/common/Harbor/start.sh --detach
```

Defaults match the public Ante evaluation flags:

```text
--yolo --output-format json --no-session-save --no-skills --check
```

The JSON event stream is stored in `/logs/agent/ante.txt`; ATIF conversion is
enabled by default and writes `trajectory.json` in the Harbor agent logs. This
initial integration intentionally does not implement Opik, Analyzer, RL
rollouts, or prompt-mode FleetSpec generation. The shared monitor and Zellij
workers remain available for concurrency and progress.

Important variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTE_VERSION` | `0.preview.71` | Exact binary version |
| `ANTE_MANIFEST_URL` | release manifest for the version | Checksum-verified artifact source |
| `ANTE_BINARY_PATH` | runner cache path | Host binary uploaded to Sandbox |
| `ANTE_PROVIDER` | `openai-compatible` | Ante provider ID |
| `ANTE_REASONING_EFFORT` | `max` | Ante `--effort` value |
| `ANTE_ARGS` | public evaluation flags | Additional headless behavior |
| `ANTE_MODEL_BASE_URL` | `${BASE_URL}/v1` | Model endpoint inside Sandbox |
| `ANTE_ENABLE_ATIF` | `true` | Generate Harbor trajectory artifact |

The Ante binary is distributed under Antigma's binary terms; this repository
contains only the Apache-2.0 adapter code and runtime preparation glue.
