"""Inventory Harbor environment plans and emit a deterministic QZ Template map."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    tomllib = None


SCHEMA_VERSION = 3
IDENTITY_VERSION = "qz-template-environment-v2"
PREVIOUS_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
LEGACY_IDENTITY_VERSION = "qz-template-image-v1"
DEFAULT_SPEC = "g.c1"
DEFAULT_IMAGE_SOURCE = "official"
SPEC_CHOICES = ("g.c1", "g.c2", "g.c4")
DATASET_KINDS = ("image", "smith", "sweverify")
ENVIRONMENT_PLAN_SCHEMA_VERSION = 2
LEGACY_ENVIRONMENT_PLAN_SCHEMA_VERSION = 1
TEMPLATE_NAME_MAX_LENGTH = 63
TEMPLATE_LABEL_MAX_LENGTH = 32
TEMPLATE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class QzTemplateMappingError(RuntimeError):
    """Raised when a benchmark cannot be represented by the mapping schema."""


@dataclass(frozen=True)
class TaskEnvironmentPlan:
    """Template inputs and per-Sandbox initialization for one Harbor task."""

    image: str
    build_steps: tuple[dict[str, Any], ...] = ()
    init_steps: tuple[dict[str, str], ...] = ()
    workdir: str | None = None
    instruction_prefix: str = ""


def _fallback_toml_string(path: Path, wanted_section: str, wanted_key: str) -> str:
    """Read one TOML string on Python versions that do not provide tomllib."""
    section = ""
    decoder = json.JSONDecoder()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != wanted_section or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != wanted_key:
            continue
        value = raw_value.lstrip()
        if value.startswith('"'):
            try:
                parsed, end = decoder.raw_decode(value)
            except json.JSONDecodeError as exc:
                raise QzTemplateMappingError(
                    f"invalid {wanted_section}.{wanted_key} in {path}"
                ) from exc
            trailing = value[end:].strip()
            if trailing and not trailing.startswith("#"):
                raise QzTemplateMappingError(
                    f"invalid {wanted_section}.{wanted_key} in {path}"
                )
            return parsed
        if value.startswith("'"):
            end = value.find("'", 1)
            if end == -1:
                raise QzTemplateMappingError(
                    f"invalid {wanted_section}.{wanted_key} in {path}"
                )
            trailing = value[end + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                raise QzTemplateMappingError(
                    f"invalid {wanted_section}.{wanted_key} in {path}"
                )
            return value[1:end]
        raise QzTemplateMappingError(
            f"{wanted_section}.{wanted_key} must be a TOML string in {path}"
        )
    raise QzTemplateMappingError(
        f"task is missing {wanted_section}.{wanted_key}: {path.parent}"
    )


def _load_toml_string(task_dir: Path, section: str, key: str) -> str:
    task_config = task_dir / "task.toml"
    if not task_config.is_file():
        raise QzTemplateMappingError(f"task.toml not found under {task_dir}")

    if tomllib is None:
        value = _fallback_toml_string(task_config, section, key)
    else:
        try:
            with task_config.open("rb") as handle:
                payload = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise QzTemplateMappingError(f"invalid TOML: {task_config}") from exc
        section_payload = payload.get(section)
        value = (
            section_payload.get(key) if isinstance(section_payload, Mapping) else None
        )

    if not isinstance(value, str) or not value.strip():
        raise QzTemplateMappingError(f"task is missing {section}.{key}: {task_dir}")
    return value.strip()


def load_task_image(task_dir: Path) -> str:
    """Return the prebuilt image declared by a local Harbor task."""
    return _load_toml_string(task_dir, "environment", "docker_image")


def load_image_environment_plan(task_dir: Path) -> TaskEnvironmentPlan:
    """Represent one task that already declares its final runnable image."""
    return TaskEnvironmentPlan(image=load_task_image(task_dir))


def load_environment_plan_manifest(path: Path) -> dict[str, TaskEnvironmentPlan]:
    """Load dataset-independent task plans from one JSON manifest."""
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QzTemplateMappingError(
            f"environment plan is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise QzTemplateMappingError("environment plan must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version not in {
        LEGACY_ENVIRONMENT_PLAN_SCHEMA_VERSION,
        ENVIRONMENT_PLAN_SCHEMA_VERSION,
    }:
        raise QzTemplateMappingError(
            "unsupported environment plan schema_version: "
            f"{payload.get('schema_version')!r}"
        )
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict):
        raise QzTemplateMappingError("environment plan is missing tasks")

    plans = {}
    for task_key, entry in tasks.items():
        if not isinstance(task_key, str) or not task_key.strip():
            raise QzTemplateMappingError("environment plan has an invalid task key")
        if not isinstance(entry, dict):
            raise QzTemplateMappingError(
                f"environment plan task {task_key!r} must be an object"
            )
        image = entry.get("image")
        build_steps = entry.get("build_steps", [])
        init_steps = entry.get("init_steps", [])
        workdir = entry.get("workdir")
        instruction_prefix = entry.get("instruction_prefix", "")
        if not isinstance(image, str) or not image.strip():
            raise QzTemplateMappingError(
                f"environment plan task {task_key!r} is missing image"
            )
        if not isinstance(build_steps, list):
            raise QzTemplateMappingError(
                f"environment plan task {task_key!r} build_steps must be a list"
            )
        if not isinstance(init_steps, list):
            raise QzTemplateMappingError(
                f"environment plan task {task_key!r} init_steps must be a list"
            )
        if schema_version == LEGACY_ENVIRONMENT_PLAN_SCHEMA_VERSION:
            unsupported_fields = {
                field for field in ("instruction_prefix", "workdir") if field in entry
            }
            if unsupported_fields:
                raise QzTemplateMappingError(
                    f"environment plan task {task_key!r} uses "
                    f"{', '.join(sorted(unsupported_fields))} with legacy "
                    "schema_version"
                )
        elif not isinstance(instruction_prefix, str):
            raise QzTemplateMappingError(
                f"environment plan task {task_key!r} instruction_prefix "
                "must be a string"
            )
        plans[task_key] = TaskEnvironmentPlan(
            image=image.strip(),
            build_steps=tuple(build_steps),
            init_steps=tuple(init_steps),
            workdir=normalize_workdir(workdir),
            instruction_prefix=instruction_prefix,
        )
    return plans


def environment_plan_for_task(
    plans: Mapping[str, TaskEnvironmentPlan],
    task_key: str,
    _task_dir: Path,
) -> TaskEnvironmentPlan:
    """Select one task from a previously loaded generic manifest."""
    try:
        return plans[task_key]
    except KeyError:
        raise QzTemplateMappingError(
            f"environment plan is missing selected task {task_key!r}"
        ) from None


def load_swesmith_task_plan(
    _task_key: str,
    task_dir: Path,
) -> TaskEnvironmentPlan:
    """Adapt the SWE-Smith convenience loader to the generic loader contract."""
    return load_swesmith_environment_plan(task_dir)


def swesmith_task_key(task_dir: Path) -> str:
    """Return the task name Harbor passes to an environment provider."""
    instance_id = _load_toml_string(task_dir, "metadata", "instance_id")
    return f"swe-smith__{instance_id}"


def load_sweverify_task_plan(
    _task_key: str,
    task_dir: Path,
) -> TaskEnvironmentPlan:
    """Adapt a generated SWE-bench Verified task to an environment plan."""
    return load_sweverify_environment_plan(task_dir)


def _build_step(step_type: str, argument: str) -> dict[str, Any]:
    return {"type": step_type, "args": [argument]}


def _load_simple_dockerfile(
    task_dir: Path,
    dataset_name: str,
) -> tuple[Path, str, list[str]]:
    dockerfile = task_dir / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        raise QzTemplateMappingError(
            f"{dataset_name} Dockerfile not found: {dockerfile}"
        )

    instructions = [
        line.strip()
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not instructions or not instructions[0].upper().startswith("FROM "):
        raise QzTemplateMappingError(
            f"{dataset_name} Dockerfile must start with FROM: {dockerfile}"
        )
    image_parts = instructions[0][5:].strip().split()
    if len(image_parts) != 1:
        raise QzTemplateMappingError(
            f"unsupported {dataset_name} FROM instruction: {instructions[0]!r}"
        )
    return dockerfile, image_parts[0], instructions[1:]


def _parse_simple_build_steps(
    instructions: Iterable[str],
    *,
    dataset_name: str,
) -> tuple[list[dict[str, Any]], str]:
    build_steps = []
    workdir = "/"
    for instruction in instructions:
        keyword, separator, argument = instruction.partition(" ")
        step_type = keyword.upper()
        argument = argument.strip()
        if not separator or step_type not in {"RUN", "USER", "WORKDIR"} or not argument:
            raise QzTemplateMappingError(
                f"unsupported {dataset_name} Dockerfile instruction: {instruction!r}"
            )
        if step_type == "WORKDIR":
            workdir = argument
        build_steps.append(_build_step(step_type, argument))
    return build_steps, workdir


def load_swesmith_environment_plan(task_dir: Path) -> TaskEnvironmentPlan:
    """Split a generated SWE-Smith Dockerfile into shared and task steps.

    This deliberately supports only the adapter's current FROM/USER/WORKDIR/RUN
    shape. Other Dockerfiles belong to a later build-context materializer.
    """
    instance_id = _load_toml_string(task_dir, "metadata", "instance_id")
    dockerfile, image, instructions = _load_simple_dockerfile(
        task_dir,
        "SWE-Smith",
    )

    expected_init = f"RUN git fetch && git checkout {instance_id}"
    if not instructions or instructions[-1] != expected_init:
        raise QzTemplateMappingError(
            "SWE-Smith Dockerfile must end with task checkout "
            f"{expected_init!r}: {dockerfile}"
        )
    build_steps, workdir = _parse_simple_build_steps(
        instructions[:-1],
        dataset_name="SWE-Smith",
    )
    build_steps.insert(0, _build_step("USER", "root"))
    return TaskEnvironmentPlan(
        image=image,
        build_steps=tuple(build_steps),
        init_steps=(
            {
                "run": f"git fetch && git checkout {shlex.quote(instance_id)}",
                "cwd": workdir,
            },
        ),
    )


def load_sweverify_environment_plan(task_dir: Path) -> TaskEnvironmentPlan:
    """Read the generated SWE-bench Verified final-image Dockerfile."""
    _, image, instructions = _load_simple_dockerfile(
        task_dir,
        "SWE-bench Verified",
    )
    build_steps, _ = _parse_simple_build_steps(
        instructions,
        dataset_name="SWE-bench Verified",
    )
    build_steps.insert(0, _build_step("USER", "root"))
    return TaskEnvironmentPlan(image=image, build_steps=tuple(build_steps))


def _task_list_entries(path: Path) -> list[str]:
    if not path.is_file():
        raise QzTemplateMappingError(f"task list not found: {path}")
    entries = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            entries.append(value)
    if not entries:
        raise QzTemplateMappingError(f"task list is empty: {path}")
    return entries


def discover_tasks(
    dataset_root: Path,
    task_list: Path | None = None,
) -> list[tuple[str, Path]]:
    """Discover task directories and return portable relative task keys."""
    root = dataset_root.expanduser().resolve()
    if not root.is_dir():
        raise QzTemplateMappingError(f"dataset root not found: {dataset_root}")

    if (root / "task.toml").is_file():
        if task_list is not None:
            raise QzTemplateMappingError(
                "--task-list cannot be used when --dataset-root is one task"
            )
        return [(root.name, root)]

    if task_list is None:
        candidates = sorted(
            path for path in root.iterdir() if (path / "task.toml").is_file()
        )
    else:
        candidates = []
        for entry in _task_list_entries(task_list):
            relative = Path(entry)
            if relative.is_absolute():
                raise QzTemplateMappingError(
                    f"task list entries must be relative paths: {entry}"
                )
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise QzTemplateMappingError(
                    f"task escapes dataset root: {entry}"
                ) from exc
            if not (candidate / "task.toml").is_file():
                raise QzTemplateMappingError(
                    f"task.toml not found for task list entry: {entry}"
                )
            candidates.append(candidate)

    if not candidates:
        raise QzTemplateMappingError(f"no Harbor tasks found under {root}")

    discovered: dict[str, Path] = {}
    for candidate in candidates:
        key = candidate.relative_to(root).as_posix()
        if key in discovered:
            raise QzTemplateMappingError(f"duplicate task in inventory: {key}")
        discovered[key] = candidate
    return sorted(discovered.items())


def normalize_build_steps(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate the small QZ build-step subset supported by this mapping."""
    normalized = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise QzTemplateMappingError(f"build step {index} must be an object")
        step_type = step.get("type")
        args = step.get("args")
        if step_type not in {"RUN", "USER", "WORKDIR"}:
            raise QzTemplateMappingError(
                f"build step {index} has unsupported type {step_type!r}"
            )
        if (
            (not isinstance(args, list) and not isinstance(args, tuple))
            or len(args) != 1
            or not isinstance(args[0], str)
            or not args[0].strip()
        ):
            raise QzTemplateMappingError(
                f"build step {index} must contain one non-empty string argument"
            )
        normalized.append({"type": step_type, "args": [args[0].strip()]})
    return normalized


