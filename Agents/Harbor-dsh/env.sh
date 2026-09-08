#!/usr/bin/env bash
set -euo pipefail

# DSH-only settings and dependency-cache helpers. This file is sourced by the
# shared Harbor environment only when AGENT=dsh-sdk-minimal.

DSH_SDK_MINIMAL_PYTHON_RUNTIME_BASENAME="${DSH_SDK_MINIMAL_PYTHON_RUNTIME_BASENAME:-dsh-sdk-minimal-python3.12-runtime.tar.gz}"
DSH_SDK_MINIMAL_CLI_VERSION="${DSH_SDK_MINIMAL_CLI_VERSION:-0.1.3-alpha.1}"
DSH_SDK_MINIMAL_SOURCE_REF="${DSH_SDK_MINIMAL_SOURCE_REF:-dsh-v0.1.3-alpha.1}"
DSH_SDK_MINIMAL_SOURCE_SHA="${DSH_SDK_MINIMAL_SOURCE_SHA:-d347e703908d0406b7a7ef80e3a0e594d86b2215}"
DSH_SDK_MINIMAL_RUNTIME_BASENAME="${DSH_SDK_MINIMAL_RUNTIME_BASENAME:-dsh-sdk-minimal-runtime-${DSH_SDK_MINIMAL_SOURCE_REF}.tar.gz}"
DSH_SDK_MINIMAL_RUNTIME_VERSION_BASENAME="${DSH_SDK_MINIMAL_RUNTIME_VERSION_BASENAME:-dsh-sdk-minimal-runtime.version}"
DSH_SDK_MINIMAL_CLI_RUNTIME_BASENAME="${DSH_SDK_MINIMAL_CLI_RUNTIME_BASENAME:-dsh-sdk-minimal-cli-runtime-${DSH_SDK_MINIMAL_CLI_VERSION}.tar.gz}"
DSH_SDK_MINIMAL_CLI_RUNTIME_VERSION_BASENAME="${DSH_SDK_MINIMAL_CLI_RUNTIME_VERSION_BASENAME:-dsh-sdk-minimal-cli-runtime.version}"
DSH_SDK_MINIMAL_MAX_TOKENS="${DSH_SDK_MINIMAL_MAX_TOKENS:-${HARBOR_MAX_TOKENS:-65536}}"
DSH_PROVIDER="${DSH_PROVIDER:-deepseek}"
DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"
DSH_CONTEXT_WINDOW="${DSH_CONTEXT_WINDOW:-200000}"
DSH_TEMPERATURE="${DSH_TEMPERATURE:-${HARBOR_TEMPERATURE:-1.0}}"
DSH_TOP_P="${DSH_TOP_P:-${HARBOR_TOP_P:-0.95}}"
DSH_RUNTIME_SOURCE_DIR="${DSH_RUNTIME_SOURCE_DIR:-$LOCAL_WHEEL_DIR}"
DSH_RUNTIME_MOUNT_PATH="${DSH_RUNTIME_MOUNT_PATH:-/opt/agent-fleet/dsh-runtime}"

harbor_dsh_validate_release() {
  local expected_version="0.1.3-alpha.1"
  local expected_ref="dsh-v0.1.3-alpha.1"
  local expected_sha="d347e703908d0406b7a7ef80e3a0e594d86b2215"

  if [[ "$DSH_SDK_MINIMAL_CLI_VERSION" != "$expected_version" \
    || "$DSH_SDK_MINIMAL_SOURCE_REF" != "$expected_ref" \
    || "$DSH_SDK_MINIMAL_SOURCE_SHA" != "$expected_sha" ]]; then
    echo "[ERROR] dsh-sdk-minimal currently supports only the pinned $expected_ref release." >&2
    return 1
  fi
}

if [[ "$HARBOR_MODEL" != "${DSH_PROVIDER}/"* ]]; then
  HARBOR_MODEL="${DSH_PROVIDER}/${HARBOR_MODEL}"
fi

