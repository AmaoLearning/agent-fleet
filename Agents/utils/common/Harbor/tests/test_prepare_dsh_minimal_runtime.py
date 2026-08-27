from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import prepare_dsh_minimal_runtime


class PrepareDshMinimalRuntimeTests(unittest.TestCase):
    def test_config_uses_pinned_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            wheel_dir = Path(temporary_name)
            config = prepare_dsh_minimal_runtime.Config.from_environment(
                {"WHEEL_DIR": str(wheel_dir)}
            )

        self.assertEqual(config.version, "0.1.0-rc.6")
        self.assertEqual(
            config.runtime_tarball.name,
            "dsh-minimal-runtime-0.1.0-rc.6.tar.gz",
        )
        self.assertEqual(
            config.python_runtime_tarball.name,
            "dsh-minimal-python3.12-runtime.tar.gz",
        )

    def test_runtime_ready_requires_matching_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            wheel_dir = Path(temporary_name)
            config = prepare_dsh_minimal_runtime.Config.from_environment(
                {"WHEEL_DIR": str(wheel_dir)}
            )
            with tarfile.open(config.runtime_tarball, "w:gz") as archive:
                info = tarfile.TarInfo("dsh-minimal-runtime/site-packages/marker")
                payload = b"ready\n"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            self.assertFalse(prepare_dsh_minimal_runtime.runtime_ready(config))
            with tarfile.open(config.python_runtime_tarball, "w:gz") as archive:
                info = tarfile.TarInfo(
                    "dsh-minimal-python3.12-runtime/bin/python3.12"
                )
                payload = b"python\n"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            config.version_file.write_text("0.1.0-rc.5\n", encoding="utf-8")
            self.assertFalse(prepare_dsh_minimal_runtime.runtime_ready(config))
            config.version_file.write_text("0.1.0-rc.6\n", encoding="utf-8")
            self.assertTrue(prepare_dsh_minimal_runtime.runtime_ready(config))

if __name__ == "__main__":
    unittest.main()
