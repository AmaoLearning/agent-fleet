"""One-time startup preparation; generated tasks use the normal Harbor path."""

import argparse
import fcntl
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

from .adapter import WebSearchAdapter


def prepare(benchmark: str, destination: Path, source_dir: Path, image: str) -> None:
    if destination.is_dir():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Two launchers may prepare the same dataset; never publish a partial tree.
    with destination.with_name(destination.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.is_dir():
            return
        if destination.exists():
            raise FileExistsError(destination)
        url = (
            "https://openaipublic.blob.core.windows.net/simple-evals/browse_comp_test_set.csv"
            if benchmark == "browsecomp" else
            f"{os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')}"
            "/datasets/google/deepsearchqa/resolve/main/DSQA-full.csv"
        )
        source = source_dir / url.rsplit("/", 1)[-1]
        print(f"[INFO] preparing {benchmark} at {destination}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as tmp:
            staging = Path(tmp)
            cached = source.is_file()
            input_path = source if cached else staging / source.name
            if not cached:
                print(f"[INFO] downloading {url}", flush=True)
                with urllib.request.urlopen(url, timeout=60) as response, input_path.open("wb") as output:
                    shutil.copyfileobj(response, output)
            tasks = staging / "tasks"
            generated = WebSearchAdapter(benchmark, input_path, tasks, image=image).run()
            # Cache only successfully validated source bytes, using atomic replace.
            if not cached:
                source_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=source_dir) as cache_tmp:
                    cached_source = Path(cache_tmp) / source.name
                    shutil.copyfile(input_path, cached_source)
                    cached_source.replace(source)
            tasks.rename(destination)
            print(f"[INFO] generated {len(generated)} {benchmark} tasks", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", choices=("browsecomp", "deepsearchqa"))
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--image", default="python:3.12-slim")
    args = parser.parse_args()
    prepare(args.benchmark, args.destination, args.source_dir, args.image)
