#!/usr/bin/env bash
# Oikonome guided installer — sets up a self-hosted instance from scratch.
# Run from the repo root:  ./install.sh
# Safe to re-run: keeps your existing .env and data; just rebuilds + restarts.
set -euo pipefail

say()  { printf "\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗ %s\033[0m\n" "$*"; exit 1; }

# step N "label" — the installer is 5 numbered steps; each prints a header
# line, then one-line ✓ summaries of what happened (details stay quiet)
STEPS=5
step() { printf "\n\033[1m[%d/%d]\033[0m %s\n" "$1" "$STEPS" "$2"; }

# spin LOGFILE CMD…  — run a long command quietly: output to LOGFILE, an
# in-place spinner + elapsed clock while it runs (plain line when stdout is
# not a tty, e.g. CI). Returns the command's status; caller shows the log
# tail on failure.
spin() {
  local log=$1 label=$2; shift 2
  if [ -t 1 ]; then
    "$@" >"$log" 2>&1 &
    local pid=$! start=$SECONDS frames='|/-\' i=0 rc
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  \033[1;36m%s\033[0m %s… %ds ' \
        "${frames:$((i++ % 4)):1}" "$label" $((SECONDS - start))
      sleep 0.5
    done
    wait "$pid"; rc=$?
    printf '\r\033[K'
    [ $rc -eq 0 ] && ok "$label — done in $((SECONDS - start))s"
    return $rc
  else
    echo "  $label…"
    "$@" >"$log" 2>&1
  fi
}

# shared prereq check — one file, also sourced by oikonome.sh, so the two
# entry points can never drift apart on what a host needs
. "$(dirname "$0")/scripts/check-prereqs.sh"

# shared version-stamp helpers — also sourced by oikonome.sh, so the
# installer and the control script can never disagree about the version
. "$(dirname "$0")/scripts/version-stamp.sh"

cd "$(dirname "$0")/docker"

echo
echo "  Oikonome — self-hosted installer"
echo "  ────────────────────────────────"

# 1. host prerequisites ------------------------------------------------------
# Missing docker/podman(+compose), curl, or openssl walks the operator
# through installing them (with re-check) BEFORE any secrets or config are
# written; non-interactive runs exit non-zero with the list instead.
# Sets COMPOSE + ENGINE (docker | podman).
step 1 "Container engine"
require_prereqs
ok "Using $COMPOSE"

# port_busy comes from scripts/check-prereqs.sh (shared with oikonome.sh)

# rewrite KEY=VALUE in .env without sed — values may contain any characters.
# chmod every time: mv replaces .env with a umask-mode temp file, and the
# file holds the database passwords + master key — keep it owner-only
set_env() {
  grep -v "^$1=" .env > .env.tmp 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >> .env.tmp
  mv .env.tmp .env
  chmod 600 .env
}

# 2. configuration -----------------------------------------------------------
step 2 "Configuration"
if [ -f .env ]; then
  ok "Existing .env found — keeping your configuration and data."
  # GUARD: the RUNNING stack is the source of truth for secrets. If this
  # instance's containers are up but .env is missing a secret they're
  # already using, the backfill below would mint a fresh random value that
  # cannot match the live database (auth failures) or decrypt stored data
  # (wrong master key). A stale/partial .env next to a running stack is not
  # a config to "repair" here — it must be recovered from the containers.
  # `|| true`: under `set -euo pipefail` a no-match grep exits 1 and pipefail
  # would abort the whole installer — an .env without COMPOSE_PROJECT_NAME
  # (older/self-host installs) is normal, not an error.
  proj=$(grep "^COMPOSE_PROJECT_NAME=" .env 2>/dev/null | cut -d= -f2- || true)
  proj=${proj:-oikonome}
  # Detect a RUNNING instance by compose LABEL, never by container name: the
  # name template differs per engine (`proj-app-1` on docker compose v2 vs
  # `proj_app_1` on podman-compose), so a `^${proj}-` name grep is dead code
  # on podman — which is exactly why the rest of this tooling resolves
  # containers via svc_container / label filters (oikonome.sh). The
  # working_dir label also catches a stub .env that lost COMPOSE_PROJECT_NAME
  # (compose then falls back to compose.yaml's `name:`, not our ${proj}).
  running=$($ENGINE ps --filter "label=com.docker.compose.project=$proj" \
            --format '{{.Names}}' 2>/dev/null)
  [ -n "$running" ] || running=$($ENGINE ps \
            --filter "label=com.docker.compose.project.working_dir=$PWD" \
            --format '{{.Names}}' 2>/dev/null)
  if [ -n "$running" ]; then
    appc=$(printf '%s\n' "$running" | grep -m1 -- '-\?app-\?' || \
           printf '%s\n' "$running" | head -n1)
    for _k in OIKONOME_MASTER_KEY OIKONOME_APP_PASSWORD POSTGRES_PASSWORD \
              OIKONOME_ADMIN_PASSWORD; do
      grep -q "^${_k}=." .env 2>/dev/null && continue
      die "docker/.env is missing ${_k}, but this instance (${proj}) is running.
   The live containers hold the real secret — generating a new one here would
   break access to the existing database. Don't run install to fix a running
   stack; recover the full docker/.env from the containers
   ($ENGINE inspect ${appc} --format '{{range .Config.Env}}{{println .}}{{end}}'),
   or upgrade WITHOUT touching secrets:  cd docker && $COMPOSE up -d --build"
    done
  fi
  # compose now REQUIRES the master key (tokens must never sit plaintext);
  # a .env from before that, or a hand-rolled one, may lack it — backfill
  # so the stack still comes up. Any secrets stored plaintext meanwhile
  # re-encrypt as each connection is next saved (TOTP seeds sweep on boot).
  MK=$(grep "^OIKONOME_MASTER_KEY=" .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$MK" ]; then
    set_env OIKONOME_MASTER_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
    warn "No OIKONOME_MASTER_KEY in .env — generated one (bank tokens encrypt at rest)."
  fi
  # Redis AUTH — older .envs predate REDIS_PASSWORD; backfill so
  # the queue stops being open on the compose network. Takes effect when
  # compose recreates the redis container below.
  RP=$(grep "^REDIS_PASSWORD=" .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$RP" ]; then
    set_env REDIS_PASSWORD "$(openssl rand -hex 16)"
    warn "No REDIS_PASSWORD in .env — generated one (job-queue AUTH)."
  fi
  # least-privilege runtime-admin role — older .envs predate it;
  # backfill so app/worker stop carrying the postgres superuser DSN. The
  # migrate one-shot creates/aligns the role before app/worker start.
  AP=$(grep "^OIKONOME_ADMIN_PASSWORD=" .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$AP" ]; then
    set_env OIKONOME_ADMIN_PASSWORD "$(openssl rand -hex 16)"
    warn "No OIKONOME_ADMIN_PASSWORD in .env — generated one (runtime-admin role)."
  fi
  # OIKONOME_CREATED: a one-time creation stamp so the manager's instance list
  # can sort instances in creation order portably — the first
  # install writes it and it never changes; existing instances that predate it
  # fall back to folder birth time. Backfilled here for older .envs too.
  CR=$(grep "^OIKONOME_CREATED=" .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$CR" ]; then
    set_env OIKONOME_CREATED "$(date +%s)"
  fi
  # compose gives real environment variables precedence over .env, so an
  # exported OIKONOME_PORT would silently publish on a different port than
  # .env (and the health check) believes — keep the two in agreement
  if [ -n "${OIKONOME_PORT:-}" ]; then
    CUR_PORT=$(grep "^OIKONOME_PORT=" .env 2>/dev/null | cut -d= -f2- || true)
    if [ "$OIKONOME_PORT" != "${CUR_PORT:-8042}" ]; then
      # the target must be free — otherwise the stack silently loses the
      # port race and another instance answers the health check
      port_busy "${OIKONOME_PORT}" \
        && die "Requested port ${OIKONOME_PORT} is already in use — pick another."
      set_env OIKONOME_PORT "$OIKONOME_PORT"
      warn "Port moves ${CUR_PORT:-8042} → ${OIKONOME_PORT} (OIKONOME_PORT is set; the compose project keeps its original name)."
    fi
  fi
else
  # An existing database with no .env means the passwords that database was
  # initialized with are gone (docker/.env was deleted without deleting
  # ../data). Freshly generated secrets would not match it and the app would
  # never come up. (Deleting the whole install folder can't hit this —
  # config and data go together.)
  if [ -f ../data/postgres/PG_VERSION ]; then
    warn "Found an existing Oikonome database in ./data, but no docker/.env (which held its passwords)."
    echo "  • To KEEP that data: restore your old docker/.env and re-run."
    echo "  • To START FRESH:    type 'fresh' to delete the old database."
    printf "  Keep data (Enter) or type 'fresh': "
    read -r WIPE </dev/tty 2>/dev/null || WIPE=""
    if [ "$WIPE" = "fresh" ]; then
      # .env is gone, and compose.yaml marks the secrets required — feed
      # placeholders so interpolation succeeds and the down actually runs
      # (values are irrelevant for stopping containers)
      POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-gone} \
        OIKONOME_APP_PASSWORD=${OIKONOME_APP_PASSWORD:-gone} \
        OIKONOME_ADMIN_PASSWORD=${OIKONOME_ADMIN_PASSWORD:-gone} \
        OIKONOME_MASTER_KEY=${OIKONOME_MASTER_KEY:-gone} \
        $COMPOSE down >/dev/null 2>&1 || true
      # rootless podman: postgres's files can be subuid-owned, so plain rm
      # gets EPERM — retry inside the user namespace (same as uninstall.sh)
      if ! rm -rf ../data/postgres 2>/dev/null; then
        if command -v podman >/dev/null 2>&1; then
          podman unshare rm -rf ../data/postgres 2>/dev/null || true
        fi
      fi
      if [ -e ../data/postgres ]; then
        die "Couldn't delete the old database (rootful Docker owns it as uid 999?). Run: sudo rm -rf '$(cd .. && pwd)/data/postgres'  then re-run ./install.sh"
      fi
      ok "Old database removed — starting fresh."
    else
      die "Stopping so your data is untouched. Restore docker/.env, then re-run ./install.sh"
    fi
  fi
  cp .env.example .env
  chmod 600 .env    # secrets land in here next — owner-only from the start
  rand() { openssl rand -hex 16; }
  # Fernet key: 32 random bytes, urlsafe base64 (encrypts bank tokens at rest)
  MASTER_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
  # host port: an exported OIKONOME_PORT wins (compose gives the
  # environment precedence over .env, so honoring it here keeps port,
  # project name, and health check in agreement); otherwise default 8042,
  # walking upward past ports already in use (e.g. another instance)
  if [ -n "${OIKONOME_PORT:-}" ]; then
    PORT=$OIKONOME_PORT
    port_busy "${PORT}" \
      && die "Requested port ${PORT} (OIKONOME_PORT) is already in use — pick another or unset it to auto-pick."
  else
    PORT=8042
    while port_busy "${PORT}"; do PORT=$((PORT+1)); done
    [ "$PORT" != "8042" ] && warn "Port 8042 is in use — this instance will use port ${PORT}."
  fi
  set_env OIKONOME_PORT "$PORT"
  # unique compose project per instance so containers/networks never clash
  set_env COMPOSE_PROJECT_NAME "oikonome-${PORT}"
  set_env POSTGRES_PASSWORD "$(rand)"
  set_env OIKONOME_APP_PASSWORD "$(rand)"
  set_env OIKONOME_ADMIN_PASSWORD "$(rand)"
  set_env REDIS_PASSWORD "$(rand)"
  set_env OIKONOME_MASTER_KEY "$MASTER_KEY"
  # one-time setup-wizard gate: only the link printed at the end (which
  # carries this token) can create the first account
  set_env OIKONOME_SETUP_TOKEN "$(openssl rand -hex 8)"
  ok "Secrets generated (db passwords + bank-token encryption key → docker/.env)"
  ok "Port ${PORT} → http://localhost:${PORT}"
  # daily-email delivery (SMTP) is configured in docker/.env when wanted;
  # recipients and schedule live in the web UI under Settings

  # Local LLM: smart categorization + receipt parsing with a bundled
  # model. ON by default for interactive installs (just press Enter) — it's
  # the recommended experience. Downloads ~1GB and wants real RAM/CPU, so we
  # still ask, and non-interactive installs stay off (can't pull silently);
  # toggle any time with ./oikonome.sh llm on|off.
  if [ -t 0 ]; then
    # model tier by host RAM: >=12GB comfortably fits the 7b,
    # which is markedly better at categorization; both are Apache-2.0
    LLM_KB=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null)
    if [ -n "$LLM_KB" ] && [ "$LLM_KB" -ge $((12 * 1024 * 1024)) ]; then
      LLM_MODEL="qwen2.5:7b";   LLM_SIZE="~5GB"
    else
      LLM_MODEL="qwen2.5:1.5b"; LLM_SIZE="~1GB"
    fi
    echo
    printf "  Enable built-in smart categorization? It runs a local model\n"
    printf "  (Qwen2.5, Apache-2.0) so vague/opaque merchants — e.g. SimpleFIN —\n"
    printf "  and receipts get auto-categorized. No API key; runs on this host.\n"
    printf "  Picked %s for this machine's RAM (%s download). [Y/n]: " \
           "$LLM_MODEL" "$LLM_SIZE"
    read -r LLM_ANS </dev/tty 2>/dev/null || LLM_ANS=""
    case $LLM_ANS in
      n|N|no|NO)
        echo "  Skipped. Enable later: ./oikonome.sh llm on" ;;
      *)  # default (Enter) or any yes → enable
        set_env COMPOSE_PROFILES "llm"
        set_env OIKONOME_LLM_URL "http://ollama:11434"
        set_env OIKONOME_LLM_MODEL "$LLM_MODEL"
        LLM_ENABLED=1
        ok "Smart categorization enabled ($LLM_MODEL) — downloads after startup."
        ;;
    esac
  fi
fi

# 2a. version stamp — refreshed on EVERY install/upgrade so the running
# containers (GUI footer, doctor bundle) report what's actually deployed
# .demo-version: written by `oikonome.sh demo` — git archive drops .git,
# so describe fails in a demo copy; the file carries the source's version
RAW=$(git describe --tags --match "$VERSION_TAG_GLOB" --always --dirty 2>/dev/null \
      || cat ../.demo-version 2>/dev/null || echo unknown)
# Show a standard-looking version. git describe gives
# "vA.B.C-M-gSHA" (nearest version tag + commits-since + sha); collapse_version
# folds it into a clean semver. See scripts/version-stamp.sh for why the patch
# is BASE + commits-since and why the tag glob matches any series.
VER=$(collapse_version "$RAW")
set_env OIKONOME_VERSION "$VER"
ok "Version ${VER}"

# one-line "what changed" for the admin-console Deployments pane — the tip
# commit subject at deploy time (git archive demo copies have no .git, so
# this is best-effort and simply blank there). NOTE_FOR pins the note to
# the version it was captured with: migrate only trusts the note when the
# marker matches, so a deploy path that bumps OIKONOME_VERSION without
# re-running install.sh can never re-serve a stale note.
VER_NOTE=$(git log -1 --format=%s 2>/dev/null || true)
set_env OIKONOME_VERSION_NOTE "$VER_NOTE"
set_env OIKONOME_VERSION_NOTE_FOR "$VER"

# build the baked manifest the admin console's version-history panel reads:
# the image carries no .git, so the history has to ship inside the package.
# It is GENERATED and gitignored.
# No-op without git or the anchor tag, which leaves an existing manifest
# exactly as shipped.
../scripts/gen-version-history.sh >/dev/null 2>&1 || true

# 2b. timezone — dates like "today" and the daily-email window follow the
# household's clock, not UTC. Detect once from the host; editable in .env.
if ! grep -q "^OIKONOME_TZ=" .env 2>/dev/null; then
  HOST_TZ=$(timedatectl show -p Timezone --value 2>/dev/null ||
            cat /etc/timezone 2>/dev/null ||
            readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)
  if [ -n "${HOST_TZ:-}" ]; then
    set_env OIKONOME_TZ "$HOST_TZ"
    ok "Timezone $HOST_TZ (from the host; change OIKONOME_TZ in docker/.env)"
  fi
fi

# 3. data folder + engine quirks ---------------------------------------------
# Everything lives inside the install folder: code, docker/.env, and the
# database at ./data/postgres. Delete the folder (after `compose down`) and
# the install is gone. OIKONOME_DATA_DIR in .env overrides the location.
step 3 "Data folder"
DATA_DIR=$(grep "^OIKONOME_DATA_DIR=" .env 2>/dev/null | cut -d= -f2- || true)
DATA_DIR=${DATA_DIR:-../data/postgres}
mkdir -p "$DATA_DIR" || die "Can't create $DATA_DIR"
# the admin console's backup/host-health cards read these via
# read-only binds — pre-create so compose never auto-creates them as root
mkdir -p ../backups ../ops-state ../ops-cmd 2>/dev/null || true
# ops-cmd is the console→host command drop-box. Default it CLOSED (0700)
# so the reboot lever is inert until the root-run
# scripts/install-host-reboot-watcher.sh enables it — that installer
# chowns this dir to the container's uid at 0700, so ONLY the app/worker
# container (not arbitrary local users or neighbour containers) can drop a
# reboot flag; a world-writable 1777 here would let anyone reboot the host.
chmod 0700 ../ops-cmd 2>/dev/null || true
ok "Database lives at ${DATA_DIR#../} (inside the install folder)"

# Rootless Podman: map the host user to the container's postgres user so the
# files in ./data stay owned by YOU (plain rm -rf works). Without this they
# would be owned by a subuid and need 'podman unshare' to touch.
OVERRIDE_MARK="# generated by install.sh — do not edit (regenerated on every run)"
if [ "$ENGINE" = podman ] && [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = "true" ]; then
  {
    echo "$OVERRIDE_MARK"
    echo "services:"
    echo "  postgres:"
    echo "    userns_mode: keep-id:uid=999,gid=999"
  } > compose.override.yaml
  ok "Rootless Podman: data files stay owned by you (plain rm -rf works)"
elif [ -f compose.override.yaml ] && head -1 compose.override.yaml | grep -qF "$OVERRIDE_MARK"; then
  rm -f compose.override.yaml   # engine changed; stale generated override
fi

# 4. build + start -----------------------------------------------------------
step 4 "Build + start"
# Major Postgres upgrade: a data folder written by an older major can't be
# opened by the newer server image — scripts/pg-major-migrate.sh dumps with
# the old image, keeps the old folder aside, and reloads into the new one
# (no-op when the majors already match or the database is external).
# After the rootless-podman override above, so the one-off container and
# the stack map the data folder to the same uid.
"$(cd .. && pwd)/scripts/pg-major-migrate.sh" "$(cd .. && pwd)" || die "Postgres major-version migration failed — see above; your data folder was not changed or was kept aside"

echo "  Building the app image and starting 4 services (app, worker,"
echo "  postgres, redis). First build downloads base images — a few minutes;"
echo "  upgrades are much faster."
BUILD_LOG=$(mktemp /tmp/oikonome-install-XXXXXX.log)
if ! spin "$BUILD_LOG" "building + starting" $COMPOSE up -d --build; then
  warn "Build failed — last 25 log lines:"
  tail -25 "$BUILD_LOG"
  echo "  Full log: $BUILD_LOG"
  exit 1
fi

# 5. wait until ready --------------------------------------------------------
step 5 "Health check"
TRIES=60
for i in $(seq 1 $TRIES); do
  # effective port = environment first, then .env — same order compose uses
  PORT=$(grep "^OIKONOME_PORT=" .env 2>/dev/null | cut -d= -f2 || true)
  PORT=${OIKONOME_PORT:-${PORT:-8042}}
  if curl -sf "http://localhost:${PORT}/readyz" >/dev/null 2>&1; then
    [ -t 1 ] && printf '\r\033[K'
    ok "App is up — database migrated, all services healthy ($((i * 5))s)"
    # opt-in local LLM: the ollama container is already up (COMPOSE_PROFILES
    # includes llm), but the model still has to be pulled once into its
    # volume. Do it now so smart categorization works on first sync.
    if [ "${LLM_ENABLED:-}" = 1 ]; then
      # scope to THIS install's project — the service label alone matches
      # every oikonome instance on the host, and the model then lands in
      # some OTHER instance's ollama container (leaving this one empty)
      PROJ=$(grep "^COMPOSE_PROJECT_NAME=" .env 2>/dev/null | cut -d= -f2 || true)
      OLLAMA_CT=$($ENGINE ps \
                    --filter "label=com.docker.compose.project=${PROJ:-oikonome}" \
                    --filter "label=com.docker.compose.service=ollama" \
                    --format '{{.Names}}' 2>/dev/null | head -1)
      if [ -n "$OLLAMA_CT" ]; then
        # ollama's API takes a few seconds after container start
        for _ in $(seq 1 30); do
          $ENGINE exec "$OLLAMA_CT" ollama list >/dev/null 2>&1 && break; sleep 2
        done
        LLM_PULL=$(grep '^OIKONOME_LLM_MODEL=' .env 2>/dev/null | cut -d= -f2 || true)
        if $ENGINE exec "$OLLAMA_CT" ollama pull "${LLM_PULL:-qwen2.5:1.5b}" >/dev/null 2>&1; then
          ok "Smart-categorization model ready (${LLM_PULL:-qwen2.5:1.5b})"
        else
          warn "Model download didn't finish — retry any time: ./oikonome.sh llm on"
        fi
      else
        warn "LLM container not found — enable later: ./oikonome.sh llm on"
      fi
    fi
    ROOT_DIR=$(cd .. && pwd)
    if [ "$DATA_DIR" = "../data/postgres" ]; then
      echo "  Everything lives in: $ROOT_DIR  (code, config, and data)"
    else
      echo "  Install folder: $ROOT_DIR   Database: $DATA_DIR"
    fi
    SETUP_TOKEN=$(grep "^OIKONOME_SETUP_TOKEN=" .env 2>/dev/null | cut -d= -f2- || true)
    # first LAN address of this machine — the localhost link is useless
    # when the browser is on another machine
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$HOST_IP" ] || HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
    # open-right box: no right border and no padding, so lines of any
    # length (long URLs, wide tokens) can never overflow the frame
    B() { printf "  │  %s\n" "$1"; }
    echo
    echo "  ┌────────────────────────────────────────────────────────────"
    # claimed instances 303 the setup link to login — curl -sf calls a 3xx
    # success, so require the actual setup FORM (200) before printing the
    # "create your account" box on an upgrade
    # Probe WITHOUT the token in the URL, so it never rides a request line
    # into the access log: GET / redirects to /setup while the instance is
    # unclaimed and to /app/ once it is (a tokenless GET /setup itself is
    # a 403 by design, so it cannot be the probe).
    CLAIM_PROBE=$(curl -s -o /dev/null -w '%{redirect_url}' \
        "http://localhost:${PORT}/" 2>/dev/null || true)
    if [ -n "$SETUP_TOKEN" ] && case "$CLAIM_PROBE" in */setup) true ;; *) false ;; esac; then
      if [ -n "${OIKONOME_ASSUME_YES:-}" ] || [ ! -t 1 ]; then
        # non-interactive (the console's reset re-runs this through the
        # host agent, which captures stdout to a log the console shows):
        # the one-time claim token must not ride there. A terminal
        # operator sees the full link; anyone else is told where it is.
        B "No account yet. Run './oikonome.sh status' on the host to see"
        B "the one-time claim link (withheld from this log on purpose)."
      else
        # unclaimed instance: the ONLY way in is this one-time setup link
        B "Create your account with this one-time link:"
        B ""
        B "  http://localhost:${PORT}/setup?token=${SETUP_TOKEN}"
        [ -n "$HOST_IP" ] \
          && B "  http://${HOST_IP}:${PORT}/setup?token=${SETUP_TOKEN}" \
          && B "  (use this one from another machine)"
      fi
    else
      B "Open:   http://localhost:${PORT}"
      [ -n "$HOST_IP" ] && B "        http://${HOST_IP}:${PORT}  (from another machine)"
    fi
    B ""
    B "1. The setup wizard creates your account."
    B "2. Help (?) has guides; Feedback reaches the instance operator."
    B ""
    B "Stop:        $COMPOSE down   (data kept)"
    B "Upgrade:     ./oikonome.sh install"
    B "Start over:  ./oikonome.sh uninstall   (removes everything)"
    echo "  └────────────────────────────────────────────────────────────"
    echo
    exit 0
  fi
  # progress bar over the 5-minute wait (plain dots when not a tty)
  if [ -t 1 ]; then
    FILL=$((i * 30 / TRIES))
    printf '\r  [%-30s] waiting for the app… %ds ' \
      "$(printf '#%.0s' $(seq 1 $((FILL > 0 ? FILL : 1))))" $((i * 5))
  else
    printf "."
  fi
  sleep 5
done
echo
warn "Still not responding after 5 minutes. Last app logs:"
echo
$COMPOSE logs app 2>&1 | tail -25
echo
echo "  Full logs:  $COMPOSE logs app"
exit 1
