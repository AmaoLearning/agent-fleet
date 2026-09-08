#!/usr/bin/env python3
"""Package the version-matched SDK runtime for the DSH adapter."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dsh_runtime_utils import atomic_output, atomic_write_text, sha256, tarball_ready

DEFAULT_SOURCE_REF = "dsh-v0.1.3-alpha.1"
DEFAULT_SOURCE_SHA = "d347e703908d0406b7a7ef80e3a0e594d86b2215"
DEFAULT_SOURCE_ARCHIVE_SHA256 = (
    "0f5eda71ba80543f70c679a67e54f48bd1be9c704df6bc1c767ee598d04d666f"
)
# Exact transitive set from python/sdk/uv.lock at DEFAULT_SOURCE_SHA.
RUNTIME_REQUIREMENTS = (
    "annotated-types==0.7.0",
    "pydantic==2.13.4",
    "pydantic-core==2.46.4",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.2",
)


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    source_ref: str
    source_sha: str
    source_archive: Path
    source_archive_sha256: str
    runtime_tarball: Path
    version_file: Path
    python_runtime_tarball: Path

    @property
    def source_version(self) -> str:
        return f"{self.source_ref}@{self.source_sha}"

    @property
    def runtime_version(self) -> str:
        dependencies = ",".join(RUNTIME_REQUIREMENTS)
        return (
            f"{self.source_version};dependencies={dependencies};"
            f"python={python_runtime_identity()}"
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        source_ref = _value(values, "DSH_SDK_MINIMAL_SOURCE_REF", DEFAULT_SOURCE_REF)
        source_sha = _value(values, "DSH_SDK_MINIMAL_SOURCE_SHA", DEFAULT_SOURCE_SHA)
        runtime_basename = _value(
            values,
            "DSH_SDK_MINIMAL_RUNTIME_BASENAME",
            f"dsh-sdk-minimal-runtime-{source_ref}.tar.gz",
        )
        return cls(
            wheel_dir=wheel_dir,
            source_ref=source_ref,
            source_sha=source_sha,
            source_archive=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_SOURCE_ARCHIVE",
                    str(wheel_dir / f"deepseek-harness-{source_sha}.tar.gz"),
                )
            ),
            source_archive_sha256=_value(
                values,
                "DSH_SDK_MINIMAL_SOURCE_ARCHIVE_SHA256",
                DEFAULT_SOURCE_ARCHIVE_SHA256,
            ),
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
                    "DSH_SDK_MINIMAL_PYTHON_RUNTIME_TARBALL",
                    str(wheel_dir / "dsh-sdk-minimal-python3.12-runtime.tar.gz"),
                )
            ),
        )


def managed_python_root() -> Path:
    """Return the uv-managed Python root used for the portable runtime."""
    python_real = Path(sys.executable).resolve()
    python_root = python_real.parents[1]
    if (
        sys.version_info[:2] != (3, 12)
        or not (python_root / "bin").is_dir()
        or not (python_root / "BUILD").is_file()
    ):
        raise RuntimeError(
            "runtime preparation requires a managed Python 3.12 "
            "python-build-standalone installation"
        )
    return python_root


def python_runtime_identity() -> str:
    """Identify the exact Python build carried into task containers."""
    python_root = Path(sys.executable).resolve().parents[1]
    try:
        build = (python_root / "BUILD").read_text(encoding="utf-8").strip()
    except OSError:
        build = f"{platform.python_implementation()}-{platform.python_version()}"
    return f"{build}-{platform.machine().lower()}"


def python_runtime_ready(path: Path, python_root: Path) -> bool:
    if not tarball_ready(path):
        return False
    expected = (python_root / "BUILD").read_bytes()
    try:
        with tarfile.open(path) as archive:
            member = archive.extractfile("dsh-sdk-minimal-python3.12-runtime/BUILD")
            return member is not None and member.read() == expected
    except (KeyError, OSError, tarfile.TarError):
        return False


def prepare_python_runtime(config: Config) -> None:
    python_root = managed_python_root()
    if python_runtime_ready(config.python_runtime_tarball, python_root):
        print(f"[prepare] reuse Python runtime: {config.python_runtime_tarball}")
        return

    with atomic_output(config.python_runtime_tarball) as temporary:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(python_root, arcname="dsh-sdk-minimal-python3.12-runtime")
        if not tarball_ready(temporary):
            raise RuntimeError("generated Python runtime archive is invalid")
    print(f"[prepare] built Python runtime: {config.python_runtime_tarball}")


def runtime_ready(config: Config) -> bool:
    if not tarball_ready(config.runtime_tarball) or not tarball_ready(
        config.python_runtime_tarball
    ):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.runtime_version


def source_archive_ready(config: Config) -> bool:
    if not tarball_ready(config.source_archive):
        return False
    return sha256(config.source_archive) == config.source_archive_sha256


def sdk_source_requirement(config: Config) -> str:
    if not source_archive_ready(config):
        raise RuntimeError(
            "checksum-pinned DSH source archive is missing; prepare the "
            "DSH CLI runtime first"
        )
    return f"{config.source_archive.resolve().as_uri()}#subdirectory=python/sdk"


def prepare(config: Config) -> None:
    prepare_python_runtime(config)
    if runtime_ready(config):
        print(
            f"[prepare] skip DSH sdk-minimal runtime (cached): {config.runtime_tarball}"
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
                *RUNTIME_REQUIREMENTS,
            ],
            check=True,
        )
        source_requirement = sdk_source_requirement(config)
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
            f"{config.runtime_version}\n", encoding="utf-8"
        )

        with atomic_output(config.runtime_tarball) as temporary_tar:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(runtime_root, arcname=runtime_root.name)
            if not tarball_ready(temporary_tar):
                raise RuntimeError("generated DSH sdk-minimal archive is invalid")

    atomic_write_text(config.version_file, f"{config.runtime_version}\n")
    print(f"[prepare] built DSH sdk-minimal runtime: {config.runtime_tarball}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-runtime-version", action="store_true")
    args = parser.parse_args(argv)
    config = Config.from_environment()
    if args.print_runtime_version:
        print(config.runtime_version)
        return 0
    prepare(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
