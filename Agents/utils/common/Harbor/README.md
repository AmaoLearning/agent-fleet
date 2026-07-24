# Harbor Runner

This directory contains the shared Harbor runner for Claude Code and OpenCode.

The normal workflow is:

```bash
cd Agents/utils/common/Harbor
vim env.sh
bash start.sh --detach
```

Use `bash start.sh` instead of `--detach` for an interactive zellij session.

When every task has finished, the monitor writes a final summary to
`$OUTPUT_PATH/summary.txt` (counts, reward rollup, result paths). Fixed
benchmark sessions close by default; set `HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0`
to keep the final pane open for inspection. All per-task results stay on disk
under `$OUTPUT_PATH`.

This completion switch does not apply to RL rollout sessions, whose workers
serve a dynamic request queue rather than a fixed task total.

Optional console-only online analysis:

```bash
HARBOR_ONLINE_ANALYSIS=1 bash start.sh --detach
```

## Minimal Setup

Point the runner at your infrastructure. `config.env` is a committed template;
copy it to a git-ignored `config.local.env` (sourced after, and overriding,
`config.env`) and set your values — including credentials — there:

```bash
cp config.env config.local.env
vim config.local.env
```

Set your model gateway and tracing preference there. Opik endpoint values are
required only when tracing is enabled:

```bash
BASE_URL=https://your-openai-compatible-endpoint
API_KEY=your-api-key
MODEL=your-model-id
TRACE_TO_OPIK=false
# When TRACE_TO_OPIK=true:
OPIK_URL=http://your-opik-host/api
OPIK_PROJECT_NAME=your-project-name
```

Then edit the run parameters in `env.sh`:

```bash
AGENT="claude-code"        # claude-code or opencode
DATASET_NAME="seta"        # built-in Harbor registry alias
TOTAL_WORKERS="80"
TB_N_CONCURRENT="80"
```

When `TRACE_TO_OPIK=true` (the default), the Opik tracing plugin is loaded from
the `third_party/agent-opik-plugin` submodule. Initialize it before a traced run:

```bash
git submodule update --init --recursive
```

For a direct host run, first execute `./scripts/setup.sh` from the repository
root. It creates a pinned Harbor/Opik control environment under
`~/.local/share/agent-fleet/harbor-runner`. The DinD runner uses the
image-owned `/opt/harbor-runner` environment instead. Workload startup only
validates the selected environment and never installs or repairs it.

## Docker Compose Overlay

Harbor runs task containers through Docker. For DinD runners where the outer
container is privileged, this runner passes a default compose overlay that keeps
task containers unprivileged:

```yaml
# Agents/utils/common/Harbor/overlays/unprivileged-task.yaml
services:
  main:
    privileged: false
```

## Datasets

Use these values in `env.sh`:

| Dataset | `DATASET_NAME` | Typical `DATASET_PATH` | Suggested workers |
| --- | --- | --- | --- |
| SETA | `seta` | `/workspace/seta-env/Harbor-Dataset` | `80` |
| SWE-Smith | `smith` | `/workspace/harbor/datasets/swesmith` | `80` |
| Terminal-Bench 2.1 | `terminalbench21` | `/workspace/terminal-bench-2-1/tasks` | `20` |
| SWE-bench Verified | `sweverify` | `/workspace/swebench-verified` | `20` |

`seta`, `terminalbench21`, and `sweverify` download from the Harbor registry
by default. `smith` remains local. For an offline or local checkout of any
dataset, use `auto` with its path:

```bash
DATASET_NAME=auto \
DATASET_PATH=/workspace/seta-env/Harbor-Dataset \
bash Agents/utils/common/Harbor/start.sh --detach
```

For any Harbor registry dataset, pass the dataset id directly and use the normal
zellij entrypoint:

```bash
DATASET_NAME=openthoughts/tasktrove-swe-rebench-v2-patched-oracle \
bash Agents/utils/common/Harbor/start.sh --detach
```

Registry runs pass `--dataset "$DATASET_NAME"` to Harbor instead of preparing a
local task file from `DATASET_PATH`.

## RL Rollout Mode

Rollout mode exposes a Polar-compatible remote Harbor service instead of
starting a fixed dataset run.  It is gated by `ROLLOUT=1`; normal benchmark
runs are unchanged.

```bash
cd Agents/utils/common/Harbor
vim ../../rl/RL-env.sh
ROLLOUT=1 bash start.sh --detach
```

