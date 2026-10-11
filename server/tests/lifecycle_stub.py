"""A staged install folder and a stand-in container engine, for driving the
lifecycle scripts (oikonome.sh, install.sh, uninstall.sh,
scripts/pg-major-migrate.sh) without a real engine or database.

The stand-in `docker` answers what the scripts ask of compose and of the
bundled postgres container from environment knobs, and appends what it was
asked to do to STUB_LOG, so a test can assert on the effect rather than on
the script's text. Every value it hands back is synthetic.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOCKER = r'''#!/bin/sh
log="${STUB_LOG:-/dev/null}"
echo "ARGS: $*" >> "$log"
if [ "$1" = compose ]; then
  shift
  case "$1" in
    version) exit 0 ;;
    up)
      # STUB_UP_FAIL_AT=N: the Nth `compose up` exits 1, the way docker
      # compose v2 does when a one-shot service it waits on fails
      [ -n "${STUB_UP_FAIL_AT:-}" ] || exit 0
      n=$(( $(cat "$log.up" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$log.up"
      [ "$n" = "$STUB_UP_FAIL_AT" ] && exit 1
      exit 0 ;;
    exec)
      shift; [ "$1" = -T ] && shift
      shift                      # service
      tool=$1; shift
      case $tool in
        pg_isready) exit "${STUB_PG_READY_RC:-0}" ;;
        pg_dump)
          [ -n "${STUB_DUMP_FILE:-}" ] && cat "$STUB_DUMP_FILE"
          exit "${STUB_DUMP_RC:-0}" ;;
        psql)
          sql=""
          for a in "$@"; do
            case $a in *SELECT*|*DROP*|*CREATE*) sql="$sql $a" ;; esac
          done
          case $sql in
            *information_schema.tables*) echo 5 ;;
            *string_agg*) echo "" ;;
            *"FROM users"*) echo "${STUB_USERS:-0}" ;;
            *DROP*) echo "DROPSQL:$sql" >> "$log" ;;
            "") { echo "LOAD-BEGIN"; cat; echo "LOAD-END"; } >> "$log" ;;
          esac
          exit 0 ;;
      esac
      exit 0 ;;
    run)
      # the external-database path: `run … migrate sh -c '<snippet>'` —
      # run the snippet against a recording psql
      while [ $# -gt 0 ] && [ "$1" != "-c" ]; do shift; done
      [ "$1" = "-c" ] || exit 0
      OIKONOME_MAINT_DSN=maint OIKONOME_ADMIN_DSN=admin \
        PATH="$STUB_BIN/psqlbin:$PATH" sh -c "$2"
      exit $? ;;
  esac
  exit 0
fi
case "$1" in
  run)
    [ -n "${STUB_RUN_OUT:-}" ] && echo "$STUB_RUN_OUT"
    exit "${STUB_RUN_RC:-0}" ;;
  ps)
    # STUB_APP_CONTAINER: the name `ps` gives for a running container
    [ -n "${STUB_APP_CONTAINER:-}" ] && echo "$STUB_APP_CONTAINER"
    exit 0 ;;
  exec)
    # `exec <container> oikonome clear-2fa --preview …` prints STUB_PREVIEW
    case " $* " in
      *" --preview "*) [ -n "${STUB_PREVIEW:-}" ] && printf '%b\n' "$STUB_PREVIEW" ;;
    esac
    exit 0 ;;
esac
exit 0
'''

PSQL = r'''#!/bin/sh
for a in "$@"; do echo "PSQLARG:$a" >> "${STUB_LOG:-/dev/null}"; done
exit 0
'''


def make_bin(root: Path, no_sleep: bool = False,
             curl_fails: bool = False) -> Path:
    binp = root / "bin"
    (binp / "psqlbin").mkdir(parents=True)
    (binp / "docker").write_text(DOCKER)
    (binp / "psqlbin" / "psql").write_text(PSQL)
    if no_sleep:
        (binp / "sleep").write_text("#!/bin/sh\nexit 0\n")
    if curl_fails:
        (binp / "curl").write_text("#!/bin/sh\nexit 7\n")
    for f in binp.rglob("*"):
        if f.is_file():
            f.chmod(0o755)
    return binp


def stage(tmp: Path, env_text: str, with_compose: bool = False) -> Path:
    """An install folder holding the real oikonome.sh, uninstall.sh and
    scripts/, a docker/.env, and an install.sh that only records it ran."""
    inst = tmp / "install"
    inst.mkdir()
    shutil.copy(ROOT / "oikonome.sh", inst / "oikonome.sh")
    shutil.copy(ROOT / "uninstall.sh", inst / "uninstall.sh")
    shutil.copytree(ROOT / "scripts", inst / "scripts")
    (inst / "docker").mkdir()
    if with_compose:
        shutil.copy(ROOT / "docker" / "compose.yaml",
                    inst / "docker" / "compose.yaml")
    if env_text is not None:
        (inst / "docker" / ".env").write_text(env_text)
    (inst / "backups").mkdir()
    (inst / "install.sh").write_text(
        "#!/bin/sh\necho ran >> \"$(dirname \"$0\")/install-ran\"\n")
    (inst / "install.sh").chmod(0o755)
    return inst


def run_env(binp: Path, log: Path, **extra) -> dict:
    env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
               STUB_LOG=str(log), STUB_BIN=str(binp))
    for k in ("OIKONOME_ASSUME_YES", "OIKONOME_DATA_DIR",
              "OIKONOME_KEEP_MASTER_KEY"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def tempdir(test) -> Path:
    t = tempfile.TemporaryDirectory()
    test.addCleanup(t.cleanup)
    return Path(t.name)


def bash(script: str, cwd=None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=env,
                          capture_output=True, text=True)
