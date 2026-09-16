"""`python -m oikonome.cli` must actually run main().

Without an `if __name__ == "__main__"` guard the module form imports
cleanly, defines the parser, invokes nothing and exits 0 — and silence with
a zero exit is exactly what success looks like. docs/master-key.md spells
the whole master-key procedure that way:

    python -m oikonome.cli keycheck
    python -m oikonome.cli rotate-master-key

so a documented rotation would "succeed" without touching a row, and the
next step tells the operator to remove OIKONOME_MASTER_KEY_OLD once it
reports clean — i.e. destroy the only key that can still decrypt the data.
The console-script form (`oikonome keycheck`) is unaffected, which is why
only the human-facing instructions break.

Both spellings are pinned here. The module form asserts on argparse's own
required-subcommand refusal (exit 2 + usage), because that is a behaviour only
reachable if main() actually ran.
"""

import subprocess
import sys
import unittest
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_DOCS = _SERVER.parent / "docs" / "master-key.md"


def _run(*args):
    return subprocess.run([sys.executable, "-m", "oikonome.cli", *args],
                          cwd=_SERVER, capture_output=True, text=True)


class ModuleEntrypointTests(unittest.TestCase):
    def test_bare_module_invocation_runs_main(self):
        """No subcommand → argparse exits 2 with usage. Without the
        __main__ guard it is exit 0 and no output at all."""
        r = _run()
        self.assertEqual(r.returncode, 2, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertIn("usage:", (r.stdout + r.stderr).lower())

    def test_module_invocation_exposes_the_subcommands(self):
        r = _run("--help")
        self.assertEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        for cmd in ("keycheck", "rotate-master-key"):
            self.assertIn(cmd, out)


class DocumentedCommandsExistTests(unittest.TestCase):
    """The other half of the same defect: docs naming a command the parser
    does not have would fail the same way, loudly instead of silently."""

    def test_master_key_doc_commands_are_real_subcommands(self):
        if not _DOCS.exists():                       # docs/ absent in a slim checkout
            self.skipTest("docs/master-key.md not present")
        text = _DOCS.read_text()
        help_out = _run("--help").stdout
        for cmd in ("keycheck", "rotate-master-key"):
            if cmd in text:
                self.assertIn(cmd, help_out,
                              f"docs/master-key.md documents `{cmd}` but the "
                              f"CLI does not offer it")
