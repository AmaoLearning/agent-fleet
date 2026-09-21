"""Read native Harbor progress; never schedules, retries, or parses agent logs."""

import json
import os
import time
from pathlib import Path


def render(job_dir: Path) -> str:
    if not (job_dir / "result.json").exists():
        job_dir = next(path.parent for path in sorted(job_dir.glob("*/result.json")))
    result = json.loads((job_dir / "result.json").read_text())
    stats = result.get("stats", {})
    lines = [f"Job: {job_dir}", f"Total: {result.get('n_total_trials', 0)}"]
    for key in ("running", "pending", "completed", "errored", "cancelled"):
        lines.append(f"{key}: {stats.get(f'n_{key}_trials', 0)}")
    lines.append(f"Updated: {result.get('updated_at', '')}")
    return "\n".join(lines)


def main() -> None:
    output = Path(os.environ["OUTPUT_PATH"])
    job_file = Path(os.environ["HARBOR_JOB_DIR_FILE"])
    exit_file = Path(os.environ["HARBOR_BENCHMARK_EXIT_FILE"])
    while True:
        print("\033[H\033[2J", end="")
        print(f"Run: {os.environ['RUN_ID']}\nOutput: {output}")
        print(f"Native concurrency: {os.environ['HARBOR_N_CONCURRENT']}")
        try:
            print(render(Path(job_file.read_text().strip())))
        except (OSError, ValueError, StopIteration):
            # Harbor can be preparing the job or rewriting its progress JSON.
            print("Waiting for Harbor progress...")
        if exit_file.exists():
            print(f"Harbor exit: {exit_file.read_text().strip()}")
            if (output / "summary.txt").exists():
                print((output / "summary.txt").read_text())
            if os.environ.get("HARBOR_ZELLIJ_CLOSE_ON_COMPLETE", "1") == "1":
                return
            while True:
                time.sleep(3600)
        print(flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
