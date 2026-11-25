"""No documented or scripted compose command may pin its own `-f`.

An install chooses its compose files in `docker/.env` — external-database
mode is switched on only by `COMPOSE_FILE=compose.yaml:compose.external-db.yaml`
— and compose ignores COMPOSE_FILE whenever `-f`/`--file` is given. A
command that passes `-f docker/compose.yaml` therefore drops the overlay on
exactly the installs that need it: `up -d` starts an empty bundled Postgres,
and a key rotation or keycheck run there reports success over a database
that holds nothing. Commands run from `docker/` with plain `docker compose`
(or through `./oikonome.sh`), so `.env` decides.
"""

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Where operators and scripts read compose commands from.
_SCANNED = ("docs", "scripts", "extras", "community-scripts", "integrations",
            "ops-cmd")
_ROOT_FILES = ("README.md", "DEVELOPMENT.md", "CONTRIBUTING.md", "SECURITY.md",
               "oikonome.sh", "install.sh", "uninstall.sh", "Makefile")
_SUFFIXES = {".md", ".sh", ".py", ".txt", ".service", ""}

# A compose command (docker/podman compose, docker-compose, podman-compose)
# carrying -f/--file before the end of that command.
_PINNED = re.compile(
    r"(?:docker|podman)[ -]compose\b[^\n|;&]*?\s(?:-f|--file)(?:[\s=])")

# path relative to the repo root → why its -f is legitimate
_ALLOWED: dict[str, str] = {}


def _candidates():
    for name in _ROOT_FILES:
        p = _ROOT / name
        if p.is_file():
            yield p
    for d in _SCANNED:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if (p.is_file() and p.suffix in _SUFFIXES
                    and "node_modules" not in p.parts):
                yield p


class ComposeCommandsHonourComposeFileTests(unittest.TestCase):
    def test_no_command_outside_the_compose_files_pins_its_own_file(self):
        hits = []
        for p in _candidates():
            rel = str(p.relative_to(_ROOT))
            if rel in _ALLOWED:
                continue
            try:
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            # join backslash-continued lines so a wrapped command is one line
            joined = re.sub(r"\\\n\s*", " ", text)
            for m in _PINNED.finditer(joined):
                line = joined.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}: ~line {line}: {m.group(0).strip()}")
        self.assertEqual(hits, [], "compose -f ignores COMPOSE_FILE (drops "
                         "the external-database overlay):\n" + "\n".join(hits))

    def test_the_pattern_catches_the_pinned_form(self):
        # the check itself: a pinned command must match, a plain one not
        self.assertRegex("docker compose -f docker/compose.yaml exec app",
                         _PINNED)
        self.assertRegex("podman compose --file=compose.yaml up -d", _PINNED)
        self.assertNotRegex("docker compose exec -T app oikonome keycheck",
                            _PINNED)
        self.assertNotRegex("[ -f compose.override.yaml ] && docker compose up",
                            _PINNED)


if __name__ == "__main__":
    unittest.main()
