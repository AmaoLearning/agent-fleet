#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"

OUT="${1:-$LAYOUT_FILE}"
worker_command="./run_harbor_worker.sh"
if [[ "$HARBOR_NATIVE_CONCURRENCY" == "1" ]]; then
  TOTAL_WORKERS=1
  worker_command="./run_harbor_registry.sh"
fi

emit_worker_pane() {
  local worker_id="$1"
  cat >> "$OUT" <<EOF
        pane {
          command "$worker_command"
          args "$worker_id"
          close_on_exit true
        }
EOF
}

mkdir -p "$(dirname "$OUT")"

cat > "$OUT" <<'EOF'
layout {
  default_tab_template {
    pane size=1 borderless=true {
      plugin location="zellij:tab-bar"
    }
    children
    pane size=2 borderless=true {
      plugin location="zellij:status-bar"
    }
  }

  tab name="overview" focus=true {
    pane split_direction="vertical" {
      pane size="50%" {
        command "./monitor_harbor.sh"
        close_on_exit true
      }
      pane size="50%" split_direction="horizontal" {
EOF

overview_end=5
if [[ "$TOTAL_WORKERS" -lt "$overview_end" ]]; then
  overview_end="$TOTAL_WORKERS"
fi

for i in $(seq 1 "$overview_end"); do
  emit_worker_pane "$i"
done

cat >> "$OUT" <<'EOF'
      }
    }
  }
EOF

start=6
while [[ "$start" -le "$TOTAL_WORKERS" ]]; do
  end=$((start + 9))
  if [[ "$end" -gt "$TOTAL_WORKERS" ]]; then
    end="$TOTAL_WORKERS"
  fi

  cat >> "$OUT" <<EOF
  tab name="workers-${start}-${end}" {
    pane split_direction="vertical" {
      pane size="50%" split_direction="horizontal" {
EOF

  left_end=$((start + 4))
  if [[ "$left_end" -gt "$end" ]]; then
    left_end="$end"
  fi
  for i in $(seq "$start" "$left_end"); do
    emit_worker_pane "$i"
  done

  cat >> "$OUT" <<'EOF'
      }
      pane size="50%" split_direction="horizontal" {
EOF

  right_start=$((start + 5))
  if [[ "$right_start" -le "$end" ]]; then
    for i in $(seq "$right_start" "$end"); do
      emit_worker_pane "$i"
    done
  fi

  cat >> "$OUT" <<'EOF'
      }
    }
  }
EOF

  start=$((end + 1))
done

cat >> "$OUT" <<'EOF'
}
EOF

echo "Wrote layout to $OUT"
