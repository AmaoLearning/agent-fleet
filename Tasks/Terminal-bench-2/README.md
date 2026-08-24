# Terminal-bench-2

Terminal-Bench task lists.

```text
harbor_terminalbench21_tasks.txt
harbor_terminalbench21_issue162_sample10.txt
tb_tasks.txt
```

`harbor_terminalbench21_tasks.txt` is used by `Agents/utils/common/Harbor`.
Use it through `Agents/utils/common/Harbor/start.sh` by setting
`DATASET_NAME=terminalbench21` and `DATASET_PATH` in
`Agents/utils/common/Harbor/env.sh`.

`harbor_terminalbench21_issue162_sample10.txt` freezes the first Issue #162
diagnostic control set. The DSH branch had no prior 10-task task file or run to
reuse when it was created; the list is `random.Random(162).sample(all_tasks,
10)` in emitted order. Reuse this exact file for later DSH/Ante comparisons:

```bash
TASK_SOURCE_FILE="$PWD/Tasks/Terminal-bench-2/harbor_terminalbench21_issue162_sample10.txt"
```

Optional online analysis:

```bash
DATASET_NAME=terminalbench21
HARBOR_ONLINE_ANALYSIS=1
```

With `DATASET_NAME=terminalbench21`, online analysis tails Harbor
top-level `*.console.log` files and reports deterministic task-status
signals.

Outputs:

```text
${OUTPUT_PATH}/online-analysis/environment-events.jsonl
${OUTPUT_PATH}/online-analysis/environment-summary.json
${RUNTIME_DIR}/online-rule-analyzer.log
${RUNTIME_DIR}/online-rule-analyzer.pid
```
