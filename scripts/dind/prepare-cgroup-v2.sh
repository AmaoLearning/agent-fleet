#!/usr/bin/env bash
# Prepare a cgroup v2 hierarchy for containers launched by nested dockerd.
set -euo pipefail

cgroup_root="${1:-/sys/fs/cgroup}"
max_attempts=100
retry_delay=0.01

if [[ ! -f "$cgroup_root/cgroup.controllers" ]]; then
  exit 0
fi

controllers=()
read -r -a controllers < "$cgroup_root/cgroup.controllers" || true
if (( ${#controllers[@]} == 0 )); then
  exit 0
fi

controller_directives=("${controllers[@]/#/+}")
controller_line="${controller_directives[*]}"

mkdir -p "$cgroup_root/init"

# A non-root cgroup cannot both contain processes and distribute domain
# controllers to children. Move DinD's own processes into /init first, then
# enable every delegated controller for the nested Docker hierarchy.
#
# Repeat because an outer `docker exec` can add a process while the initial
# process list is being drained. Bound the retries so a persistent cgroup
# write failure cannot block dockerd startup forever.
for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  xargs -r -n 1 < "$cgroup_root/cgroup.procs" \
    > "$cgroup_root/init/cgroup.procs" || :

  if {
    printf '%s\n' "$controller_line" \
      > "$cgroup_root/cgroup.subtree_control"
  } 2>/dev/null; then
    exit 0
  fi

  if (( attempt < max_attempts )); then
    sleep "$retry_delay"
  fi
done

printf 'failed to enable cgroup v2 controllers after %d attempts: %s\n' \
  "$max_attempts" "$controller_line" >&2
exit 1
