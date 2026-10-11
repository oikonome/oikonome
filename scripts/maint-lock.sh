# Sourced (bash) by every process that takes the instance's maintenance lock,
# <install>/.maintenance.lock: ./oikonome.sh (restore, reset, uninstall),
# scripts/backup.sh and scripts/pg-major-migrate.sh. Not run on its own.
#
# The lock itself is flock(1) on descriptor 9. Beside it, the HOLDER file
# <install>/.maintenance.lock.holder says who took it — "pid epoch operation"
# — so a backup that times out waiting can tell the operator what it waited
# for. The rules that keep that answer honest:
#
#   * every process that takes the lock writes the holder, right after
#     flock returns, so the file never names an earlier holder;
#   * each clears it on the way out — only while it still names itself;
#   * a reader never calls the lock stale from the file's age: it asks
#     whether the recorded process still has the lock file open. Age alone
#     cannot tell a long migration from a hung one, and telling an operator
#     to delete the lock under a running migration lets a backup start
#     against a half-loaded database.

# Sets MAINT_PID to this shell's own pid, which is what holds descriptor 9
# — inside a subshell too, where $$ still names the parent. $BASHPID needs
# bash 4; on bash 3.2 (macOS) it is the parent of a child that the
# command substitution EXECs, so no extra shell sits between the two. Never
# call this inside $(...) itself: that would be the substitution's pid.
maint_self_pid() {
  MAINT_PID=${BASHPID:-}
  [ -n "$MAINT_PID" ] || MAINT_PID=$(exec sh -c 'printf "%s" "$PPID"')
}

# maint_holder_write <install-root> <operation>
maint_holder_write() {
  maint_self_pid
  printf '%s %s %s\n' "$MAINT_PID" "$(date +%s)" "${2:-maintenance}" \
    > "$1/.maintenance.lock.holder" 2>/dev/null || true
}

# maint_holder_clear <install-root> — remove the holder if it names us
maint_holder_clear() {
  local f="$1/.maintenance.lock.holder" pid _rest
  [ -r "$f" ] || return 0
  read -r pid _rest < "$f" 2>/dev/null || return 0
  maint_self_pid
  [ "$pid" = "$MAINT_PID" ] && rm -f "$f"
  return 0
}

# maint_pid_holds <pid> <lock-file>: 0 = that process has the file open,
# 1 = it does not (or is gone), 2 = this host cannot tell.
maint_pid_holds() {
  local pid=$1 lock=$2 fd
  [ -d "/proc/$pid" ] || ps -p "$pid" >/dev/null 2>&1 || return 1
  if [ -d "/proc/$pid/fd" ]; then
    [ -r "/proc/$pid/fd" ] || return 2
    for fd in /proc/"$pid"/fd/*; do
      [ "$(readlink "$fd" 2>/dev/null)" = "$lock" ] && return 0
    done
    return 1
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -p "$pid" 2>/dev/null | grep -qF -- "$lock" && return 0
    return 1
  fi
  return 2
}

# maint_skip_message <install-root> <stale-hours>: the line a backup that
# could not get the lock prints. STALE only when the recorded holder no
# longer has the lock open AND has been gone past the stale age — a
# migration that is still running is named as running however long it
# takes, and a host that cannot tell says so instead of guessing.
maint_skip_message() {
  local root=$1 stale_h=$2 lock h_pid h_at h_op age msg alive st
  msg="backup skipped: a restore/reset is running on this instance"
  lock="$(cd "$root" && pwd -P)/.maintenance.lock"
  if ! { [ -r "$root/.maintenance.lock.holder" ] \
         && read -r h_pid h_at h_op < "$root/.maintenance.lock.holder" \
         && [ "${h_at:-x}" -eq "${h_at:-x}" ] 2>/dev/null; }; then
    printf '%s\n' "$msg (the holder did not record itself — find it with: fuser $lock)"
    return 0
  fi
  age=$(( $(date +%s) - h_at ))
  # `|| st=$?`: the callers run under `set -e`, which a bare non-zero
  # answer here would end
  st=0
  maint_pid_holds "$h_pid" "$lock" || st=$?
  case $st in
    0)
      printf '%s\n' "$msg — '$h_op' (pid $h_pid) has held the lock for $(( age / 60 )) minutes and is still running; it ends when that does" ;;
    1)
      if [ "$age" -ge $(( stale_h * 3600 )) ]; then
        alive="no longer running"
        ps -p "$h_pid" >/dev/null 2>&1 && alive="running, but no longer holding it"
        printf '%s\n' "backup skipped: STALE maintenance lock — taken by '$h_op' (pid $h_pid, $alive) $(( age / 3600 )) hours ago, longer than ${stale_h}h. A process that inherited the lock from it (an open ./oikonome.sh session, a child it started) still holds $lock; find it with: fuser $lock, end it, and backups resume"
      else
        printf '%s\n' "$msg"
      fi ;;
    *)
      printf '%s\n' "$msg — taken by '$h_op' (pid $h_pid) $(( age / 60 )) minutes ago; check what holds it with: fuser $lock" ;;
  esac
}
