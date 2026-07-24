#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M)-harbor-tui}"
. "$SCRIPT_DIR/env.sh"

if [[ "$ROLLOUT" == "1" && "${FLEET_BATCH_HARBOR_RUNS:-1}" != "1" ]]; then
  printf '[ERROR] ROLLOUT=1 supports only one Harbor run per Batch; rollout listeners share RL_PORT=%s\n' \
    "$RL_PORT" >&2
  exit 2
fi

DETACH_MODE=false
if [[ "${1:-}" == "--detach" ]]; then
  DETACH_MODE=true
  shift
fi
if [[ -z "$HARBOR_ZELLIJ_KEEP_ON_FAILURE" ]]; then
  if [[ "$DETACH_MODE" == "true" || ( -t 0 && -t 1 ) ]]; then
    HARBOR_ZELLIJ_KEEP_ON_FAILURE=1
  else
    HARBOR_ZELLIJ_KEEP_ON_FAILURE=0
  fi
fi
case "$HARBOR_ZELLIJ_KEEP_ON_FAILURE" in
  0|1) export HARBOR_ZELLIJ_KEEP_ON_FAILURE ;;
  *)
    printf '[ERROR] HARBOR_ZELLIJ_KEEP_ON_FAILURE must be 0 or 1\n' >&2
    exit 2
    ;;
esac

# Explicit names still win for normal benchmark zellij sessions.
ZELLIJ_SESSION_NAME="${ZELLIJ_SESSION_NAME:-${RL_ZELLIJ_SESSION_NAME:-$HARBOR_ZELLIJ_SESSION_NAME}}"

harbor_print_run_receipt() {
  printf '[RUN] status: starting\n'
  printf '[RUN] RUN_ID: %s\n' "$RUN_ID"
  printf '[RUN] Zellij session: %s\n' "$ZELLIJ_SESSION_NAME"
  printf '[RUN] output: %s\n' "$OUTPUT_PATH"
  printf '[RUN] summary: %s/summary.txt\n' "$OUTPUT_PATH"
}

harbor_report_foreground_result() {
  local zellij_status="$1"
  local benchmark_status=""

  echo
  if [[ -f "$OUTPUT_PATH/summary.txt" ]]; then
    cat "$OUTPUT_PATH/summary.txt"
  else
    echo "[ERROR] summary unavailable: $OUTPUT_PATH/summary.txt" >&2
  fi

  if harbor_uses_registry_dataset; then
    if [[ -s "$HARBOR_BENCHMARK_EXIT_FILE" ]]; then
      benchmark_status="$(cat "$HARBOR_BENCHMARK_EXIT_FILE" 2>/dev/null || true)"
    fi
    if [[ "$benchmark_status" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]; then
      return "$benchmark_status"
    fi
  elif [[ -f "$OUTPUT_PATH/summary.txt" && "$zellij_status" -eq 0 ]]; then
    return 0
  fi
  if [[ "$zellij_status" -ne 0 ]]; then
    echo "[ERROR] Zellij exited with status $zellij_status before Harbor recorded completion." >&2
    return "$zellij_status"
  fi
  echo "[ERROR] Zellij ended before Harbor recorded a completion status." >&2
  return 1
}

harbor_stop_rollout_zellij_sessions() {
  local session
  while IFS= read -r session; do
    case "$session" in
      harbor-rollout-*)
        zellij kill-session "$session" >/dev/null 2>&1 || true
        zellij delete-session "$session" >/dev/null 2>&1 || true
        ;;
    esac
  done < <(zellij list-sessions --short 2>/dev/null || true)
}

ensure_zellij_web_sharing_config() {
  local config_file="${ZELLIJ_CONFIG_FILE:-$HOME/.config/zellij/config.kdl}"
  mkdir -p "$(dirname "$config_file")"
  if [[ -f "$config_file" ]] && grep -qE '^[[:space:]]*web_sharing[[:space:]]+' "$config_file"; then
    sed -i -E 's/^[[:space:]]*web_sharing[[:space:]]+".*"$/web_sharing "on"/' "$config_file"
  else
    printf '\nweb_sharing "on"\n' >> "$config_file"
  fi
}