def normalize_init_steps(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Validate ordered commands executed after a fresh Sandbox is created."""
    normalized = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise QzTemplateMappingError(f"init step {index} must be an object")
        command = step.get("run")
        cwd = step.get("cwd")
        if not isinstance(command, str) or not command.strip():
            raise QzTemplateMappingError(
                f"init step {index} is missing a non-empty run command"
            )
        normalized_step = {"run": command.strip()}
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.strip():
                raise QzTemplateMappingError(f"init step {index} has an invalid cwd")
            normalized_step["cwd"] = cwd.strip()
        normalized.append(normalized_step)
    return normalized


def normalize_instruction_prefix(value: Any) -> str:
    """Validate the exact leading instruction text removed before agent run."""
    if not isinstance(value, str):
        raise QzTemplateMappingError("instruction_prefix must be a string")
    return value


def normalize_workdir(value: Any) -> str | None:
    """Validate an optional absolute task execution directory."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise QzTemplateMappingError("workdir must be a non-empty string")
    workdir = value.strip()
    if not PurePosixPath(workdir).is_absolute():
        raise QzTemplateMappingError(f"workdir must be absolute: {workdir!r}")
    return workdir


def template_identity(
    image: str,
    spec: str,
    image_source: str,
    build_steps: Iterable[Mapping[str, Any]] = (),
    *,
    identity_version: str = IDENTITY_VERSION,
) -> str:
    """Return the stable identity for one QZ Template input tuple."""
    normalized_steps = normalize_build_steps(build_steps)
    identity_payload: dict[str, Any] = {
        "identity_version": identity_version,
        "image": image,
        "image_source": image_source,
        "spec": spec,
    }
    if identity_version == IDENTITY_VERSION:
        identity_payload["build_steps"] = normalized_steps
    elif identity_version == LEGACY_IDENTITY_VERSION:
        if normalized_steps:
            raise QzTemplateMappingError(
                "legacy Template identity does not support build steps"
            )
    else:
        raise QzTemplateMappingError(
            f"unsupported Template identity_version: {identity_version!r}"
        )
    payload = json.dumps(
        identity_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def template_name(image: str, identity: str) -> str:
    """Build a deterministic QZ-safe alias from an image and its identity."""
    leaf = image.rsplit("/", 1)[-1].split("@", 1)[0]
    label = re.sub(r"[^A-Za-z0-9]+", "_", leaf).strip("_").lower()
    label = (label or "image")[:TEMPLATE_LABEL_MAX_LENGTH].rstrip("_")
    name = f"af_{label}_{identity[:16]}"
    name = name[:TEMPLATE_NAME_MAX_LENGTH].rstrip("_")
    if TEMPLATE_NAME_PATTERN.fullmatch(name) is None:
        raise QzTemplateMappingError(f"failed to build a QZ-safe name for {image!r}")
    return name


def build_inventory(
    *,
    benchmark: str,
    tasks: Iterable[tuple[str, Path]],
    spec: str = DEFAULT_SPEC,
    image_source: str = DEFAULT_IMAGE_SOURCE,
    plan_loader: Callable[[str, Path], TaskEnvironmentPlan] | None = None,
) -> dict[str, Any]:
    """Build a deterministic schema-v3 task-to-Template inventory."""
    benchmark = benchmark.strip()
    image_source = image_source.strip()
    if not benchmark:
        raise QzTemplateMappingError("benchmark name must not be empty")
    if spec not in SPEC_CHOICES:
        raise QzTemplateMappingError(f"unsupported QZ spec: {spec}")
    if not image_source:
        raise QzTemplateMappingError("image source must not be empty")

    templates: dict[str, dict[str, Any]] = {}
    task_map: dict[str, dict[str, Any]] = {}
    failures = []
    for task_key, task_dir in sorted(tasks):
        try:
            plan = (
                plan_loader(task_key, task_dir)
                if plan_loader is not None
                else load_image_environment_plan(task_dir)
            )
            image = plan.image.strip()
            if not image:
                raise QzTemplateMappingError(
                    f"environment plan has an empty image: {task_dir}"
                )
            build_steps = normalize_build_steps(plan.build_steps)
            init_steps = normalize_init_steps(plan.init_steps)
            workdir = normalize_workdir(plan.workdir)
            instruction_prefix = normalize_instruction_prefix(
                plan.instruction_prefix
            )
        except QzTemplateMappingError as exc:
            failures.append(f"{task_key}: {exc}")
            continue
        identity = template_identity(image, spec, image_source, build_steps)
        template_key = f"sha256:{identity}"
        templates.setdefault(
            template_key,
            {
                "image": image,
                "image_source": image_source,
                "spec": spec,
                "build_steps": build_steps,
                "template_id": None,
                "template_name": template_name(image, identity),
            },
        )
        task_map[task_key] = {
            "docker_image": image,
            "init_steps": init_steps,
            "template_key": template_key,
        }
        if workdir is not None:
            task_map[task_key]["workdir"] = workdir
        if instruction_prefix:
            task_map[task_key]["instruction_prefix"] = instruction_prefix

    if failures:
        details = "\n".join(f"- {failure}" for failure in failures)
        raise QzTemplateMappingError(
            f"cannot inventory {len(failures)} task(s):\n{details}"
        )

    return {
        "benchmark": benchmark,
        "identity_version": IDENTITY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "tasks": dict(sorted(task_map.items())),
        "templates": dict(sorted(templates.items())),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def preserve_template_ids(
    inventory: dict[str, Any],
    existing_path: Path,
) -> int:
    """Carry forward bindings whose full content identity is unchanged."""
    if not existing_path.is_file():
        return 0
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QzTemplateMappingError(
            f"existing mapping is not valid UTF-8 JSON: {existing_path}"
        ) from exc
    if not isinstance(existing, dict):
        raise QzTemplateMappingError(
            f"existing mapping must be a JSON object: {existing_path}"
        )
    if existing.get("schema_version") not in {
        PREVIOUS_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
        raise QzTemplateMappingError(
            f"existing mapping has unsupported schema_version: {existing_path}"
        )
    if existing.get("identity_version") != IDENTITY_VERSION:
        raise QzTemplateMappingError(
            f"existing mapping has unsupported identity_version: {existing_path}"
        )
    existing_templates = existing.get("templates")
    if not isinstance(existing_templates, dict):
        raise QzTemplateMappingError(
            f"existing mapping is missing templates: {existing_path}"
        )

    preserved = 0
    for template_key, template in inventory["templates"].items():
        previous = existing_templates.get(template_key)
        if not isinstance(previous, dict):
            continue
        template_id = previous.get("template_id")
        if template_id is None:
            continue
        if not isinstance(template_id, str) or not template_id.strip():
            raise QzTemplateMappingError(
                f"existing mapping Template {template_key!r} has an invalid template_id"
            )
        template["template_id"] = template_id.strip()
        preserved += 1
    return preserved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory Harbor task images for QZ Template mapping."
    )
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--task-list", type=Path)
    parser.add_argument("--dataset-kind", choices=DATASET_KINDS, default="image")
    parser.add_argument("--environment-plan-file", type=Path)
    parser.add_argument("--spec", choices=SPEC_CHOICES, default=DEFAULT_SPEC)
    parser.add_argument("--image-source", default=DEFAULT_IMAGE_SOURCE)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = parse_args(argv)
    try:
        tasks = discover_tasks(args.dataset_root, args.task_list)
        if args.environment_plan_file is not None and args.dataset_kind != "image":
            raise QzTemplateMappingError(
                "use either --environment-plan-file or a generated dataset kind"
            )
        if args.environment_plan_file is not None:
            plans = load_environment_plan_manifest(args.environment_plan_file)
            plan_loader = partial(environment_plan_for_task, plans)
        elif args.dataset_kind == "smith":
            tasks = [(swesmith_task_key(task_dir), task_dir) for _, task_dir in tasks]
            plan_loader = load_swesmith_task_plan
        elif args.dataset_kind == "sweverify":
            plan_loader = load_sweverify_task_plan
        else:
            plan_loader = None
        inventory = build_inventory(
            benchmark=args.benchmark,
            tasks=tasks,
            spec=args.spec,
            image_source=args.image_source,
            plan_loader=plan_loader,
        )
        if args.output is None:
            json.dump(inventory, stdout, ensure_ascii=False, indent=2, sort_keys=True)
            stdout.write("\n")
        else:
            preserve_template_ids(inventory, args.output.expanduser())
            _write_json(args.output, inventory)
            print(
                f"wrote {len(inventory['tasks'])} tasks and "
                f"{len(inventory['templates'])} unique environments to {args.output}",
                file=stderr,
            )
        return 0
    except (OSError, QzTemplateMappingError, ValueError) as exc:
        print(f"error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
