# Shared docker/.env readers — sourced by oikonome.sh, install.sh, scripts/backup.sh,
# uninstall.sh and scripts/pg-major-migrate.sh, so every script that acts on
# the database folder or the database itself reads the setting the way
# compose does. A literal `grep ^KEY= | cut` read keeps the quotes of
# KEY="…", keeps a trailing ` # comment`, misses `export KEY=` and returns
# every line of a key set twice; compose strips, drops, accepts and takes
# the last. When the two disagree the stack runs on one folder while a
# reset deletes (or an uninstall keeps, or a major-version migration looks
# in) another.

# dotenv_get FILE KEY — KEY from FILE read the way compose reads it: the
# last assignment wins, a leading `export ` and a trailing CR are dropped, a
# quoted value loses its quotes and an unquoted one its trailing
# ` # comment`. Prints nothing when FILE or KEY is absent.
dotenv_get() {
  [ -f "$1" ] || return 0
  awk -v k="$2" -v q="'" '
    { sub(/\r$/, "") }
    $0 ~ "^[ \t]*(export[ \t]+)?" k "[ \t]*=" {
      v = $0
      sub("^[ \t]*(export[ \t]+)?" k "[ \t]*=[ \t]*", "", v)
      c = substr(v, 1, 1)
      if (c == "\"" || c == q) {
        v = substr(v, 2); i = index(v, c); if (i) v = substr(v, 1, i - 1)
      } else {
        sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v)
      }
      val = v; seen = 1
    }
    END { if (seen) print val }' "$1"
}

# canon_path PATH — PATH made absolute (against $PWD), `.` and `..` folded
# lexically (exactly what compose does to a bind-mount source before it
# hands it to the engine), then the longest part of it that exists resolved
# through symlinks. Two spellings of one folder print the same string, so a
# "is this inside the install folder" test can compare prefixes safely —
# `$HERE/docker/../../x` is NOT inside $HERE, though it starts with it.
canon_path() {
  local p=$1 seg out="" d rest=""
  local -a segs
  case $p in /*) : ;; *) p="$PWD/$p" ;; esac
  IFS=/ read -ra segs <<< "$p"
  for seg in "${segs[@]}"; do
    case $seg in
      ""|.) : ;;
      ..) out=${out%/*} ;;
      *) out="$out/$seg" ;;
    esac
  done
  [ -n "$out" ] || out=/
  # the deepest ancestor that can be entered (a database folder is often
  # mode 0700 and owned by the container's uid: it is kept as spelled)
  d=$out
  while [ "$d" != / ] && ! { [ -d "$d" ] && [ -x "$d" ]; }; do
    rest="/${d##*/}$rest"
    d=${d%/*}; [ -n "$d" ] || d=/
  done
  local real
  real=$(cd -P "$d" 2>/dev/null && pwd -P) && d=$real
  [ "$d" = / ] && d=""
  out="$d$rest"
  printf '%s\n' "${out:-/}"
}

# data_dir_resolve DOCKER_DIR DEFAULT — the database folder the postgres
# service mounts, as a canonical absolute path: OIKONOME_DATA_DIR from the
# environment first, then DOCKER_DIR/.env (the precedence compose applies),
# relative values resolved against DOCKER_DIR as compose resolves them.
# DEFAULT (resolved the same way) when it is unset; empty when DEFAULT is.
data_dir_resolve() {
  local dir=$1 v
  v=${OIKONOME_DATA_DIR:-$(dotenv_get "$dir/.env" OIKONOME_DATA_DIR)}
  v=${v:-$2}
  [ -n "$v" ] || return 0
  case $v in /*) : ;; *) v="$dir/$v" ;; esac
  canon_path "$v"
}

# probe_host BIND_ADDRESS — the host part of a URL that reaches the app
# port from this machine. With OIKONOME_BIND_ADDRESS pinned to one address
# the port is published there only, so `localhost` finds nothing and every
# readiness probe reads a healthy app as down; a wildcard (or unset) bind
# answers on loopback. IPv6 literals are bracketed for use in a URL.
probe_host() {
  case ${1:-} in
    ""|0.0.0.0|::|"[::]") echo localhost ;;
    \[*\]) echo "$1" ;;
    *:*) echo "[$1]" ;;
    *) echo "$1" ;;
  esac
}

# backup_dir_resolve DOCKER_DIR — the folder scheduled and manual backups
# land in and the admin console reads (compose binds it at /state/backups):
# OIKONOME_BACKUP_DIR from the environment first, then DOCKER_DIR/.env, a
# relative value resolved against DOCKER_DIR as compose resolves a bind
# source; DOCKER_DIR/../backups when unset.
backup_dir_resolve() {
  local dir=$1 v
  v=${OIKONOME_BACKUP_DIR:-$(dotenv_get "$dir/.env" OIKONOME_BACKUP_DIR)}
  v=${v:-../backups}
  case $v in /*) : ;; *) v="$dir/$v" ;; esac
  canon_path "$v"
}

# pg_data_version DIR ENGINE IMAGE — the PG_VERSION of the Postgres data
# folder DIR. Prints the major and returns 0; prints nothing and returns 0
# when DIR holds no cluster (absent, or no PG_VERSION in it); returns 2
# when DIR exists but its contents cannot be read. Under rootful Docker the
# postgres entrypoint hands the folder to the container's uid at mode 0700,
# so the host user who runs the upgrade cannot look inside, and reading
# that as "no database" is how an upgrade would start a new server major
# on an old cluster. That case is read from inside a throwaway container
# of IMAGE (the stack's own postgres image, so nothing extra is pulled),
# the same way a reset deletes those files. label=disable: the folder
# carries the postgres container's private SELinux label, which a probe
# must neither be refused by nor overwrite.
pg_data_version() {
  local dir=$1 engine=$2 image=$3 out
  [ -e "$dir" ] || return 0
  if [ -r "$dir" ] && [ -x "$dir" ]; then
    if [ -f "$dir/PG_VERSION" ] && [ -r "$dir/PG_VERSION" ]; then
      tr -dc '0-9' < "$dir/PG_VERSION"; echo
      return 0
    fi
    [ -e "$dir/PG_VERSION" ] || return 0
  fi
  out=$("$engine" run --rm --network none --security-opt label=disable \
          --entrypoint sh -v "$dir":/d:ro "$image" \
          -c 'if [ -f /d/PG_VERSION ]; then cat /d/PG_VERSION; else echo none; fi' \
          2>/dev/null) || return 2
  out=$(printf '%s' "$out" | tr -d '\r\n ')
  case $out in
    none) return 0 ;;
    *[!0-9]*|"") return 2 ;;
  esac
  echo "$out"
}
