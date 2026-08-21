from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_task_instruction as task_instruction
import qz_template_mapping as mapping
import qz_template_resolver as resolver


class QzTaskInstructionTest(unittest.TestCase):
    def write_mapping(self, root: Path, prefix: str) -> Path:
        image = "example/final:v1"
        identity = mapping.template_identity(image, "g.c1", "official")
        path = root / "mapping.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": mapping.SCHEMA_VERSION,
                    "identity_version": mapping.IDENTITY_VERSION,
                    "tasks": {
                        "suite/task-a": {
                            "docker_image": image,
                            "init_steps": [],
                            "instruction_prefix": prefix,
                            "template_key": f"sha256:{identity}",
                        }
                    },
                    "templates": {
                        f"sha256:{identity}": {
                            "image": image,
                            "image_source": "official",
                            "spec": "g.c1",
                            "build_steps": [],
                            "template_id": None,
                            "template_name": mapping.template_name(image, identity),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_exact_prefix_is_removed_for_registry_qualified_task_name(self):
        prefix = "setup block\n\n---\n\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_mapping(Path(temporary), prefix)

            result = task_instruction.mapped_agent_instruction(
                path,
                "publisher/dataset/suite/task-a",
                prefix + "Fix the bug.\n\nExtra instruction.",
            )

        self.assertEqual(result, "Fix the bug.\n\nExtra instruction.")

    def test_prefix_mismatch_and_empty_agent_task_fail_closed(self):
        prefix = "setup block\n\n---\n\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_mapping(Path(temporary), prefix)
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "does not match",
            ):
                task_instruction.mapped_agent_instruction(
                    path,
                    "task-a",
                    "different setup\nFix the bug.",
                )
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "no agent instruction",
            ):
                task_instruction.mapped_agent_instruction(
                    path,
                    "task-a",
                    prefix,
                )

    def test_harbor_task_patch_applies_the_mapping_before_agent_run(self):
        prefix = "setup block\n\n---\n\n"

        class FakeTask:
            def __init__(self, _task_dir):
                self.name = "publisher/dataset/suite/task-a"
                self.instruction = prefix + "Fix the bug."

        harbor_module = types.ModuleType("harbor")
        models_module = types.ModuleType("harbor.models")
        task_package = types.ModuleType("harbor.models.task")
        task_module = types.ModuleType("harbor.models.task.task")
        task_module.Task = FakeTask
        fake_modules = {
            "harbor": harbor_module,
            "harbor.models": models_module,
            "harbor.models.task": task_package,
            "harbor.models.task.task": task_module,
        }

        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_mapping(Path(temporary), prefix)
            with (
                patch.dict(sys.modules, fake_modules),
                patch.dict(
                    os.environ,
                    {resolver.MAPPING_ENV_VAR: str(path)},
                    clear=False,
                ),
            ):
                task_instruction.patch_harbor_task_instruction()
                task = FakeTask("unused")

        self.assertEqual(task.instruction, "Fix the bug.")


if __name__ == "__main__":
    unittest.main()
