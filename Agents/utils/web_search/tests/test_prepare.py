import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from web_search_adapter.prepare import prepare

REPO = Path(__file__).resolve().parents[4]
CSV = b"problem,problem_category,answer,answer_type\nQuestion,Test,Answer,Single Answer\n"


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.destination = self.root / "tasks"
        self.sources = self.root / "sources"
        for name, value in (("EXPECTED_COUNTS", 1), ("EXPECTED_SHA256", hashlib.sha256(CSV).hexdigest())):
            patcher = patch.dict(f"web_search_adapter.adapter.{name}", {"deepsearchqa": value})
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self):
        prepare("deepsearchqa", self.destination, self.sources, "python:3.12-slim")

    def test_download_cache_and_reuse_even_empty_directory(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(CSV)) as download:
            self.prepare()
        self.assertEqual(download.call_count, 1)
        self.assertEqual((self.sources / "DSQA-full.csv").read_bytes(), CSV)
        with patch("urllib.request.urlopen", side_effect=AssertionError("must reuse")):
            self.prepare()
            self.destination = self.root / "empty"
            self.destination.mkdir()
            self.prepare()
            self.destination = self.root / "from-cache"
            self.prepare()
        self.assertTrue((self.destination / "deepsearchqa-000000/task.toml").is_file())

    def test_failed_download_or_validation_does_not_publish_and_can_retry(self):
        for error in (OSError("offline"), None):
            with (
                patch("urllib.request.urlopen", side_effect=error, return_value=io.BytesIO(b"invalid")),
                self.assertRaises((OSError, ValueError)),
            ):
                self.prepare()
            self.assertFalse(self.destination.exists())
            self.assertFalse(self.sources.exists())
            self.assertFalse(list(self.root.glob(".tasks-*")))
        with patch("urllib.request.urlopen", return_value=io.BytesIO(CSV)):
            self.prepare()
        self.assertTrue(self.destination.is_dir())

    def test_generation_failure_does_not_publish_partial_tasks(self):
        with (
            patch("urllib.request.urlopen", return_value=io.BytesIO(CSV)),
            patch("web_search_adapter.adapter.WebSearchAdapter._write_task", side_effect=OSError("full")),
            self.assertRaises(OSError),
        ):
            self.prepare()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.sources.exists())
        self.assertFalse(list(self.root.glob(".tasks-*")))

    def test_concurrent_starts_generate_only_once(self):
        with (
            patch("urllib.request.urlopen", return_value=io.BytesIO(CSV)) as download,
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            list(pool.map(lambda _: self.prepare(), range(4)))
        self.assertEqual(download.call_count, 1)

    def test_huggingface_mirror_and_existing_file(self):
        with (
            patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com/"}),
            patch("urllib.request.urlopen", return_value=io.BytesIO(CSV)) as download,
        ):
            self.prepare()
        self.assertEqual(download.call_args.args[0],
                         "https://hf-mirror.com/datasets/google/deepsearchqa/resolve/main/DSQA-full.csv")
        self.destination = self.root / "file"
        self.destination.touch()
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_shell_guard_and_default_path_do_not_generate_on_source(self):
        for name, enabled, expected in (
            ("browsecomp", "1", "web-search/tasks/browsecomp"),
            ("deepsearchqa", "1", "web-search/tasks/deepsearchqa"),
            ("browsecomp", "0", ""),
            ("seta", "1", ""),
        ):
            with self.subTest(name=name, enabled=enabled):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; printf "%s" "${DATASET_PATH:-}"',
                     "bash", str(REPO / "Agents/utils/web_search/env.sh")],
                    env={"PATH": os.environ["PATH"], "DATASET_NAME": name,
                         "HARBOR_CC_WEB_MCP_ENABLED": enabled, "AGENT_FLEET_CACHE_DIR": str(self.root)},
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(result.stdout, str(self.root / expected) if expected else "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_web_alias_without_path_never_uses_seta_default(self):
        for name in ("browsecomp", "deepsearchqa"):
            for enabled in (None, "", "0", "1"):
                with self.subTest(name=name, enabled=enabled):
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1"; printf "%s" "$DATASET_PATH"',
                         "bash", str(REPO / "Agents/utils/common/Harbor/env.sh")],
                        env={"PATH": os.environ["PATH"], "HOME": str(self.root),
                             "AGENT_FLEET_CONFIG_LOADED_ROOT": str(REPO), "OPIK_URL": "",
                             "AGENT_FLEET_CACHE_DIR": str(self.root), "DATASET_NAME": name,
                             **({"HARBOR_CC_WEB_MCP_ENABLED": enabled} if enabled is not None else {})},
                        capture_output=True, text=True, check=False,
                    )
                    if enabled == "1":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, str(self.root / "web-search/tasks" / name))
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f"{name} requires DATASET_PATH", result.stderr)
                    self.assertNotIn("/workspace/seta-env/Harbor-Dataset", result.stdout)

    def test_startup_helpers_reuse_directory_without_python(self):
        self.destination.mkdir()
        for enabled, name in ((None, "browsecomp"), ("0", "browsecomp"),
                              ("", "browsecomp"), ("1", "browsecomp"),
                              (None, "deepsearchqa"), ("0", "deepsearchqa"),
                              ("", "deepsearchqa"), ("1", "deepsearchqa"), ("1", "unrelated")):
            result = subprocess.run(
                ["bash", "-c", ('source "$1"; '
                 'if declare -F harbor_prepare_web_search_dataset >/dev/null; then echo web-env-loaded; fi; '
                 'harbor_ensure_dataset; harbor_generate_task_file "$HOME/tasks.txt"; '
                 'harbor_validate_local_task_selection'),
                 "bash", str(REPO / "Agents/utils/common/Harbor/env.sh")],
                env={"PATH": os.environ["PATH"], "HOME": str(self.root),
                     "AGENT_FLEET_CONFIG_LOADED_ROOT": str(REPO), "OPIK_URL": "",
                     "DATASET_NAME": name, "DATASET_PATH": str(self.destination),
                     "HARBOR_OPIK_PYTHON": "/missing/python",
                     **({"HARBOR_CC_WEB_MCP_ENABLED": enabled} if enabled is not None else {})},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual("web-env-loaded" in result.stdout, enabled == "1")

    def test_disabled_or_unrelated_missing_dataset_does_not_call_adapter(self):
        for enabled, name in (("0", "browsecomp"), ("1", "unrelated")):
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; harbor_prepare_web_search_dataset',
                 "bash", str(REPO / "Agents/utils/web_search/env.sh")],
                env={"PATH": os.environ["PATH"], "DATASET_NAME": name,
                     "DATASET_PATH": str(self.destination), "HARBOR_CC_WEB_MCP_ENABLED": enabled,
                     "HARBOR_OPIK_PYTHON": "/missing/python"},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
