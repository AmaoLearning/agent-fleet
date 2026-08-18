#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/dataset/0/environment" "$tmp/home"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uv"
chmod +x "$tmp/bin/uv"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uvx"
chmod +x "$tmp/bin/uvx"
printf '#!/usr/bin/env bash\necho "ELF 64-bit executable"\n' > "$tmp/bin/file"
chmod +x "$tmp/bin/file"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/0/task.toml"
printf 'FROM ubuntu:24.04\n' > "$tmp/dataset/0/environment/Dockerfile"

run_dry() {
  local sbx_api_key="$1"
  local qz_template="$2"
  local qz_timeout="${3:-}"
  local agent="${4:-oracle}"
  local force_build="${5:-0}"
  local e2b_api_key="${6:-}"
  env -i \
    AGENT="$agent" \
    TB_FORCE_BUILD="$force_build" \
    QZ_SANDBOX_TIMEOUT_SEC="$qz_timeout" \
    PATH="$tmp/bin:/usr/bin:/bin" \
    HOME="$tmp/home" \
    DATASET_NAME=auto \
    DATASET_PATH="$tmp/dataset" \
    INCLUDE_TASKS=0 \
    OUTPUT_PATH="$tmp/output" \
    TB_DRY_RUN=1 \
    TB_N_CONCURRENT=1 \
    TB_MAX_RETRIES=0 \
    TB_ENVIRONMENT_TYPE=qz \
    SBX_API_KEY="$sbx_api_key" \
    E2B_API_KEY="$e2b_api_key" \
    QZ_SANDBOX_TEMPLATE="$qz_template" \
    bash "$HARBOR_DIR/harboropik.sh" 2>&1
}

# A configured qz run passes the adapter import path and nothing docker- or
# opensandbox-specific.
qz_run="$(run_dry sbx_fake_key fake_template)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' <<< "$qz_run" >/dev/null
if [[ "$(grep -oF -- '--env qz_e2b_sandbox:QzSandboxEnvironment' \
  <<< "$qz_run" | wc -l | tr -d ' ')" != "1" ]]; then
  echo 'qz command must contain exactly one qz environment argument' >&2
  exit 1
fi
if grep -F -- '--extra-docker-compose' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains a Docker compose overlay' >&2
  exit 1
fi
if grep -F -- '--ek image_ref=' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains OpenSandbox image arguments' >&2
  exit 1
fi
if grep -F -- '--mounts-json' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains host bind mounts' >&2
  exit 1
fi

# A missing key must fail launch validation before Harbor runs.
if missing_key="$(run_dry '' fake_template)"; then
  echo 'qz launch unexpectedly succeeded without an API key' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires SBX_API_KEY' <<< "$missing_key" >/dev/null
fi

# A legacy E2B_API_KEY is accepted only when it is visibly a qz sbx_ key. An
# ambient cloud-E2B key must fail before Harbor can send it to the qz endpoint.
legacy_key_run="$(run_dry '' fake_template '' oracle 0 sbx_legacy_key)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' \
  <<< "$legacy_key_run" >/dev/null
if cloud_key_run="$(run_dry '' fake_template '' oracle 0 e2b_cloud_key)"; then
  echo 'qz launch unexpectedly accepted an ambient cloud-E2B API key' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires SBX_API_KEY' <<< "$cloud_key_run" >/dev/null
fi

# A missing template must fail launch validation before Harbor runs.
if missing_template="$(run_dry sbx_fake_key '')"; then
  echo 'qz launch unexpectedly succeeded without a template' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires QZ_SANDBOX_TEMPLATE' <<< "$missing_template" >/dev/null
fi

# An invalid timeout must fail launch validation before Harbor runs.
for bad_timeout in abc 0 14401; do
  if bad_run="$(run_dry sbx_fake_key fake_template "$bad_timeout")"; then
    echo "qz launch unexpectedly succeeded with QZ_SANDBOX_TIMEOUT_SEC=$bad_timeout" >&2
    exit 1
  else
    grep -F -- 'QZ_SANDBOX_TIMEOUT_SEC must be an integer between 1 and 14400' \
      <<< "$bad_run" >/dev/null
  fi
done

# A valid timeout passes through.
valid_run="$(run_dry sbx_fake_key fake_template 600)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' <<< "$valid_run" >/dev/null

# Non-oracle agents must fail launch validation: their delivery mechanisms
# cannot reach a qz sandbox yet.
for agent in claude-code opencode; do
  if agent_run="$(run_dry sbx_fake_key fake_template '' "$agent")"; then
    echo "qz launch unexpectedly succeeded with AGENT=$agent" >&2
    exit 1
  else
    grep -F -- 'qz currently supports AGENT=oracle only' <<< "$agent_run" >/dev/null
  fi
done

# force_build has no meaning for platform-registered templates.
for force_build in 1 true; do
  if fb_run="$(run_dry sbx_fake_key fake_template '' oracle "$force_build")"; then
    echo "qz launch unexpectedly succeeded with TB_FORCE_BUILD=$force_build" >&2
    exit 1
  else
    grep -F -- 'TB_FORCE_BUILD is not supported on qz' <<< "$fb_run" >/dev/null
  fi
done

echo 'test_harboropik_qz.sh passed'
