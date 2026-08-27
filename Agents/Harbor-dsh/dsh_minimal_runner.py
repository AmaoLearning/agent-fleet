#!/usr/bin/env python3
"""Run one official DeepSeek Harness minimal SDK turn."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness  # type: ignore[import-not-found]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--cordis", type=Path, required=True)
    args = parser.parse_args()

    with DeepSeekHarness(
        provider="deepseek-official",
        model=os.environ["DSH_MODEL"],
        cwd=str(Path.cwd()),
        session_root=os.environ["DSH_SESSION_ROOT"],
        cordis=str(args.cordis),
    ) as harness:
        result = harness.run(args.prompt)

    print(result.final_response)
    if result.finish_reason != "completed":
        print(
            f"dsh-minimal finished with {result.finish_reason or 'no reason'}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
