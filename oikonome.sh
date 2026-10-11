#!/usr/bin/env bash
# Oikonome manager — the one entry point for running self-hosted instances.
# /oikonome.sh            interactive menu: lists every instance on this
#                            machine (any folder), pick one, then manage it
# /oikonome.sh <command>  non-interactive (CI/scripts), always manages
#                            the install folder this script lives in:
#       install | status | logs | backup | restore <file> | reset | uninstall
# Zero dependencies beyond what install.sh already requires; install.sh,
# uninstall.sh and scripts/backup.sh remain the workers (and stay usable
# directly as back-compat entry points).
set -euo pipefail

# Everything this script writes itself is owner-only from its first byte.
# The manager's own files are the sensitive ones: the pre-restore snapshot
# is the WHOLE database in plaintext SQL (every email, the entire ledger,
# balances, phone numbers, entity details), docker/.env's temp file holds
# the database passwords and the master key, and a community script's
# credentials.json holds an API token. Each of those was chmod 600'd only
# AFTER it was written — on a dump that takes minutes to produce, that is
# minutes of a world-readable window for any other account on the host to
# race. umask closes the window instead of narrowing it; the chmods stay as
# belt and braces for filesystems with no unix permission bits. Same
# reasoning, same value as scripts/backup.sh and scripts/pg-major-migrate.sh.
umask 077

# …but an install FOLDER is not a secret, and 077 on one breaks the
# product. An instance folder is a docker build context: its files are
# COPYed into the image root-owned and then read at runtime by the
# container's unprivileged uid 10001, and install.sh pre-creates ./backups
# and ./ops-state for the app container's read-only binds — all of which
# need the conventional mode, and both `cp` and `tar` apply the umask to
# what they create. So materialising a tree (clone/extract) and running
# install.sh keep 022, exactly as they did before; each chmods its own
# secrets (docker/.env, ops-cmd) itself.
shared_umask() { (umask 022; "$@"); }

# SCRIPT_HOME = where this script lives (never changes); ROOT = the
# instance being managed (the interactive menu can point it at any
# instance on the machine — subcommands always manage SCRIPT_HOME)
SCRIPT_HOME=$(cd "$(dirname "$0")" && pwd)
ROOT=$SCRIPT_HOME

# collapse_version / describe_version / VERSION_TAG_GLOB — one file, also
# sourced by install.sh, so the installer and this script can never disagree
# about what version an instance is running
. "$SCRIPT_HOME/scripts/version-stamp.sh"
# dotenv_get / canon_path / data_dir_resolve — the docker/.env reads every
# lifecycle script shares, so none of them acts on a different database
# folder or database than the one compose gave the stack
. "$SCRIPT_HOME/scripts/env-file.sh"

say()  { printf "\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$*"; }
die()  { printf "\033[1;31m✗ %s\033[0m\n" "$*"; exit 1; }

# One lock for every operation that reads or rewrites the whole database:
# the backup (scripts/backup.sh takes it itself, scheduled or via this
# script — cmd_backup must NOT take it too) and the console-driven
# restore / reset / uninstall. Unlocked against each other, a nightly
# dump could start under a restore and capture half of it, or a reset
# could pull the stack down under a running pg_dump. Waits up to
# OIKONOME_MAINT_LOCK_WAIT seconds (default
# ten minutes) and then refuses,
# so a hung operation blocks the next one loudly rather than silently.
# The descriptor is inherited across exec, so uninstall keeps it.
# The lock lasts as long as the process holding descriptor 9, so a command
# that takes it from the interactive menu runs in its own subshell
# (in_lock_scope): the menu loop outlives every command, and a lock taken
# in the menu's own shell would stay held — skipping every backup — until
# the operator quit. The holder file names who took it and when, so a
# backup that times out can say who it waited for; it is cleared when the
# shell holding the lock exits (scripts/maint-lock.sh has the rules).
maint_lock() {
  command -v flock >/dev/null 2>&1 || return 0   # no flock: as before
  . "$SCRIPT_HOME/scripts/maint-lock.sh"
  exec 9>"$ROOT/.maintenance.lock"
  flock -w "${OIKONOME_MAINT_LOCK_WAIT:-600}" 9 || die \
    "another backup/restore/reset is still running on this instance — wait for it, or set OIKONOME_MAINT_LOCK_WAIT"
  maint_holder_write "$ROOT" "${1:-maintenance}"
  # the shell that took the lock is the one that gives it back: the menu's
  # subshell (in_lock_scope), or the whole run of a one-shot command. The
  # trap goes with an exec (uninstall), whose process keeps the pid and the
  # descriptor and so stays the recorded holder until it ends.
  trap 'maint_holder_clear "$ROOT"' EXIT
  # a child that takes the lock itself (scripts/pg-major-migrate.sh, under
  # the reinstall a reset runs) would open its own description of the file
  # and wait on ours; this tells it the lock is already held for it
  export OIKONOME_MAINT_LOCK_HELD="$ROOT"
}

# Run a command that takes the maintenance lock in a subshell, so the lock
# is released however the command ends — success, a cancelled prompt, a
# `return 1`, or a `die` — and the menu that called it keeps going.
in_lock_scope() { ( "$@" ); }

# ── shared plumbing ──────────────────────────────────────────────────────────

# GNU/BSD portability (macOS ships BSD stat/date and no `hostname -I`)
mtime()  { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }
fmt_ts() { date -d "@$1" '+%m/%d/%y %H:%M' 2>/dev/null \
           || date -r "$1" '+%m/%d/%y %H:%M'; }
lan_ip() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$ip" ] || ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  printf '%s' "$ip"
}

# sets COMPOSE + ENGINE (docker|podman) via the prereq check shared with
# install.sh: missing engine/curl/openssl gets a guided install-and-recheck
# walk on a terminal, a clear non-zero exit otherwise — one file, so the
# two entry points can never drift apart on what a host needs
find_compose() {
  . "$SCRIPT_HOME/scripts/check-prereqs.sh"
  require_prereqs
}

# KEY from docker/.env read the way compose reads it: the last assignment
# wins, a leading `export ` and a trailing CR are dropped, a quoted value
# loses its quotes and an unquoted one its trailing ` # comment`. Empty if
# unset or there is no .env yet.
env_value() {
  dotenv_get "$ROOT/docker/.env" "$1"
}

# KEY from docker/.env — the same compose reading as env_value. Every
# setting this script acts on is one the running stack was given: a literal
# `grep ^KEY=` read misses `export KEY=`, keeps quotes and returns both
# lines of a key set twice, and a COMPOSE_PROJECT_NAME read that way sends
# backup, restore, reset and uninstall to another project's containers and
# volumes.
env_get() {
  env_value "$1"
}

# env_get_in DIR KEY — the same reading of another instance's docker/.env
env_get_in() {
  dotenv_get "$1/docker/.env" "$2"
}

# master_key_normal VALUE — prints VALUE as a master key (32 bytes, urlsafe
# base64: 43 characters and one '='), or fails. The standard alphabet's
# + and / decode to the same bytes as - and _, so they are rewritten, not
# refused. install.sh applies the same test to the key it is handed.
master_key_normal() {
  local k; k=$(printf '%s' "$1" | tr '+/' '-_')
  case $k in *[!A-Za-z0-9_=-]*|"") return 1;; esac
  case ${k%=} in *=*) return 1;; esac
  [ "${#k}" -eq 44 ] && [ "${k%=}" != "$k" ] || return 1
  printf '%s' "$k"
}

# The docker/.env lines a reset carries into the reinstall: every operator
# setting (port aside, which travels separately), minus what install.sh
# mints for a new instance — the database and queue passwords, the master
# key (handed over on its own, checked), the setup token, the version
# stamp. On an external database POSTGRES_PASSWORD is the provider's admin
# password, which nothing here can mint, so it is carried too.
# reset_carried_settings [external]
reset_carried_settings() {
  [ -f "$ROOT/docker/.env" ] || return 0
  awk -v ext="${1:-}" '
    { sub(/\r$/, "") }
    /^[ \t]*(#|$)/ { next }
    {
      line = $0; sub(/^[ \t]*(export[ \t]+)?/, "", line)
      eq = index(line, "="); if (!eq) next
      k = substr(line, 1, eq - 1); sub(/[ \t]+$/, "", k)
      v = substr(line, eq + 1); sub(/^[ \t]+/, "", v)
      if (k !~ /^[A-Za-z_][A-Za-z0-9_]*$/) next
      if (k == "POSTGRES_PASSWORD" && ext != "") { print k "=" v; next }
      if (k ~ /^(POSTGRES_PASSWORD|OIKONOME_APP_PASSWORD|OIKONOME_ADMIN_PASSWORD|REDIS_PASSWORD|OIKONOME_MASTER_KEY|OIKONOME_SETUP_TOKEN|OIKONOME_PORT|OIKONOME_VERSION|OIKONOME_VERSION_NOTE|OIKONOME_VERSION_NOTE_FOR|OIKONOME_INSTALLED_COMMIT)$/) next
      print k "=" v
    }' "$ROOT/docker/.env"
}

# KEY from docker/.env read as a BOOLEAN: true only for 1/true/yes/on
# (case-insensitive) — the same spelling the server's env_flag uses. A bare
# non-empty test would read KEY=0, the most natural way to write "off",
# as ON.
env_flag() {
  case "$(env_get "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    1|true|yes|on) return 0;;
    *) return 1;;
  esac
}

# set/clear KEY=VALUE in docker/.env without sed (values may contain anything).
# Every spelling of KEY is replaced (`export KEY=`, `KEY = …`), not only
# `KEY=`: a left-over line that compose reads after ours would win.
# chmod every time: mv replaces .env with a umask-mode temp file, and the
# file holds the database passwords + master key — keep it owner-only
set_env() {
  local envf="$ROOT/docker/.env"
  grep -Ev "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$envf" > "$envf.tmp" 2>/dev/null || true
  [ -n "${2:-}" ] && printf '%s=%s\n' "$1" "$2" >> "$envf.tmp"
  mv "$envf.tmp" "$envf"
  chmod 600 "$envf"
}

# COMPOSE_PROFILES is a comma list (tls, llm, …) — add/remove without
# clobbering the others (https and llm can both be on)
add_profile() {
  local cur; cur=$(env_get COMPOSE_PROFILES)
  case ",$cur," in *",$1,"*) return 0;; esac
  set_env COMPOSE_PROFILES "$(printf '%s,%s' "$cur" "$1" | sed 's/^,//')"
}
remove_profile() {
  local cur; cur=$(env_get COMPOSE_PROFILES)
  set_env COMPOSE_PROFILES \
    "$(printf '%s' "$cur" | tr ',' '\n' | grep -vx "$1" | paste -sd, -)"
}

# container name for a compose service, resolved via the labels both
# engines set — NEVER by guessing the name: docker compose v2 names
# containers {proj}-{svc}-1, podman-compose/v1 {proj}_{svc}_1, so a
# hardcoded form always breaks one engine. svc_container PROJECT SERVICE
svc_container() {
  $ENGINE ps \
    --filter "label=com.docker.compose.project=$1" \
    --filter "label=com.docker.compose.service=$2" \
    --format '{{.Names}}' 2>/dev/null | head -1
}

app_port() {
  # environment first, then .env — the same precedence compose applies,
  # so we always probe the port the container actually published
  local p
  p=${OIKONOME_PORT:-$(env_get OIKONOME_PORT)}
  echo "${p:-8042}"
}

# Rootless Podman hides client addresses: every published-port connection
# reaches the app through a user-space forwarder, so the app sees its own
# pod-network address as the peer of every client. Auto-ban, the per-IP
# limits and OIKONOME_ADMIN_IPS then act on that one address, and with TLS
# on the app port stays open to the LAN from inside the trusted-proxy range,
# so a direct client can forge X-Forwarded-*. Networking is left as it is —
# changing it silently would break working installs — but the operator is
# told, with the two remedies. Needs ENGINE (find_compose) set.
engine_is_rootless_podman() {
  [ "${ENGINE:-}" = podman ] || return 1
  [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ]
}
warn_rootless_podman() {
  engine_is_rootless_podman || return 0
  warn "Rootless Podman: the app cannot see real client addresses."
  echo "  Every client reaches the app from the pod network, so auto-ban,"
  echo "  per-IP rate limits and OIKONOME_ADMIN_IPS all act on the pod's"
  echo "  address — bans never fire and a LAN-range ADMIN_IPS locks everyone out."
  if [ -n "$(env_get OIKONOME_DOMAIN)" ]; then
    echo "  With HTTPS on, port $(app_port) is still reachable from the LAN and"
    echo "  its clients arrive from inside the trusted-proxy range, so they can"
    echo "  forge X-Forwarded-For / X-Forwarded-Proto."
  fi
  echo "  Remedies (either one):"
  echo "    • run the stack as root, or under Docker; or"
  echo "    • publish the app port on 127.0.0.1 only (OIKONOME_BIND_ADDRESS=127.0.0.1"
  echo "      in docker/.env) and front it with a reverse proxy on this host that"
  echo "      sets X-Forwarded-For. On a loopback bind the app trusts its own port"
  echo "      forwarder by itself; the host proxy does NOT go in"
  echo "      OIKONOME_TRUSTED_PROXIES (it never appears as the peer here)."
  echo "      See docs/reverse-proxy.md."
}

