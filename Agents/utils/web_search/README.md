# Shared Web Search Preparation

Implementation shared by the [BrowseComp](../../../Tasks/BrowseComp/) and
[DeepSearchQA](../../../Tasks/DeepSearchQA/) task entrypoints. With
`HARBOR_CC_WEB_MCP_ENABLED=1`, startup prepares a missing selected dataset once.
Generated task directories use the existing Harbor runner or HTTP rollout
listener; conversion never runs inside an agent trial.
Source validation and reward semantics remain dataset-specific.

Official task counts:

| Dataset | Tasks |
| --- | ---: |
| BrowseComp | 1266 |
| DeepSearchQA | 900 |
| Total | 2166 |

Counts were verified with Python's CSV parser against these source hashes:

```text
browse_comp_test_set.csv  7b24471cd5b3eb2a46830a14802b5c029ea62f488ff75a0f88af7923d1454abf
DSQA-full.csv              25d48dcf7efa872e5467032e8b8eedf38d301f59a252d0da95cda584baa78396
```

Generation rejects a source whose parsed task count or SHA-256 does not match
these official files. Validation runs before `--limit` or `--task-ids` filtering.

## Automatic Preparation

After the normal host setup and private model/sandbox configuration:

```bash
HARBOR_CC_WEB_MCP_ENABLED=1 OPIK_URL= \
  ./scripts/run_fleet.sh --taskset browsecomp --agent opencode --workers 50
```

Use `--taskset deepsearchqa` for DeepSearchQA. The dedicated [env.sh](env.sh)
defaults `DATASET_PATH` to `$AGENT_FLEET_CACHE_DIR/web-search/tasks/$DATASET_NAME`
(`~/.cache/agent-fleet/web-search/tasks/...` by default). An explicit
`DATASET_PATH` takes precedence. **Only directory existence is checked for reuse**,
including empty directories: no download, regeneration, or source/hash checks.
To regenerate, remove the old directory deliberately or choose a new path.

If missing, startup downloads the official CSV and calls the existing adapter.
Generation uses a lock and publishes the complete directory only on success;
failure stops startup without leaving a reusable partial dataset. Merely sourcing
configuration or stopping the service does not prepare data.

Source CSVs are cached under `$AGENT_FLEET_CACHE_DIR/web-search/sources/`.
For offline hosts, place `browse_comp_test_set.csv` or `DSQA-full.csv` there
before starting. DeepSearchQA downloads honor the existing `HF_ENDPOINT`
(e.g. `https://hf-mirror.com`); BrowseComp uses OpenAI's public blob endpoint.
New generation retains the source checks below and uses
`HARBOR_OPENSANDBOX_IMAGE_REF` if set, otherwise `python:3.12-slim`.
No new enable flag is needed: disabling `HARBOR_CC_WEB_MCP_ENABLED` leaves
manual preparation and other datasets unchanged.
With this switch disabled, `browsecomp` and `deepsearchqa` require an explicit
`DATASET_PATH` to prepared tasks; they never fall back to the SETA directory.

## Manual Preparation (Optional)

```bash
cd Agents/utils/web_search
uv run browsecomp-adapter \
  --input /data/browse_comp_test_set.csv \
  --output-dir /data/harbor/browsecomp

uv run deepsearchqa-adapter \
  --input /data/DSQA-full.csv \
  --output-dir /data/harbor/deepsearchqa
```

Use `--limit`, `--task-ids`, `--overwrite`, and `--image` for smaller or
environment-specific generations. `--image` selects both the Dockerfile base
and Harbor's prebuilt `environment.docker_image`; it defaults to
`python:3.12-slim`. OpenSandbox deployments should pass an image already
available to their configured registry.

## Agent Fleet

For rollout, select the primary dataset using the same `DATASET_NAME`; the
default `RL_DATASET_NAME` and `RL_DATASET_ROOT` inherit it. A missing primary
web dataset is prepared before opening the listener:

```bash
export RL_AGENT=claude-code
export RL_ENVIRONMENT_TYPE=opensandbox
export ROLLOUT=1 DATASET_NAME=browsecomp
export HARBOR_CC_WEB_MCP_ENABLED=1
export EXA_API_KEY=anonymous
export OPIK_URL=
bash Agents/utils/common/Harbor/start.sh
```

Existing `RL_DATASET_NAME` / `RL_DATASET_ROOT` overrides are respected. When
using those directly, set both name and root. Additional already-prepared
datasets can still be registered with `RL_DATASET_ROOTS`, e.g.
`browsecomp=/data/harbor/browsecomp,deepsearchqa=/data/harbor/deepsearchqa`.

Requests select `dataset_name=browsecomp` or `dataset_name=deepsearchqa` and a
generated task ID. Reward and trajectory collection use the existing Harbor
verifier and `rollout_details` path; do not configure `RL_RESULT_PROCESSOR`.

The task data does not embed a search provider, credential, or agent runtime.
Enable the deployment's [shared Exa MCP](../common/mcp/README.md)
separately. Its existing content-addressed cache and OpenSandbox S3 transport
deliver the same filtered tools to each supported harness. Anonymous mode
does not require a paid Exa key and is subject to rate limits.

Claude uses its existing strict MCP settings. OpenCode uses native `mcp.web`
configuration and disables native `websearch`/`webfetch` when this MCP is enabled.
Both pass only the public task instruction to the filter, not verifier answers.
Pi has an existing extension loading interface; its Exa integration is pending
confirmation of the reusable extension. Do not treat Pi as live-validated yet.

Keep `MODEL` as the exact gateway model ID, including any slash in that ID.
If a harness needs a provider prefix, configure `HARBOR_MODEL` separately, e.g.
`MODEL=organization/model HARBOR_MODEL=custom/organization/model` for OpenCode,
or `HARBOR_MODEL=gateway-host/organization/model` for Pi. The judge uses `MODEL`,
never the harness-only provider prefix.

The verifier reuses the current trial's OpenAI-compatible `HARBOR_API_BASE`,
`API_KEY`, and `MODEL`, including trusted request headers. It does not
require a second judge endpoint.

## Test

```bash
uv run python -m unittest discover -s tests -v
```

The original integration validated all 2166 generated tasks with Harbor 0.18.0
and a Claude Code OpenSandbox smoke. New harness validation is recorded
separately; successful setup alone is not a completed benchmark. Search service
capacity and judge choice must be fixed before reporting benchmark scores.
