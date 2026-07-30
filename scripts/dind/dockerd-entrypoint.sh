#!/bin/sh
# Start dockerd for the DinD path, or pass an explicit command through unchanged.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ]; then
  set -- dockerd \
    --host=unix:///var/run/docker.sock \
    "$@"
fi

if [ "${1:-}" = dockerd ]; then
  "$SCRIPT_DIR/prepare-cgroup-v2.sh"
  find /run /var/run -iname 'docker*.pid' -delete 2>/dev/null || true
fi

exec "$@"
