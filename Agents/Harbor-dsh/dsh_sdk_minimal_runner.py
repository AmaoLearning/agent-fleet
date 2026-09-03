#!/usr/bin/env python3
"""Run one task through DSH's version-matched ``sdk-minimal`` profile."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from deepseek_harness import DeepSeekHarness


def main() -> None:
    """Launch the official profile and print its final assistant response."""
    parser = argparse.ArgumentParser()
    configured_home = os.environ.get("DSH_HOME", "")
    parser.add_argument("prompt", help="Task for the minimal agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=Path(configured_home) if configured_home.strip() else None,
    )
    parser.add_argument("--dsh-bin", type=Path, required=True)
    parser.add_argument("--profile", default="sdk-minimal")
    parser.add_argument("--session-id")
    parser.add_argument("--provider", default="deepseek-official")
    parser.add_argument(
        "--model",
        default=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
    )
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument("--max-tokens", type=int)
    args = parser.parse_args()
    if args.dsh_home is None:
        parser.error("--dsh-home or a non-empty DSH_HOME is required")

    with DeepSeekHarness(
        provider=args.provider,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        max_tokens=args.max_tokens,
        cwd=str(args.workspace.resolve()),
        dsh_home=str(args.dsh_home.resolve()),
        dsh_bin=str(args.dsh_bin.resolve()),
        profile=args.profile,
    ) as harness:
        result = harness.run(args.prompt, session_id=args.session_id)
    print(result.final_response)


if __name__ == "__main__":
    main()
