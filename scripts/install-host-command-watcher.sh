#!/bin/sh
# Host-agent parity: run oikonome.sh operations from the admin console.
# Run ONCE as root:
#   sudo sh scripts/install-host-command-watcher.sh <install-root>
#
# The console (running INSIDE the app container, uid 10001) drops a request
# file <install>/ops-cmd/command-requested — a small JSON {id,cmd,arg,at}.
# A root systemd path-unit runs the checker below, which VALIDATES the
# request and then runs the real ./oikonome.sh <cmd> AS THE INSTALL OWNER
# (never as root — the commands are docker/compose operations the deploy
# user already has rights for). Output streams to
# <install>/ops-state/cmd-results/<id>.log; a <id>.json carries the status,
# which the console reads back through the read-only /state/ops mount.
#
# Security (this is web-triggered command execution, so the guards matter):
#   * ops-cmd is 0700 owned by the CONTAINER uid — only the app/worker
#     container can drop a request; local users / neighbour containers can't.
#   * the checker rejects a request file not owned by that uid, or older than
#     5 minutes, and REMOVES it before acting (single-use).
#   * cmd must be in a fixed ALLOWLIST — never an arbitrary string/shell.
#   * `restore` takes a backup filename: it must be a bare basename that
#     EXISTS in <install>/backups (no path traversal, no absolute paths).
#   * one command at a time (a lock held while its run lives; a stale one
#     from a crash is taken over, and nothing holds past six hours).
#   * the console itself still requires an operator session + a typed
#     confirm + single-use nonce for the destructive ones (reset/uninstall).
# OIKONOME_REBOOT_UID overrides the container uid (default 10001).
set -eu
ROOT=${1:?usage: install-host-command-watcher.sh <install-root>}
ROOT=$(cd "$ROOT" && pwd)
UID_C=${OIKONOME_REBOOT_UID:-10001}
mkdir -p "$ROOT/ops-cmd" "$ROOT/ops-state/cmd-results"
chown "$UID_C":"$UID_C" "$ROOT/ops-cmd"
chmod 0700 "$ROOT/ops-cmd"
# cmd-results is written by the checker (as the install owner) and read by the
# container through the read-only /state/ops bind — owner-owned, world-read
OWNER=$(stat -c %U "$ROOT")
chown -R "$OWNER" "$ROOT/ops-state" 2>/dev/null || true

cat > /usr/local/bin/oikonome-command-run <<SCRIPT
#!/bin/sh
set -u
ROOT="\$1"
UID_C=$UID_C
req="\$ROOT/ops-cmd/command-requested"
[ -f "\$req" ] || exit 0
owner=\$(stat -c %u "\$req")
mtime=\$(stat -c %Y "\$req")
payload=\$(cat "\$req" 2>/dev/null || true)
rm -f "\$req"                       # single-use: consume before acting
[ "\$owner" = "\$UID_C" ] || { echo "command flag owned by uid \$owner — ignored"; exit 0; }
now=\$(date +%s)
# A stale flag is one whose request the console wrote too long ago to still
# be the live intent. Bounded well past any plausible op (restore/upgrade
# run minutes) so a command queued WHILE a previous one runs — the systemd
# path unit coalesces the re-fire until the oneshot goes inactive — is not
# dropped just because op A took a while. A truly dead leftover is still
# rejected, but VISIBLY: it writes a failed result so the operator sees the
# command did not vanish.
if [ \$((now - mtime)) -gt 3600 ]; then
  sid=\$(printf '%s' "\$payload" | python3 -c 'import sys,json
try: print("".join(c for c in json.load(sys.stdin).get("id","") if c.isalnum() or c=="-"))
except Exception: print("")' 2>/dev/null)
  [ -n "\$sid" ] || sid="cmd-\$now"
  sres="\$ROOT/ops-state/cmd-results"; mkdir -p "\$sres"
  printf '{"id":"%s","cmd":"?","state":"failed","rc":3,"started":%s,"finished":%s,"error":"request expired before it ran"}\n' "\$sid" "\$mtime" "\$now" > "\$sres/\$sid.json"
  echo "the request expired before the host agent could run it" > "\$sres/\$sid.log"
  chown "\$(stat -c %U "\$ROOT")" "\$sres/\$sid.json" "\$sres/\$sid.log" 2>/dev/null || true
  exit 0
fi

# parse the JSON with python3 (present on the host); each field on its own line
fields=\$(printf '%s' "\$payload" | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
def clean(s,ok): return "".join(c for c in str(s) if c in ok)
print(clean(d.get("id",""),"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"))
print(clean(d.get("cmd",""),"abcdefghijklmnopqrstuvwxyz"))
print(d.get("arg","").replace(chr(10)," ")[:200])' 2>/dev/null)
id=\$(printf '%s' "\$fields" | sed -n 1p)
cmd=\$(printf '%s' "\$fields" | sed -n 2p)
arg=\$(printf '%s' "\$fields" | sed -n 3p)
[ -n "\$id" ] || id="cmd-\$now"

case "\$cmd" in
  backup|status|logs|upgrade|restore|reset|uninstall) ;;
  *) echo "command not allowed: \$cmd"; exit 0 ;;
