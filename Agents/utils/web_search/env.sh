#!/usr/bin/env bash
set -euo pipefail

# Web search reuses the MCP switch and the normal dataset path override.
# Sourcing configuration never downloads data or generates tasks.
if [[ "${HARBOR_CC_WEB_MCP_ENABLED:-0}" == "1" ]]; then
  case "$DATASET_NAME" in
    browsecomp|deepsearchqa)
      DATASET_PATH="${DATASET_PATH:-$AGENT_FLEET_CACHE_DIR/web-search/tasks/$DATASET_NAME}"
      ;;
  esac
fi

harbor_prepare_web_search_dataset() {
  local name="${1:-$DATASET_NAME}" destination="${2:-$DATASET_PATH}"
  [[ "${HARBOR_CC_WEB_MCP_ENABLED:-0}" == "1" ]] || return 0
  case "$name" in browsecomp|deepsearchqa) ;; *) return 0 ;; esac
  [[ ! -d "$destination" ]] || return 0
  PYTHONPATH="$REPO_ROOT/Agents/utils/web_search/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$HARBOR_OPIK_PYTHON" -m web_search_adapter.prepare "$name" "$destination" \
    --source-dir "$AGENT_FLEET_CACHE_DIR/web-search/sources" \
    --image "${HARBOR_OPENSANDBOX_IMAGE_REF:-python:3.12-slim}"
}
