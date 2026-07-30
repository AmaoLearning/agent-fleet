#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CGROUP_V2_ROOT="$TMP_DIR/cgroup-v2"
mkdir -p "$CGROUP_V2_ROOT"
printf '%s\n' 'cpu io memory pids' > "$CGROUP_V2_ROOT/cgroup.controllers"
printf '%s\n' 101 202 > "$CGROUP_V2_ROOT/cgroup.procs"
: > "$CGROUP_V2_ROOT/cgroup.subtree_control"

"$REPO_ROOT/scripts/dind/prepare-cgroup-v2.sh" "$CGROUP_V2_ROOT"

test -d "$CGROUP_V2_ROOT/init"
printf '%s\n' 101 202 > "$TMP_DIR/expected-procs"
cmp "$TMP_DIR/expected-procs" "$CGROUP_V2_ROOT/init/cgroup.procs"
grep -Fxq -- '+cpu +io +memory +pids' "$CGROUP_V2_ROOT/cgroup.subtree_control"

EMPTY_CONTROLLERS_ROOT="$TMP_DIR/empty-controllers"
mkdir -p "$EMPTY_CONTROLLERS_ROOT"
printf '\n' > "$EMPTY_CONTROLLERS_ROOT/cgroup.controllers"
printf '%s\n' 303 > "$EMPTY_CONTROLLERS_ROOT/cgroup.procs"
printf '%s\n' 'unchanged' > "$EMPTY_CONTROLLERS_ROOT/cgroup.subtree_control"

"$REPO_ROOT/scripts/dind/prepare-cgroup-v2.sh" "$EMPTY_CONTROLLERS_ROOT"

test ! -e "$EMPTY_CONTROLLERS_ROOT/init"
grep -Fxq 'unchanged' "$EMPTY_CONTROLLERS_ROOT/cgroup.subtree_control"

WRITE_FAILURE_ROOT="$TMP_DIR/write-failure"
mkdir -p "$WRITE_FAILURE_ROOT"
printf '%s\n' 'cpu' > "$WRITE_FAILURE_ROOT/cgroup.controllers"
printf '%s\n' 404 > "$WRITE_FAILURE_ROOT/cgroup.procs"
mkdir "$WRITE_FAILURE_ROOT/cgroup.subtree_control"

if "$REPO_ROOT/scripts/dind/prepare-cgroup-v2.sh" "$WRITE_FAILURE_ROOT" \
  > "$TMP_DIR/write-failure.stdout" \
  2> "$TMP_DIR/write-failure.stderr"; then
  echo "expected a persistent cgroup write failure" >&2
  exit 1
fi
grep -Fxq \
  'failed to enable cgroup v2 controllers after 100 attempts: +cpu' \
  "$TMP_DIR/write-failure.stderr"

CGROUP_V1_ROOT="$TMP_DIR/cgroup-v1"
mkdir -p "$CGROUP_V1_ROOT"
"$REPO_ROOT/scripts/dind/prepare-cgroup-v2.sh" "$CGROUP_V1_ROOT"
test ! -e "$CGROUP_V1_ROOT/init"
