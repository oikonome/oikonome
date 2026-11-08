"""A Postgres major upgrade reloads into a server of the NEW major.

The bundled database runs an image built locally from the base named in
compose.yaml, under a tag that stays the same across majors. Compose only
builds that tag when it is missing, so on an existing install a plain
`compose up` starts the image cached from the OLD major: the reload lands in
a fresh old-major cluster, the migration reports success and deletes its
dump, and the next rebuild starts the new major on that folder, where
postgres refuses the files and restarts in a loop. The migration must build
the image from the new base before it starts the new cluster, and must
refuse to reload into a server of any other major.

The stand-in engine keeps one cached image per tag and one data folder, and
behaves like compose: `up` builds only a missing image, `build` rebuilds it
from the base_image line, and the server initialises an empty folder with
its own major.
"""

import re
import unittest

from . import lifecycle_stub as ls

ROOT = ls.ROOT

ENGINE = r'''#!/bin/sh
st="$STUB_STATE"
echo "ARGS: $*" >> "$st/log"
base_major() {
  grep -oE 'base_image: docker.io/library/postgres:[0-9]+' compose.yaml \
    | head -1 | grep -oE '[0-9]+$'
}
build() {
  [ -n "${STUB_BUILD_NOOP:-}" ] && [ -f "$st/image" ] && return 0
  base_major > "$st/image"
}
server() {   # the cluster a container of the cached image runs, if it can
  [ -f "$STUB_DATA/PG_VERSION" ] || cp "$st/image" "$STUB_DATA/PG_VERSION"
  cmp -s "$st/image" "$STUB_DATA/PG_VERSION"
}
if [ "$1" = compose ]; then
  shift
  case "$1" in
    version) exit 0 ;;
    build) build; exit $? ;;
    up)
      case " $* " in *" --build "*) build ;; esac
      [ -f "$st/image" ] || build
      exit 0 ;;
    exec)
      shift; [ "$1" = -T ] && shift; shift
      tool=$1; shift
      server || exit 2
      case $tool in
        pg_isready) exit 0 ;;
        psql)
          case "$*" in
            *server_version_num*) echo "$(cat "$st/image")0000" ;;
            *information_schema.tables*) printf 'accounts\t3\n' ;;
            *) cat > /dev/null ;;
          esac
          exit 0 ;;
      esac
      exit 0 ;;
  esac
  exit 0
fi
case "$1" in
  exec)   # the one-off old-major reader
    case "$*" in
      *pg_dumpall*) head -c 4096 /dev/zero | tr '\0' x ;;
      *information_schema.tables*) printf 'accounts\t3\n' ;;
    esac
    exit 0 ;;
esac
exit 0
'''


class MajorUpgradeTests(unittest.TestCase):

    def _run(self, cached_major="18", **knobs):
        t = ls.tempdir(self)
        inst = ls.stage(t, "COMPOSE_PROJECT_NAME=acme\n", with_compose=True)
        compose = inst / "docker" / "compose.yaml"
        src = compose.read_text()
        have = re.search(r"base_image: docker.io/library/postgres:(\d+)",
                         src).group(1)
        want = str(int(have) + 1)
        # the next release: the base names the next major
        compose.write_text(src.replace(
            f"base_image: docker.io/library/postgres:{have}-",
            f"base_image: docker.io/library/postgres:{want}-"))
        data = inst / "data" / "postgres"
        data.mkdir(parents=True)
        (data / "PG_VERSION").write_text(f"{have}\n")
        state = t / "state"
        state.mkdir()
        # every existing install already built its tag at the old major
        (state / "image").write_text(f"{cached_major or have}\n")
        binp = t / "bin"
        binp.mkdir()
        for name, body in (("docker", ENGINE),
                           ("sudo", '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n'),
                           ("sleep", "#!/bin/sh\nexit 0\n")):
            (binp / name).write_text(body)
            (binp / name).chmod(0o755)
        r = ls.bash(f'bash "{inst}/scripts/pg-major-migrate.sh" "{inst}"',
                    env=ls.run_env(binp, t / "unused", STUB_STATE=state,
                                   STUB_DATA=data, OIKONOME_MAINT_LOCK_WAIT=1,
                                   **knobs))
        return r, have, want, data, inst

    def test_the_new_cluster_is_written_by_the_new_major(self):
        r, have, want, data, _ = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((data / "PG_VERSION").read_text().strip(), want,
                         "the reload went into a cluster of the cached old "
                         "image, which the new server cannot open")
        self.assertIn(f"{have} → {want} migrated", r.stdout)

    def test_a_server_of_another_major_is_refused_with_the_way_back(self):
        r, have, _want, _data, inst = self._run(STUB_BUILD_NOOP=1)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("YOUR DATABASE IS SAFE", r.stderr)
        self.assertNotIn("migrated", r.stdout)
        # the dump is kept, and the old folder is still aside
        self.assertTrue(list((inst / "data").glob(f"pg{have}-to-*.sql")))
        self.assertTrue(list((inst / "data").glob(f"postgres.pg{have}-*")))


if __name__ == "__main__":
    unittest.main()