harbor_dsh_runtime_version_ready() {
  local path="$1" expected
  expected="$(
    "$HARBOR_OPIK_PYTHON" "$HARBOR_DSH_DIR/prepare_dsh_sdk_minimal_runtime.py" \
      --print-runtime-version
  )" || return 1
  [[ -n "$expected" ]] && grep -Fqx -- "$expected" "$path"
}

harbor_dsh_cli_runtime_version_ready() {
  local path="$1" expected
  expected="$(
    DSH_CLI_VERSION="$DSH_SDK_MINIMAL_CLI_VERSION" \
      DSH_CLI_SOURCE_REF="$DSH_SDK_MINIMAL_SOURCE_REF" \
      DSH_CLI_SOURCE_SHA="$DSH_SDK_MINIMAL_SOURCE_SHA" \
      "$HARBOR_OPIK_PYTHON" \
      "$HARBOR_DSH_DIR/prepare_dsh_sdk_minimal_cli_runtime.py" \
      --print-runtime-version
  )" || return 1
  [[ -n "$expected" ]] && grep -Fqx -- "$expected" "$path"
}

harbor_dsh_cache_ready() {
  harbor_tar_file_ready "$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_PYTHON_RUNTIME_BASENAME" \
    && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_RUNTIME_BASENAME" \
    && harbor_dsh_runtime_version_ready \
      "$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_RUNTIME_VERSION_BASENAME" \
    && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_CLI_RUNTIME_BASENAME" \
    && harbor_dsh_cli_runtime_version_ready \
      "$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_CLI_RUNTIME_VERSION_BASENAME"
}

harbor_dsh_prepare_runtime() {
  WHEEL_DIR="$LOCAL_WHEEL_DIR" \
    DSH_CLI_VERSION="$DSH_SDK_MINIMAL_CLI_VERSION" \
    DSH_CLI_SOURCE_REF="$DSH_SDK_MINIMAL_SOURCE_REF" \
    DSH_CLI_SOURCE_SHA="$DSH_SDK_MINIMAL_SOURCE_SHA" \
    DSH_CLI_RUNTIME_BASENAME="$DSH_SDK_MINIMAL_CLI_RUNTIME_BASENAME" \
    DSH_CLI_RUNTIME_VERSION_FILE="$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_CLI_RUNTIME_VERSION_BASENAME" \
    NPM_CONFIG_REGISTRY="$NPM_CONFIG_REGISTRY" \
    "$HARBOR_OPIK_PYTHON" "$HARBOR_DSH_DIR/prepare_dsh_sdk_minimal_cli_runtime.py" \
      2>&1 | tee -a "$LOCAL_DEPS_LOG_FILE" || return 1

  WHEEL_DIR="$LOCAL_WHEEL_DIR" \
    DSH_SDK_MINIMAL_SOURCE_REF="$DSH_SDK_MINIMAL_SOURCE_REF" \
    DSH_SDK_MINIMAL_SOURCE_SHA="$DSH_SDK_MINIMAL_SOURCE_SHA" \
    DSH_SDK_MINIMAL_RUNTIME_BASENAME="$DSH_SDK_MINIMAL_RUNTIME_BASENAME" \
    DSH_SDK_MINIMAL_RUNTIME_VERSION_FILE="$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_RUNTIME_VERSION_BASENAME" \
    DSH_SDK_MINIMAL_PYTHON_RUNTIME_TARBALL="$LOCAL_WHEEL_DIR/$DSH_SDK_MINIMAL_PYTHON_RUNTIME_BASENAME" \
    "$HARBOR_OPIK_PYTHON" "$HARBOR_DSH_DIR/prepare_dsh_sdk_minimal_runtime.py" \
      2>&1 | tee -a "$LOCAL_DEPS_LOG_FILE"
}

harbor_dsh_disable_wheel_transport() {
  : > "$EFFECTIVE_WHEEL_URL_FILE"
  : > "$EFFECTIVE_CLAUDE_TGZ_URL_FILE"
  unset HARBOR_LOCAL_WHEEL_SERVER_URL HARBOR_LOCAL_CLAUDE_TGZ_URL
}