# every artifact stays inside the install folder — backups included.
# (External schedulers that want a shared spot call scripts/backup.sh
# with their own destination.)
backup_dir() {
  echo "$ROOT/backups"
}

newest_backup() {
  ls -1t "$(backup_dir)"/oikonome-*.sql.gz 2>/dev/null | head -1 || true
}

# read one line from the terminal when there is one, else from stdin — so the
# menu works normally, `curl | bash`-style stdin still gets real keyboard
# input, and scripts can pipe confirmations in. tty_read VARNAME
# Sets TTY_EOF=1 when the input source is exhausted (Ctrl-D / piped input
# ran out) so menus can exit instead of looping on their Enter-default.
TTY_EOF=0
tty_read() {
  TTY_EOF=0
  if { : </dev/tty; } 2>/dev/null; then
    read -r "$1" </dev/tty || { TTY_EOF=1; printf -v "$1" ''; }
  else
    read -r "$1" || { TTY_EOF=1; printf -v "$1" ''; }
  fi
}

# confirm_word WORD PROMPT — returns 0 only if the user types WORD exactly
confirm_word() {
  # non-interactive bypass for the admin-console host-agent: the console
  # already did the typed-confirm + single-use nonce in the browser, and a
  # root watcher validated the request before running us. OIKONOME_ASSUME_YES
  # is set ONLY by that watcher (oikonome-command-run), never in a shell.
  [ -n "${OIKONOME_ASSUME_YES:-}" ] && return 0
  local ans=""
  printf "  %s" "$2"
  tty_read ans
  if [ "$ans" != "$1" ]; then
    echo "  Aborted — nothing touched."
    return 1
  fi
}

human_age() { # seconds → "5m" / "3h" / "2d"
  local s=$1
  if   [ "$s" -lt 3600 ];  then echo "$((s / 60))m"
  elif [ "$s" -lt 86400 ]; then echo "$((s / 3600))h"
  else                          echo "$((s / 86400))d"
  fi
}

wait_ready() { # wait_ready [tries] — poll /readyz until the app answers
  local port tries i fill
  port=$(app_port)
  tries=${1:-60}
  for i in $(seq 1 "$tries"); do
    if curl -sf "http://localhost:${port}/readyz" >/dev/null 2>&1; then
      [ -t 1 ] && printf '\r\033[K'
      ok "Healthy: http://localhost:${port}/readyz answers."
      return 0
    fi
    # progress bar when on a terminal, plain dots otherwise (logs/CI)
    if [ -t 1 ]; then
      fill=$((i * 30 / tries)); [ "$fill" -lt 1 ] && fill=1
      printf '\r  [%-30s] waiting for the app… %ds ' \
        "$(printf '#%.0s' $(seq 1 $fill))" $((i * 2))
    else
      printf "."
    fi
    sleep 2
  done
  echo
  warn "App not answering on /readyz (port ${port}). Check: ./oikonome.sh logs"
  return 1
}

# ── commands ─────────────────────────────────────────────────────────────────

# pre_upgrade_snapshot — dump the running database into ./backups before
# an upgrade's migrations touch it. Several migrations rewrite stored data
# for good (account numbers cut to their last four digits, account ids
# renamed), and the image is rebuilt in place under the same tag, so
# without this dump there is no earlier state to go back to. Held to the
# rule the pre-restore snapshot keeps: a database with accounts on it is
# never migrated without one. Only a database that is already up is
# dumped: starting postgres here could start the NEW release's server
# image on a folder an older major wrote.
pre_upgrade_snapshot() {
  [ -f "$ROOT/docker/.env" ] || return 0      # first install: nothing to keep
  find_compose
  local snap part users
  if ! ( cd "$ROOT/docker" && pg_ready ); then
    warn "The database isn't running, so no pre-upgrade snapshot was taken."
    local last; last=$(newest_backup)
    if [ -n "$last" ]; then
      echo "  Your newest backup is the way back if this upgrade goes wrong:"
      echo "    $last"
    else
      echo "  There is no backup in $(backup_dir) either: if this upgrade goes"
      echo "  wrong there is nothing to go back to."
    fi
    return 0
  fi
  users=$( cd "$ROOT/docker" && pg_app_sql -tA -c "SELECT count(*) FROM users;" 2>/dev/null | tr -dc '0-9' ) || true
  users=${users:-0}
  snap="$(backup_dir)/oikonome-pre-upgrade-$(date +%Y%m%d-%H%M%S).sql.gz"
  # written under a dotted .partial name and moved into place only once it
  # checks out (as backup.sh does), so an interrupted dump is never listed
  # by the restore picker as the newest way back
  part="$(backup_dir)/.$(basename "$snap").partial"
  shared_umask mkdir -p "$(backup_dir)"
  say "Snapshotting the database before the upgrade…"
  if ( umask 077; ( cd "$ROOT/docker" && pg_dump_db 2>/dev/null ) | gzip > "$part" ) \
      && dump_looks_real "$part" && mv -f "$part" "$snap"; then
    chmod 600 "$snap" 2>/dev/null || true   # the whole database in plaintext
    ok "Pre-upgrade snapshot: $snap"
    PRE_UPGRADE_SNAPSHOT=$snap
    return 0
  fi
  rm -f "$part"
  if [ "$users" -gt 0 ]; then
    die "Couldn't snapshot the database before upgrading, and it holds $users account(s) — nothing was changed. Free disk space or take a backup (./oikonome.sh backup), then retry."
  fi
  warn "Couldn't snapshot the database (empty instance?) — upgrading without one."
}

# non_interactive — true when nobody is at a terminal to read a warning
# (the admin console's host agent, cron, CI): an upgrade that could not
# fetch the new code must fail there, or the console records "done".
non_interactive() { [ -n "${OIKONOME_ASSUME_YES:-}" ] || [ ! -t 1 ]; }

cmd_install() {
  [ -f "$ROOT/install.sh" ] || die "No install.sh in $ROOT."
  # Held from before the snapshot until install.sh returns: a backup or
  # restore running meanwhile would be dumped half-loaded into the snapshot
  # (or have its load raced by the rebuild), and the host's reboot check
  # defers to this lock, so a nightly reboot never lands mid-migration.
  # pg-major-migrate, run by install.sh, sees OIKONOME_MAINT_LOCK_HELD and
  # does not wait on it. The menu runs this in in_lock_scope.
  maint_lock upgrade
  PRE_UPGRADE_SNAPSHOT=""
  # Upgrade means UPGRADE: pull the latest code first when this install
  # is a git checkout with an upstream (a plain download just installs
  # what's there). --ff-only so a dirty/diverged tree warns instead of
  # merging surprises into someone's instance.
  if git -C "$ROOT" rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
    local before after pull_err
    before=$(describe_version "$ROOT"); before=${before:-?}
    git -C "$ROOT" fetch -q 2>/dev/null || warn "Couldn't reach the code's upstream (offline?)."
    if git -C "$ROOT" rev-parse -q --verify '@{upstream}' >/dev/null 2>&1 \
        && ! git -C "$ROOT" merge-base HEAD '@{upstream}' >/dev/null 2>&1; then
      # The upstream was replaced by a history this checkout shares no
      # commit with (the published repository is recreated, not rewritten).
      # No pull can ever succeed from here, and installing as-is would
      # rebuild the old version every time while reporting success.
      die "This checkout's history and the upstream's no longer share a commit (the
  published repository was recreated), so it cannot be updated by a pull —
  nothing was changed. Your settings (docker/.env), data and backups are not
  tracked by git, so moving the code across is safe:
    ./oikonome.sh backup
    git -C '$ROOT' reset --hard '@{upstream}'
    ./oikonome.sh install
  (git reset --hard discards edits you made to tracked files; 'git status' lists them.)"
    fi
    # Snapshot whenever the code about to be installed is not the code
    # that last migrated this database — not merely when git still has
    # commits to pull. A tree the operator pulled by hand, main checked
    # out again after a rollback, or a retry after an install that failed
    # part-way all have HEAD == upstream, yet their migrations run on the
    # database just the same. install.sh records the commit once an
    # install comes up healthy (OIKONOME_INSTALLED_COMMIT); no record at
    # all (an instance from before it) reads as new code.
    local head
    head=$(git -C "$ROOT" rev-parse -q --verify HEAD 2>/dev/null || true)
    if [ "$head" != "$(git -C "$ROOT" rev-parse '@{upstream}' 2>/dev/null)" ] \
        || [ -z "$head" ] || [ "$head" != "$(env_get OIKONOME_INSTALLED_COMMIT)" ]; then
      pre_upgrade_snapshot
    fi
    if pull_err=$(git -C "$ROOT" pull --ff-only -q 2>&1); then
      after=$(describe_version "$ROOT"); after=${after:-?}
      if [ "$before" = "$after" ]; then
        ok "Code already current ($after)."
      else
        ok "Code updated: $before → $after"
      fi
    else
      warn "Couldn't update the code: ${pull_err:-git pull failed}"
      echo "  (Usually local edits to tracked files — 'git -C $ROOT status' lists them.)"
      non_interactive && die "Upgrade stopped: the new code could not be fetched, so nothing was installed."
      warn "Installing the code as-is ($before)."
    fi
  else
    # a plain download: the files may already be a newer release
    pre_upgrade_snapshot
  fi
  local rc=0
  shared_umask "$ROOT/install.sh" || rc=$?
  if [ -n "$PRE_UPGRADE_SNAPSHOT" ]; then
    # said whether the install worked or not: a release that will not start
    # is exactly when the way back is needed
    echo "  To undo this upgrade: check out the previous version, run ./install.sh,"
    echo "  then ./oikonome.sh restore $(basename "$PRE_UPGRADE_SNAPSHOT")  (docs/quickstart.md → Upgrades)"
  fi
  [ "$rc" -eq 0 ] || return "$rc"
  # install.sh has already passed the prereq check, so this resolves the
  # engine without prompting
  find_compose
  warn_rootless_podman
}


instances() {
  # machine-wide discovery from compose labels (docker + podman both set
  # them). `ps -a`, not `ps`: a stopped instance's containers still carry
  # the labels, and an instance must not vanish from the picker just
  # because its stack is stopped. One line per instance:
  # dir|project|port|state (running when its app container is running).
  # `{{.Label "key"}}`, NEVER `{{index .Labels "key"}}`: docker's ps
  # formatter exposes .Labels as a STRING, so index is a hard template
  # error — the whole ps fails and every instance read as "stopped"
  # (podman's formatter exposes a map, which hides the bug).
  # `{{.State}}` for the same reason: one word ("running") on both
  # engines, instead of parsing the human "Up 3 hours (healthy)" text.
  $ENGINE ps -a --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.service"}}|{{.Ports}}|{{.State}}' 2>/dev/null \
    | awk -F'|' '$1 ~ /^oikonome/ && $3 == "app" {
        dir = $2; sub(/\/docker$/, "", dir);
        port = ""; if (match($4, /0\.0\.0\.0:[0-9]+/)) { port = substr($4, RSTART+8, RLENGTH-8) }
        state = ($5 == "running") ? "running" : "stopped"
        # recreates can leave several app containers per project — a
        # running one wins; keep the first port seen either way
        if (!(dir in st) || state == "running") st[dir] = state
        if (!(dir in po) || po[dir] == "") { po[dir] = port; pr[dir] = $1 }
      }
      END { for (d in st) print d "|" pr[d] "|" po[d] "|" st[d] }' \
    | sort || true
}

source_checkout() {
  # `git config oikonome.source true` marks a checkout as SOURCE (a
  # developer's working copy): the manager then never lists it as an
  # instance and never offers to install into it — "new" always clones
  # to a separate folder. Per-clone config, so end users (who install
  # right where they cloned) are unaffected.
  [ "$(git -C "$SCRIPT_HOME" config --get oikonome.source 2>/dev/null)" = true ]
}

# _created_epoch DIR — a stable "when was this instance created" sort key.
# Prefers the OIKONOME_CREATED stamp install.sh writes (portable, survives
# across filesystems + backup/restore); falls back to the folder's birth time
# (stat %W, on filesystems that record it — existing instances predating the
# stamp still order correctly), then the .env mtime as a last resort.
_created_epoch() {
  local dir=$1 c
  c=$(env_get_in "$dir" OIKONOME_CREATED)
  if [ -n "$c" ]; then printf '%s' "$c"; return; fi
  c=$(stat -c %W "$dir" 2>/dev/null || echo 0)
  if [ -n "$c" ] && [ "$c" != 0 ]; then printf '%s' "$c"; return; fi
  stat -c %Y "$dir/docker/.env" 2>/dev/null || echo 0
}

