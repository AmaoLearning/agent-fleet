#!/usr/bin/env python3
"""Prepare a portable, pinned DeepSeek Harness Python SDK runtime."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    version: str
    runtime_tarball: Path
    version_file: Path
    python_runtime_tarball: Path

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        version = _value(values, "DSH_MINIMAL_SDK_VERSION", "0.1.0-rc.6")
        runtime_basename = _value(
            values,
            "DSH_MINIMAL_RUNTIME_BASENAME",
            f"dsh-minimal-runtime-{version}.tar.gz",
        )
        return cls(
            wheel_dir=wheel_dir,
            version=version,
            runtime_tarball=Path(
                _value(
                    values,
                    "DSH_MINIMAL_RUNTIME_TARBALL",
                    str(wheel_dir / runtime_basename),
                )
            ),
            version_file=Path(
                _value(
                    values,
                    "DSH_MINIMAL_RUNTIME_VERSION_FILE",
                    str(wheel_dir / "dsh-minimal-runtime.version"),
                )
            ),
            python_runtime_tarball=Path(
                _value(
                    values,
                    "DSH_MINIMAL_PYTHON_RUNTIME_TARBALL",
                    str(wheel_dir / "dsh-minimal-python3.12-runtime.tar.gz"),
                )
            ),
        )


def tarball_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with tarfile.open(path) as archive:
            archive.getmembers()
    except (OSError, tarfile.TarError):
        return False
    return True


def runtime_ready(config: Config) -> bool:
    if not tarball_ready(config.runtime_tarball) or not tarball_ready(
        config.python_runtime_tarball
    ):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.version


def prepare_python_runtime(config: Config) -> None:
    if tarball_ready(config.python_runtime_tarball):
        print(
            "[prepare] skip DSH minimal Python runtime (cached): "
            f"{config.python_runtime_tarball}"
        )
        return

    python_real = Path(sys.executable).resolve()
    python_root = python_real.parents[1]
    if sys.version_info[:2] != (3, 12) or not (python_root / "bin").is_dir():
        raise RuntimeError(
            "DSH minimal runtime preparation must run with a managed Python 3.12"
        )

    config.python_runtime_tarball.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config.python_runtime_tarball.name}.",
        suffix=".tmp",
        dir=config.python_runtime_tarball.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(
                python_root,
                arcname="dsh-minimal-python3.12-runtime",
            )
        if not tarball_ready(temporary):
            raise RuntimeError("generated DSH minimal Python archive is invalid")
        os.replace(temporary, config.python_runtime_tarball)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        "[prepare] built DSH minimal Python runtime: "
        f"{config.python_runtime_tarball}"
    )


def prepare(config: Config) -> None:
    prepare_python_runtime(config)
    if runtime_ready(config):
        print(f"[prepare] skip DSH minimal runtime (cached): {config.runtime_tarball}")
        return
    config.wheel_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-fleet-dsh-minimal-", dir="/tmp"
    ) as temporary_name:
        temporary = Path(temporary_name)
        runtime_root = temporary / "dsh-minimal-runtime"
        site_packages = runtime_root / "site-packages"
        site_packages.mkdir(parents=True)

        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--target",
                str(site_packages),
                f"deepseek-harness-sdk=={config.version}",
            ],
            check=True,
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from importlib.metadata import version; "
                    "import deepseek_harness; "
                    "print(version('deepseek-harness-sdk'))"
                ),
            ],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(site_packages)},
        )
        normalized_expected = re.sub(r"[-.]", "", config.version)
        normalized_actual = re.sub(r"[-.]", "", completed.stdout.strip())
        if normalized_actual != normalized_expected:
            raise RuntimeError(
                "prepared DSH minimal SDK version mismatch: "
                f"expected {config.version!r}, got {completed.stdout.strip()!r}"
            )

        file_descriptor, temporary_tar_name = tempfile.mkstemp(
            prefix=f".{config.runtime_tarball.name}.",
            suffix=".tmp",
            dir=config.runtime_tarball.parent,
        )
        os.close(file_descriptor)
        temporary_tar = Path(temporary_tar_name)
        try:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(runtime_root, arcname=runtime_root.name)
            if not tarball_ready(temporary_tar):
                raise RuntimeError("generated DSH minimal runtime archive is invalid")
            os.replace(temporary_tar, config.runtime_tarball)
        finally:
            temporary_tar.unlink(missing_ok=True)

    temporary_version = config.version_file.with_suffix(".version.tmp")
    temporary_version.write_text(f"{config.version}\n", encoding="utf-8")
    os.replace(temporary_version, config.version_file)
    print(f"[prepare] built DSH minimal runtime: {config.runtime_tarball}")


def main() -> int:
    prepare(Config.from_environment())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
