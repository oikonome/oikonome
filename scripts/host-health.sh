#!/bin/sh
# host security/health snapshot for the admin console. The
# console runs inside a container and can't see the host's apt state, so
# a host timer writes this JSON into <install>/ops-state/ (bind-mounted
# read-only at /state/ops). Run from cron/systemd, e.g. every 30 min:
#   */30 * * * *  /path/to/oikonome/scripts/host-health.sh /path/to/oikonome
# Works on Debian/Ubuntu (apt); other hosts get uptime/disk only.
set -eu
ROOT=${1:?usage: host-health.sh <install-root>}
OUT="$ROOT/ops-state/host-health.json"
mkdir -p "$ROOT/ops-state"

updates=0; security=0
if command -v apt-get >/dev/null 2>&1; then
    sim=$(apt-get -s upgrade 2>/dev/null || true)
    updates=$(printf '%s\n' "$sim" | grep -c '^Inst ' || true)
    security=$(printf '%s\n' "$sim" | grep '^Inst ' | grep -ci security || true)
fi
reboot=false; reboot_pkgs=""
if [ -f /var/run/reboot-required ]; then
    reboot=true
    reboot_pkgs=$(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null || true)
fi
up=$(cut -d. -f1 /proc/uptime)
disk=$(df -P "$ROOT" | awk 'NR==2 {print $5}' | tr -d '%')

# every oikonome container on this host (instances share the pane)
containers="[]"
ENG=""
command -v docker >/dev/null 2>&1 && ENG=docker
[ -z "$ENG" ] && command -v podman >/dev/null 2>&1 && ENG=podman
if [ -n "$ENG" ]; then
    containers=$($ENG ps -a --format '{{.Names}}	{{.State}}	{{.Status}}' 2>/dev/null \
      | grep -i oikonome | sort | awk -F'	' '
        BEGIN { printf "[" ; first=1 }
        { gsub(/"/, "", $0)
          if (!first) printf ","
          first=0
          printf "{\"name\":\"%s\",\"state\":\"%s\",\"status\":\"%s\"}", $1, $2, $3 }
        END { printf "]" }')
    [ -n "$containers" ] || containers="[]"
fi

# per-container cpu/mem for the console's infra-stress panel.
# One --no-stream sample; numbers parsed loosely (docker prints "1.23%"
# and "150MiB / 1.9GiB", podman close enough). Failure leaves "[]".
container_stats="[]"
if [ -n "$ENG" ]; then
    container_stats=$($ENG stats --no-stream --format '{{.Name}}	{{.CPUPerc}}	{{.MemUsage}}' 2>/dev/null \
      | grep -i oikonome | sort | awk -F'	' '
        BEGIN { printf "[" ; first=1 }
        { gsub(/"/, "", $0); gsub(/%/, "", $2)
          split($3, mu, " / ")
          if (!first) printf ","
          first=0
          printf "{\"name\":\"%s\",\"cpu_pct\":%s,\"mem\":\"%s\"}", $1, ($2==""?"null":$2), mu[1] }
        END { printf "]" }')
    [ -n "$container_stats" ] || container_stats="[]"
fi

cat > "$OUT.tmp" <<JSON
{"generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "updates_pending": ${updates:-0},
 "security_pending": ${security:-0},
 "reboot_required": $reboot,
 "reboot_pkgs": "$(printf '%s' "$reboot_pkgs" | sed 's/"/\\"/g')",
 "uptime_seconds": $up,
 "disk_used_pct": ${disk:-0},
 "containers": $containers,
 "container_stats": $container_stats}
JSON
mv "$OUT.tmp" "$OUT"