The service provides `GET /health`, `GET /datasets`,
`GET /datasets/{dataset_name}/tasks`, and `POST /run_trial`.  Requests are
queued, then per-submission zellij workers run the same `harboropik.sh` path as
normal benchmark workers, so task panes keep the regular agent/tool logs.

Each `/run_trial` request must include a top-level `ray_submission_id`. The
service uses it to create/reuse one
`harbor-rollout-<agent>-<dataset>-<ray_submission_id>` zellij session; requests
without it are rejected instead of being queued without workers.

For Docker usage, publish the listener port and run the same command inside the
container. Build the runner directly from this repository first:

```bash
docker build \
  -f scripts/dind/Dockerfile \
  -t agent-fleet-harbor-runner:local \
  .

docker run -d --name harbor-rollout \
  -p 19001:19001 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /workspace:/workspace \
  agent-fleet-harbor-runner:local sleep infinity

docker exec harbor-rollout bash -lc '
  cd /workspace/agent-fleet/Agents/utils/common/Harbor
  ROLLOUT=1 RL_HOST=0.0.0.0 RL_PORT=19001 bash start.sh --detach
'
```

For foreground debugging, run the same launcher without `--detach`. The
listener still creates per-submission zellij worker sessions for requests with
a top-level `ray_submission_id`.

```bash
cd Agents/utils/common/Harbor
ROLLOUT=1 bash start.sh
```

## Harbor Monitor

`start.sh` automatically starts one monitor for each Harbor benchmark run.
Set `HARBOR_MONITOR_ENABLED=0` to disable it. The monitor reads Fleet queue
artifacts for local datasets and Harbor job/trial results for registry datasets.

Equivalent queue monitor command:

```bash
RUN_DIR="$PWD/runs/example"
MONITOR_DIR="$RUN_DIR/monitor"

python3 Agents/utils/common/Harbor/scripts/monitor.py \
  --run-dir "$RUN_DIR" \
  --agent claude-code \
  --output "$MONITOR_DIR/monitor-latest.json" \
  --user-report-output "$MONITOR_DIR/user-notify-latest.json" \
  --analyzer-handover-output "$MONITOR_DIR/analyzer-handover-latest.json" \
  --runner-action-output "$MONITOR_DIR/runner-action-latest.json" \
  --follow --interval 30
```

Omit `--follow` for one sample. Control commands are optional executable files
inside `RUN_DIR`; arguments are allowed but shell syntax is not. If absent or
failed, the action becomes `notify`.

For automatic runs, optional run-local controls can be set with
`HARBOR_MONITOR_RESTART_CMD` and `HARBOR_MONITOR_STOP_CMD`.

| Output | Used by | Content |
| --- | --- | --- |
| `monitor-latest.json` | Debugging | Full state and evidence |
| `user-notify-latest.json` | User | Objective status and required human action |
| `analyzer-handover-latest.json` | Analyzer | Tasks requiring deeper analysis |
| `runner-action-latest.json` | Runner | `wait`, `restart`, `stop`, or `notify`, plus execution result |

All files are refreshed on each sample. The actual action is
`runner-action-latest.json.type`; the user report filename does not imply
`notify` was triggered.

| Observed state | Action |
| --- | --- |
| Worker active, including `suspected_stalled` | `wait` |
| Worker active past `--configured-timeout` | `notify` and continue monitoring |
| Tasks unfinished with no live worker | `restart`; after `--max-retries`, `notify` |
| Every task has a terminal queue record | `stop` |

Automatic restart is only used when tasks remain and no worker is alive.

## Harbor Fixer MVP

`scripts/fixer.py` implements Fixer stage 1: Fix Plan Generation, stage 2:
Fix Exec, stage 3: Fix Verification, and stage 4: Fix Report. Stage 1 reads an
Analyzer output directory containing:

```text
analyzer-report-latest.json
env-infra-tasks-latest.json
fix-line-index-latest.jsonl
```

The Fixer builds one task input per `env_fail` / `infra_fail` task, dispatches
task-level summaries through isolated no-session Pi coding-agent subprocesses,
validates each JSON summary with stdlib checks, and asks a benchmark-level Pi
agent for a validated `fix-plan-latest.json`. Before invoking Pi, the Python
harness deterministically collects the target environment and bounded project
context. There is no model-driven inspection agent.

Prepare inputs and prompt files without invoking agents:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --prepare-only \
  --write-prompts