all_instances() {
  # INSTALLED instances only: any with containers (compose labels, `ps -a`
  # so stopped ones show too), plus installed folders whose stack is fully
  # DOWN (no containers at all): the folder this script lives in and the
  # ~/oikonome-* homes where "new"/demo instances are created —
  # still manageable: upgrade, restore, status. A fresh never-installed
  # checkout is NOT an instance — the picker offers it as "new" instead.
  # SCRIPT_HOME never changes; ROOT follows the pick.
  # Output is CREATION-ORDERED (oldest first) so the list is stable across
  # runs — dev, then demo1, then demo2 always show 1/2/3.
  local seen=$'\n' line dir proj port state d p buf=""
  while IFS= read -r line; do
    if [ -z "$line" ]; then continue; fi
    IFS='|' read -r dir proj port state <<<"$line"
    # stopped containers publish no port — fall back to the instance's .env
    if [ -z "$port" ]; then
      p=$(env_get_in "$dir" OIKONOME_PORT)
      port=${p:-8042}
    fi
    seen="$seen$dir"$'\n'
    buf="$buf$(_created_epoch "$dir")|$dir|$proj|$port|$state"$'\n'
  done < <(instances)
  for d in "$SCRIPT_HOME" "$HOME"/oikonome-*; do
    if [ "$d" = "$SCRIPT_HOME" ] && source_checkout; then continue; fi
    if [ ! -f "$d/docker/.env" ]; then continue; fi
    case $seen in *$'\n'"$d"$'\n'*) continue;; esac
    seen="$seen$d"$'\n'
    p=$(env_get_in "$d" OIKONOME_PORT)
    proj=$(env_get_in "$d" COMPOSE_PROJECT_NAME)
    buf="$buf$(_created_epoch "$d")|$d|${proj:-—}|${p:-8042}|stopped"$'\n'
  done
  # sort by the creation epoch (numeric), then drop the sort key
  printf '%s' "$buf" | grep -v '^$' | sort -t'|' -k1,1n -s | cut -d'|' -f2-
}

list_instances() {
  echo
  echo "  All Oikonome instances on this machine:"
  all_instances | awk -F'|' \
    '{ printf "    • %-34s :%-5s %-8s (%s)\n", $1, $3, $4, $2 }'
}

new_instance() {
  # "n" in the picker. A never-installed script folder IS the new
  # instance (the git-clone-then-run flow); from an already-installed
  # folder, clone the checkout to a new folder first. Either way the
  # installer runs immediately and auto-picks a free port (unless
  # OIKONOME_PORT is exported).
  local dest
  if [ -f "$SCRIPT_HOME/docker/compose.yaml" ] \
     && [ ! -f "$SCRIPT_HOME/docker/.env" ] && ! source_checkout; then
    ROOT=$SCRIPT_HOME
  else
    # Enter accepts a generated collision-free default: ~/oikonome-<id>
    local suggest
    while :; do
      suggest="$HOME/oikonome-$(tr -dc a-f0-9 </dev/urandom | head -c6)"
      [ -e "$suggest" ] || break
    done
    printf "  Folder for the new instance [Enter = %s]: " "${suggest/#$HOME/\~}"
    tty_read dest
    [ -n "$dest" ] || dest=$suggest
    dest=${dest/#\~/$HOME}
    [ -e "$dest" ] && { warn "$dest already exists."; return 1; }
    git -C "$SCRIPT_HOME" rev-parse --git-dir >/dev/null 2>&1 \
      || { warn "$SCRIPT_HOME is not a git checkout — git clone the repo into a new folder and run its ./oikonome.sh instead."; return 1; }
    say "Cloning $SCRIPT_HOME → $dest …"
    shared_umask git clone -q "$SCRIPT_HOME" "$dest" || { warn "clone failed"; return 1; }
    ROOT=$dest
  fi
  say "Installing new instance at $ROOT …"
  shared_umask "$ROOT/install.sh" || { warn "install did not complete"; return 1; }
}

pick_instance() {
  # the menu's opening move: choose which INSTALLED instance to manage —
  # or create a new one — no matter where the script was run from. Sets
  # ROOT. From a fresh never-installed checkout, "new here" is the
  # Enter-default (the git-clone-then-run flow).
  local rows r dir proj port state def i n choice kind fresh_here=0
  rows=()
  while IFS= read -r __l; do rows+=("$__l"); done < <(all_instances)
  if [ -f "$SCRIPT_HOME/docker/compose.yaml" ] \
     && [ ! -f "$SCRIPT_HOME/docker/.env" ] && ! source_checkout; then
    fresh_here=1
  fi
  [ ${#rows[@]} -gt 0 ] || [ "$fresh_here" = 1 ] \
    || die "No Oikonome instances found (nothing running, and $SCRIPT_HOME is not a checkout). git clone the repo and run its ./oikonome.sh."
  # Enter-default: the script's own installed instance when it's in the
  # list, else instance 1 when any exist, else "n" (new) — so a bare
  # Enter always does the obvious thing. Decided BEFORE printing so the
  # default row carries the ❯ marker.
  def=n
  i=1
  for r in "${rows[@]}"; do
    [ "${r%%|*}" = "$SCRIPT_HOME" ] && def=$i
    i=$((i + 1))
  done
  [ "$def" = n ] && [ ${#rows[@]} -gt 0 ] && def=1
  echo
  echo "  Oikonome instances on this machine:"
  i=1
  for r in "${rows[@]}"; do
    IFS='|' read -r dir proj port state <<<"$r"
    # running = green, anything else = yellow; ❯ marks the Enter-default
    case $state in
      running) state=$(printf '\033[32m%-8s\033[0m' "$state");;
      *)       state=$(printf '\033[33m%-8s\033[0m' "$state");;
    esac
    if [ "$i" = "$def" ]; then
      printf "  \033[1m❯ %d) %-34s :%-5s %s (%s)\033[0m\n" "$i" "$dir" "$port" "$state" "$proj"
    else
      printf "    %d) %-34s :%-5s %s (%s)\n" "$i" "$dir" "$port" "$state" "$proj"
    fi
    i=$((i + 1))
  done
  [ ${#rows[@]} -gt 0 ] || echo "    (none installed yet)"
  if [ "$fresh_here" = 1 ]; then
    if [ "$def" = n ]; then
      printf "  \033[1m❯ n) New instance here: %s (auto-picks a free port, installs)\033[0m\n" "$SCRIPT_HOME"
    else
      echo "    n) New instance here: $SCRIPT_HOME (auto-picks a free port, installs)"
    fi
  else
    if [ "$def" = n ]; then
      printf "  \033[1m❯ n) New instance (clones this checkout to a new folder, installs)\033[0m\n"
    else
      echo "    n) New instance (clones this checkout to a new folder, installs)"
    fi
  fi
  if [ ${#rows[@]} -gt 0 ]; then
    printf "  Choose [1-%d, n] (Enter = %s): " "${#rows[@]}" "$def"
  else
    printf "  Choose (Enter = n): "
  fi
  tty_read choice
  [ -n "$choice" ] || choice=$def
  case $choice in n|N)
    # real or demo? Creating an instance should offer the synthetic-data
    # demo up front, not hide it behind the menu's 'd'.
    echo
    echo "    r) Real instance — connect your accounts, bring in your data"
    echo "    d) Demo instance — 10 years of synthetic data, for a look around"
    printf "  Which kind? [r/d] (Enter = r): "
    tty_read kind
    case $kind in
      d|D) cmd_demo "" || return 1
           # the demo is up and running — re-show the picker with it listed
           pick_instance; return $?;;
      ""|r|R) new_instance; return $?;;
      *) warn "Unknown choice: $kind"; return 1;;
    esac;;
  esac
  case $choice in q|Q|quit|exit) exit 0;; esac
  case $choice in *[!0-9]*|'') warn "Not a number: $choice"; return 1;; esac
  n=$choice
  { [ "$n" -ge 1 ] && [ "$n" -le ${#rows[@]} ]; } \
    || { warn "Out of range: $n"; return 1; }
  dir=${rows[$((n - 1))]%%|*}
  [ -d "$dir/docker" ] \
    || { warn "$dir has no docker/ folder — was the install moved?"; return 1; }
  ROOT=$dir
}

cmd_status() {
  find_compose
  cd "$ROOT/docker"
  local port ver last size age
  port=$(app_port)

  echo
  say "Services"
  # concise per-service summary straight from the engine (no compose ps
  # table, no podman-compose stderr banner). Match this instance's
  # containers by project name when .env pins one, else by the compose
  # working-dir label (both docker and podman-compose set it).
  # `{{.Label "key"}}`, not `{{index .Labels …}}` — see instances().
  local proj rows
  proj=$(env_get COMPOSE_PROJECT_NAME)
  if [ -n "$proj" ]; then
    rows=$($ENGINE ps -a --filter "label=com.docker.compose.project=$proj" \
      --format '{{.Label "com.docker.compose.service"}}|{{.Status}}' 2>/dev/null || true)
  else
    rows=$($ENGINE ps -a --filter "label=com.docker.compose.project.working_dir=$ROOT/docker" \
      --format '{{.Label "com.docker.compose.service"}}|{{.Status}}' 2>/dev/null || true)
  fi
  if [ -n "$rows" ]; then
    printf '%s\n' "$rows" | sort | awk -F'|' '{
      st = tolower($2)
      if      ($2 ~ /^Up/ && $2 ~ /unhealthy/) st = "up (unhealthy)"
      else if ($2 ~ /^Up/ && $2 ~ /healthy/)   st = "up (healthy)"
      else if ($2 ~ /^Up/)                     st = "up"
      printf "    • %-10s %s\n", $1, st
    }'
  else
    warn "Stack is down (run: ./oikonome.sh install)"
  fi

  echo
  say "Instance"
  echo "  Folder:   $ROOT"
  echo "  Port:     $port  (http://localhost:${port})"
  ver=$(describe_version "$ROOT" --dirty)
  echo "  Version:  ${ver:-unknown (not a git checkout)}"

  last=$(newest_backup)
  if [ -n "$last" ]; then
    size=$(du -h "$last" | cut -f1)
    age=$(human_age $(($(date +%s) - $(mtime "$last"))))
    echo "  Backup:   $(basename "$last")  (${size}, ${age} ago, in $(backup_dir))"
  else
    echo "  Backup:   none yet  (run: ./oikonome.sh backup)"
  fi

  if curl -sf "http://localhost:${port}/readyz" >/dev/null 2>&1; then
    ok "Health:   /readyz answers — app is up."
  else
    warn "Health:   /readyz NOT answering on port ${port}."
  fi

  # unclaimed instance (no account yet): reprint the one-time setup link.
  # Probe WITHOUT the token in the URL, so the one-time token never rides a
  # request line into uvicorn's access log (which the host agent's `logs`
  # snapshot would otherwise capture to a world-readable file). A tokenless
  # GET /setup is a 403 by design and says nothing about the claim state,
  # so it is read off GET /'s redirect instead: /setup while unclaimed,
  # /app/ once claimed.
  local tok host_ip loc
  tok=$(env_get OIKONOME_SETUP_TOKEN)
  loc=$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 5 \
             "http://localhost:${port}/" 2>/dev/null || echo "")
  if [ -n "$tok" ] && case "$loc" in */setup) true ;; *) false ;; esac; then
    if [ -n "${OIKONOME_ASSUME_YES:-}" ] || [ ! -t 1 ]; then
      # the host agent captures this to a world-readable log and the admin
      # console surfaces it — the one-time claim token must not ride there.
      # A terminal operator sees the full link; the console is told where
      # to get it instead.
      warn "Setup:    no account yet — run './oikonome.sh status' on the"
      echo "            host to see the one-time claim link (the token is"
      echo "            withheld from this log on purpose)."
    else
      warn "Setup:    no account yet — create it with this one-time link:"
      echo "            http://localhost:${port}/setup?token=${tok}"
      host_ip=$(lan_ip)
      [ -n "$host_ip" ] && \
        echo "            http://${host_ip}:${port}/setup?token=${tok}  (from another machine)"
    fi
  fi
  warn_rootless_podman
  echo
  list_instances
}

cmd_logs() {
  find_compose
  cd "$ROOT/docker"
  # The host-agent runs this non-interactively (OIKONOME_ASSUME_YES) and
  # captures the output to a file. `logs -f` NEVER exits while app/worker
  # are up, so following there would pin the single oneshot systemd unit
  # forever and silently wedge EVERY later host op — backup, restore,
  # uninstall included. Non-interactive gets a bounded snapshot; only a
  # real terminal follows.
  if [ -n "${OIKONOME_ASSUME_YES:-}" ]; then
    # the snapshot lands in a file the console shows to any operator
    # session: the access log's request lines carry every one-time link
    # (reset, invite, export ticket), so those are scrubbed at the source
    # — the same shapes the feedback bundle scrubs: query tickets, invite
    # links, bearer headers, key=value secrets, envelope ciphertext, and
    # member addresses (one leading character kept, like the bundle).
    # The query-param and token-path lists in the first two rules are a
    # copy of TOKEN_QUERY_PARAMS / TOKEN_PATH_SEGMENTS in
    # server/oikonome/web/feedback.py (bash cannot import it); a shape
    # minted but missing from one list would ride out verbatim, so
    # server/tests/test_log_link_scrubbing.py holds the copies equal.
    PYTHONUNBUFFERED=1 $COMPOSE logs --tail=200 app worker 2>&1 \
      | sed -E \
          -e 's#([?&](token|t|ticket|code|invite)=)[A-Za-z0-9._-]+#\1<redacted>#g' \
          -e 's#(/(invite|link)/)[A-Za-z0-9._-]{8,}#\1<redacted>#g' \
          -e 's#([Aa]uthorization:[[:space:]]*[Bb]earer[[:space:]]+)[^[:space:]]+#\1<redacted>#g' \
          -e 's#\b((token|api[_-]?key|secret|password|passwd|pwd|access[_-]?token|client[_-]?secret)['"'"'"]?[[:space:]]*[:=][[:space:]]*['"'"'"]?)[^[:space:]'"'"'"&,}]+#\1<redacted>#Ig' \
          -e 's#(enc:(v1|cp1):)[A-Za-z0-9=_-]+#\1<redacted>#g' \
          -e 's#\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}#\1<redacted>@<redacted>#g' \
      || true
    return
  fi
  say "Tailing app + worker logs — Ctrl-C to stop."
  # no-op INT handler: Ctrl-C kills the compose child (children get default
  # signal handling) but this script survives and returns to the menu
  trap ' ' INT
  # PYTHONUNBUFFERED: podman-compose (Python) block-buffers `logs -f` when
  # stdout is not a tty — lines never appear if the output is piped. No-op
  # for docker compose (Go).
  PYTHONUNBUFFERED=1 $COMPOSE logs -f --tail=50 app worker || true
  trap - INT
  echo
}

