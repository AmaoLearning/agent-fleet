from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_repository_environment_plan as repository_plan
import qz_template_mapping as mapping

REVISION = "f52f0bf7dcb5885aa912e2f0422824772d1a3931"


def instruction(repository: str = "elastic/synthetics") -> str:
    return f"""## Environment Setup (complete these steps first)

```bash
cd /testbed
git clone https://github.com/{repository}.git . && git checkout {REVISION}
npm ci
npm run build
```

---

Fix the failing recorder test without changing its public API.
"""


class QzRepositoryEnvironmentPlanTest(unittest.TestCase):
    def make_task(self, root: Path, name: str, text: str | None = None) -> Path:
        task = root / name
        task.mkdir(parents=True)
        (task / "task.toml").write_text("[environment]\n", encoding="utf-8")
        (task / "instruction.md").write_text(
            text if text is not None else instruction(),
            encoding="utf-8",
        )
        return task

    def test_parse_setup_extracts_repository_revision_and_exact_prefix(self):
        text = instruction()

        setup = repository_plan.parse_repository_setup(text, task_name="task-a")

        self.assertEqual(setup.repository, "elastic/synthetics")
        self.assertEqual(setup.revision, REVISION)
        self.assertEqual(setup.workdir, "/testbed")
        self.assertTrue(text.startswith(setup.instruction_prefix))
        self.assertEqual(
            text[len(setup.instruction_prefix) :],
            "Fix the failing recorder test without changing its public API.\n",
        )

    def test_cli_joins_tasks_to_final_images_and_feeds_mapping_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            task_key = "tasktrove-benchmark-elastic__synthetics-316"
            self.make_task(root, task_key)
            catalog = Path(temporary) / "images.jsonl"
            catalog.write_text(
                json.dumps(
                    {
                        "instance_id": "elastic__synthetics-316",
                        "repo": "elastic/synthetics",
                        "base_commit": REVISION,
                        "image_name": (
                            "docker.io/swerebenchv2/elastic-synthetics:316-f52f0bf"
                        ),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            plan_path = Path(temporary) / "environment-plan.json"
            mapping_path = Path(temporary) / "mapping.json"

            plan_result = repository_plan.main(
                [
                    "--dataset-root",
                    str(root),
                    "--image-catalog",
                    str(catalog),
                    "--output",
                    str(plan_path),
                ],
                stderr=io.StringIO(),
            )
            mapping_result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "repository-benchmark",
                    "--environment-plan-file",
                    str(plan_path),
                    "--output",
                    str(mapping_path),
                ],
                stderr=io.StringIO(),
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            inventory = json.loads(mapping_path.read_text(encoding="utf-8"))

        self.assertEqual(plan_result, 0)
        self.assertEqual(mapping_result, 0)
        self.assertEqual(plan["schema_version"], 2)
        task_plan = plan["tasks"][task_key]
        self.assertEqual(task_plan["build_steps"], [])
        self.assertEqual(task_plan["init_steps"], [])
        self.assertEqual(task_plan["workdir"], "/testbed")
        self.assertIn("git clone", task_plan["instruction_prefix"])
        self.assertEqual(inventory["schema_version"], 3)
        mapped_task = inventory["tasks"][task_key]
        self.assertEqual(
            mapped_task["instruction_prefix"],
            task_plan["instruction_prefix"],
        )
        self.assertEqual(mapped_task["workdir"], "/testbed")
        template = next(iter(inventory["templates"].values()))
        self.assertEqual(
            template["image"],
            "docker.io/swerebenchv2/elastic-synthetics:316-f52f0bf",
        )
        self.assertEqual(template["build_steps"], [])

    def test_plan_fails_closed_when_setup_shape_or_image_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsupported = self.make_task(
                root,
                "unsupported",
                "Clone the repository and fix the bug.\n",
            )
            missing = self.make_task(root, "missing")
            mismatch = self.make_task(root, "mismatch")
            catalog = {
                "other-task": repository_plan.FinalImageRecord(
                    task_id="other-task",
                    repository="elastic/synthetics",
                    revision=REVISION,
                    image="example/final:v1",
                ),
                "mismatch": repository_plan.FinalImageRecord(
                    task_id="mismatch",
                    repository="other/project",
                    revision=REVISION,
                    image="example/wrong:v1",
                ),
            }

            with self.assertRaises(mapping.QzTemplateMappingError) as context:
                repository_plan.build_environment_plan(
                    [
                        ("unsupported", unsupported),
                        ("missing", missing),
                        ("mismatch", mismatch),
                    ],
                    catalog,
                )

        message = str(context.exception)
        self.assertIn("cannot build repository environment plan for 3 task(s)", message)
        self.assertIn("must start with the supported Environment Setup block", message)
        self.assertIn("image catalog has no final image for task ID", message)
        self.assertIn("but catalog task 'mismatch' identifies other/project", message)

    def test_catalog_suffix_match_requires_an_explicit_boundary(self):
        record = repository_plan.FinalImageRecord(
            task_id="elastic__synthetics-316",
            repository="elastic/synthetics",
            revision=REVISION,
            image="example/final:v1",
        )
        catalog = {record.task_id: record}

        self.assertIs(
            repository_plan._catalog_record_for_task(
                catalog,
                "tasktrove-benchmark-elastic__synthetics-316",
            ),
            record,
        )
        self.assertIs(
            repository_plan._catalog_record_for_task(
                catalog,
                "publisher/dataset/elastic__synthetics-316",
            ),
            record,
        )
        with self.assertRaisesRegex(
            mapping.QzTemplateMappingError,
            "has no final image",
        ):
            repository_plan._catalog_record_for_task(
                catalog,
                "unrelatedelastic__synthetics-316",
            )

    def test_repository_normalization_matches_common_clone_forms(self):
        expected = "elastic/synthetics"
        self.assertEqual(
            repository_plan.normalize_repository(
                "https://github.com/elastic/synthetics.git"
            ),
            expected,
        )
        self.assertEqual(
            repository_plan.normalize_repository(
                "git@github.com:elastic/synthetics.git"
            ),
            expected,
        )
        self.assertEqual(
            repository_plan.normalize_repository("elastic/synthetics"),
            expected,
        )

    def test_repository_normalization_preserves_self_hosted_ports(self):
        first = repository_plan.normalize_repository(
            "https://git.example:8443/org/repo.git"
        )
        second = repository_plan.normalize_repository(
            "https://git.example:9443/org/repo.git"
        )

        self.assertEqual(first, "git.example:8443/org/repo")
        self.assertEqual(second, "git.example:9443/org/repo")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