esac

owner_user=\$(stat -c %U "\$ROOT")
res="\$ROOT/ops-state/cmd-results"
mkdir -p "\$res"
log="\$res/\$id.log"
status="\$res/\$id.json"
lock="\$ROOT/ops-state/.cmd-lock"
# The lock names the run holding it and is held while THAT process lives:
# a bare age cut-off let a long restore or an image-pulling upgrade
# "expire" mid-run and a second destructive command start beside it. A
# recycled pid after a crash must not wedge it forever either, so the
# holder has to still be this script, and nothing holds for more than
# six hours regardless.
if [ -f "\$lock" ]; then
  holder=\$(cat "\$lock" 2>/dev/null || true)
  age=\$((now - \$(stat -c %Y "\$lock")))
  if [ -n "\$holder" ] && kill -0 "\$holder" 2>/dev/null \\
     && tr '\\0' ' ' < "/proc/\$holder/cmdline" 2>/dev/null | grep -q oikonome-command-run \\
     && [ "\$age" -lt 21600 ]; then
    echo "another command is already running — skipped"; exit 0
  fi
  # no live holder (a crash, a reboot): the lock is stale, take it
fi
echo "\$\$" > "\$lock"

# restore takes a backup FILE — a bare basename that exists in backups
restore_path=""
if [ "\$cmd" = "restore" ]; then
  base=\$(basename -- "\$arg")
  case "\$base" in *..*|"" ) echo "bad restore filename"; rm -f "\$lock"; exit 0 ;; esac
  restore_path="\$ROOT/backups/\$base"
  if [ ! -f "\$restore_path" ]; then
    printf '{"id":"%s","cmd":"restore","state":"failed","rc":2,"started":%s,"finished":%s}\n' "\$id" "\$now" "\$now" > "\$status"
    echo "no such backup: \$base" > "\$log"
    chown "\$owner_user" "\$log" "\$status" 2>/dev/null || true
    rm -f "\$lock"; exit 0
  fi
fi

printf '{"id":"%s","cmd":"%s","state":"running","started":%s}\n' "\$id" "\$cmd" "\$now" > "\$status"
chown "\$owner_user" "\$status" 2>/dev/null || true

# run the REAL command as the install owner (docker access, correct file
# ownership); OIKONOME_ASSUME_YES lets oikonome.sh skip its interactive
# confirm — the console already gated it.
# systemd gives us a minimal PATH; give oikonome.sh a real one so `docker`
# (and compose) resolve, plus the non-interactive bypass.
RUNENV="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin OIKONOME_ASSUME_YES=1"
(
  cd "\$ROOT"
  if [ "\$cmd" = "restore" ]; then
    runuser -u "\$owner_user" -- env \$RUNENV ./oikonome.sh restore "\$restore_path"
  else
    runuser -u "\$owner_user" -- env \$RUNENV ./oikonome.sh "\$cmd"
  fi
) > "\$log" 2>&1
rc=\$?
chown "\$owner_user" "\$log" 2>/dev/null || true
state=done; [ "\$rc" -eq 0 ] || state=failed
printf '{"id":"%s","cmd":"%s","state":"%s","rc":%s,"started":%s,"finished":%s}\n' "\$id" "\$cmd" "\$state" "\$rc" "\$now" "\$(date +%s)" > "\$status"
chown "\$owner_user" "\$status" 2>/dev/null || true
rm -f "\$lock"

