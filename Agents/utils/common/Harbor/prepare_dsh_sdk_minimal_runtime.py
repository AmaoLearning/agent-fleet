#!/usr/bin/env python3
"""Package the version-matched DSH Python SDK for offline task sandboxes."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from Agents.utils.common.Harbor.prepare_dsh_minimal_runtime import (
    prepare_python_runtime,
    tarball_ready,
)

DEFAULT_SOURCE_REF = "dsh-v0.1.2-alpha.2"
DEFAULT_SOURCE_SHA = "0a53fb55bea101816fa226bb964ae2bed71c343b"


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    source_ref: str
    source_sha: str
    source_dir: Path | None
    runtime_tarball: Path
    version_file: Path
    python_runtime_tarball: Path

    @property
    def source_version(self) -> str:
        return f"{self.source_ref}@{self.source_sha}"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        source_ref = _value(
            values, "DSH_SDK_MINIMAL_SOURCE_REF", DEFAULT_SOURCE_REF
        )
        source_sha = _value(
            values, "DSH_SDK_MINIMAL_SOURCE_SHA", DEFAULT_SOURCE_SHA
        )
        source_dir_value = values.get("DSH_SDK_MINIMAL_SOURCE_DIR", "").strip()
        runtime_basename = _value(
            values,
            "DSH_SDK_MINIMAL_RUNTIME_BASENAME",
            f"dsh-sdk-minimal-runtime-{source_ref}.tar.gz",
        )
        return cls(
            wheel_dir=wheel_dir,
            source_ref=source_ref,
            source_sha=source_sha,
            source_dir=Path(source_dir_value) if source_dir_value else None,
            runtime_tarball=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_RUNTIME_TARBALL",
                    str(wheel_dir / runtime_basename),
                )
            ),
            version_file=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_RUNTIME_VERSION_FILE",
                    str(wheel_dir / "dsh-sdk-minimal-runtime.version"),
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


def runtime_ready(config: Config) -> bool:
    if not tarball_ready(config.runtime_tarball) or not tarball_ready(
        config.python_runtime_tarball
    ):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.source_version


def prepare(config: Config) -> None:
    # Reuse the exact portable Python layout already consumed by dsh-minimal.
    prepare_python_runtime(config)  # type: ignore[arg-type]
    if runtime_ready(config):
        print(
            "[prepare] skip DSH sdk-minimal runtime (cached): "
            f"{config.runtime_tarball}"
        )
        return

    config.wheel_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-fleet-dsh-sdk-minimal-", dir="/tmp"
    ) as temporary_name:
        temporary = Path(temporary_name)
        runtime_root = temporary / "dsh-sdk-minimal-runtime"
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
                "pydantic>=2.12,<3",
            ],
            check=True,
        )
        if config.source_dir is None:
            source_requirement = (
                "deepseek-harness-sdk @ "
                "https://github.com/deepseek-ai/deepseek-harness/archive/"
                f"{config.source_sha}.tar.gz#subdirectory=python/sdk"
            )
        else:
            completed = subprocess.run(
                ["git", "-C", str(config.source_dir), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            )
            if completed.stdout.strip() != config.source_sha:
                raise RuntimeError(
                    "DSH SDK source checkout mismatch: "
                    f"expected {config.source_sha}, got {completed.stdout.strip()}"
                )
            sdk_dir = config.source_dir / "python" / "sdk"
            if not (sdk_dir / "pyproject.toml").is_file():
                raise RuntimeError(f"DSH SDK source is missing: {sdk_dir}")
            source_requirement = str(sdk_dir)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(site_packages),
                source_requirement,
            ],
            check=True,
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from deepseek_harness import DeepSeekHarnessConfig; "
                    "fields=DeepSeekHarnessConfig.__dataclass_fields__; "
                    "required={'profile','dsh_home','dsh_bin','reasoning_effort'}; "
                    "missing=required-fields.keys(); "
                    "assert not missing, sorted(missing); print('sdk-minimal-api-ok')"
                ),
            ],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(site_packages)},
        )
        if completed.stdout.strip() != "sdk-minimal-api-ok":
            raise RuntimeError("prepared SDK failed its profile API conformance check")
        (runtime_root / "SOURCE_VERSION").write_text(
            f"{config.source_version}\n", encoding="utf-8"
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
                raise RuntimeError("generated DSH sdk-minimal archive is invalid")
            os.replace(temporary_tar, config.runtime_tarball)
        finally:
            temporary_tar.unlink(missing_ok=True)

    temporary_version = config.version_file.with_suffix(".version.tmp")
    temporary_version.write_text(f"{config.source_version}\n", encoding="utf-8")
    os.replace(temporary_version, config.version_file)
    print(f"[prepare] built DSH sdk-minimal runtime: {config.runtime_tarball}")


def main() -> int:
    prepare(Config.from_environment())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
