from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import prepare_dsh_runtime


class PrepareDshRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_uses_pinned_defaults_and_overrides(self) -> None:
        config = prepare_dsh_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_VERSION": "0.1.1-rc.2",
                "NPM_CONFIG_REGISTRY": "https://npm.example.test",
            }
        )
        self.assertEqual(config.version, "0.1.1-rc.2")
        self.assertEqual(
            config.runtime_tarball,
            self.root / "dsh-runtime-0.1.1-rc.2.tar.gz",
        )
        self.assertEqual(config.node_runtime_tarball, self.root / "node-runtime.tar.xz")
        self.assertEqual(
            config.portable_node_runtime_tarball,
            self.root / "node-runtime.tar.gz",
        )
        self.assertEqual(config.npm_registry_url, "https://npm.example.test")

    def test_runtime_ready_requires_archive_and_exact_version(self) -> None:
        config = prepare_dsh_runtime.Config.from_environment(
            {"WHEEL_DIR": str(self.root), "DSH_VERSION": "0.1.1-rc.2"}
        )
        with tarfile.open(config.runtime_tarball, "w:gz") as archive:
            payload = b"runtime"
            info = tarfile.TarInfo("bin/dsh")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config.version_file.write_text("0.1.1-rc.1\n", encoding="utf-8")
        self.assertFalse(prepare_dsh_runtime.runtime_ready(config))
        config.version_file.write_text("0.1.1-rc.2\n", encoding="utf-8")
        self.assertTrue(prepare_dsh_runtime.runtime_ready(config))


    def test_prepare_converts_node_archive_for_minimal_task_images(self) -> None:
        config = prepare_dsh_runtime.Config.from_environment(
            {"WHEEL_DIR": str(self.root), "DSH_VERSION": "0.1.1-rc.2"}
        )
        with tarfile.open(config.node_runtime_tarball, "w:xz") as archive:
            for name, payload in (
                ("node-v22/bin/node", b"#!/bin/sh\necho 22\n"),
                ("node-v22/bin/npm", b"#!/bin/sh\nexit 0\n"),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        with tarfile.open(config.runtime_tarball, "w:gz") as archive:
            payload = b"runtime"
            info = tarfile.TarInfo("bin/dsh")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config.version_file.write_text("0.1.1-rc.2\n", encoding="utf-8")

        prepare_dsh_runtime.prepare(config)

        self.assertTrue(
            prepare_dsh_runtime.tarball_ready(config.portable_node_runtime_tarball)
        )
        with tarfile.open(config.portable_node_runtime_tarball) as archive:
            self.assertIn("node-v22/bin/node", archive.getnames())

if __name__ == "__main__":
    unittest.main()
