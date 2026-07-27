#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=fleet_spec_io.sh
source "$SCRIPT_DIR/fleet_spec_io.sh"

err() { printf '[ERROR] %s\n' "$*" >&2; }

INPUTS=()
OUTPUT=""
DETACH=0
DRY_RUN=0
SPEC_SEEN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--spec) SPEC_SEEN=1; shift ;;
    -)
      (( SPEC_SEEN )) || { err "--spec must be provided before its inputs"; exit 2; }
      INPUTS[${#INPUTS[@]}]="$1"; shift
      ;;
    -o|--output)
      [[ $# -ge 2 && -n "$2" ]] || { err "--output requires a non-empty file path"; exit 2; }
      if fleet_spec_is_option_shaped "$2"; then
        err "--output requires a file path; use ./$2 for a file literally named $2"
        exit 2
      fi
      OUTPUT="$2"; shift 2
      ;;
    -d|--detach) DETACH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -t|--taskset|--task|-a|--agent|-n|--workers)
      err "--spec cannot be combined with --taskset, --task, --agent, or --workers"
      exit 2
      ;;
    -h|--help) exec bash "$SCRIPT_DIR/run_fleet.sh" --help ;;
    -*) err "unknown --spec option: $1"; exit 2 ;;
    *)
      (( SPEC_SEEN )) || { err "--spec must be provided before its inputs"; exit 2; }
      INPUTS[${#INPUTS[@]}]="$1"; shift
      ;;
  esac
done

(( SPEC_SEEN )) || { err "fleet_spec.sh is an internal --spec dispatcher"; exit 2; }
[[ ${#INPUTS[@]} -gt 0 ]] || { err "--spec requires at least one file path or -"; exit 2; }
fleet_spec_validate_output_path "$OUTPUT" || exit $?
fleet_spec_load_many "${INPUTS[@]}" || exit $?

if [[ -n "$OUTPUT" ]]; then
  if (( FLEET_SPEC_COUNT == 1 )); then
    fleet_spec_write "$OUTPUT" "$(jq -c '.[0]' <<<"$FLEET_SPECS_JSON")"
  else
    fleet_spec_write "$OUTPUT" "$FLEET_SPECS_JSON"
  fi
fi

if (( FLEET_SPEC_COUNT > 1 )); then
  (( DETACH == 0 )) || printf '[INFO] --detach is implicit for multi-run spec input\n' >&2
  batch_input="$(mktemp "${TMPDIR:-/tmp}/fleet-specs.XXXXXX")"
  trap 'rm -f -- "$batch_input"' EXIT HUP INT TERM
  printf '%s\n' "$FLEET_SPECS_JSON" >"$batch_input"
  exec 3<"$batch_input"
  rm -f -- "$batch_input"
  trap - EXIT HUP INT TERM
  batch_args=(--spec /dev/fd/3)
  (( DRY_RUN == 0 )) || batch_args+=(--dry-run)
  exec bash "$SCRIPT_DIR/fleet_batch.sh" "${batch_args[@]}"
fi

FLEET_SPEC_JSON="$(jq -c '.[0]' <<<"$FLEET_SPECS_JSON")"
TASKSET="$(jq -r '.taskset' <<<"$FLEET_SPEC_JSON")"
run_args=(--taskset "$TASKSET")
if jq -e 'has("task")' <<<"$FLEET_SPEC_JSON" >/dev/null; then
  run_args+=("--task=$(jq -r '.task' <<<"$FLEET_SPEC_JSON")")
fi
if jq -e 'has("agent")' <<<"$FLEET_SPEC_JSON" >/dev/null; then
  run_args+=(--agent "$(jq -r '.agent' <<<"$FLEET_SPEC_JSON")")
fi
if jq -e 'has("workers")' <<<"$FLEET_SPEC_JSON" >/dev/null; then
  run_args+=(--workers "$(jq -r '.workers' <<<"$FLEET_SPEC_JSON")")
fi
(( DETACH == 0 )) || run_args+=(--detach)
(( DRY_RUN == 0 )) || run_args+=(--dry-run)
exec bash "$SCRIPT_DIR/run_fleet.sh" "${run_args[@]}"
