# Harbor DeepSeek Harness SDK minimal

`dsh_sdk_minimal_harbor.py` runs DeepSeek Harness's official `sdk-minimal`
profile through the version-matched Python JSON-RPC SDK. The default pins the
DSH CLI release and SDK source to `dsh-v0.1.3-alpha.1` at commit
`d347e703908d0406b7a7ef80e3a0e594d86b2215`.

The checksum-pinned CLI, Python 3.12 runtime, and SDK are prepared once on the
runner and mounted into Docker or OpenSandbox tasks. Agent setup performs no
package-manager downloads. Runtime traces are written under `/logs/agent`.

The adapter supports only `permission_mode=danger-full-access`, the native
DeepSeek provider route, and no Skills or MCP servers. A DSH Cordis plugin sets
`temperature` through `agent/request`, contributes `top_p` through the official
DeepSeek request-extension registry. DSH handles fragmented tool-call identity
itself. The provider retry policy additionally treats an incomplete SSE stream
as retryable.

## Configuration

Keep credentials in ignored `config.local.env` or the process environment.

```bash
export AGENT=dsh-sdk-minimal
export DSH_PROVIDER=deepseek
export BASE_URL=https://gateway.example.test
export API_KEY=replace-me
export MODEL=your-wire-model-id

export DSH_CONTEXT_WINDOW=200000
export DSH_TEMPERATURE=1.0
export DSH_TOP_P=0.95
export DSH_SDK_MINIMAL_MAX_TOKENS=65536
```

The release pin is intentionally not configurable: the CLI, SDK dependencies,
and archive checksum must advance together.

Run through the shared Harbor entry point:

```bash
export DATASET_NAME=terminalbench21
export HARBOR_ENVIRONMENT_TYPE=docker
export HARBOR_RUNS=1
export HARBOR_N_CONCURRENT=1
bash Agents/utils/common/Harbor/start.sh
```
