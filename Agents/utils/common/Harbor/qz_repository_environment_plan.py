"""Build generic QZ environment plans for repository tasks with final images."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

import qz_template_mapping as mapping

SETUP_BLOCK_PATTERN = re.compile(
    r"\A(?P<prefix>"
    r"## Environment Setup \(complete these steps first\)\r?\n"
    r"\r?\n"
    r"```bash\r?\n"
    r"(?P<script>.*?)\r?\n"
    r"```\r?\n"
    r"\r?\n"
    r"---\r?\n"
    r"\r?\n"
    r")",
    re.DOTALL,
)


@dataclass(frozen=True)
class RepositorySetup:
    """Structured identity and exact prompt prefix for one repository task."""

    repository: str
    revision: str
    workdir: str
    instruction_prefix: str


@dataclass(frozen=True)
class FinalImageRecord:
    """One authoritative final image and the task identity it implements."""

    task_id: str
    repository: str
    revision: str
    image: str


def normalize_repository(value: str) -> str:
    """Normalize a clone URL or catalog repository into one stable key."""
    repository = value.strip()
    if not repository:
        raise mapping.QzTemplateMappingError("repository must not be empty")

    if "://" in repository:
        parsed = urllib.parse.urlsplit(repository)
        path = parsed.path
        if parsed.hostname in {"github.com", "www.github.com"}:
            repository = path
        else:
            host = parsed.hostname or ""
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            repository = f"{host}/{path.lstrip('/')}"
    else:
        scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:]+):(.+)", repository)
        if scp_match:
            host, path = scp_match.groups()
            repository = path if host == "github.com" else f"{host}/{path}"

    repository = repository.strip("/")
    repository = repository.removesuffix(".git")
    if not repository or "/" not in repository:
        raise mapping.QzTemplateMappingError(
            f"repository must identify an owner and project: {value!r}"
        )
    return repository


def normalize_revision(value: str) -> str:
    revision = value.strip()
    if not revision:
        raise mapping.QzTemplateMappingError("revision must not be empty")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        return revision.lower()
    return revision


def parse_repository_setup(instruction: str, *, task_name: str) -> RepositorySetup:
    """Parse the exact leading setup contract used by repository task archives."""
    match = SETUP_BLOCK_PATTERN.match(instruction)
    if match is None:
        raise mapping.QzTemplateMappingError(
            f"{task_name} instruction must start with the supported "
            "Environment Setup block"
        )
    if not instruction[match.end() :].strip():
        raise mapping.QzTemplateMappingError(
            f"{task_name} instruction has no agent task after Environment Setup"
        )

    commands = [
        line.strip()
        for line in match.group("script").splitlines()
        if line.strip()
    ]
    if len(commands) < 2:
        raise mapping.QzTemplateMappingError(
            f"{task_name} setup must start with cd and git clone/checkout"
        )
    try:
        cd_command = shlex.split(commands[0])
        checkout_command = shlex.split(commands[1])
    except ValueError as exc:
        raise mapping.QzTemplateMappingError(
            f"{task_name} setup contains invalid shell quoting"
        ) from exc
    if len(cd_command) != 2 or cd_command[0] != "cd":
        raise mapping.QzTemplateMappingError(
            f"{task_name} setup must start with one cd command"
        )
    workdir = cd_command[1]
    if not PurePosixPath(workdir).is_absolute():
        raise mapping.QzTemplateMappingError(
            f"{task_name} setup workdir must be absolute: {workdir!r}"
        )
    if (
        len(checkout_command) != 8
        or checkout_command[0:2] != ["git", "clone"]
        or checkout_command[3:7] != [".", "&&", "git", "checkout"]
    ):
        raise mapping.QzTemplateMappingError(
            f"{task_name} setup must clone into '.' and then checkout a revision"
        )

    return RepositorySetup(
        repository=normalize_repository(checkout_command[2]),
        revision=normalize_revision(checkout_command[7]),
        workdir=workdir,
        instruction_prefix=match.group("prefix"),
    )


def _catalog_records(path: Path) -> list[Mapping[str, Any]]:
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise mapping.QzTemplateMappingError(
            f"image catalog is not valid UTF-8: {path}"
        ) from exc

    records: Any
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        records = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise mapping.QzTemplateMappingError(
                    f"invalid JSON in image catalog {path}:{line_number}"
                ) from exc
            records.append(record)
    else:
        try:
            records = json.loads(text)
        except json.JSONDecodeError as exc:
            raise mapping.QzTemplateMappingError(
                f"image catalog is not valid JSON: {path}"
            ) from exc

    if not isinstance(records, list) or not all(
        isinstance(record, Mapping) for record in records
    ):
        raise mapping.QzTemplateMappingError(
            "image catalog must be a JSON array or JSON Lines objects"
        )
    return records


def load_image_catalog(
    path: Path,
    *,
    task_field: str,
    repository_field: str,
    revision_field: str,
    image_field: str,
) -> dict[str, FinalImageRecord]:
    """Index authoritative final-image records by their explicit task ID."""
    catalog: dict[str, FinalImageRecord] = {}
    for index, record in enumerate(_catalog_records(path)):
        task_id = record.get(task_field)
        repository = record.get(repository_field)
        revision = record.get(revision_field)
        image = record.get(image_field)
        values = (task_id, repository, revision, image)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise mapping.QzTemplateMappingError(
                f"image catalog record {index} must contain non-empty string "
                f"fields {task_field!r}, {repository_field!r}, "
                f"{revision_field!r}, and {image_field!r}"
            )
        normalized_task_id = task_id.strip()
        if normalized_task_id in catalog:
            raise mapping.QzTemplateMappingError(
                f"image catalog contains duplicate task ID {normalized_task_id!r}"
            )
        catalog[normalized_task_id] = FinalImageRecord(
            task_id=normalized_task_id,
            repository=normalize_repository(repository),
            revision=normalize_revision(revision),
            image=image.strip(),
        )
    if not catalog:
        raise mapping.QzTemplateMappingError(f"image catalog is empty: {path}")
    return catalog


def _catalog_record_for_task(
    catalog: Mapping[str, FinalImageRecord],
    task_key: str,
) -> FinalImageRecord:
    record = catalog.get(task_key)
    if record is not None:
        return record

    suffix_matches = [
        record
        for key, record in catalog.items()
        if task_key.endswith((f"/{key}", f"-{key}"))
    ]
    if not suffix_matches:
        raise mapping.QzTemplateMappingError(
            f"image catalog has no final image for task ID {task_key!r}"
        )
    longest_length = max(len(record.task_id) for record in suffix_matches)
    longest = [
        record for record in suffix_matches if len(record.task_id) == longest_length
    ]
    if len(longest) != 1:
        matches = ", ".join(repr(record.task_id) for record in longest)
        raise mapping.QzTemplateMappingError(
            f"task {task_key!r} ambiguously matches image catalog IDs: {matches}"
        )
    return longest[0]


def build_environment_plan(
    tasks: Sequence[tuple[str, Path]],
    catalog: Mapping[str, FinalImageRecord],
) -> dict[str, Any]:
    """Join repository tasks to final images without executing prompt setup."""
    plans: dict[str, dict[str, Any]] = {}
    failures = []
    for task_key, task_dir in tasks:
        try:
            instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
            setup = parse_repository_setup(instruction, task_name=task_key)
            record = _catalog_record_for_task(catalog, task_key)
            if (record.repository, record.revision) != (
                setup.repository,
                setup.revision,
            ):
                raise mapping.QzTemplateMappingError(
                    f"{task_key} setup identifies {setup.repository}@{setup.revision}, "
                    f"but catalog task {record.task_id!r} identifies "
                    f"{record.repository}@{record.revision}"
                )
        except (OSError, mapping.QzTemplateMappingError) as exc:
            failures.append(f"{task_key}: {exc}")
            continue
        plans[task_key] = {
            "image": record.image,
            "build_steps": [],
            "init_steps": [],
            "workdir": setup.workdir,
            "instruction_prefix": setup.instruction_prefix,
        }

    if failures:
        details = "\n".join(f"- {failure}" for failure in failures)
        raise mapping.QzTemplateMappingError(
            f"cannot build repository environment plan for {len(failures)} task(s):\n"
            f"{details}"
        )
    return {
        "schema_version": mapping.ENVIRONMENT_PLAN_SCHEMA_VERSION,
        "tasks": dict(sorted(plans.items())),
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a generic QZ environment plan for repository tasks backed "
            "by authoritative final images."
        )
    )
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--task-list", type=Path)
    parser.add_argument("--image-catalog", required=True, type=Path)
    parser.add_argument("--task-field", default="instance_id")
    parser.add_argument("--repository-field", default="repo")
    parser.add_argument("--revision-field", default="base_commit")
    parser.add_argument("--image-field", default="image_name")
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
        tasks = mapping.discover_tasks(args.dataset_root, args.task_list)
        catalog = load_image_catalog(
            args.image_catalog,
            task_field=args.task_field,
            repository_field=args.repository_field,
            revision_field=args.revision_field,
            image_field=args.image_field,
        )
        plan = build_environment_plan(tasks, catalog)
        if args.output is None:
            json.dump(plan, stdout, ensure_ascii=False, indent=2, sort_keys=True)
            stdout.write("\n")
        else:
            _write_json(args.output, plan)
            print(
                f"wrote {len(plan['tasks'])} repository task plans to {args.output}",
                file=stderr,
            )
        return 0
    except (OSError, mapping.QzTemplateMappingError, ValueError) as exc:
        print(f"error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
