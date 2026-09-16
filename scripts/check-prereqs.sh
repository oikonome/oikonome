#!/usr/bin/env bash
# Host-prerequisite check shared by install.sh and oikonome.sh — source this
# file, then call require_prereqs. On return, COMPOSE and ENGINE are set and
# a container engine (with compose), curl, and openssl all exist.
#
# When something is missing, an interactive terminal gets a guided loop:
# what's missing in plain language, the install commands for common systems,
# and an Enter-to-re-check prompt — with an optional offer to run the package
# manager that only fires on an explicit yes (never silently, never assuming
# root, never curl|sh). Non-interactive runs (CI=1 or no terminal) get a
# clear non-zero exit with the same list instead, so nothing ever hangs.
#
# Host Python is deliberately NOT a prerequisite — the app runs inside the
# container image.

# port_busy PORT — is something already listening on this TCP port?
# Shared here so install.sh (port auto-pick) and oikonome.sh (https port
# check) can never drift apart. GNU/BSD portability: Linux has ss; macOS
# gets lsof. No tool at all → assume free (the bind will fail loudly).
port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | grep -q ":$1 "
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

# same detection order the installer has always used: docker with the
# compose plugin, podman with the compose subcommand, standalone
# podman-compose. One engine is enough — never ask for both.
prereq_detect_compose() {
  COMPOSE="" ENGINE=""
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    COMPOSE="podman compose"
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE="podman-compose"
  else
    return 1
  fi
  ENGINE=${COMPOSE%% *}
  # (case, not `[ ] &&`: a false test as a function's last command would
  # return 1 and set -e would kill the sourcing script)
  case $ENGINE in podman-compose) ENGINE=podman;; esac
  # podman-compose ≥1.1 puts every service in ONE pod, whose isolated
  # userns maps container UIDs differently than a standalone `podman run`.
  # The bind-mounted postgres data dir (created under the classic rootless
  # mapping) then reads as permission-denied to the pod's postgres uid —
  # initdb loops on a perfectly good database. `in_pod=false` runs the
  # services as plain containers (the classic mapping the dir was made
  # with). Exported here so every compose invocation inherits it; docker
  # and podman's native `compose` ignore the var. `podman compose` on
  # Fedora often SHELLS OUT to podman-compose, so match either.
  case $COMPOSE in *podman*compose*|"podman compose")
    export PODMAN_COMPOSE_IN_POD=false ;;
  esac
}

# one token per line: engine / curl / openssl — empty output means all good
prereq_missing() {
  prereq_detect_compose >/dev/null 2>&1 || echo engine
  command -v curl    >/dev/null 2>&1 || echo curl
  command -v openssl >/dev/null 2>&1 || echo openssl
}

# is there a terminal we can prompt on? `curl | bash` keeps /dev/tty even
# though stdin is the pipe (same trick as oikonome.sh's tty_read); CI=1
# forces the non-interactive path even on a terminal
prereq_interactive() {
  [ -z "${CI:-}" ] && { { : </dev/tty; } 2>/dev/null || [ -t 0 ]; }
}

# prereq_read VAR — one line from the terminal when there is one, else from
# stdin; non-zero on EOF so callers can bail instead of looping forever
prereq_read() {
  if { : </dev/tty; } 2>/dev/null; then
    IFS= read -r "$1" </dev/tty
  else
    IFS= read -r "$1"
  fi
}

# prereq_pkgs apt|dnf TOKEN… → package list for that family. The engine
# suggestion follows each distro's path of least resistance (Docker packages
# on Debian/Ubuntu, Podman on Fedora/RHEL where it's first-class) — either
# engine works, so we suggest one, not both.
prereq_pkgs() {
  local fam=$1 m out=""; shift
  for m in "$@"; do
    case $fam:$m in
      apt:engine) out="$out docker.io docker-compose-v2";;
      dnf:engine) out="$out podman podman-compose";;
      *:curl)     out="$out curl";;
      *:openssl)  out="$out openssl";;
    esac
  done
  printf '%s' "${out# }"
}

# plain-language list of what's missing + concrete install commands
prereq_explain() {
  local m
  echo
  echo "  Oikonome needs a few host tools that are missing on this machine:"
  for m in "$@"; do
    case $m in
      engine)
        # an engine binary without its compose half is the common near-miss —
        # name the actual gap instead of re-suggesting the whole engine
        if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
          echo "    • the compose half of your container engine (the 'docker compose'"
          echo "      plugin, or podman-compose) — the engine itself is already here"
        else
          echo "    • a container engine — Docker (with compose) or Podman (either works)"
        fi;;
      curl)    echo "    • curl     — used to check the app came up";;
      openssl) echo "    • openssl  — used to generate your instance's secrets";;
    esac
  done
  echo
  echo "  Install commands for common systems:"
  echo "    Debian/Ubuntu:  sudo apt install $(prereq_pkgs apt "$@")"
  echo "    Fedora/RHEL:    sudo dnf install $(prereq_pkgs dnf "$@")"
  echo "    macOS / other:  install Docker Desktop (docs.docker.com/desktop/) or"
  echo "                    Podman (podman.io/docs/installation), plus curl and openssl"
}

# offer to run the package manager for the operator — only on an explicit
# yes, and only via the distro's own tool (no curl|sh). Returns 0 when a
# command was actually run (the caller re-checks), 1 otherwise.
prereq_offer_pkg() {
  local cmd="" ans=""
  if command -v apt-get >/dev/null 2>&1; then
    cmd="apt install $(prereq_pkgs apt "$@")"
  elif command -v dnf >/dev/null 2>&1; then
    cmd="dnf install $(prereq_pkgs dnf "$@")"
  else
    return 1   # unknown package manager — the operator installs by hand
  fi
  # never assume root: prefix sudo only when we aren't already root
  [ "${EUID:-$(id -u)}" = 0 ] || cmd="sudo $cmd"
  echo
  printf "  Run it for you now?  %s  [y/N]: " "$cmd"
  prereq_read ans || { echo; return 1; }
  case $ans in y|Y|yes|YES) ;; *) return 1;; esac
  # -y for the package manager itself: consent was just given above, and
  # stdin may not be the terminal (sudo still asks its password on /dev/tty)
  if ! $cmd -y; then
    echo "  That didn't finish cleanly — you can install by hand instead."
  fi
  return 0
}

# the entry point both install.sh and oikonome.sh call. Loops until every
# prerequisite is present (or the operator quits) — nothing downstream
# (secrets, config, compose) runs while something is missing.
require_prereqs() {
  local missing ans
  missing=$(prereq_missing)
  if [ -n "$missing" ]; then
    if ! prereq_interactive; then
      # word splitting intended: $missing is a whitespace-separated token list
      prereq_explain $missing
      echo
      echo "  Install the tools above, then re-run this command."
      exit 1
    fi
    while :; do
      prereq_explain $missing
      if ! prereq_offer_pkg $missing; then
        echo
        printf "  Install the above (another terminal is fine), then press Enter to re-check — or 'q' to quit: "
        prereq_read ans || { echo; exit 1; }   # EOF: don't loop forever
        case $ans in q|Q|quit|exit) echo "  Stopped — nothing was set up yet."; exit 1;; esac
      fi
      missing=$(prereq_missing)
      [ -z "$missing" ] && break
      echo
      echo "  Still missing — checking again:"
    done
  fi
  # the checks above ran in $(…) subshells; set COMPOSE/ENGINE in THIS shell
  prereq_detect_compose
}
