# BrowseComp

The official 1,266-question BrowseComp dataset, run as native Harbor tasks.
Dataset source and evaluation protocol: [openai/simple-evals](https://github.com/openai/simple-evals).

## Run

With the normal host setup and private model/sandbox configuration ready:

```bash
HARBOR_CC_WEB_MCP_ENABLED=1 OPIK_URL= \
  ./scripts/run_fleet.sh --taskset browsecomp --agent opencode --workers 50
```

Startup automatically prepares the dataset if its directory is missing.
The default is `$AGENT_FLEET_CACHE_DIR/web-search/tasks/browsecomp`; override
it with `DATASET_PATH`. Existing directories are reused without validation.
See the [shared guide](../../Agents/utils/web_search/README.md) for offline
source caching, image selection, and rollout configuration.

## Manual Preparation (Optional)

From the repository root, convert the official, unmodified CSV explicitly:

```bash
uv run --project Agents/utils/web_search python Tasks/BrowseComp/adapter.py \
  --input /data/browse_comp_test_set.csv --output-dir /data/harbor/browsecomp
```

For OpenSandbox, add `--image <available-registry-image>` with Python 3 available.
`--limit`, `--task-ids`, and `--overwrite` are optional. Full-source count and
SHA-256 validation happen before filtering. Keep generated tasks outside Git.
No conversion runs during agent execution; already prepared tasks can be reused.

## Run A Prepared Dataset

Use the existing Harbor runner and your private model/sandbox configuration:

```bash
AGENT=opencode DATASET_NAME=browsecomp DATASET_PATH=/data/harbor/browsecomp \
  HARBOR_CC_WEB_MCP_ENABLED=1 EXA_API_KEY=anonymous OPIK_URL= \
  TOTAL_WORKERS=50 HARBOR_N_CONCURRENT=50 bash Agents/utils/common/Harbor/start.sh
```

Use `AGENT=claude-code` or `opencode`. Pi requires a compatible MCP extension;
its pending integration is described in the [shared guide](../../Agents/utils/web_search/README.md).
For HTTP rollout, register `browsecomp=/data/harbor/browsecomp` in
`RL_DATASET_ROOTS`, then submit `dataset_name=browsecomp` and a generated task ID
such as `browsecomp-000000` through the existing rollout endpoint.

The verifier uses the trial's OpenAI-compatible model gateway and gives binary
correctness reward. Answer format, judge prompts, and task IDs are unchanged.
Anonymous Exa is rate-limited; concurrency is not a guarantee of service capacity.
