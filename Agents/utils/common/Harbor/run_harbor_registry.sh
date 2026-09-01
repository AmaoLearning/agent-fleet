#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"

set +e
if [[ "${HARBOR_DRY_RUN:-0}" == "1" ]]; then
  "$SCRIPT_DIR/harboropik.sh"
  status="$?"
elif harbor_prepare_agent_runtime; then
  "$SCRIPT_DIR/harboropik.sh"
  status="$?"
else
  echo "failed to prepare registry agent runtime" >&2
  status=1
fi
set -e

show_registry_summary() {
  echo
  if [[ -f "$OUTPUT_PATH/summary.txt" ]]; then
    cat "$OUTPUT_PATH/summary.txt"
  else
    echo "summary unavailable: $OUTPUT_PATH/summary.txt"
  fi
}

record_registry_exit() {
  local status="$1"
  local exit_dir exit_tmp

  exit_dir="$(dirname "$HARBOR_BENCHMARK_EXIT_FILE")"
  exit_tmp="${HARBOR_BENCHMARK_EXIT_FILE}.tmp.${BASHPID:-$$}"
  if ! mkdir -p "$exit_dir"; then
    echo "failed to create Harbor completion directory: $exit_dir" >&2
    return 1
  fi
  if ! printf '%s\n' "$status" > "$exit_tmp"; then
    rm -f -- "$exit_tmp" || true
    echo "failed to write Harbor completion status: $exit_tmp" >&2
    return 1
  fi
  if [[ -d "$HARBOR_BENCHMARK_EXIT_FILE" ]]; then
    rm -f -- "$exit_tmp" || true
    echo "Harbor completion target is a directory: $HARBOR_BENCHMARK_EXIT_FILE" >&2
    return 1
  fi
  if ! mv -f -- "$exit_tmp" "$HARBOR_BENCHMARK_EXIT_FILE"; then
    rm -f -- "$exit_tmp" || true
    echo "failed to publish Harbor completion status: $HARBOR_BENCHMARK_EXIT_FILE" >&2
    return 1
  fi
  return 0
}

# A zero process status is complete only when the summary writer found the
# aggregate Harbor result. Keep incomplete and failed panes available so the
# error that preceded the summary cannot disappear behind "Bye from Zellij!".
if [[ "$status" -eq 0 ]] &&
   ! grep -qx 'status:      complete' "$OUTPUT_PATH/summary.txt" 2>/dev/null; then
  status=1
fi

# harboropik.sh records its own exit after it starts, but registry runtime
# preparation happens before that script and can fail without triggering its
# EXIT trap. Publish the wrapper's normalized terminal status before any pane
# is held open so foreground and detached controllers always see completion.
skip_failure_pane_hold=0
if ! record_registry_exit "$status"; then
  echo "[WARN] Harbor completion status could not be published; continuing failure diagnostics" >&2
  if [[ "$status" -eq 0 ]]; then
    status=1
    skip_failure_pane_hold=1
  fi
fi

if [[ "$status" -ne 0 ]]; then
  show_registry_summary
  if [[ "${HARBOR_ZELLIJ_KEEP_ON_FAILURE:-1}" == "1" ]]; then
    if [[ "$skip_failure_pane_hold" == "1" ]]; then
      echo
      echo "Harbor completed, but its completion status could not be published; not keeping this pane open."
    else
      echo
      echo "Harbor failed; keeping this pane open for diagnostics."
      echo "Press Ctrl-q to leave Zellij after reviewing the error above."
      while true; do
        sleep 3600
      done
    fi
  fi
  exit "$status"
fi

if [[ "$HARBOR_ZELLIJ_CLOSE_ON_COMPLETE" != "1" ]]; then
  show_registry_summary
  echo "HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0; keeping final registry pane open"
  while true; do
    sleep 3600
  done
fi

exit "$status"
