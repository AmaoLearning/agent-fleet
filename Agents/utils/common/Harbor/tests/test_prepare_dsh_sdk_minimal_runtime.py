from __future__ import annotations

import tempfile
import unittest

from Agents.utils.common.Harbor import prepare_dsh_sdk_minimal_runtime


class PrepareDshSdkMinimalRuntimeTests(unittest.TestCase):
    def test_defaults_pin_official_release_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            config = prepare_dsh_sdk_minimal_runtime.Config.from_environment(
                {"WHEEL_DIR": temporary_name}
            )

        self.assertEqual(config.source_ref, "dsh-v0.1.2-alpha.2")
        self.assertEqual(
            config.source_sha, "0a53fb55bea101816fa226bb964ae2bed71c343b"
        )
        self.assertEqual(
            config.runtime_tarball.name,
            "dsh-sdk-minimal-runtime-dsh-v0.1.2-alpha.2.tar.gz",
        )

    def test_runtime_receipt_must_match_ref_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            config = prepare_dsh_sdk_minimal_runtime.Config.from_environment(
                {"WHEEL_DIR": temporary_name}
            )
            config.runtime_tarball.touch()
            config.python_runtime_tarball.touch()
            config.version_file.write_text(config.source_version, encoding="utf-8")

            self.assertFalse(
                prepare_dsh_sdk_minimal_runtime.runtime_ready(config)
            )


if __name__ == "__main__":
    unittest.main()
