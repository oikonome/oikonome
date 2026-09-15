#!/bin/sh
# host-side watcher for the admin console's Reboot button.
# Run ONCE as root:  sudo sh scripts/install-host-reboot-watcher.sh <install-root>
# The console writes <install>/ops-cmd/reboot-requested; the path-unit runs
# a checker that validates the flag, removes it, and reboots.
#
# The ops-cmd drop-box is chowned to the
# CONTAINER's uid at 0700 (never world-writable 1777) so ONLY the app/worker
# container can drop a flag — arbitrary local users and neighbour
# containers can't. The checker additionally (a) rejects a flag not owned
# by that uid and (b) refuses to reboot if the box rebooted in the last 10
# minutes (a compromised container could otherwise loop-reboot the host).
# OIKONOME_REBOOT_UID overrides the container uid (default 10001, the
# Dockerfile's app user; correct for rootful docker, where container uid ==
# host uid).
set -eu
ROOT=${1:?usage: install-host-reboot-watcher.sh <install-root>}
ROOT=$(cd "$ROOT" && pwd)
UID_C=${OIKONOME_REBOOT_UID:-10001}
mkdir -p "$ROOT/ops-cmd"
chown "$UID_C":"$UID_C" "$ROOT/ops-cmd"
chmod 0700 "$ROOT/ops-cmd"

# the check logic lives in a real script — inline ExecStart shell was a
# trap twice over (systemd %-specifier expansion ate `stat -c %Y`, and
# heredoc escaping made it unreviewable). $UID_C is baked in at install.
cat > /usr/local/bin/oikonome-reboot-check <<SCRIPT
#!/bin/sh
set -u
ROOT="\$1"
UID_C=$UID_C
f="\$ROOT/ops-cmd/reboot-requested"
[ -f "\$f" ] || exit 0
owner=\$(stat -c %u "\$f")
mtime=\$(stat -c %Y "\$f")
rm -f "\$f"
# the flag must have been written BY THE CONTAINER (its uid), not some
# other local user who managed to reach the dir
if [ "\$owner" != "\$UID_C" ]; then
    echo "reboot flag owned by uid \$owner (not \$UID_C) — ignored"
    exit 0
fi
now=\$(date +%s)
age=\$((now - mtime))
if [ "\$age" -gt 300 ]; then
    echo "stale reboot flag ignored (age \${age}s)"
    exit 0
fi
# loop-DoS guard: at most one console reboot per 10 min. The stamp lives
# in ops-state (persists across the reboot), so a flag written right after
# the box comes back is suppressed.
stamp="\$ROOT/ops-state/.last-console-reboot"
if [ -f "\$stamp" ]; then
    since=\$((now - \$(stat -c %Y "\$stamp")))
    if [ "\$since" -lt 600 ]; then
        echo "reboot suppressed: last console reboot \${since}s ago"
        exit 0
    fi
fi
mkdir -p "\$ROOT/ops-state"; : > "\$stamp"
wall "Oikonome operator requested a reboot (60s grace)"
# grace period: signed-in users are watching a countdown the console set
sleep 60
systemctl reboot
SCRIPT
chmod 755 /usr/local/bin/oikonome-reboot-check

# Namespace the units per install dir — a host with several instances (one
# on 8042, a second on 8043) would otherwise have the second install
# clobber the first's shared-name unit, so only one instance's reboot button
# works.
TAG=$(basename "$ROOT" | tr -cd 'a-zA-Z0-9-')
SVC="oikonome-reboot-watcher-$TAG"
cat > "/etc/systemd/system/$SVC.service" <<UNIT
[Unit]
Description=Oikonome console-requested host reboot ($TAG)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/oikonome-reboot-check $ROOT
UNIT
cat > "/etc/systemd/system/$SVC.path" <<UNIT
[Unit]
Description=Watch for Oikonome console reboot requests ($TAG)
[Path]
PathExists=$ROOT/ops-cmd/reboot-requested
Unit=$SVC.service
[Install]
WantedBy=multi-user.target
UNIT
# retire the shared-name unit older installs left behind
systemctl disable --now oikonome-reboot-watcher.path 2>/dev/null || true
rm -f /etc/systemd/system/oikonome-reboot-watcher.path \
      /etc/systemd/system/oikonome-reboot-watcher.service
systemctl daemon-reload
systemctl reset-failed "$SVC.service" "$SVC.path" 2>/dev/null || true
systemctl enable --now "$SVC.path"
echo "reboot watcher installed for $ROOT (unit $SVC, drop-box uid $UID_C, 0700)"