cmd_backup() {
  local dest
  # NO maint_lock here: scripts/backup.sh takes the same lock itself, and a
  # child that re-opens the lock file contends with its parent's held flock
  # (per open-file-description, not per process) — holding it here would
  # make every on-demand backup wait the full timeout and then "skip".
  [ -f "$ROOT/scripts/backup.sh" ] || die "No scripts/backup.sh in $ROOT."
  dest=$(backup_dir)
  say "Backing up to $dest …"
  "$ROOT/scripts/backup.sh" "$ROOT" "$dest"
}

# ---- database client plumbing (external-db aware) -------------------
# When docker/.env routes compose through compose.external-db.yaml
# (COMPOSE_FILE=…external-db…) there is no postgres container — every
# maintenance psql/pg_dump/pg_isready runs the app image's client tools in
# a one-off `migrate` container, whose env carries the provider-admin DSNs
# (OIKONOME_ADMIN_DSN → the app db, OIKONOME_MAINT_DSN → the maintenance
# db for DROP/CREATE DATABASE). Local installs keep the exec-in-postgres
# path byte-for-byte.
db_external() {
  case $(env_value COMPOSE_FILE) in *external-db*) return 0;; esac
  return 1
}
db_run() {  # db_run '<shell snippet using $OIKONOME_*_DSN>'
  $COMPOSE run --rm --no-deps -T migrate sh -c "$1"
}
pg_ready() {
  if db_external; then db_run 'exec pg_isready -q -d "$OIKONOME_ADMIN_DSN"' >/dev/null 2>&1
  else $COMPOSE exec -T postgres pg_isready -U postgres >/dev/null 2>&1; fi
}
pg_dump_db() {  # plain-SQL dump of the app database to stdout (portable:
                # --no-owner so a dump restores under any admin user)
  if db_external; then db_run 'exec pg_dump --no-owner "$OIKONOME_ADMIN_DSN"'
  else $COMPOSE exec -T postgres pg_dump --no-owner -U postgres oikonome; fi
}
pg_app_sql() {  # psql flags/statements against the app database
  if db_external; then db_run "exec psql \"\$OIKONOME_ADMIN_DSN\" $(printf '%q ' "$@")"
  else $COMPOSE exec -T postgres psql -U postgres -d oikonome "$@"; fi
}
pg_maint_sql() {  # psql against the MAINTENANCE db (DROP/CREATE DATABASE)
  if db_external; then db_run "exec psql \"\$OIKONOME_MAINT_DSN\" $(printf '%q ' "$@")"
  else $COMPOSE exec -T postgres psql -U postgres -d postgres "$@"; fi
}
drop_recreate_db() {
  # The name is spliced into SQL, so it is checked and then quoted as an
  # identifier: unquoted, psql folds `Ledger` to `ledger` and would force-
  # drop a DIFFERENT database on a shared cluster while leaving the app's
  # own in place, and a hyphenated name is a syntax error that makes every
  # reset and restore fail. Quoted, it is the exact name the app's DSN uses.
  local db; db=$(db_name) || return 1
  pg_maint_sql -q \
    -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE);" \
    -c "CREATE DATABASE \"$db\";"
}
db_name() {
  # OIKONOME_DB_NAME names the database only in external-db mode: the
  # bundled postgres's DSNs, POSTGRES_DB and every dump/load above use
  # `oikonome` whatever the setting says, so a value left over from an
  # external setup must not make a reset or restore drop some other name
  # while loading into `oikonome`.
  db_external || { echo oikonome; return 0; }
  # read the way compose reads it, since compose builds the app's DSN from it
  local n
  n=$(env_value OIKONOME_DB_NAME)
  n=${n:-oikonome}
  case $n in
    *[!A-Za-z0-9_-]*)
      warn "OIKONOME_DB_NAME in docker/.env ('$n') may use only letters, digits, '_' and '-' — nothing touched." >&2
      return 1;;
  esac
  [ "${#n}" -le 63 ] || { warn "OIKONOME_DB_NAME is longer than PostgreSQL's 63-character limit — nothing touched." >&2; return 1; }
  echo "$n"
}

# dump_looks_real FILE — 0 when FILE can plausibly be an Oikonome dump.
# Sets REJECT to the reason when it isn't.
#
# Everything downstream of the drop treats "no error" as "restored": gzip
# reports success on an empty stream and psql -v ON_ERROR_STOP=1 exits 0
# when handed zero statements, so a backup whose pg_dump was cut off
# mid-write (the postgres container stopped by a concurrent reset/upgrade)
# loads as a clean success over a database we just dropped. Refuse it
# BEFORE anything is destroyed: the gzip container must be intact, and
# what comes out of it must carry actual schema or data.
dump_looks_real() {
  local file=$1 head_txt
  REJECT=""
  if ! gzip -t "$file" 2>/dev/null; then
    REJECT="it is not a readable gzip file — truncated, corrupt, or not a backup"
    return 1
  fi
  # 256KB is far past pg_dump's header (comments + SET lines) and reaches
  # the first CREATE TABLE of any real dump, without decompressing a
  # multi-GB file just to look at it.
  head_txt=$(gunzip -c "$file" 2>/dev/null | head -c 262144 || true)
  if [ "${#head_txt}" -le 1024 ]; then
    REJECT="it decompresses to only ${#head_txt} bytes — an empty or truncated dump, not a backup"
    return 1
  fi
  case $head_txt in
    *"CREATE TABLE"*|*"COPY "*|*"INSERT INTO"*) return 0;;
  esac
  REJECT="it contains no schema or data (no CREATE TABLE / COPY / INSERT)"
  return 1
}

# restore_failed MESSAGE — the database has been dropped and the restore
# cannot be completed. Put the pre-restore snapshot back if we have one,
# then die telling the operator exactly where their data is. Reads $pre
# from its caller (cmd_restore).
restore_failed() {
  warn "$1"
  if [ -n "${pre:-}" ]; then
    restore_rollback "$pre" \
      && die "restore failed, but your data was rolled back intact. Snapshot kept at: $pre"
    die "restore failed AND the automatic rollback failed — the database is EMPTY. Reload the snapshot: ./oikonome.sh restore $(basename "$pre")"
  fi
  die "restore failed — the database is EMPTY (no rollback snapshot could be taken); fix the dump and restore again"
}

restore_rollback() {
  # Reload the pre-restore snapshot ($1) after a failed drop/recreate or
  # dump load. Returns 0 when the database is back exactly as it was.
  say "Rolling back to the snapshot taken a moment ago…"
  drop_recreate_db >/dev/null 2>&1 || true
  if gunzip -c "$1" | pg_app_sql -q -v ON_ERROR_STOP=1 >/dev/null 2>&1; then
    $COMPOSE up -d >/dev/null 2>&1 || true
    ok "Database rolled back — everything is as it was before this restore."
    return 0
  fi
  return 1
}