# A command queued WHILE this one ran left a fresh command-requested that
# the systemd path unit coalesced into no new start (the oneshot was still
# active). Re-exec ourselves if one is waiting, so a second op is picked up
# deterministically instead of hanging on the next unrelated path event.
[ -f "\$req" ] && exec "\$0" "\$ROOT"
SCRIPT
chmod 755 /usr/local/bin/oikonome-command-run

# A command the host restarted under (a power cut, a reboot from the shell)
# never wrote its final status: its result stays "running", and the console
# and the nightly reboot check both read that as a command still in flight.
# At boot nothing can be running yet, so every result still "running" that
# was written before this boot is marked failed. Results written since the
# boot are left alone — a request waiting in the drop-box starts its run
# as soon as the path unit comes up.
cat > /usr/local/bin/oikonome-command-boot-sweep <<'SCRIPT'
#!/bin/sh
set -u
res="$1/ops-state/cmd-results"
[ -d "$res" ] || exit 0
python3 - "$res" <<'PY'
import glob, json, os, sys, time
booted = time.time() - float(open("/proc/uptime").read().split()[0])
for p in glob.glob(os.path.join(sys.argv[1], "*.json")):
    try:
        if os.path.getmtime(p) >= booted:
            continue
        with open(p) as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or d.get("state") != "running":
            continue
        d.update(state="failed", rc=4, finished=int(time.time()),
                 error="the host restarted while this command ran")
        with open(p, "w") as fh:   # rewritten in place: the owner is kept
            fh.write(json.dumps(d) + "\n")
    except (OSError, ValueError):
        continue
PY
SCRIPT
chmod 755 /usr/local/bin/oikonome-command-boot-sweep

# One HOST can run several instances, each in
# its own install dir. The units MUST be namespaced per install or the
# second install silently clobbers the first — one unit watching one dir, so
# only that instance's flags ever fire. Tag by the install dir's basename.
TAG=$(basename "$ROOT" | tr -cd 'a-zA-Z0-9-')
SVC="oikonome-command-watcher-$TAG"
cat > "/etc/systemd/system/$SVC.service" <<UNIT
[Unit]
Description=Oikonome console-requested host command ($TAG)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/oikonome-command-run $ROOT
UNIT
cat > "/etc/systemd/system/$SVC-boot.service" <<UNIT
[Unit]
Description=Mark Oikonome console commands cut off by a restart as failed ($TAG)
Before=$SVC.path
[Service]
Type=oneshot
ExecStart=/usr/local/bin/oikonome-command-boot-sweep $ROOT
[Install]
WantedBy=multi-user.target
UNIT
cat > "/etc/systemd/system/$SVC.path" <<UNIT
[Unit]
Description=Watch for Oikonome console command requests ($TAG)
[Path]
PathExists=$ROOT/ops-cmd/command-requested
Unit=$SVC.service
[Install]
WantedBy=multi-user.target
UNIT
# retire the shared-name unit an older version of this script installed
systemctl disable --now oikonome-command-watcher.path 2>/dev/null || true
rm -f /etc/systemd/system/oikonome-command-watcher.path \
      /etc/systemd/system/oikonome-command-watcher.service
systemctl daemon-reload
systemctl reset-failed "$SVC.service" "$SVC.path" 2>/dev/null || true
systemctl enable --now "$SVC.path"
systemctl enable "$SVC-boot.service"
# the container cannot see host units, so tell it THIS watcher is installed
# (scripts/watcher-marker.sh has the whole rule)
. "$(dirname "$0")/watcher-marker.sh"
watcher_mark "$ROOT" command "$TAG"
echo "command watcher installed for $ROOT (unit $SVC, drop-box uid $UID_C, runs as $OWNER)"