```

Full stage-1 run:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --pi-bin pi \
  --pi-provider harbor-fixer \
  --pi-model "$HARBOR_FIXER_MODEL" \
  --pi-base-url "$BASE_URL" \
  --pi-api-key-env HARBOR_FIXER_API_KEY \
  --workspace-root /path/to/workspace \
  --max-concurrency 4
```

Pi is invoked as `pi --mode json --print --no-session` with tools disabled for
task, planning, and report agents. Task subagents additionally use
`--thinking off` because they only compress one task into the validated summary
schema; main planning retains the model's default thinking level.
Before planning, Fixer writes a
non-secret, read-only host and dependency snapshot to `target-environment.json`.
The Python harness also deterministically records the workspace structure,
common project manifests, Analyzer artifact state, and bounded Analyzer evidence
excerpts in `target-context.json`. It blocks sensitive paths, skips binary or
oversized files, and redacts secret-like output. Both artifacts are included in
`main-agent-input.json`; no model chooses additional paths or receives file
tools. Missing or inaccessible evidence paths are recorded as `unavailable`
with a reason and do not abort planning; an unreadable `--workspace-root`
remains a fatal input error.
`--workspace-root` defaults to the current directory and must be readable.
Fixer records compact Pi events, stderr, rendered prompts, and provenance under
`pi-agent-events/`, `pi-agent-stderr/`, `pi-agent-prompts/`, and
`pi-agent-provenance/`.
`--pi-model` defaults to `HARBOR_FIXER_MODEL`, then `MODEL`;
`--pi-base-url` defaults to `HARBOR_FIXER_BASE_URL`, then `BASE_URL`.
No model id is assumed when those values are unset; agent stages fail fast with
`pi_model_not_configured` unless a model is provided.
If `HARBOR_FIXER_API_KEY` is not set but `API_KEY` is present, the CLI copies
that value into the configured Fixer API-key environment variable for the child
process.

Stage-2 command execution:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --exec-only \
  --fix-plan /path/to/fixer-output/fix-plan-latest.json \
  --workspace-root /path/to/workspace \
  --output-dir /path/to/fixer-output
```

Exec reads and validates the fix plan, writes `exec-input.json`, then executes
each plan in list order and each command in plan order with `bash -lc`. Relative
command `cwd` values are resolved under `--workspace-root`, which defaults to
the current directory. If a command fails, Exec records the failure, skips the
remaining commands in that plan, and continues with the next plan. It does not
retry, repair, or call agents.

Exec writes `exec-result-latest.json` plus full per-command stdout/stderr logs
under `command-logs/`. The CLI exits non-zero when any executed plan fails, but
the result artifact is still written.

Stage-3 verification:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --verify-only \
  --fix-plan /path/to/fixer-output/fix-plan-latest.json \
  --exec-result /path/to/fixer-output/exec-result-latest.json \
  --analyzer-output /path/to/analyzer-output \
  --verification-run-dir /path/to/new-harbor-run \
  --output-dir /path/to/fixer-output \
  --verification-task-limit-per-plan 2 \
  --monitor-policy auto \
  --monitor-wait-timeout 3600 \
  --monitor-poll-interval 30
```

Add `--rerun-command /path/to/run-verification-wrapper` when the verifier should
launch the smoke verification run before reading it. By default Stage 3 samples
at most two tasks from each successfully executed fix plan using a deterministic
stable-hash policy, writes `verification-smoke-tasks.txt` and
`verification-smoke-selection.json`, and treats unsampled plan tasks as
`not_sampled`. A passing smoke sample marks the plan fixed; it is not a full
benchmark rerun.

The rerun wrapper receives these environment variables:

| Variable | Value |
| --- | --- |
| `TASK_SOURCE_FILE` | Fixer-generated smoke task list |
| `TASK_FILE` | `<verification-run-dir>/tasks.txt` |
| `OUTPUT_PATH` | `--verification-run-dir` |
| `RESET_RUN` | `1` |
| `HARBOR_FIXER_SMOKE_SELECTION` | Fixer-generated selection manifest |

The rerun wrapper must treat `TASK_SOURCE_FILE` as an ordered contract and copy
or consume it unchanged as `TASK_FILE`. It must not rebuild, sort, or replace
the list from Analyzer output, `LOCAL_TASKS`, or a dataset scan: each line
number is the `smoke_task_index` recorded in `HARBOR_FIXER_SMOKE_SELECTION`.

These paths are passed as absolute paths. Fixer also clears inherited
run-scoped Harbor path variables so the smoke run cannot reuse the Analyzer
baseline run's queue, runtime, jobs, or monitor directories.

