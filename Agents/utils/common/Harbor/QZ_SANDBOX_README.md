# qz Sandbox Quick Start

This guide runs Harbor tasks in qz (SII Inspire, qz.sii.edu.cn) sandboxes:
each task executes in an isolated, disposable sandbox instance managed by the
platform.

## Prerequisites

- A qz platform account that belongs to a project.
- A machine on the SII internal network (it must reach
  `qz-sbx-api.sii.edu.cn`), for example a platform CPU Notebook.
- `./scripts/setup.sh` has been run from the repository root, and the model
  gateway is configured (`BASE_URL` / `API_KEY` / `MODEL` in
  `config.local.env`).

## Platform-side setup (web console)

Entry point: 作业中心 (Job Center) → Sandbox.

1. **Create a Sandbox Key** on the「Sandbox Key」tab and copy the key
   (starts with `sbx_`).
2. **Create a Template** (the sandbox boot image) on the「Template 列表」tab:
   - Name: letters, digits, and underscores only;
   - Compute spec: fixed on the Template (e.g. g.c2 = 2 vCPU / 8 GB); create
     another Template for a different spec;
   - Sandbox Key: must be the key from step 1 — Templates are bound to a key;
   - Image: an official image (e.g. `sandbox-base`, Ubuntu 24.04 +
     Python 3.12), or a custom image pushed to the platform image registry
     (镜像管理) first.
3. Wait until the Template status is ready.

## Repository-side configuration

Add to `config.local.env` (never commit the key):

```bash
RL_ENVIRONMENT_TYPE=qz
SBX_API_KEY=sbx_xxx                # from step 1
QZ_SANDBOX_TEMPLATE=your_template  # Template name (or ID) from step 2
# QZ_SANDBOX_TIMEOUT_SEC=14400     # max sandbox lifetime; 4h is the platform cap
```

If a Harbor runner environment was installed before this provider existed,
rebuild it once:

```bash
rm -rf ~/.local/share/agent-fleet/harbor-runner
bash Agents/utils/common/Harbor/setup_runner_env.sh
```

## Run one task

```bash
cd Agents/utils/common/Harbor

AGENT=oracle \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
TB_N_CONCURRENT=1 \
bash start.sh
```

Scale up the worker count after a single task passes. The launcher currently
accepts `AGENT=oracle` only on qz: the claude-code/opencode delivery
mechanisms (runner-local wheel server, hook bind mounts) cannot reach a qz
sandbox, and agent runtime delivery lands together with the per-task template
pipeline.

## Limitations

- Tasks must be **single-container with a prebuilt image**
  (`environment.docker_image` in `task.toml`); docker-compose tasks and
  on-the-fly Dockerfile builds are not supported — the environment image must
  be registered as a Template beforehand.
- Fixed-template mode is intentional in this first provider version: all tasks
  share the Template named by `QZ_SANDBOX_TEMPLATE`. A run may contain one task
  or multiple tasks that use the same environment image; the operator is
  responsible for selecting a compatible Template. Datasets with a different
  environment per task require the follow-up content-addressed Template
  registration and mapping pipeline.
- Task network policies (no-network / allowlist) are not verified on qz yet;
  do not rely on network isolation for now.

## Troubleshooting

| Error | Cause and fix |
| --- | --- |
| `template 'xxx' not found` | Name misspelled, or the Template is bound to a different key |
| `Timeout cannot be greater than 4 hours` | Lower `QZ_SANDBOX_TIMEOUT_SEC` to 14400 or below |
| `No available resources` | Platform pool is full; retry later, contact the platform if it persists |
| Connection timeout / DNS failure | The machine is not on the SII internal network |
| 401 | The key is invalid or deleted; check the「Sandbox Key」page |

Protocol details and the adapter implementation live in the module docstring
of `qz_e2b_sandbox.py`.