harbor_start_online_analysis_if_enabled() {
  if [[ "$HARBOR_ONLINE_ANALYSIS" != "1" ]]; then
    return 0
  fi
  if [[ -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_ONLINE_ANALYSIS_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      return 0
    fi
  fi

  mkdir -p "$HARBOR_ONLINE_ANALYSIS_DIR" "$RUNTIME_DIR"
  local online_analysis_profile
  online_analysis_profile="$(harbor_dataset_kind)"
  nohup setsid python3 "$SCRIPT_DIR/scripts/online_rule_analyzer.py" \
    "$OUTPUT_PATH" \
    --follow \
    --profile "$online_analysis_profile" \
    --poll-interval "$HARBOR_ONLINE_ANALYSIS_POLL_INTERVAL" \
    --output-dir "$HARBOR_ONLINE_ANALYSIS_DIR" \
    >>"$HARBOR_ONLINE_ANALYSIS_LOG_FILE" 2>&1 &
  printf '%s\n' "$!" >"$HARBOR_ONLINE_ANALYSIS_PID_FILE"
}

harbor_start_monitor_if_enabled() {
  [[ "$HARBOR_MONITOR_ENABLED" == "1" ]] || return 0
  [[ "$ROLLOUT" != "1" ]] || return 0
  if ! harbor_uses_registry_dataset && [[ ! -s "$TASK_FILE" ]]; then
    echo "[ERROR] cannot start Harbor monitor without a materialized task file: $TASK_FILE" >&2
    return 1
  fi
  mkdir -p "$HARBOR_MONITOR_DIR" "$RUNTIME_DIR"
  (
  flock 9
  if [[ -f "$HARBOR_MONITOR_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_MONITOR_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      if harbor_monitor_pid_matches_run "$existing_pid"; then
        exit 0
      fi
      echo "[ERROR] refusing to replace unrelated process from $HARBOR_MONITOR_PID_FILE: pid=$existing_pid" >&2
      exit 1
    fi
    rm -f "$HARBOR_MONITOR_PID_FILE"
  fi

  local -a monitor_args=(
    --run-dir "$OUTPUT_PATH"
    --queue-dir "$QUEUE_DIR"
    --agent "$AGENT"
    --output "$HARBOR_MONITOR_DIR/monitor-latest.json"
    --user-report-output "$HARBOR_MONITOR_DIR/user-notify-latest.json"
    --analyzer-handover-output "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json"
    --runner-action-output "$HARBOR_MONITOR_DIR/runner-action-latest.json"
    --follow
    --interval "$HARBOR_MONITOR_INTERVAL"
    --startup-grace "$HARBOR_MONITOR_STARTUP_GRACE"
    --stall-seconds "$HARBOR_MONITOR_STALL_SECONDS"
    --max-retries "$HARBOR_MONITOR_MAX_RETRIES"
  )
  if harbor_uses_registry_dataset; then
    monitor_args+=(
      --harbor-job-dir-file "$HARBOR_JOB_DIR_FILE"
      --harbor-pid-file "$HARBOR_BENCHMARK_PID_FILE"
      --harbor-exit-file "$HARBOR_BENCHMARK_EXIT_FILE"
    )
  else
    monitor_args+=(--task-file "$TASK_FILE")
  fi
  if [[ -n "$HARBOR_MONITOR_CONFIGURED_TIMEOUT" ]]; then
    monitor_args+=(--configured-timeout "$HARBOR_MONITOR_CONFIGURED_TIMEOUT")
  fi
  if [[ -n "$HARBOR_MONITOR_RESTART_CMD" ]]; then
    monitor_args+=(--restart-cmd "$HARBOR_MONITOR_RESTART_CMD")
  fi
  if [[ -n "$HARBOR_MONITOR_STOP_CMD" ]]; then
    monitor_args+=(--stop-cmd "$HARBOR_MONITOR_STOP_CMD")
  fi
  nohup setsid python3 "$SCRIPT_DIR/scripts/monitor.py" "${monitor_args[@]}" 9>&- \
    >>"$HARBOR_MONITOR_LOG_FILE" 2>&1 &
  local monitor_pid="$!"
  printf '%s\n' "$monitor_pid" > "$HARBOR_MONITOR_PID_FILE"
  for _ in $(seq 1 50); do
    [[ -f "$HARBOR_MONITOR_DIR/monitor-latest.json" ]] && exit 0
    if ! kill -0 "$monitor_pid" >/dev/null 2>&1; then
      echo "[ERROR] Harbor monitor exited during startup; see $HARBOR_MONITOR_LOG_FILE" >&2
      exit 1
    fi
    sleep 0.1
  done
  echo "[ERROR] Harbor monitor did not produce a startup sample; see $HARBOR_MONITOR_LOG_FILE" >&2
  exit 1
  ) 9>"$RUNTIME_DIR/harbor-monitor.lock"
}

harbor_init_run_dirs
if [[ "$ROLLOUT" != "1" ]] && harbor_uses_registry_dataset; then
  : > "$HARBOR_JOB_DIR_FILE"
  rm -f "$HARBOR_BENCHMARK_PID_FILE" "$HARBOR_BENCHMARK_EXIT_FILE"
fi
if [[ "$ROLLOUT" != "1" ]]; then
  harbor_validate_agent
  harbor_ensure_dataset
else
  mkdir -p "$RL_TRIALS_DIR" "$RL_ACTIVE_DIR" "$RL_QUEUE_DIR/pending" "$RL_QUEUE_DIR/results" "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT" "$(dirname "$RL_TRACE_LOG")"
  touch "$RL_TRACE_LOG"
fi
if [[ "${RESET_RUN:-0}" == "1" ]]; then
  if [[ "$ROLLOUT" == "1" ]]; then
    "$RL_UTILS_DIR/run_rl_rollout_server.sh" --stop >/dev/null 2>&1 || true
    harbor_stop_rollout_zellij_sessions
  else
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  fi
  harbor_reset_run_state
  if [[ "$ROLLOUT" == "1" ]]; then
    rm -rf "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT" 2>/dev/null || true
    mkdir -p "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT"
    rm -f "$RL_ACTIVE_DIR"/*.json "$RL_QUEUE_DIR"/pending/*.json "$RL_QUEUE_DIR"/results/*.json "$RL_TRACE_LOG" "$RL_SERVER_LOG" 2>/dev/null || true
    touch "$RL_TRACE_LOG"
  fi
fi

if [[ $# -gt 0 ]]; then
  if [[ "$ROLLOUT" != "1" ]] && ! harbor_uses_registry_dataset; then
    harbor_prepare_task_file
  fi
  if [[ "$ROLLOUT" != "1" ]]; then
    harbor_start_online_analysis_if_enabled
    harbor_start_monitor_if_enabled
  fi
  exec "$@"
fi

if [[ "$ROLLOUT" != "1" ]] && ! harbor_uses_registry_dataset; then
  harbor_prepare_task_file
fi
if [[ "$ROLLOUT" != "1" ]]; then
  harbor_start_online_analysis_if_enabled
fi
cd "$SCRIPT_DIR"

if [[ "$ROLLOUT" == "1" ]]; then
  if [[ "$DETACH_MODE" == "true" ]]; then
    exec "$RL_UTILS_DIR/run_rl_rollout_server.sh" --detach
  fi
  exec "$RL_UTILS_DIR/run_rl_rollout_server.sh"
fi

if harbor_uses_registry_dataset; then
  "$SCRIPT_DIR/gen_harbor_registry_zellij_layout.sh" "$LAYOUT_FILE"
else
  "$SCRIPT_DIR/gen_harbor_zellij_layout.sh" "$LAYOUT_FILE"
fi
ensure_zellij_web_sharing_config

if [[ "$DETACH_MODE" == "true" ]]; then
  zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  zellij_cmd="$(printf 'stty rows 54 cols 172; exec zellij --session %q --new-session-with-layout %q' "$ZELLIJ_SESSION_NAME" "$LAYOUT_FILE")"
  nohup setsid env -u ZELLIJ_SESSION_NAME TERM=xterm-256color script -q \
    -c "$zellij_cmd" \
    "$RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.typescript" \
    >"$RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.log" 2>&1 &

  started=false
  for _ in $(seq 1 30); do
    if zellij list-sessions --short 2>/dev/null | grep -qx "$ZELLIJ_SESSION_NAME"; then
      started=true
      break
    fi
    sleep 1
  done

  if [[ "$started" != "true" ]]; then
    echo "failed to create zellij session: $ZELLIJ_SESSION_NAME" >&2
    echo "zellij log: $RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.log" >&2
    exit 1
  fi

  if ! harbor_start_monitor_if_enabled; then
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    exit 1
  fi

  harbor_print_run_receipt
  printf '[RUN] attach: zellij attach %s\n' "$ZELLIJ_SESSION_NAME"
  printf '%s\n' "$ZELLIJ_SESSION_NAME"
  exit 0
fi

harbor_start_monitor_if_enabled
harbor_print_run_receipt
zellij_status=0
env -u ZELLIJ_SESSION_NAME zellij \
  --session "$ZELLIJ_SESSION_NAME" \
  --new-session-with-layout "$LAYOUT_FILE" || zellij_status="$?"
final_status=0
harbor_report_foreground_result "$zellij_status" || final_status="$?"
exit "$final_status"