Verification is code-only: it does not call agents, repair failures, infer new
root causes, or produce a human summary. It records fixed-rule task statuses in
`verification-result-latest.json`, using Harbor Monitor's
`complete_success` / `complete_failed` / `complete_unknown` / `not_complete`
classification. With `--monitor-policy auto` or `on`, it writes a one-sample
monitor snapshot under `verification-monitor/` when no snapshot already exists.
When `--rerun-command` is provided with monitor policy `auto` or `on`, the
verifier polls Harbor Monitor until the new run reaches a terminal monitor
state or `--monitor-wait-timeout` expires.

Stage-4 report:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --report-only \
  --verification-result /path/to/fixer-output/verification-result-latest.json \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --pi-bin pi \
  --pi-provider harbor-fixer \
  --pi-model "$HARBOR_FIXER_MODEL" \
  --pi-base-url "$BASE_URL" \
  --baseline-run-dir /path/to/old-harbor-run \
  --baseline-monitor-policy auto
```

Report keeps structured facts code-generated from Analyzer, Fix Plan, Exec,
Verification, and optional baseline Monitor artifacts. It writes two primary
outputs:

- `fix-report-latest.json` is the machine-readable contract.
- `fix-report-latest.md` is the human-readable report. It includes the concrete
  Analyzer problems and root causes, the planned approach and suggested
  commands, trial execution results, before/after verification, sampled task
  results in a table, and explicit stage failures or interruptions.

The report agent is only used for the bounded `summary` object shared by both
outputs. It does not change task statuses, plan results, old-run facts, new-run
facts, commands, or failure evidence; the Markdown body is rendered
deterministically from those artifacts and redacts secret-like command values.
If both summary-agent attempts fail or return invalid JSON, the reporter still
writes both reports with a deterministic summary, preserves the attempt errors
in `generation_errors`, and adds a fallback caveat.

For many benchmarks, Stage 1 and Stage 4 can be run from a batch manifest:

```json
{
  "schema_version": 1,
  "kind": "harbor_fixer_batch_manifest",
  "benchmarks": [
    {
      "benchmark_id": "example",
      "analyzer_output": "/path/to/analyzer-output",
      "output_dir": "/path/to/fixer-output",
      "verification_result": "/path/to/verification-result-latest.json",
      "baseline_run_dir": "/path/to/old-harbor-run"
    }
  ]
}
```

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --batch-plan-only \
  --batch-manifest /path/to/fixer-batch-manifest.json \
  --output-dir /path/to/batch-output \
  --pi-bin pi \
  --pi-provider harbor-fixer \
  --pi-model "$HARBOR_FIXER_MODEL" \
  --pi-base-url "$BASE_URL" \
  --benchmark-concurrency 4
```

Use `--batch-report-only` with the same manifest shape plus
`verification_result` paths for report generation. Batch mode continues other
benchmarks when one item fails, preserves manifest order in the final result,
and writes `batch-result-latest.json` with `success`, `partial_failed`, or
`failed`. Each successful report item records both `report_path` and
`human_report_path`. Use separate per-benchmark `output_dir` values.
`--max-concurrency` limits task agents within one benchmark;
`--benchmark-concurrency` limits benchmarks running in parallel.

### Pi Runtime Boundary

`scripts/harbor_fixer/pi_subprocess.py` is the Fixer-owned low-level subprocess
boundary used by `harbor_fixer/agent_invocation.py`. It does not build Fixer prompts, collect target
context, generate plans, execute commands, verify tasks, or classify reports.
It:

- normalizes the OpenAI-compatible base URL and writes an isolated Pi
  `models.json`;
- builds a minimal child environment, optionally adding the gateway host to
  `NO_PROXY`;
- starts one independent `pi --mode json --print --no-session` process with the
  requested tool, extension, skill, context-file, and thinking restrictions;
- captures and compacts JSONL events while retaining lifecycle, final-message,
  tool, and retry evidence;
- extracts the final assistant JSON, classifies provider/truncation/process
  failures, enforces timeout cleanup, and returns provenance to the caller.

Each call gets an isolated Pi home and work directory, so concurrent task or
benchmark agents do not share sessions or mutable Pi state. The module remains
inside `harbor_fixer` because no other Harbor component uses this runtime.
It exposes transport errors such as
`pi_provider_request_failed:connection_error`, but the provider may not retain
the underlying DNS, TLS, or connection-reset detail.

## More Details

Architecture, script roles, task resolution, and full variable descriptions are in [STRUCT.md](./STRUCT.md).
