from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_template_mapping as mapping


class QzTemplateMappingTest(unittest.TestCase):
    def make_task(self, root: Path, name: str, image: str | None) -> Path:
        task = root / name
        task.mkdir(parents=True)
        environment = "[environment]\n"
        if image is not None:
            environment += f"docker_image = {json.dumps(image)}\n"
        (task / "task.toml").write_text(environment, encoding="utf-8")
        return task

    def make_smith_task(
        self,
        root: Path,
        name: str,
        image: str,
        *,
        common_command: str = "mkdir -p /logs",
    ) -> Path:
        task = root / name
        (task / "environment").mkdir(parents=True)
        (task / "task.toml").write_text(
            f"[metadata]\ninstance_id = {json.dumps(name)}\n",
            encoding="utf-8",
        )
        (task / "environment" / "Dockerfile").write_text(
            f"""FROM {image}
WORKDIR /testbed
RUN apt-get update && apt-get install -y git
RUN {common_command}
RUN git fetch && git checkout {name}
""",
            encoding="utf-8",
        )
        return task

    def make_sweverify_task(self, root: Path, name: str, image: str) -> Path:
        task = root / name
        (task / "environment").mkdir(parents=True)
        (task / "task.toml").write_text(
            "[environment]\nbuild_timeout_sec = 1800.0\n",
            encoding="utf-8",
        )
        (task / "environment" / "Dockerfile").write_text(
            f"""# Generated SWE-bench Verified adapter
FROM {image}
WORKDIR /testbed
RUN curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
RUN mkdir -p /logs
""",
            encoding="utf-8",
        )
        return task

    def test_inventory_deduplicates_identical_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_task(root, "task-a", "ubuntu:24.04")
            second = self.make_task(root, "task-b", "ubuntu:24.04")

            inventory = mapping.build_inventory(
                benchmark="terminalbench21",
                tasks=[("task-b", second), ("task-a", first)],
            )

        self.assertEqual(list(inventory["tasks"]), ["task-a", "task-b"])
        self.assertEqual(len(inventory["templates"]), 1)
        template_keys = {task["template_key"] for task in inventory["tasks"].values()}
        self.assertEqual(len(template_keys), 1)

    def test_identity_changes_with_every_template_input(self):
        baseline = mapping.template_identity("ubuntu:24.04", "g.c1", "official")
        self.assertEqual(
            baseline,
            "fa1e8ad968860c07b4ac6781615ea1876a087e0a4ca17cd5a692b69486a7d137",
        )
        self.assertEqual(
            mapping.template_name("ubuntu:24.04", baseline),
            "af_ubuntu_24_04_fa1e8ad968860c07",
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:22.04", "g.c1", "official"),
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:24.04", "g.c2", "official"),
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:24.04", "g.c1", "custom"),
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity(
                "ubuntu:24.04",
                "g.c1",
                "official",
                [{"type": "RUN", "args": ["apt-get update"]}],
            ),
        )

    def test_swesmith_tasks_share_build_plan_but_keep_task_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_smith_task(root, "instance-a", "smith/base:v1")
            second = self.make_smith_task(root, "instance-b", "smith/base:v1")

            inventory = mapping.build_inventory(
                benchmark="smith",
                tasks=[("instance-a", first), ("instance-b", second)],
                plan_loader=mapping.load_swesmith_task_plan,
            )

        self.assertEqual(len(inventory["templates"]), 1)
        template = next(iter(inventory["templates"].values()))
        self.assertEqual(
            template["build_steps"],
            [
                {"type": "USER", "args": ["root"]},
                {"type": "WORKDIR", "args": ["/testbed"]},
                {
                    "type": "RUN",
                    "args": ["apt-get update && apt-get install -y git"],
                },
                {"type": "RUN", "args": ["mkdir -p /logs"]},
            ],
        )
        self.assertEqual(
            inventory["tasks"]["instance-a"]["init_steps"],
            [
                {
                    "run": "git fetch && git checkout instance-a",
                    "cwd": "/testbed",
                }
            ],
        )
        self.assertEqual(
            inventory["tasks"]["instance-b"]["init_steps"][0]["run"],
            "git fetch && git checkout instance-b",
        )

    def test_swesmith_common_build_change_creates_another_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_smith_task(root, "instance-a", "smith/base:v1")
            second = self.make_smith_task(
                root,
                "instance-b",
                "smith/base:v1",
                common_command="mkdir -p /different-logs",
            )

            inventory = mapping.build_inventory(
                benchmark="smith",
                tasks=[("instance-a", first), ("instance-b", second)],
                plan_loader=mapping.load_swesmith_task_plan,
            )

        self.assertEqual(len(inventory["templates"]), 2)

    def test_swesmith_quotes_instance_id_in_task_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = self.make_smith_task(
                Path(temporary),
                "instance;touch marker",
                "smith/base:v1",
            )

            plan = mapping.load_swesmith_environment_plan(task)

        self.assertEqual(
            plan.init_steps[0]["run"],
            "git fetch && git checkout 'instance;touch marker'",
        )

    def test_swesmith_cli_uses_harbor_task_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_smith_task(root, "instance-a", "smith/base:v1")
            output = Path(temporary) / "mapping.json"

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--dataset-kind",
                    "smith",
                    "--benchmark",
                    "smith",
                    "--output",
                    str(output),
                ],
                stderr=io.StringIO(),
            )
            inventory = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(list(inventory["tasks"]), ["swe-smith__instance-a"])
        self.assertEqual(
            inventory["tasks"]["swe-smith__instance-a"]["init_steps"][0]["run"],
            "git fetch && git checkout instance-a",
        )

    def test_sweverify_cli_reads_generated_final_image_dockerfiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_sweverify_task(
                root,
                "astropy__astropy-12907",
                "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest",
            )
            output = Path(temporary) / "mapping.json"

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--dataset-kind",
                    "sweverify",
                    "--benchmark",
                    "sweverify",
                    "--output",
                    str(output),
                ],
                stderr=io.StringIO(),
            )
            inventory = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        template = next(iter(inventory["templates"].values()))
        self.assertEqual(
            template["image"],
            "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest",
        )
        self.assertEqual(
            template["build_steps"],
            [
                {"type": "USER", "args": ["root"]},
                {"type": "WORKDIR", "args": ["/testbed"]},
                {
                    "type": "RUN",
                    "args": ["curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh"],
                },
                {"type": "RUN", "args": ["mkdir -p /logs"]},
            ],
        )
        self.assertEqual(
            inventory["tasks"]["astropy__astropy-12907"]["init_steps"],
            [],
        )

    def test_generic_manifest_supports_checkout_reset_and_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            for task_name in ("checkout", "reset", "clone"):
                self.make_task(root, task_name, None)
            plan_path = Path(temporary) / "environment-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tasks": {
                            "checkout": {
                                "image": "shared/base:v1",
                                "build_steps": [
                                    {"type": "USER", "args": ["root"]},
                                    {"type": "WORKDIR", "args": ["/testbed"]},
                                ],
                                "init_steps": [
                                    {
                                        "run": "git fetch && git checkout instance-a",
                                        "cwd": "/testbed",
                                    }
                                ],
                            },
                            "reset": {
                                "image": "shared/base:v1",
                                "build_steps": [
                                    {"type": "USER", "args": ["root"]},
                                    {"type": "WORKDIR", "args": ["/testbed"]},
                                ],
                                "init_steps": [
                                    {
                                        "run": "git reset --hard abc && git clean -fd && git checkout abc",
                                        "cwd": "/testbed",
                                    }
                                ],
                            },
                            "clone": {
                                "image": "clone/base:v1",
                                "init_steps": [
                                    {
                                        "run": "git clone https://github.com/org/repo.git /testbed && git -C /testbed checkout def",
                                        "cwd": "/",
                                    }
                                ],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            output = Path(temporary) / "mapping.json"

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "generic",
                    "--environment-plan-file",
                    str(plan_path),
                    "--output",
                    str(output),
                ],
                stderr=io.StringIO(),
            )
            inventory = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(
            inventory["tasks"]["checkout"]["template_key"],
            inventory["tasks"]["reset"]["template_key"],
        )
        template = next(iter(inventory["templates"].values()))
        self.assertEqual(
            template["build_steps"][0],
            {"type": "USER", "args": ["root"]},
        )
        self.assertIn(
            "git reset --hard",
            inventory["tasks"]["reset"]["init_steps"][0]["run"],
        )
        self.assertIn(
            "git clone",
            inventory["tasks"]["clone"]["init_steps"][0]["run"],
        )

    def test_schema_v2_manifest_carries_an_absolute_task_workdir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            task = self.make_task(root, "task-a", None)
            plan_path = Path(temporary) / "environment-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "tasks": {
                            "task-a": {
                                "image": "example/final:v1",
                                "workdir": "/root/repository",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            plans = mapping.load_environment_plan_manifest(plan_path)

            inventory = mapping.build_inventory(
                benchmark="repository",
                tasks=[("task-a", task)],
                plan_loader=lambda task_key, task_dir: (
                    mapping.environment_plan_for_task(plans, task_key, task_dir)
                ),
            )

        self.assertEqual(
            inventory["tasks"]["task-a"]["workdir"],
            "/root/repository",
        )

    def test_manifest_rejects_relative_task_workdir(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment-plan.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "tasks": {
                            "task-a": {
                                "image": "example/final:v1",
                                "workdir": "relative/path",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                mapping.QzTemplateMappingError,
                "workdir must be absolute",
            ):
                mapping.load_environment_plan_manifest(path)

    def test_template_name_is_stable_and_qz_safe(self):
        identity = mapping.template_identity(
            "registry.example/team/task-image:v1", "g.c1", "official"
        )
        first = mapping.template_name("registry.example/team/task-image:v1", identity)
        second = mapping.template_name("registry.example/team/task-image:v1", identity)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), mapping.TEMPLATE_NAME_MAX_LENGTH)
        self.assertRegex(first, re.compile(r"^[A-Za-z0-9_]+$"))

    def test_discover_tasks_can_follow_a_task_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_task(root, "task-a", "ubuntu:24.04")
            self.make_task(root, "task-b", "debian:12")
            task_list = root / "selected.txt"
            task_list.write_text("# smoke\ntask-b\n", encoding="utf-8")

            tasks = mapping.discover_tasks(root, task_list)

        self.assertEqual([key for key, _ in tasks], ["task-b"])

    def test_missing_image_reports_every_unsupported_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_task(root, "task-a", None)
            second = self.make_task(root, "task-b", None)

            with self.assertRaises(mapping.QzTemplateMappingError) as context:
                mapping.build_inventory(
                    benchmark="terminalbench21",
                    tasks=[("task-a", first), ("task-b", second)],
                )

        message = str(context.exception)
        self.assertIn("cannot inventory 2 task(s)", message)
        self.assertIn("task-a", message)
        self.assertIn("task-b", message)

    def test_single_task_dataset_uses_directory_name_as_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "task.toml").write_text(
                '[environment]\ndocker_image = "ubuntu:24.04"\n',
                encoding="utf-8",
            )

            tasks = mapping.discover_tasks(root)

        self.assertEqual(tasks, [(root.name, root.resolve())])

    def test_regeneration_preserves_only_matching_template_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            arguments = [
                "--dataset-root",
                str(root),
                "--benchmark",
                "terminalbench21",
                "--output",
                str(output),
            ]
            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            template_key = payload["tasks"]["task-a"]["template_key"]
            payload["templates"][template_key]["template_id"] = "template-1"
            payload["schema_version"] = mapping.PREVIOUS_SCHEMA_VERSION
            output.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            regenerated = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            regenerated["templates"][template_key]["template_id"],
            "template-1",
        )
        self.assertEqual(regenerated["schema_version"], mapping.SCHEMA_VERSION)

    def test_regeneration_refuses_invalid_existing_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            arguments = [
                "--dataset-root",
                str(root),
                "--benchmark",
                "terminalbench21",
                "--output",
                str(output),
            ]
            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            template = next(iter(payload["templates"].values()))
            template["template_id"] = " "
            invalid = json.dumps(payload)
            output.write_text(invalid, encoding="utf-8")

            stderr = io.StringIO()
            result = mapping.main(arguments, stderr=stderr)

            self.assertEqual(output.read_text(encoding="utf-8"), invalid)

        self.assertEqual(result, 1)
        self.assertIn("invalid template_id", stderr.getvalue())

    def test_cli_writes_deterministic_mapping_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            stderr = io.StringIO()

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "terminalbench21",
                    "--output",
                    str(output),
                ],
                stdout=io.StringIO(),
                stderr=stderr,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["schema_version"], mapping.SCHEMA_VERSION)
        self.assertEqual(payload["identity_version"], "qz-template-environment-v2")
        template = next(iter(payload["templates"].values()))
        self.assertIsNone(template["template_id"])
        self.assertEqual(template["build_steps"], [])
        self.assertEqual(payload["tasks"]["task-a"]["init_steps"], [])
        self.assertIn("1 tasks and 1 unique environments", stderr.getvalue())

    def test_cli_does_not_write_partial_output_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", None)
            output = Path(temporary) / "mapping.json"
            stderr = io.StringIO()

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "terminalbench21",
                    "--output",
                    str(output),
                ],
                stdout=io.StringIO(),
                stderr=stderr,
            )
            output_exists = output.exists()

        self.assertEqual(result, 1)
        self.assertFalse(output_exists)
        self.assertIn("environment.docker_image", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