cmd_restore() {
  local file=${1:-} dest choice n i f
  maint_lock restore
  dest=$(backup_dir)

  if [ -z "$file" ]; then
    # interactive: list dumps newest-first with date + size, pick a number
    DUMPS=()
    while IFS= read -r __l; do DUMPS+=("$__l"); done \
      < <(ls -1t "$dest"/oikonome-*.sql.gz 2>/dev/null)
    [ ${#DUMPS[@]} -gt 0 ] || die "No backups found in $dest"
    echo
    say "Backups in $dest (newest first):"
    i=1
    for f in "${DUMPS[@]}"; do
      printf "  %2d) %s  %s  %s\n" "$i" "$(basename "$f")" \
        "$(fmt_ts "$(mtime "$f")")" \
        "$(du -h "$f" | cut -f1)"
      i=$((i + 1))
    done
    printf "  Restore which one? [1-%d, Enter cancels]: " "${#DUMPS[@]}"
    tty_read choice
    [ -n "$choice" ] || { echo "  Cancelled."; return 0; }
    case $choice in *[!0-9]*|'') die "Not a number: $choice";; esac
    n=$choice
    { [ "$n" -ge 1 ] && [ "$n" -le ${#DUMPS[@]} ]; } || die "Out of range: $n"
    file=${DUMPS[$((n - 1))]}
  else
    # bare filename → look in the backup dir
    [ -f "$file" ] || file="$dest/$file"
    [ -f "$file" ] || die "Backup file not found: ${1}"
  fi

  # Canonicalize to an ABSOLUTE path before the `cd "$ROOT/docker"` below —
  # a relative path (e.g. `restore backups/foo.sql.gz`, exactly what `backup`
  # prints) resolves here but is stranded once we change directories, and the
  # load step then fails with a bogus "bad dump".
  case $file in
    /*) : ;;
    *) file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")" ;;
  esac

  # Screen the dump BEFORE the confirm — never ask an operator to sign off
  # on a restore that cannot work, and never drop a live database for one.
  if ! dump_looks_real "$file"; then
    die "That file cannot be restored: $REJECT
  File: $file
  Nothing was touched. Pick another backup (./oikonome.sh restore) — the
  newest one is not always the good one: a backup interrupted mid-dump
  lands with the newest timestamp."
  fi

  # an external database's name is spliced into the drop below: a name the
  # drop would refuse stops here, before the confirm and the snapshot
  if db_external; then
    db_name >/dev/null || die "Fix OIKONOME_DB_NAME in docker/.env, then re-run restore."
  fi

  echo
  echo "  This REPLACES the current database with:"
  echo "    $file"
  echo "  Everything added since that dump will be LOST."
  confirm_word restore "Type 'restore' to confirm: " || return 1

  find_compose
  cd "$ROOT/docker"

  say "Making sure the stack is up…"
  $COMPOSE up -d
  for i in $(seq 1 30); do
    pg_ready && break
    [ "$i" = 30 ] && die "postgres never became ready"
    sleep 2
  done

  say "Stopping app + worker (they hold database connections)…"
  $COMPOSE stop app worker >/dev/null 2>&1 || true   # FORCE below covers stragglers

  # Snapshot the CURRENT database BEFORE the drop — without it a
  # truncated/wrong dump leaves the database empty with nothing to fall back
  # on ("fix the dump and restore again" is no help once the data is gone).
  # The snapshot
  # lands next to the other dumps, so it's restorable like any backup.
  local pre had_users
  pre="$dest/oikonome-pre-restore-$(date +%Y%m%d-%H%M%S).sql.gz"
  # The backups DIRECTORY deliberately keeps the conventional mode: unlike
  # backup.sh's external destination, this one is bind-mounted read-only
  # into the app container (../backups:/state/backups) so the admin
  # console's backup card can list it, and uid 10001 cannot traverse an
  # 0700 directory owned by the host user. The protection that matters is
  # on the dumps themselves, which are 0600 whoever can open the folder.
  shared_umask mkdir -p "$dest"
  # How much this restore is risking, measured BEFORE the drop: an
  # instance with accounts on it must never end up with none. Any error
  # (fresh database, no schema yet) reads as zero.
  had_users=$(pg_app_sql -tA -c "SELECT count(*) FROM users;" 2>/dev/null | tr -dc '0-9')
  had_users=${had_users:-0}
  say "Snapshotting the current database first…"
  # The snapshot lands under the same oikonome-*.sql.gz name the picker
  # lists, so it is held to the same standard as any other backup: a
  # snapshot that isn't a real dump is not a rollback point, and keeping
  # it would only offer the operator a second way to wipe themselves.
  if pg_dump_db 2>/dev/null | gzip > "$pre" && dump_looks_real "$pre"; then
    chmod 600 "$pre" 2>/dev/null || true   # a dump is the whole DB in plaintext
    ok "Rollback point saved: $pre"
  else
    rm -f "$pre"; pre=""
    # proceed snapshot-less ONLY when there is genuinely nothing
    # to lose. A snapshot can fail on a full disk or a pg_dump hiccup with
    # the database very much alive — dropping it then would destroy the
    # only copy. A non-zero account count means the schema exists AND
    # someone has signed up.
    if [ "$had_users" -gt 0 ]; then
      die "Couldn't snapshot the current database, and it is NOT empty ($had_users account(s)) — refusing to drop it. Free disk space or take a backup (./oikonome.sh backup), then retry."
    fi
    warn "Couldn't snapshot the current database (new/empty instance?) — continuing without a rollback point."
  fi

  # explicit || die on the critical steps: when the menu calls this function
  # with `cmd_restore || warn`, bash suppresses errexit inside it — a failed
  # drop or load must NOT fall through to the next step
  say "Dropping and recreating the database…"
  if ! drop_recreate_db; then
    # the DROP may have succeeded with only the CREATE failing —
    # "untouched" is not a safe claim. If we hold a snapshot, put it back.
    if [ -n "$pre" ]; then
      restore_rollback "$pre" \
        && die "drop/recreate failed, but your data was rolled back intact. Snapshot kept at: $pre"
      die "drop/recreate failed AND the rollback failed — reload the snapshot once postgres is healthy: ./oikonome.sh restore $(basename "$pre")"
    fi
    die "drop/recreate failed — check ./oikonome.sh logs"
  fi

  say "Loading dump (this can take a minute)…"
  if ! gunzip -c "$file" | pg_app_sql -q -v ON_ERROR_STOP=1 >/dev/null; then
    restore_failed "Dump load FAILED — the file may be truncated or not an Oikonome backup."
  fi

  # psql's exit code says "no statement errored", not "the data is there" —
  # a dump with nothing in it errors on nothing. Ask the database itself
  # what it now holds before we call this a restore.
  say "Verifying the restored database…"
  local ntab missing nusers
  ntab=$(pg_app_sql -tA -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';" 2>/dev/null | tr -dc '0-9')
  missing=$(pg_app_sql -tA -c "SELECT coalesce(string_agg(t, ', '), '') FROM unnest(ARRAY['tenants','users','accounts','transactions']) AS t WHERE to_regclass('public.' || t) IS NULL;" 2>/dev/null | tr -d '\r ')
  if [ "${ntab:-0}" -lt 1 ] || [ -n "$missing" ]; then
    restore_failed "The load reported success but the database is EMPTY or incomplete (${ntab:-0} tables${missing:+, missing: $missing}) — that dump was not a real backup."
  fi
  nusers=$(pg_app_sql -tA -c "SELECT count(*) FROM users;" 2>/dev/null | tr -dc '0-9')
  nusers=${nusers:-0}
  if [ "$nusers" -lt 1 ] && [ "$had_users" -gt 0 ]; then
    # A dump truncated after the schema but before the data loads without
    # a single psql error and leaves the instance with its tables and none
    # of its rows. Trading $had_users accounts for zero is not a restore.
    restore_failed "The dump loaded without errors but restored NO accounts, replacing an instance that had $had_users — it is truncated (schema only, no data)."
  fi
  if [ "$nusers" -lt 1 ]; then
    warn "Restored $ntab tables, but there are no accounts in them — was this dump taken before anyone signed up?"
  else
    ok "Restored $ntab tables, $nusers account(s)."
  fi

  # migrations run at app-container start (advisory-locked); /readyz only
  # answers once they're done, so the health wait below covers both
  say "Starting app + worker (migrations run at start)…"
  say "Health check…"
  # The start itself is tested, not run bare: under docker compose v2 a
  # one-shot migrate service that fails makes `up -d` exit non-zero, and
  # under errexit a bare call would end the script before the rollback.
  if ! $COMPOSE up -d || ! wait_ready 60; then
    # The current code cannot start on what was just loaded (a migration
    # that fails on an old dump, a schema it cannot read). Left here, the
    # instance stays down on data nothing runs on, so put the snapshot
    # taken above back; the dump itself is untouched and can be tried again.
    # app + worker first: their restart loop would otherwise reconnect and
    # re-run migrations into the database being reloaded.
    # With no snapshot (the instance was empty) there is nothing to go back
    # to, and the restored data stays where it is.
    [ -n "$pre" ] || return 1
    $COMPOSE stop app worker >/dev/null 2>&1 || true
    restore_failed "The app did not come up on the restored database (see ./oikonome.sh logs). The dump is unchanged: $file"
  fi

  # The restored ciphertexts must decrypt under THIS instance's
  # OIKONOME_MASTER_KEY — a dump from another install loads fine, but its
  # bank tokens / TOTP seeds were encrypted under THAT install's key, and
  # nothing would say so until syncs die with crypto errors. Probe now.
  local proj cname kout
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" app)
  if [ -n "$cname" ]; then
    if kout=$($ENGINE exec "$cname" oikonome keycheck 2>&1); then
      ok "Encryption key check: the restored secrets decrypt under this instance's key."
    else
      case $kout in
        *"invalid choice"*) : ;;  # image predates `oikonome keycheck` — skip quietly
        *)
          warn "The restored data was encrypted under a DIFFERENT master key!"
          echo "  Bank tokens and 2FA seeds in this dump will NOT decrypt here."
          echo "  Fix: copy OIKONOME_MASTER_KEY from the install this dump came"
          echo "  from (its docker/.env) into this docker/.env, then apply it:"
          echo "    ./oikonome.sh install"
          echo "  (everything else — transactions, accounts, budgets — is fine)"
          ;;
      esac
    fi
  fi
  ok "Restore complete: $(basename "$file")"
}

cmd_reset() {
  local data_dir keep_port raw_key old_key="" new_key="" saved="" external=""
  local settings db_host
  case ${1:-} in
    "") : ;;
    --new-key) new_key=1 ;;
    *) die "Usage: ./oikonome.sh reset [--new-key]" ;;
  esac
  maint_lock reset
  # --new-key: mint a fresh master key too (e.g. the old one may have
  # leaked). Without it the reinstall keeps the old key, because every
  # backup in ./backups was encrypted under it: its two-factor seeds,
  # webhook secrets and API keys decrypt under that key and no other.
  # Either way the old key is written beside the backups before anything
  # is deleted, since docker/.env, its only other copy, is replaced below.
  # Read and check the key BEFORE anything is touched. It is read the way
  # compose reads it (quotes, CR, inline comment), so a key the running
  # stack accepts is the key carried over; a value that is not a key at all
  # stops the reset here, with the database and docker/.env still intact.
  raw_key=$(env_value OIKONOME_MASTER_KEY)
  if [ -n "$raw_key" ]; then
    old_key=$(master_key_normal "$raw_key") || die \
      "OIKONOME_MASTER_KEY in docker/.env is not a master key (32 bytes, urlsafe base64) — nothing touched. Correct that line (docs/master-key.md), then re-run reset."
  fi
  db_external && external=1
  settings=$(reset_carried_settings "$external")

  # read the data location BEFORE deleting .env, the way compose reads it
  # (OIKONOME_DATA_DIR overrides; relative to docker/), so the folder this
  # deletes is the folder postgres mounted. Unset, the whole ./data goes.
  data_dir=$(data_dir_resolve "$ROOT/docker" "$ROOT/data")
  if [ -n "$external" ]; then
    # checked before the confirm, so a name the drop would refuse stops
    # the reset with nothing deleted
    db_name >/dev/null || die "Fix OIKONOME_DB_NAME in docker/.env, then re-run reset."
  fi
  db_host=$(env_value OIKONOME_DB_HOST)

  echo
  echo "  This WIPES the instance at $ROOT:"
  if [ -n "$external" ]; then
    echo "    • stack down; the database $(db_name) on ${db_host:-the external server} is"
    echo "      DROPPED and created again empty"
  else
    echo "    • stack down, DATABASE deleted ($data_dir)"
  fi
  echo "    • linked banks are disconnected at the provider (link them again after)"
  echo "    • the generated passwords and the setup link are replaced"
  echo "    • then reinstalls → the fresh setup wizard"
  echo "  Kept: the code, your backups, and every other setting in docker/.env —"
  echo "  port, HTTPS domain, bind address, external database, admin console,"
  echo "  email (SMTP), LLM, data folder and provider keys."
  if [ -n "$old_key" ] && [ -z "$new_key" ]; then
    echo "  The master key is kept as well, and a copy is saved beside the"
    echo "  backups: restoring one brings back its two-factor seeds, webhook"
    echo "  secrets and API keys. Its banks have to be linked again — this reset"
    echo "  disconnects them, so no backup can bring them back."
  elif [ -n "$old_key" ]; then
    echo "  The master key is replaced: backups taken before now cannot restore"
    echo "  their two-factor seeds, webhook secrets or API keys without the OLD"
    echo "  key. It is saved next to them, owner-only, as"
    echo "  $(backup_dir)/master-key-before-reset-<date>.env —"
    echo "  move it to a password manager (docs/master-key.md). Banks are linked"
    echo "  again after any restore: this reset disconnects them."
  fi
  echo "  This cannot be undone."
  confirm_word reset "Type 'reset' to confirm: " || return 1
  if [ -n "$old_key" ]; then
    # written before anything is deleted, and the reset stops if it cannot
    # be: a key that only lived in the .env about to go is unrecoverable,
    # and so is one handed to a reinstall that then fails
    saved="$(backup_dir)/master-key-before-reset-$(date +%Y%m%d-%H%M%S).env"
    ( umask 077; mkdir -p "$(backup_dir)" \
        && printf '# master key the backups in this folder were taken under (docs/master-key.md)\nOIKONOME_MASTER_KEY=%s\n' \
             "$old_key" > "$saved" ) \
      || die "Couldn't save the master key to $saved — nothing deleted."
    chmod 600 "$saved"
    ok "Master key saved: $saved"
  fi

  find_compose
  cd "$ROOT/docker"

  # remember the port before .env is deleted: a reset regenerates
  # secrets, but the instance should not silently move ports
  keep_port=$(app_port)

  # down WITHOUT -v, deliberately (uninstall is the one that uses -v):
  # the named volumes hold no user data — redis has no volume, the
  # database is a bind mount deleted below — just the multi-GB ollama
  # model cache and Caddy's certs, both worth keeping across a reset
  # (same port → same project name → the reinstall reattaches them).
  # Linked banks hold their access tokens only in this database. Release
  # them at the provider while the app can still run, or every Item keeps
  # billing (and keeps its bank consent) with no handle left to remove it.
  # Best effort: what could not be released is named, for the dashboard.
  local proj cname
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" app)
  if [ -n "$cname" ]; then
    say "Disconnecting linked banks at the provider…"
    $ENGINE exec "$cname" python -m oikonome.db.reset --release-links \
      || warn "Some bank links could not be released (listed above) — remove them in the provider's dashboard."
  else
    warn "The app isn't running, so linked banks can't be released here — remove them in the provider's dashboard (Plaid: Items), or they keep billing."
  fi

  say "Stopping the stack…"
  $COMPOSE down >/dev/null 2>&1 || true

  if [ -n "$external" ]; then
    # There is no data folder to remove: the database lives on the external
    # server, and only dropping it there deletes it — the same drop a
    # restore makes, through the migrate service's maintenance connection.
    # The app and admin roles stay; the reinstall's migrate gives them the
    # new passwords.
    say "Dropping the database $(db_name) on ${db_host:-the external server} …"
    drop_recreate_db || die "Couldn't drop the database on the external server — docker/.env and the database are untouched. Check the connection (./oikonome.sh status), then re-run reset."
  else
    say "Deleting the database at $data_dir …"
    if ! rm -rf "$data_dir" 2>/dev/null; then
      # rootless podman: postgres's files belong to a subuid, so plain rm
      # gets EPERM — deleting inside the user namespace is the fix
      if command -v podman >/dev/null 2>&1; then
        podman unshare rm -rf "$data_dir" 2>/dev/null || true
      fi
      # rootful docker: postgres's files belong to the container uid (e.g.
      # 999), so a host rm gets EPERM. A throwaway ROOT container removes them
      # without needing host sudo — the fix for headless/non-root callers like
      # the admin-console host-agent.
      if [ -e "$data_dir" ] && command -v docker >/dev/null 2>&1; then
        docker run --rm -v "$data_dir":/d busybox \
          find /d -mindepth 1 -delete >/dev/null 2>&1 || true
        rmdir "$data_dir" 2>/dev/null || rm -rf "$data_dir" 2>/dev/null || true
      fi
    fi
    if [ -e "$data_dir" ] && [ -n "$(ls -A "$data_dir" 2>/dev/null)" ]; then
      die "Couldn't delete $data_dir (files owned by the container uid). Run: sudo rm -rf '$data_dir'  then re-run reset."
    fi
    rm -rf "$data_dir" 2>/dev/null || true
  fi
  rm -f "$ROOT/docker/.env" "$ROOT/docker/compose.override.yaml"
  ok "Database and generated passwords removed; settings carried over."

  # The kept settings and key travel in the environment, never on a
  # command line, where any account on the host could read them in ps.
  if [ -z "$new_key" ] && [ -n "$old_key" ]; then
    say "Reinstalling (fresh passwords, same master key, same settings, fresh setup wizard)…"
  else
    say "Reinstalling (fresh secrets, same settings, fresh setup wizard)…"
  fi
  (
    export OIKONOME_PORT="$keep_port" OIKONOME_RESET_SETTINGS="$settings"
    if [ -z "$new_key" ] && [ -n "$old_key" ]; then
      export OIKONOME_KEEP_MASTER_KEY="$old_key"
    fi
    shared_umask "$ROOT/install.sh"
  )
}

# The proxy trust `https <domain>` writes when the operator set none: the
# engines' container NAT ranges (podman netavark 10.88/15, docker 172.16/12).
HTTPS_PROXY_TRUST="10.88.0.0/15,172.16.0.0/12"

cmd_https() {
  # /oikonome.sh https <domain>   → front the app with a TLS reverse proxy
  # /oikonome.sh https off        → turn it back off (plain http)
  local domain=${1:-} port envf="$ROOT/docker/.env"
  [ -f "$envf" ] || die "No install here yet — run ./oikonome.sh install first."
  find_compose
  if [ "$domain" = off ] || [ "$domain" = disable ]; then
    local was; was=$(env_get OIKONOME_DOMAIN)
    remove_profile tls
    set_env OIKONOME_DOMAIN ""
    # Undo what `https <domain>` pinned, and only that: a base URL left at
    # https://<domain> makes every emailed link (password reset, invites,
    # the daily email) point at a host nothing serves any more, and the
    # container-network trust left with no proxy in front lets a client
    # that connects straight to the app port forge its address and scheme
    # in X-Forwarded-*. Values the operator wrote themselves are kept.
    if [ -n "$was" ] && [ "$(env_get OIKONOME_BASE_URL)" = "https://$was" ]; then
      set_env OIKONOME_BASE_URL ""
    fi
    if [ "$(env_get OIKONOME_TRUSTED_PROXIES)" = "$HTTPS_PROXY_TRUST" ]; then
      set_env OIKONOME_TRUSTED_PROXIES ""
    fi
    say "Stopping the TLS proxy…"
    ( cd "$ROOT/docker" && COMPOSE_PROFILES=tls $COMPOSE down caddy \
        >/dev/null 2>&1 || true )
    ok "HTTPS off — the app serves plain http on :$(app_port) again."
    echo "  Re-run install to apply: ./oikonome.sh install"
    return 0
  fi
  [ -n "$domain" ] || die "Usage: ./oikonome.sh https <domain>   (or: https off)"
  case $domain in *.*) : ;; *) die "Give a real hostname (e.g. oiko.example.com), not '$domain'.";; esac

  # Caddy binds host 80/443 (or OIKONOME_HTTP_PORT/OIKONOME_HTTPS_PORT from
  # env) — say so up front when something else already holds them (another
  # proxy, a second instance's Caddy) instead of letting compose fail late.
  # Skipped when THIS instance's Caddy is the listener (re-run of https).
  local http_port https_port p busy=""
  http_port=$(env_get OIKONOME_HTTP_PORT);   http_port=${http_port:-80}
  https_port=$(env_get OIKONOME_HTTPS_PORT); https_port=${https_port:-443}
  local proj
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  if [ -z "$(svc_container "$proj" caddy)" ]; then
    for p in "$http_port" "$https_port"; do
      if port_busy "$p"; then busy="$busy $p"; fi
    done
    if [ -n "$busy" ]; then
      warn "Port(s)$busy are already in use by something else on this host."
      echo "  Caddy needs them free. Either stop whatever is listening there,"
      echo "  or move this instance's proxy ports in docker/.env, e.g.:"
      echo "    OIKONOME_HTTP_PORT=8080"
      echo "    OIKONOME_HTTPS_PORT=8443"
      echo "  (Certificates still need the domain reachable on 80/443 — with"
      echo "  moved ports, forward 80→${http_port} and 443→${https_port} at your router.)"
      echo "  Continuing — compose will report the bind failure if they stay taken."
    fi
  fi

  # the Caddyfile: automatic HTTPS for the domain, reverse-proxy to the app
  # (domain written literally — quoted heredoc so bash doesn't touch it)
  # Behind Cloudflare (OIKONOME_BEHIND_CLOUDFLARE=1) trust CF edge
  # ranges + read the real visitor from Cf-Connecting-Ip so client_ip still
  # resolves the real client (spoof-safe: a direct-origin forger, peer not in
  # a CF range, is ignored). Refresh the list if CF changes it: cloudflare.com/ips.
  cf_global="" cf_rp=""
  if env_flag OIKONOME_BEHIND_CLOUDFLARE; then
    cf_ranges="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"
    cf_global="{
	servers {
		trusted_proxies static ${cf_ranges}
		client_ip_headers Cf-Connecting-Ip
	}
}

"
    cf_rp=" {
		header_up X-Forwarded-For {client_ip}
	}"
  fi
  cat > "$ROOT/docker/Caddyfile" <<CADDY
${cf_global}${domain} {
	reverse_proxy app:8080${cf_rp}
	encode zstd gzip
	# security headers at the edge too. The app sets the same
	# set itself (SecurityHeadersMiddleware); this covers responses Caddy
	# originates (errors while the app is down/restarting) and holds if
	# the app layer is ever misconfigured. The ? op means set-if-absent
	# (deferred until the upstream response is present) — a plain set
	# here DUPLICATED each header alongside the app's copy. HSTS is safe
	# here: this site block only ever serves HTTPS. CSP stays app-side
	# (path-dependent).
	header {
		?Strict-Transport-Security "max-age=31536000; includeSubDomains"
		?X-Content-Type-Options "nosniff"
		?X-Frame-Options "DENY"
		?Referrer-Policy "strict-origin-when-cross-origin"
		# genericize the upstream Server banner
		# (uvicorn) — low-value recon otherwise. -Via drops Caddy's hop
		# marker too.
		Server "Oikonome"
		-Via
	}
}
CADDY
  set_env OIKONOME_DOMAIN "$domain"
  set_env OIKONOME_BASE_URL "https://$domain"
  # Secure cookies/HSTS honor X-Forwarded-Proto only from peers in
  # OIKONOME_TRUSTED_PROXIES. Caddy connects from the engine's container
  # network — host-internal NAT ranges (podman netavark 10.88/15, docker
  # 172.16/12) a LAN client can never source from — so trusting them
  # enables the proxy without trusting the LAN. Operator overrides kept.
  [ -n "$(env_get OIKONOME_TRUSTED_PROXIES)" ] \
    || set_env OIKONOME_TRUSTED_PROXIES "$HTTPS_PROXY_TRUST"
  add_profile tls
  ok "Wrote docker/Caddyfile and pinned the base URL to https://$domain."
  echo
  echo "  Caddy will fetch a real certificate automatically. For that the"
  echo "  domain must resolve to THIS machine and Caddy must be able to"
  echo "  complete a challenge:"
  echo "    • Public / port-forwarded (80+443 reachable): works out of the box."
  echo "    • LAN-only: point $domain at this box in your LAN DNS and either"
  echo "      forward 80/443, or use a DNS-01 provider (custom Caddy build)."
  echo "    • Easiest LAN option with NO certs to manage: Tailscale —"
  echo "      'tailscale serve https / http://localhost:$(app_port)' gives a"
  echo "      trusted https URL with zero cert management."
  warn_rootless_podman
  echo
  say "Bringing the stack up with the TLS proxy…"
  shared_umask "$ROOT/install.sh"
  echo
  ok "Open https://$domain — cookies are Secure, base URL pinned."
}


# bundled model tiers — both Apache-2.0. The 7b is markedly better at
# categorization; the 1.5b runs anywhere. Auto-picked by host RAM unless a
# model is named explicitly (./oikonome.sh llm on <model>).
LLM_MODEL_SMALL=qwen2.5:1.5b     # ~1 GB download
LLM_MODEL_BIG=qwen2.5:7b         # ~4.7 GB download, wants ~8 GB free RAM
pick_llm_model() {
  local kb; kb=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null)
  if [ -n "$kb" ] && [ "$kb" -ge $((12 * 1024 * 1024)) ]; then
    printf '%s' "$LLM_MODEL_BIG"
  else
    printf '%s' "$LLM_MODEL_SMALL"
  fi
}

# the ollama container's memory cap must fit the LARGEST model wired up.
# Measured: qwen2.5vl:7b peaks ~8 GB parsing one receipt photo — under a
# 3600m cap the runner is memcg-OOM-killed mid-parse and every receipt
# upload 500s. Vision models carry image-token buffers on top of
# weights, so they rank above the same-size chat tier.
llm_mem_limit() {
  case $1 in
    *vl:7b*|*vl:14b*|*:14b*) printf 10g;;
    *:7b*|*vl:3b*)           printf 6g;;
    *)                       printf 3600m;;
  esac
}
mem_to_mb() {  # "10g" / "3600m" → MB, for only-raise comparison
  case $1 in
    *g) printf '%s' "$(( ${1%g} * 1024 ))";;
    *m) printf '%s' "${1%m}";;
    *)  printf 0;;
  esac
}
# raise (never lower) OLLAMA_MEM_LIMIT for this model, so enabling vision
# doesn't shrink a cap already sized for a bigger chat model
llm_bump_mem() {
  local want cur; want=$(llm_mem_limit "$1"); cur=$(env_get OLLAMA_MEM_LIMIT)
  if [ -z "$cur" ] || [ "$(mem_to_mb "$want")" -gt "$(mem_to_mb "$cur")" ]; then
    set_env OLLAMA_MEM_LIMIT "$want"
  fi
}

# pull the model into the running ollama container + wire the app to it.
# used by both `llm on` and the installer's opt-in prompt.
llm_enable() {
  local model=${1:-$(pick_llm_model)} cname
  add_profile llm
  set_env OIKONOME_LLM_URL "http://ollama:11434"
  set_env OIKONOME_LLM_MODEL "$model"
  llm_bump_mem "$model"
  say "Starting the local model server (Ollama)…"
  ( cd "$ROOT/docker" && $COMPOSE up -d ollama ) || return 1
  # scope to THIS install's compose project — filtering on the service
  # label alone matches every oikonome instance on the host, and the model
  # then gets pulled into some OTHER instance's ollama container
  local proj; proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" ollama)
  [ -n "$cname" ] || { warn "ollama container didn't start — see ./oikonome.sh logs"; return 1; }
  # wait for the server to answer before pulling
  local i
  for i in $(seq 1 30); do
    $ENGINE exec "$cname" ollama list >/dev/null 2>&1 && break
    sleep 2
  done
  say "Downloading $model (one time; cached in a volume)…"
  $ENGINE exec "$cname" ollama pull "$model" || { warn "model download failed — retry later with: ./oikonome.sh llm on"; return 1; }
  ok "Model $model ready — smart categorization + receipt parsing are on."
}

cmd_llm() {
  # /oikonome.sh llm on   → bundle a local model (Ollama) + wire it up
  # /oikonome.sh llm off  → stop it, back to manual categorization
  local action=${1:-} model=${2:-}
  [ -f "$ROOT/docker/.env" ] || die "No install here yet — run ./oikonome.sh install first."
  find_compose
  case $action in
    off|disable)
      remove_profile llm
      set_env OIKONOME_LLM_URL ""
      set_env OIKONOME_LLM_MODEL ""
      say "Stopping the local model server…"
      # `stop <service>` (not `down <service>`): podman-compose and older
      # docker compose don't take service args on `down` — the error is
      # swallowed and the container keeps running, unmanaged once the
      # profile is off
      ( cd "$ROOT/docker" && COMPOSE_PROFILES=llm $COMPOSE stop ollama \
          >/dev/null 2>&1 || true )
      say "Restarting the app…"
      ( cd "$ROOT/docker" && $COMPOSE up -d >/dev/null 2>&1 || true )
      ok "Local LLM off — categorization is the aggregator's + manual overrides."
      echo "  (The downloaded model volume is kept; 'llm on' reuses it.)"
      return 0;;
    on)
      llm_enable "$model" || return 1
      say "Restarting the app to use the model…"
      ( cd "$ROOT/docker" && $COMPOSE up -d ) >/dev/null 2>&1 || true
      echo
      ok "Local LLM ON. Runs on THIS host (CPU works; a GPU is much faster)."
      echo "  Categorizes vague/opaque merchants (e.g. SimpleFIN) + parses"
      echo "  receipts. Turn off any time: ./oikonome.sh llm off"
      return 0;;
    vision)
      # the bundled chat tiers (qwen2.5) are text-only — receipt
      # images/PDFs need a vision-capable model alongside them.
      model=${model:-qwen2.5vl:3b}
      printf '%s' "$(env_get COMPOSE_PROFILES)" | grep -qw llm \
        || die "Turn the bundled LLM on first: ./oikonome.sh llm on"
      local proj cname
      # vision models need more headroom than the chat tiers — resize the
      # container's memory cap (and recreate it) BEFORE pulling
      llm_bump_mem "$model"
      ( cd "$ROOT/docker" && COMPOSE_PROFILES=llm $COMPOSE up -d ollama ) \
        >/dev/null 2>&1 || true
      proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
      cname=$(svc_container "$proj" ollama)
      [ -n "$cname" ] || die "ollama container not running — ./oikonome.sh llm on"
      say "Downloading $model (one time; ~3 GB for qwen2.5vl:3b)…"
      $ENGINE exec "$cname" ollama pull "$model" \
        || die "model download failed — retry: ./oikonome.sh llm vision"
      ok "Vision model $model ready."
      echo "  Finish in the web UI: Settings → AI →"
      echo "  Vision model = $model   (receipts then parse automatically)"
      return 0;;
    *) die "Usage: ./oikonome.sh llm <on|off|vision> [model]";;
  esac
}

# ── reset data, keep logins ────────────────────────────────────────────────
cmd_reset_data() {
  # Optional tenant id: wipe just that household. Without it this is EVERY
  # tenant on the instance — one household on a self-host, every customer
  # on a hosted box, and the confirmation has to be able to tell those two
  # apart before anything is deleted.
  local tenant=${1:-}
  [ -f "$ROOT/docker/.env" ] || die "No install here yet."
  find_compose
  local proj cname iname inv
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" app)
  [ -n "$cname" ] || die "app container not running — ./oikonome.sh install first"
  iname=$(env_get COMPOSE_PROJECT_NAME); iname=${iname:-$(basename "$ROOT")}

  # Ask the database what is actually there, and show it. "The instance at
  # $ROOT" reads the same whether it holds one household or forty.
  say "Counting what would be wiped…"
  inv=$($ENGINE exec "$cname" python -m oikonome.db.reset --summary ${tenant:+"$tenant"}) \
    || die "couldn't read the instance's data — is the app container healthy? (./oikonome.sh status)"

  echo
  echo "  This PERMANENTLY wipes financial data on:"
  echo "    instance: $iname   ($ROOT)"
  printf '%s\n' "$inv" | sed 's/^/    /'
  echo
  echo "    • transactions, accounts, bills, receipts, goals, settings — all of it"
  echo "    • logins are KEPT — everyone signs in with the same password"
  echo "    • the guided setup wizard runs again on the next visit"
  echo "  Backups are untouched. This cannot be undone."
  [ -n "$tenant" ] || echo "  To wipe ONE household instead: ./oikonome.sh reset-data <tenant-id>"
  echo
  # The instance name, not a generic word — the same bar `uninstall`
  # already sets, because typing 'reset-data' from muscle memory in the
  # wrong terminal costs exactly as much as the wrong uninstall.
  confirm_word "$iname" "Type the instance name ('$iname') to confirm: " || return 1
  if [ -n "$tenant" ]; then
    $ENGINE exec "$cname" python -m oikonome.db.reset "$tenant" \
      || die "reset failed — see the output above"
  else
    $ENGINE exec "$cname" python -m oikonome.db.reset --all \
      || die "reset failed — see the output above"
  fi
  ok "Data wiped, logins kept. Sign in and the wizard starts fresh."
}

# ── lost password + authenticator + recovery codes ────────────────
cmd_clear_2fa() {
  # The one door for a person who has lost every factor at once: the
  # container CLI removes TOTP, passkeys and recovery codes, signs out
  # every session and device, and emails the account. The password stays;
  # hand them a reset link right after (menu p). VERIFY WHO IS ASKING
  # FIRST — this is the operator asserting an identity check, and the
  # audit row records that they did.
  local email=${1:-}
  [ -f "$ROOT/docker/.env" ] || die "No install here yet."
  find_compose
  if [ -z "$email" ]; then
    printf "  Email of the account to clear the second factor on: "
    tty_read email
    [ -n "$email" ] || { warn "No email given — nothing done."; return 1; }
  fi
  local proj cname
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" app)
  [ -n "$cname" ] || die "app container not running — ./oikonome.sh install first"
  # An owner's clear also deletes the logins of members who joined through
  # that owner's recent invites (they may be what a thief planted). The
  # prompt names them, and the clear is handed that same list back
  # (--expect-members): if someone joined while the prompt waited, it
  # refuses rather than remove a login the operator never saw named.
  local members expect
  members=$($ENGINE exec "$cname" oikonome clear-2fa --preview "$email") || {
    warn "No account with the email '$email' on this instance, or it could not be read."
    return 1
  }
  say "This removes the authenticator, every passkey and every recovery code"
  say "for $email, and signs out all of their sessions and devices."
  if [ -n "$members" ]; then
    warn "It also DELETES these household logins, who joined through this"
    warn "owner's invites in the last few days (re-invite any that are genuine):"
    printf '%s\n' "$members" | sed 's/^/      /'
  fi
  printf "  Type the email again to confirm: "
  local again; tty_read again
  [ "$again" = "$email" ] || { warn "Emails differ — nothing done."; return 1; }
  # the address leads each preview line
  expect=$(printf '%s\n' "$members" | awk 'NF { print $1 }' | paste -sd, -)
  if ! $ENGINE exec "$cname" oikonome clear-2fa --expect-members "$expect" "$email"; then
    warn "Nothing was cleared for '$email' (see above) — no such account, the clear failed, or the household changed since the list was shown."
    return 1
  fi
  echo
  say "Done. Give them a password next: ./oikonome.sh reset-password $email"
}

# ── owner recovery without SMTP ────────────────────────────────────
cmd_reset_password() {
  # /forgot on the login page only helps when SMTP is configured; this is
  # the host-side path: the container CLI mints the same one-time token
  # (1-hour TTL, single use, revokes every session on use) and we turn its
  # /reset path into a clickable URL for the operator to hand over.
  local email=${1:-}
  [ -f "$ROOT/docker/.env" ] || die "No install here yet."
  find_compose
  if [ -z "$email" ]; then
    printf "  Email of the account to reset: "
    tty_read email
    [ -n "$email" ] || { warn "No email given — nothing done."; return 1; }
  fi
  local proj cname
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  cname=$(svc_container "$proj" app)
  [ -n "$cname" ] || die "app container not running — ./oikonome.sh install first"
  local path
  # the CLI exits 1 for an unknown email — report that gently instead of
  # dying (this runs from the menu too); its stderr notes are ours to print
  if ! path=$($ENGINE exec "$cname" oikonome reset-password "$email" 2>/dev/null); then
    warn "No account with the email '$email' on this instance."
    return 1
  fi
  # prefer the pinned base URL (https installs); otherwise offer both the
  # local and the LAN address, since the browser may be on another machine
  local base port host_ip
  base=$(env_get OIKONOME_BASE_URL)
  port=$(app_port)
  echo
  say "One-time password-reset link (valid 1 hour, single use):"
  if [ -n "$base" ]; then
    echo "    ${base%/}${path}"
  else
    echo "    http://localhost:${port}${path}"
    host_ip=$(lan_ip)
    if [ -n "$host_ip" ]; then
      echo "    http://${host_ip}:${port}${path}  (from another machine)"
    fi
  fi
  echo "  Opening it sets a new password and signs the account out everywhere."
  # the same reset the emailed link performs, so the same rules: an account
  # with a second factor proves itself with a recovery code, and the reset
  # then removes every factor (docs/troubleshooting.md)
  echo "  If the account uses an authenticator app or a passkey, the link first"
  echo "  asks for one of its recovery codes, and the reset then removes the"
  echo "  authenticator, every passkey and the recovery codes — set them up again"
  echo "  after signing in. Recovery codes lost too? Run"
  echo "  ./oikonome.sh clear-2fa $email  first (verify who is asking)."
}

# ── start at boot (Podman) ─────────────────────────────────────────────────
# Every service is `restart: unless-stopped`. Docker's daemon applies that
# at boot, so a Docker host brings the stack back by itself. Podman has no
# daemon, and nothing applies a restart policy at boot: a rebooted Podman
# host leaves the instance down until someone starts it. `start-at-boot`
# writes a systemd unit that runs `compose up -d` at boot — a user unit
# for rootless Podman, with lingering turned on so the user's manager
# starts without anyone logging in, or a system unit when run as root.
# /oikonome.sh start-at-boot [on|off]
cmd_start_at_boot() {
  local action=${1:-on} proj unit dir scope="" wanted first rest="" run
  [ -f "$ROOT/docker/.env" ] || die "No install here yet — run ./oikonome.sh install first."
  case $action in on|off) : ;; *) die "Usage: ./oikonome.sh start-at-boot [on|off]" ;; esac
  find_compose
  proj=$(env_get COMPOSE_PROJECT_NAME); proj=${proj:-oikonome}
  unit="oikonome-$proj.service"
  if [ "$(id -u)" = 0 ]; then
    dir=/etc/systemd/system; wanted=multi-user.target
  else
    dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; scope=--user; wanted=default.target
  fi
  if [ "$action" = off ]; then
    command -v systemctl >/dev/null 2>&1 || die "systemd isn't available on this host."
    systemctl $scope disable "$unit" >/dev/null 2>&1 || true
    rm -f "$dir/$unit"
    systemctl $scope daemon-reload >/dev/null 2>&1 || true
    ok "Start at boot turned off ($unit removed)."
    return 0
  fi
  if [ "$ENGINE" = docker ]; then
    ok "Docker restarts this stack at boot by itself (with docker.service enabled) — nothing to set up."
    return 0
  fi
  command -v systemctl >/dev/null 2>&1 || die \
    "systemd isn't available here — start the stack at boot with this host's init system: cd '$ROOT/docker' && $COMPOSE up -d"
  first=${COMPOSE%% *}
  [ "$first" = "$COMPOSE" ] || rest=" ${COMPOSE#* }"
  run="$(command -v "$first")$rest"
  mkdir -p "$dir"
  cat > "$dir/$unit" <<UNIT
# written by ./oikonome.sh start-at-boot — remove with ./oikonome.sh start-at-boot off
[Unit]
Description=Oikonome instance $proj ($ROOT)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$ROOT/docker
Environment=PODMAN_COMPOSE_IN_POD=false
ExecStart=$run up -d
ExecStop=$run stop
TimeoutStartSec=900

[Install]
WantedBy=$wanted
UNIT
  chmod 644 "$dir/$unit"
  systemctl $scope daemon-reload || die "systemctl daemon-reload failed — is a systemd manager running for this user?"
  systemctl $scope enable "$unit" >/dev/null || die "couldn't enable $unit"
  ok "The stack now starts at boot ($dir/$unit)."
  if [ -n "$scope" ]; then
    if loginctl enable-linger "$(id -un)" 2>/dev/null; then
      ok "Lingering on for $(id -un): the unit starts at boot without a login."
    else
      warn "Couldn't turn on lingering — until it is on, the unit starts only when $(id -un) logs in."
      echo "  An administrator can: sudo loginctl enable-linger $(id -un)"
    fi
  fi
}

cmd_uninstall() {
  [ -f "$ROOT/uninstall.sh" ] || die "No uninstall.sh in $ROOT."
  maint_lock uninstall
  # uninstall.sh confirms ('delete'), downs the stack, and removes the WHOLE
  # folder — possibly including this script, so replace ourselves instead
  # of returning
  exec "$ROOT/uninstall.sh"
}

# ── entry ────────────────────────────────────────────────────────────────────

# ── community-script installer ─────────────────────────────────────
# Turns the documented community-script pattern into one command:
# /oikonome.sh script              list scripts + installed state
# /oikonome.sh script add NAME     copy to a stable home, write a config
#                                     skeleton pointed at THIS instance,
#                                     install the systemd user timer
# /oikonome.sh script remove NAME  disable the timer, keep the folder
# Credentials stay host-side by design: the config holds a scoped script
# token (minted in Settings → Connections), never your login password.
SCRIPTS_DIR=${OIKONOME_SCRIPTS_DIR:-$HOME/.local/share/oikonome/scripts}

cmd_script() {
  local action=${1:-} name=${2:-}
  local src="$SCRIPT_HOME/community-scripts"
  [ -d "$src" ] || die "community-scripts/ not found next to this script"
  case $action in
    ""|list)
      echo "  Community scripts:"
      local d n mark
      for d in "$src"/*/; do
        n=$(basename "$d")
        mark=""
        [ -d "$SCRIPTS_DIR/$n" ] && mark="  ✓ installed → $SCRIPTS_DIR/$n"
        printf "    %-10s%s\n" "$n" "$mark"
      done
      echo
      echo "  Install one:   ./oikonome.sh script add <name>"
      echo "  What they are: docs/community-scripts.md"
      ;;
    add)
      [ -n "$name" ] || die "which one? ./oikonome.sh script add <name>"
      [ -d "$src/$name" ] || die "no community script named '$name' — ./oikonome.sh script lists them"
      local dst="$SCRIPTS_DIR/$name"
      mkdir -p "$dst"
      cp -r "$src/$name/." "$dst/"
      # credentials skeleton pointed at THIS instance (the scripts read
      # OIKONOME_CREDENTIALS, which the unit below pins to this file); the
      # user pastes a script token + adds source creds per the README
      if [ ! -f "$dst/credentials.json" ]; then
        printf '{\n  "url": "http://localhost:%s",\n  "api_token": "PASTE FROM Settings -> Connections -> Script tokens"\n}\n' \
          "$(app_port)" > "$dst/credentials.json"
        chmod 600 "$dst/credentials.json"
      fi
      ok "Installed to $dst"
      # systemd user units: rewrite the sample path to the real install and
      # pin the credentials file per script
      local unit installed_units=""
      if command -v systemctl >/dev/null 2>&1 && [ -d "$dst/systemd" ]; then
        mkdir -p "$HOME/.config/systemd/user"
        for unit in "$dst"/systemd/*; do
          sed -e "s|%h/oikonome/community-scripts/$name|$dst|g" \
              -e "/^\[Service\]/a Environment=OIKONOME_CREDENTIALS=$dst/credentials.json" \
              "$unit" > "$HOME/.config/systemd/user/$(basename "$unit")"
          installed_units=1
        done
        systemctl --user daemon-reload 2>/dev/null || true
      fi
      say "Next steps:"
      echo "    1. Mint a token in the web UI: Settings → Connections →"
      echo "       Script tokens (name it '$name') and paste it into"
      echo "       $dst/credentials.json"
      echo "    2. Read $dst/README.md — wire up the collect half /"
      echo "       source credentials"
      if [ -n "$installed_units" ]; then
        echo "    3. Enable the schedule:"
        echo "       systemctl --user enable --now oikonome-$name.timer"
      else
        echo "    3. Schedule it yourself (cron/launchd) — see the README"
      fi
      ;;
    remove)
      [ -n "$name" ] || die "which one? ./oikonome.sh script remove <name>"
      if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now "oikonome-$name.timer" 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/oikonome-$name.timer" \
              "$HOME/.config/systemd/user/oikonome-$name.service"
        systemctl --user daemon-reload 2>/dev/null || true
        ok "Timer disabled and units removed"
      fi
      if [ -d "$SCRIPTS_DIR/$name" ]; then
        warn "Kept $SCRIPTS_DIR/$name (it may hold your credentials) — delete it yourself when ready."
      fi
      echo "  Also revoke its token: Settings → Connections → Script tokens."
      ;;
    *) die "usage: ./oikonome.sh script [list|add <name>|remove <name>]";;
  esac
}

# one command → a separate throwaway instance seeded with ~10
# years of synthetic household data, wizard skipped, creds printed.
# The demo lives in its own sibling folder on its own port; delete the
# folder (or run its ./oikonome.sh uninstall) to discard it.
demo_cleanup() {
  # a demo that failed mid-build must not leave an orphan
  # folder + running containers behind (each retry mints a fresh
  # oikonome-demo-XXXX, so they would pile up). Best-effort: tear down the
  # demo's own compose project, then remove its folder.
  # Also the teardown half of `demo clean`.
  [ -n "${1:-}" ] && [ -d "$1" ] || return 0
  warn "${2:-Cleaning up the failed demo instance at $1}"
  (cd "$1/docker" 2>/dev/null && $COMPOSE down -v >/dev/null 2>&1) || true
  # data/postgres is owned by the container's uid (root under docker) —
  # the same removal ladder as uninstall.sh: plain rm, the podman user
  # namespace, then a non-interactive sudo before giving up with the
  # command to run
  rm -rf "$1" 2>/dev/null || true
  [ -d "$1" ] && command -v podman >/dev/null 2>&1 \
    && podman unshare rm -rf "$1" 2>/dev/null || true
  [ -d "$1" ] && sudo -n rm -rf "$1" 2>/dev/null || true
  if [ -d "$1" ]; then
    warn "Couldn't fully remove $1 (container-owned files)."
    echo "    Run: sudo rm -rf '$1'"
  fi
}

cmd_demo_clean() {
  # successful demos deliberately each get their own folder
  # (parallel demos), so they accumulate until swept — this is the sweep.
  find_compose
  local dirs=() d ans
  for d in "$HOME"/oikonome-demo-*/; do
    [ -d "$d" ] && dirs+=("${d%/}")
  done
  if [ ${#dirs[@]} -eq 0 ]; then
    ok "No demo instances found (no ~/oikonome-demo-* folders)."
    return 0
  fi
  say "Demo instances to remove (containers, volumes and folder):"
  for d in "${dirs[@]}"; do echo "    $d"; done
  printf "  Remove all %s? [y/N] " "${#dirs[@]}"
  tty_read ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "  Aborted — nothing touched."; return 1;;
  esac
  for d in "${dirs[@]}"; do
    demo_cleanup "$d" "Removing demo instance at $d"
  done
  ok "Removed ${#dirs[@]} demo instance(s)."
}

cmd_demo() {
  local seed=${1:-}
  if [ "$seed" = "clean" ]; then cmd_demo_clean; return $?; fi
  find_compose
  local dest
  dest="$HOME/oikonome-demo-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
  say "Creating a demo instance at $dest"
  # The whole demo tree is materialised at the conventional umask, not this
  # script's 077: it is a docker build context whose files are COPYed into
  # the image root-owned and read at runtime by the container's uid 10001,
  # and both tar and cp apply the umask to the files they create — 0600
  # here means an image whose own package files it cannot read.
  (
    umask 022
    mkdir -p "$dest"
    if [ -d "$SCRIPT_HOME/.git" ] && command -v git >/dev/null 2>&1; then
      git -C "$SCRIPT_HOME" archive HEAD | tar -x -C "$dest"
    else
      # a lived-in checkout carries heavy build artifacts the demo copy must
      # not drag along (node_modules alone is hundreds of MB; the image build
      # recreates all of it inside the container anyway)
      (cd "$SCRIPT_HOME" && tar -cf - --exclude=./data --exclude=./docker/.env \
          --exclude=./.git --exclude=./backups \
          --exclude=./webapp/node_modules --exclude=./webapp/dist \
          --exclude=./server/.venv --exclude=__pycache__ \
          --exclude=.pytest_cache .) | tar -xf - -C "$dest"
    fi
    # git archive drops .git — leave the source's version for install.sh,
    # already collapsed (the copy has no tags to describe from). The demo
    # stamps exactly what its source stamps: a suffix here would show in
    # the demo's footer, and nothing reads one.
    srcver=$(describe_version "$SCRIPT_HOME")
    [ -n "$srcver" ] && printf '%s\n' "$srcver" > "$dest/.demo-version"
    # …and drops the generated (untracked) build manifest with it, so carry it
    # over: without .git the demo copy cannot regenerate one, and the admin
    # console's version-history panel would come up empty.
    cp "$SCRIPT_HOME/server/oikonome/version_history.tsv" \
       "$dest/server/oikonome/version_history.tsv" 2>/dev/null || true
  ) || { demo_cleanup "$dest"; die "could not lay down the demo folder at $dest"; }
  say "Installing (fresh port, own containers — your instance is untouched)…"
  (cd "$dest" && shared_umask ./install.sh </dev/null) \
    || { demo_cleanup "$dest"; die "demo install failed"; }
  local port proj cname
  port=$(env_get_in "$dest" OIKONOME_PORT)
  proj=$(env_get_in "$dest" COMPOSE_PROJECT_NAME)
  say "Generating the synthetic household (this is quick)…"
  # label-based lookup: a hardcoded ${proj}_app_1 only matches
  # podman-compose/v1 names — docker compose v2 uses {proj}-app-1
  cname=$(svc_container "$proj" app)
  [ -n "$cname" ] \
    || { demo_cleanup "$dest"; die "demo app container never came up — demo removed; check ./oikonome.sh logs on your main instance"; }
  local out
  out=$($ENGINE exec "$cname" oikonome demo-seed ${seed:+--seed "$seed"}) \
    || { demo_cleanup "$dest"; die "demo-seed failed — the half-built demo instance was removed"; }
  local email pw sd ntx
  email=$(printf '%s' "$out" | sed -n 's/.*"email": *"\([^"]*\)".*/\1/p')
  pw=$(printf '%s' "$out" | sed -n 's/.*"password": *"\([^"]*\)".*/\1/p')
  sd=$(printf '%s' "$out" | sed -n 's/.*"seed": *\([0-9]*\).*/\1/p')
  ntx=$(printf '%s' "$out" | sed -n 's/.*"transactions": *\([0-9]*\).*/\1/p')
  echo
  # open-right box: credential/URL lengths vary per run, so a closed
  # fixed-width frame would go ragged — no right border, nothing overflows
  # both addresses, like the install banner — the browser is often on
  # another machine than the box that ran the seed
  local host_ip
  host_ip=$(lan_ip)
  echo "  ┌──────────────────────────────────────────────────────────"
  echo "  │  Demo instance ready — ${ntx:-?} transactions, 10 years"
  echo "  │"
  printf "  │  URL:       http://localhost:%s/app/\n" "$port"
  if [ -n "$host_ip" ]; then
    printf "  │             http://%s:%s/app/   (from another machine)\n" \
      "$host_ip" "$port"
  fi
  printf "  │  Sign in:   %s\n" "$email"
  printf "  │  Password:  %s   (also pre-filled on the login page)\n" "$pw"
  printf "  │  Seed:      %s   (re-run: ./oikonome.sh demo %s)\n" "$sd" "$sd"
  echo "  │"
  echo "  │  Discard:   $dest/oikonome.sh uninstall"
  echo "  │             (all demos at once: ./oikonome.sh demo clean)"
  echo "  └──────────────────────────────────────────────────────────"
}


usage() {
  echo "Usage: ./oikonome.sh [install|upgrade|status|logs|backup|restore <file>|reset [--new-key]|reset-data [tenant]|reset-password [email]|clear-2fa [email]|uninstall|start-at-boot [on|off]|demo [seed|clean]|https <domain>|llm <on|off|vision> [model]|script <list|add|remove> [name]]"
  echo "  upgrade is an alias for install (same in-place path)."
  echo "  llm vision downloads the receipt/tax-document vision model."
  echo "Run with no arguments for the interactive menu."
}

menu() {
  local choice def health
  find_compose            # ENGINE, for instance discovery
  pick_instance || exit 1 # sets ROOT — every action below manages it
  while true; do
    echo
    if [ -f "$ROOT/docker/.env" ]; then
      # live health dot: green = /readyz answers, red = installed but down
      if curl -sf --max-time 1 "http://localhost:$(app_port)/readyz" >/dev/null 2>&1; then
        health=$(printf '\033[32m●\033[0m running')
      else
        health=$(printf '\033[31m●\033[0m down')
      fi
      printf "  \033[1mOikonome — %s\033[0m  :%s  %s\n" "$ROOT" "$(app_port)" "$health"
      def=2
    else
      printf "  \033[1mOikonome — %s\033[0m  (not installed yet)\n" "$ROOT"
      def=1
    fi
    echo "  ──────────────────────────────────────"
    if [ "$def" = 1 ]; then
      printf "  \033[1m❯ 1) Install / Upgrade\033[0m\n"
    else
      echo "    1) Install / Upgrade"
    fi
    if [ "$def" = 2 ]; then
      printf "  \033[1m❯ 2) Status\033[0m\n"
    else
      echo "    2) Status"
    fi
    echo "    3) Logs (follow; Ctrl-C returns here)"
    echo "    4) Backup now"
    echo "    5) Restore from backup"
    echo "    6) Reset (wipe data, fresh wizard)"
    echo "    7) Uninstall (remove everything)"
    echo "    8) Reset data only (keep logins; wizard runs again)"
    echo "    p) Reset a user's password (locked out — prints a one-time link)"
    echo "    f) Clear a user's second factor (lost authenticator AND recovery codes)"
    # show the LLM toggle reflecting current state
    if printf '%s' "$(env_get COMPOSE_PROFILES)" | grep -qw llm; then
      echo "    l) Smart categorization (local LLM): ON — turn off"
    else
      echo "    l) Smart categorization (local LLM): off — turn on"
    fi
    echo "    d) New demo instance (synthetic data, own port)"
    echo "    s) Switch instance"
    echo "    q) Quit"
    printf "  Choose [1-8, d, f, l, p, s, q] (Enter = %s): " "$def"
    tty_read choice
    [ "$TTY_EOF" = 1 ] && exit 0          # input exhausted — leave quietly
    [ -n "$choice" ] || choice=$def
    # menu actions may fail (e.g. stack down) — report and re-menu, don't die
    case $choice in
      1) in_lock_scope cmd_install || warn "install failed";;
      2) cmd_status    || warn "status failed";;
      3) cmd_logs      || warn "logs failed";;
      4) cmd_backup    || warn "backup failed";;
      5) in_lock_scope cmd_restore || warn "restore did not complete";;
      6) in_lock_scope cmd_reset   || warn "reset did not complete";;
      7) cmd_uninstall;;
      8) cmd_reset_data || warn "reset-data did not complete";;
      p|P) cmd_reset_password || warn "reset-password did not complete";;
      f|F) cmd_clear_2fa || warn "clear-2fa did not complete";;
      l|L)
        if printf '%s' "$(env_get COMPOSE_PROFILES)" | grep -qw llm; then
          cmd_llm off || warn "couldn't disable the LLM"
        else
          cmd_llm on  || warn "couldn't enable the LLM"
        fi;;
      d|D) cmd_demo    || warn "demo did not complete";;
      s|S) pick_instance || warn "no instance selected";;
      q|Q|quit|exit) exit 0;;
      *) warn "Unknown choice: $choice";;
    esac
  done
}

case ${1:-} in
  "")        menu;;
  install|upgrade) cmd_install;;
  status)    cmd_status;;
  logs)      cmd_logs;;
  backup)    cmd_backup;;
  restore)   cmd_restore "${2:-}";;
  reset)     cmd_reset "${2:-}";;
  uninstall) cmd_uninstall;;
  start-at-boot) cmd_start_at_boot "${2:-}";;
  https)     cmd_https "${2:-}";;
  llm)       find_compose; cmd_llm "${2:-}" "${3:-}";;
  script)    cmd_script "${2:-}" "${3:-}";;
  reset-data) cmd_reset_data "${2:-}";;
  reset-password) cmd_reset_password "${2:-}";;
  clear-2fa) cmd_clear_2fa "${2:-}";;
  demo)      cmd_demo "${2:-}";;
  -h|--help|help) usage;;
  *)         usage; exit 1;;
esac
