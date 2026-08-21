# QZ Template Mapping Inventory

`qz_template_mapping.py` inventories image-backed Harbor task environments and
writes deterministic input for the per-task QZ Template resolver. An
environment plan can contain common Template build steps and per-task Sandbox
initialization. Inventory makes no QZ API calls and creates no Templates.

## Inventory a benchmark

Point the tool at a directory whose immediate children are Harbor tasks:

```bash
cd Agents/utils/common/Harbor

python qz_template_mapping.py \
  --dataset-root /workspace/terminal-bench-2-1/tasks \
  --benchmark terminalbench21 \
  --task-list ../../../../Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt \
  --spec g.c1 \
  --output /tmp/terminalbench21-qz-templates.json
```

Without `--task-list`, every immediate child containing `task.toml` is
inventoried. `--dataset-root` may also point directly at one task. The command
fails without writing a partial mapping if any selected task cannot be
represented by the selected dataset kind.

### Generic Environment Plan input

Datasets that do not declare a final `environment.docker_image` can provide a
dataset-independent JSON manifest. The keys match the selected relative task
keys; the mapping tool does not need dataset-specific code:

```json
{
  "schema_version": 1,
  "tasks": {
    "task-a": {
      "image": "example/shared-base:v1",
      "build_steps": [
        {"type": "USER", "args": ["root"]},
        {"type": "WORKDIR", "args": ["/testbed"]},
        {"type": "RUN", "args": ["apt-get install -y git"]}
      ],
      "init_steps": [
        {"run": "git reset --hard abc && git checkout abc", "cwd": "/testbed"}
      ]
    },
    "task-b": {
      "image": "example/clone-base:v1",
      "build_steps": [],
      "init_steps": [
        {"run": "git clone https://github.com/org/repo.git /testbed", "cwd": "/"}
      ]
    }
  }
}
```

```bash
python qz_template_mapping.py \
  --dataset-root /path/to/harbor/tasks \
  --benchmark benchmark-name \
  --task-list /path/to/selected-tasks.txt \
  --environment-plan-file /path/to/environment-plan.json \
  --output /tmp/benchmark-qz-templates.json
```

This is the generic integration surface for checkout, reset, clone, or other
image-backed task initialization. The resolver and QZ provider only consume the
resulting schema-v2 mapping and do not branch on dataset name.

### SWE-Smith convenience producer

For SWE-Smith, the built-in producer reads the generated adapter's base image,
sets the Template build user to `root`, keeps the common `WORKDIR` / `RUN`
steps, then moves the final task checkout into fresh-Sandbox initialization:

```bash
python qz_template_mapping.py \
  --dataset-root /workspace/harbor/datasets/swesmith \
  --dataset-kind smith \
  --benchmark smith \
  --task-list ../../../../Tasks/SWE-smith/harbor_tasks.txt \
  --spec g.c1 \
  --output /tmp/smith-qz-templates.json
```

This producer intentionally supports the current SWE-Smith adapter shape only;
it is not a general Dockerfile or build-context materializer.
The task-list entries remain raw instance IDs, while the emitted mapping keys
use Harbor's `swe-smith__<instance_id>` task names.

### SWE-bench Verified convenience producer

SWE-bench Verified tasks do not declare `environment.docker_image` in
`task.toml`. Their generated adapter Dockerfiles start from a per-task final
SWE image and add `WORKDIR` / `RUN` steps that use Docker's root default.
The producer records an explicit `USER root` before those steps so QZ preserves
that build and runtime user. Inventory them directly without an intermediate
manifest:

```bash
python qz_template_mapping.py \
  --dataset-root /workspace/swebench-verified \
  --dataset-kind sweverify \
  --benchmark sweverify \
  --task-list ../../../../Tasks/SWE-verify/harbor_tasks.txt \
  --spec g.c1 \
  --output /tmp/sweverify-qz-templates.json
```

These tasks already use task-specific final images, so all Dockerfile steps
remain Template build steps and the generated task initialization is empty.

## Schema v2

The output is deterministic: it contains no generation timestamp, absolute
dataset path, credentials, or live platform state.

```json
{
  "benchmark": "terminalbench21",
  "identity_version": "qz-template-environment-v2",
  "schema_version": 2,
  "tasks": {
    "adaptive-rejection-sampler": {
      "docker_image": "example/task-image:tag",
      "init_steps": [
        {
          "cwd": "/testbed",
          "run": "git fetch && git checkout instance-id"
        }
      ],
      "template_key": "sha256:..."
    }
  },
  "templates": {
    "sha256:...": {
      "image": "example/task-image:tag",
      "image_source": "official",
      "spec": "g.c1",
      "build_steps": [
        {"type": "USER", "args": ["root"]},
        {"type": "WORKDIR", "args": ["/testbed"]},
        {"type": "RUN", "args": ["apt-get install -y git"]}
      ],
      "template_id": null,
      "template_name": "af_task_image_tag_..."
    }
  }
}
```

