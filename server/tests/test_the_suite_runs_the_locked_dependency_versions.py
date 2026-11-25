"""The suite and the image run one resolution of the dependencies.

The image installs `server/requirements.txt` and nothing else. A suite that
ran on whatever happened to be in the venv would certify versions production
does not run — and a release nothing here ever imported would reach
production at the next image build. So: every dependency pyproject.toml
declares is pinned in the file, every pin carries a hash, and every pinned
package installed here is at exactly its pinned version.

The suite's own test-only tools (`requirements-test.txt`, resolved from
`requirements-test.in` against the server's pins) are held to the same
rules, and the image never installs them.
"""

import re
import tomllib
import unittest
from importlib import metadata
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
LOCK = SERVER / "requirements.txt"
TEST_LOCK = SERVER / "requirements-test.txt"
TEST_IN = SERVER / "requirements-test.in"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lock: Path = LOCK) -> dict[str, str]:
    """name -> version for every requirement line; a line that is not an
    exact pin followed by a hash fails the parse."""
    pins: dict[str, str] = {}
    lines = lock.read_text().splitlines()
    for i, line in enumerate(lines):
        if not line or line[0] in "# ":
            continue
        m = re.match(r"([A-Za-z0-9_.-]+)==([^ ;\\]+)", line)
        if not m:
            raise AssertionError(f"not an exact pin: {line!r}")
        if "--hash=sha256:" not in (lines[i + 1] if i + 1 < len(lines) else ""):
            raise AssertionError(f"pin without a hash: {line!r}")
        pins[_norm(m.group(1))] = m.group(2)
    return pins


class LockedDependencies(unittest.TestCase):
    def test_every_declared_dependency_is_pinned(self):
        project = tomllib.loads(
            (SERVER / "pyproject.toml").read_text())["project"]
        declared = (project["dependencies"]
                    + project["optional-dependencies"]["model"])
        pins = _pins()
        missing = [d for d in declared
                   if _norm(re.match(r"[A-Za-z0-9_.-]+", d).group(0))
                   not in pins]
        self.assertEqual(missing, [],
                         "declared but not pinned — run `make lock`")

    def test_the_venv_holds_the_pinned_versions(self):
        drift = []
        for name, want in sorted({**_pins(), **_pins(TEST_LOCK)}.items()):
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                continue      # another platform's wheel, or an optional extra
            if have != want:
                drift.append(f"{name}: installed {have}, pinned {want}")
        self.assertEqual(drift, [],
                         "the venv is not the pinned resolution — "
                         "run `make sync`")

    def test_the_image_installs_only_the_pinned_resolution(self):
        docker = (SERVER.parent / "docker" / "Dockerfile").read_text()
        self.assertIn(
            "pip install --require-hashes -r server/requirements.txt", docker)
        self.assertIn("pip install --no-deps ./server", docker)
        self.assertNotRegex(docker, r"pip install [^\n]*\./server\[")
        self.assertNotIn("requirements-test", docker)

    def test_the_test_tools_are_pinned_and_agree_with_the_server(self):
        """Every tool the .in file names is pinned exactly in the hashed
        file, which installs the suite's own tools only: a package the
        server also pins has the server's version."""
        wanted = [line.split("==")[0] for line in TEST_IN.read_text().splitlines()
                  if line and not line.startswith("#")]
        pins = _pins(TEST_LOCK)
        self.assertEqual([w for w in wanted if _norm(w) not in pins], [],
                         "named but not pinned — run `make lock`")
        server = _pins()
        clash = [f"{n}: test {v}, server {server[n]}"
                 for n, v in pins.items() if n in server and server[n] != v]
        self.assertEqual(clash, [])
        # the suite cannot silently skip what it never installed
        import hypothesis
        self.assertEqual(hypothesis.__version__, pins["hypothesis"])


if __name__ == "__main__":
    unittest.main()
