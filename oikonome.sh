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
maint_lock() {
  command -v flock >/dev/null 2>&1 || return 0   # no flock: as before
  exec 9>"$ROOT/.maintenance.lock"
  flock -w "${OIKONOME_MAINT_LOCK_WAIT:-600}" 9 || die \
    "another backup/restore/reset is still running on this instance — wait for it, or set OIKONOME_MAINT_LOCK_WAIT"
}

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

# KEY from docker/.env (empty if unset / no .env yet)
env_get() {
  grep "^$1=" "$ROOT/docker/.env" 2>/dev/null | cut -d= -f2- || true
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
# chmod every time: mv replaces .env with a umask-mode temp file, and the
# file holds the database passwords + master key — keep it owner-only
set_env() {
  local envf="$ROOT/docker/.env"
  grep -v "^$1=" "$envf" > "$envf.tmp" 2>/dev/null || true
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

cmd_install() {
  [ -f "$ROOT/install.sh" ] || die "No install.sh in $ROOT."
  # Upgrade means UPGRADE: pull the latest code first when this install
  # is a git checkout with an upstream (a plain download just installs
  # what's there). --ff-only so a dirty/diverged tree warns instead of
  # merging surprises into someone's instance.
  if git -C "$ROOT" rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
    local before after
    before=$(describe_version "$ROOT"); before=${before:-?}
    if git -C "$ROOT" pull --ff-only -q 2>/dev/null; then
      after=$(describe_version "$ROOT"); after=${after:-?}
      if [ "$before" = "$after" ]; then
        ok "Code already current ($after)."
      else
        ok "Code updated: $before → $after"
      fi
    else
      warn "Couldn't update (local changes or diverged history) — installing the code as-is."
    fi
  fi
  shared_umask "$ROOT/install.sh"
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
  c=$(grep "^OIKONOME_CREATED=" "$dir/docker/.env" 2>/dev/null | cut -d= -f2- || true)
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
      p=$(grep "^OIKONOME_PORT=" "$dir/docker/.env" 2>/dev/null | cut -d= -f2- || true)
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
    # `|| true`: a sparse .env must not trip set -e
    p=$(grep "^OIKONOME_PORT=" "$d/docker/.env" 2>/dev/null | cut -d= -f2- || true)
    proj=$(grep "^COMPOSE_PROJECT_NAME=" "$d/docker/.env" 2>/dev/null | cut -d= -f2- || true)
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
  # Probe WITHOUT the token in the URL — GET /setup answers 200 (unclaimed
  # form) or 303 (claimed → /app/) on its own, so the one-time token never
  # rides a request line into uvicorn's access log (which the host agent's
  # `logs` snapshot would otherwise capture to a world-readable file).
  # A tokenless GET /setup is a 403 by design, so the claim state is read
  # off GET /'s redirect instead: /setup while unclaimed, /app/ once
  # claimed.
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
  grep -q "^COMPOSE_FILE=.*external-db" "$ROOT/docker/.env" 2>/dev/null
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
  local db; db=$(db_name)
  pg_maint_sql -q \
    -c "DROP DATABASE IF EXISTS $db WITH (FORCE);" \
    -c "CREATE DATABASE $db;"
}
db_name() {
  local n
  n=$(grep "^OIKONOME_DB_NAME=" "$ROOT/docker/.env" 2>/dev/null | cut -d= -f2-)
  echo "${n:-oikonome}"
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
  maint_lock
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
  $COMPOSE up -d
  say "Health check…"
  wait_ready 60 || return 1

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
  local data_dir keep_port
  maint_lock
  echo
  echo "  This WIPES the instance at $ROOT:"
  echo "    • stack down, DATABASE deleted (./data), secrets regenerated"
  echo "    • then reinstalls → the fresh setup wizard"
  echo "  The code and your backups are kept. This cannot be undone."
  confirm_word reset "Type 'reset' to confirm: " || return 1

  find_compose
  cd "$ROOT/docker"

  # read the data location BEFORE deleting .env (OIKONOME_DATA_DIR overrides;
  # its value is relative to docker/, matching install.sh)
  data_dir=$(env_get OIKONOME_DATA_DIR)
  if [ -n "$data_dir" ]; then
    case $data_dir in
      /*) : ;;                                # absolute — use as-is
      *)  data_dir="$ROOT/docker/$data_dir";; # relative to docker/, like compose
    esac
  else
    data_dir="$ROOT/data"
  fi

  # remember the port before .env is deleted: a reset regenerates
  # secrets, but the instance should not silently move ports
  keep_port=$(app_port)

  # down WITHOUT -v, deliberately (uninstall is the one that uses -v):
  # the named volumes hold no user data — redis has no volume, the
  # database is a bind mount deleted below — just the multi-GB ollama
  # model cache and Caddy's certs, both worth keeping across a reset
  # (same port → same project name → the reinstall reattaches them).
  say "Stopping the stack…"
  $COMPOSE down >/dev/null 2>&1 || true

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
  rm -f "$ROOT/docker/.env" "$ROOT/docker/compose.override.yaml"
  ok "Data and secrets removed."

  say "Reinstalling (fresh secrets, fresh setup wizard)…"
  shared_umask env OIKONOME_PORT="$keep_port" "$ROOT/install.sh"
}

cmd_https() {
  # /oikonome.sh https <domain>   → front the app with a TLS reverse proxy
  # /oikonome.sh https off        → turn it back off (plain http)
  local domain=${1:-} port envf="$ROOT/docker/.env"
  [ -f "$envf" ] || die "No install here yet — run ./oikonome.sh install first."
  find_compose
  if [ "$domain" = off ] || [ "$domain" = disable ]; then
    remove_profile tls
    set_env OIKONOME_DOMAIN ""
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
    || set_env OIKONOME_TRUSTED_PROXIES "10.88.0.0/15,172.16.0.0/12"
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
  say "This removes the authenticator, every passkey and every recovery code"
  say "for $email, and signs out all of their sessions and devices."
  printf "  Type the email again to confirm: "
  local again; tty_read again
  [ "$again" = "$email" ] || { warn "Emails differ — nothing done."; return 1; }
  if ! $ENGINE exec "$cname" oikonome clear-2fa "$email"; then
    warn "No account with the email '$email' on this instance, or the clear failed."
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
  echo "  (Two-factor settings are unchanged.)"
}

cmd_uninstall() {
  [ -f "$ROOT/uninstall.sh" ] || die "No uninstall.sh in $ROOT."
  maint_lock
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
  port=$(grep "^OIKONOME_PORT=" "$dest/docker/.env" | cut -d= -f2)
  proj=$(grep "^COMPOSE_PROJECT_NAME=" "$dest/docker/.env" | cut -d= -f2)
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
  echo "Usage: ./oikonome.sh [install|upgrade|status|logs|backup|restore <file>|reset|reset-data [tenant]|reset-password [email]|clear-2fa [email]|uninstall|demo [seed|clean]|https <domain>|llm <on|off|vision> [model]|script <list|add|remove> [name]]"
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
      1) cmd_install   || warn "install failed";;
      2) cmd_status    || warn "status failed";;
      3) cmd_logs      || warn "logs failed";;
      4) cmd_backup    || warn "backup failed";;
      5) cmd_restore   || warn "restore did not complete";;
      6) cmd_reset     || warn "reset did not complete";;
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
  reset)     cmd_reset;;
  uninstall) cmd_uninstall;;
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