`template_key` is SHA-256 over canonical JSON containing `identity_version`,
the exact image reference, `image_source`, QZ spec, and ordered `build_steps`.
Tasks with the same Template inputs share one Template entry. Changing any
member or build-step order creates a new key and alias. `init_steps` are not
part of Template identity, so tasks with different checkout commands can share
one Template while still receiving isolated Sandboxes. Aliases contain only
letters, digits, and underscores.

The supported build-step subset is `USER`, `WORKDIR`, and `RUN`. The exact
image string is intentionally preserved. Digest-qualified image
references are preferred because mutable tags can move while retaining the
same v2 key. Resolving a registry tag to a manifest digest is outside this
inventory phase.

Existing schema-v1 mappings remain readable and behave as empty `build_steps`
plus empty `init_steps`. New inventory output always uses schema v2.

## Resolve or materialize one task

`qz_template_resolver.py` consumes the mapping one task at a time. Benchmark
runs are read-only: they resolve the cached ID or deterministic alias through
the live QZ API and reject missing, non-ready, or identity-mismatched
Templates. The live Template must expose the mapping's content-derived alias
and QZ spec. After a fresh Sandbox is created, schema-v2 `init_steps` run in
order as the Template's default user before agent setup/run; any non-zero step
aborts the trial. Benchmark runs never create a Template implicitly.

Before a live `resolve`, `bind`, or `materialize` command, either export the QZ
API variables or load the repository-local configuration:

```bash
source ./env.sh
```

Resolve one task without changing the mapping or platform:

```bash
python qz_template_resolver.py resolve \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler
```

Bind an existing ready Template that has the mapping's deterministic alias and
QZ spec:

```bash
python qz_template_resolver.py bind \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler \
  --template-id existing_template_id
```

Explicitly create or reuse only one task through Template Manager v1:

```bash
python qz_template_resolver.py materialize \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler
```

## Materialize an explicit task batch

Use the batch tool only during Template preparation. It requires an explicit
task list and intentionally has no `--all` mode:

```bash
python qz_template_batch_materialize.py \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task-list /path/to/selected-tasks.txt \
  --workers 8
```

The tool resolves the selected tasks before making QZ API calls, groups them by
`template_key`, and runs at most `--workers` unique Template operations at once.
Mapping writes stay serialized in the main process, so successful IDs are saved
atomically without concurrent workers overwriting each other.

Failures are isolated per unique Template. The command writes a JSON result to
stdout and exits non-zero if any Template failed. Rerunning the same command
reuses IDs already saved in the mapping and ready deterministic aliases.

The write commands record `template_id` only after live status, deterministic
alias, and QZ spec validation succeed. QZ's read API does not expose the source
image, so the server-returned alias is the live content-identity commitment: it
is derived from the exact image reference, image source, spec, and common
build steps in the mapping. A legacy Template without that alias must be
materialized under the deterministic name instead of being bound by ID.

Enable per-task selection in the runner with:

```bash
QZ_SANDBOX_TEMPLATE_MAP=/absolute/path/to/terminalbench21-qz-templates.json
```

Set either `QZ_SANDBOX_TEMPLATE_MAP` or the backward-compatible fixed
`QZ_SANDBOX_TEMPLATE`, never both. The mapping is a cache, not the platform
fact source: every reuse is checked through the live API.

Regenerating an existing output preserves a resolved `template_id` only when
its full `template_key` is unchanged. The resolver never rebuilds or deletes a
same-name Template automatically.

## Acceptance sequence

1. Inventory the selected Terminal-Bench task list and report selected task
   count, unique image count, and any task that cannot be represented.
2. Use `adaptive-rejection-sampler` as the first real single-task acceptance;
   its mapped Template must reach ready before one Oracle trial runs.
3. Select a second task with a different `template_key`; run both sequentially
   and verify each receives its own Template ID, reward is recorded, exceptions
   are zero, artifacts exist, and temporary Sandboxes are deleted.
4. Only after those pass, run repeated trials of one task to verify Template
   reuse, then a mixed-image small batch before increasing concurrency.

This phase does not build or push Docker images. Add that workflow only for a
real selected task whose image is not already available to QZ Template build.
