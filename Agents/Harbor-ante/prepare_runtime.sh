#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTE_VERSION="${ANTE_VERSION:-0.preview.71}"
ANTE_RUNTIME_DIR="${ANTE_RUNTIME_DIR:-$SCRIPT_DIR/.runtime/$ANTE_VERSION}"
ANTE_BINARY_PATH="${ANTE_BINARY_PATH:-$ANTE_RUNTIME_DIR/ante}"
ANTE_MANIFEST_URL="${ANTE_MANIFEST_URL:-https://download.ante.run/releases/v${ANTE_VERSION}/manifest.json}"

installed_version() {
  [[ -x "$ANTE_BINARY_PATH" ]] || return 1
  "$ANTE_BINARY_PATH" --version 2>/dev/null | awk '
    $1 == "ante" && NF >= 2 { print $2; exit }
    NF >= 1 { print $1; exit }
  '
}

current_version="$(installed_version || true)"
if [[ "$current_version" == "$ANTE_VERSION" || "$current_version" == "v$ANTE_VERSION" ]]; then
  printf '[prepare] using cached Ante %s: %s\n' "$current_version" "$ANTE_BINARY_PATH"
  exit 0
fi

mkdir -p "$ANTE_RUNTIME_DIR"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

if [[ -n "${ANTE_SOURCE_BINARY:-}" ]]; then
  [[ -x "$ANTE_SOURCE_BINARY" ]] || {
    printf '[ERROR] ANTE_SOURCE_BINARY is not executable: %s\n' "$ANTE_SOURCE_BINARY" >&2
    exit 1
  }
  install -m 0755 "$ANTE_SOURCE_BINARY" "$ANTE_BINARY_PATH"
else
  installer="$tmp_dir/install.sh"
  curl -fsSL https://download.ante.run/install.sh -o "$installer"
  ANTE_INSTALL_DIR="$ANTE_RUNTIME_DIR" NO_MODIFY_PATH=true \
    bash "$installer" "$ANTE_MANIFEST_URL"
fi

current_version="$(installed_version || true)"
if [[ "$current_version" != "$ANTE_VERSION" && "$current_version" != "v$ANTE_VERSION" ]]; then
  printf '[ERROR] Ante version mismatch: expected %s, got %s\n' \
    "$ANTE_VERSION" "${current_version:-<none>}" >&2
  exit 1
fi
printf '[prepare] prepared Ante %s: %s\n' "$current_version" "$ANTE_BINARY_PATH"
