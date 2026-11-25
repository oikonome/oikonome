"""No screen on either client shows a person the raw text of a failure.

`String(error)` printed "Error: 500: {"detail":"…"}" on twenty web pages and
"TypeError: Network request failed" on the phone; a "Couldn't load: 422"
named a bare status; the native passkey sheet's "NotAllowedError: …" was
read out as it came. Every failed read now says itself through the shared
failed line (failLine / FailedLine), and every other failure through
errText, which falls back to the shared cause (failCause) — the server's
own sentence when it sent one, else the cause in the app's words.
(specs/screen-states.md)

This is the raw-text guard: it reads every screen source on both clients
and fails when a raw rendering of an error creeps back.
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"

# What a raw rendering looks like in a source file. Each names the shape,
# not a file, so a new screen is covered the day it is written.
_RAW = [
    # the failure's own string inside JSX
    ("String(error) on screen",
     re.compile(r"\{\s*String\(\s*(?:[\w.]+\.)?(?:error|err|e)\s*\)\s*\}")),
    # "Couldn't load: {…}" and the like: the failed line says these now
    ("a hand-made failed line around the failure",
     re.compile(r"Couldn(?:'|&apos;)t [^\n{}<>\"`]*:\s*\{\s*(?:String|errText)\(")),
    ("the failure's message on screen",
     re.compile(r"\{\s*(?:e|err|error|[\w.]+\.error)\.message\s*\}")),
    ("the failure's message pasted into a sentence",
     re.compile(r"\$\{\s*(?:e|err|error)(?:\.message)?\s*\}"
                r"|\+\s*(?:e|err|error)\.message\b")),
    ("a native error's name and message passed on",
     re.compile(r"err\?\.name\s*\|\|")),
    ("a status printed on its own",
     re.compile(r"Couldn(?:'|&apos;)t load: \$\{")),
    ("a JSON body on screen", re.compile(r"\{\s*JSON\.stringify\(")),
    ("a stack on screen", re.compile(r"\{\s*[\w.]*\.stack\s*\}")),
    # the old hand-written retry lines: the failed line's Try again
    # replaces them, with the real cause in front
    ("a hand-written retry line",
     re.compile(r"(?:tap|pull down|reload) to retry")),
]


def _sources():
    for tree, exts in ((_WEB, ("*.tsx", "*.ts")), (_MOB, ("*.tsx", "*.ts"))):
        for ext in exts:
            for p in sorted(tree.rglob(ext)):
                if "node_modules" in p.parts or p.name.endswith(".generated.ts"):
                    continue
                yield p


class NoRawErrorText(unittest.TestCase):
    def test_no_screen_renders_a_failure_raw(self):
        if not _WEB.exists() or not _MOB.exists():
            self.skipTest("client sources not present")
        hits = []
        for p in _sources():
            src = p.read_text()
            for name, rx in _RAW:
                for m in rx.finditer(src):
                    line = src.count("\n", 0, m.start()) + 1
                    hits.append(f"{p.relative_to(_APP)}:{line}: {name}: "
                                f"{m.group(0)!r}")
        self.assertEqual(hits, [], "\n".join(hits))

    def test_errtext_falls_back_to_the_shared_cause(self):
        """errText is what every write's failure line reads; with no
        sentence from the server it says the cause, never "TypeError:
        Failed to fetch" or a status on its own."""
        web = (_WEB / "api" / "client.ts").read_text()
        fn = web[web.index("export function errText("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('failCause(e, "web")', fn)
        mob = (_MOB / "lib" / "api.ts").read_text()
        fn = mob[mob.index("export function errText("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('failCause(e, "phone")', fn)
        self.assertLess(fn.index("failCause("), fn.index("e.detail"))
        self.assertNotIn("Couldn't load: ${e.status}", mob)
        self.assertNotIn("error page (${status})", web)

    def test_the_last_raw_lines_are_the_failed_line(self):
        for p, what in ((_WEB / "pages" / "Today.tsx", "Today"),
                        (_WEB / "pages" / "Help.tsx", "help"),
                        (_WEB / "pages" / "Alerts.tsx", "alerts"),
                        (_WEB / "pages" / "Reimburse.tsx", "reimbursements"),
                        (_WEB / "pages" / "Feedback.tsx", "the feedback page"),
                        (_WEB / "pages" / "Merchants.tsx", "this merchant"),
                        (_MOB / "app" / "merchant.tsx", "this merchant"),
                        (_WEB / "pages" / "Assistant.tsx", "the history"),
                        (_MOB / "app" / "assistant.tsx", "the history"),
                        (_MOB / "app" / "txn.tsx", "the categories"),
                        (_WEB / "pages" / "Reimburse.tsx", "the deposits"),
                        (_MOB / "app" / "reimburse.tsx", "the deposits")):
            self.assertIn(f'what="{what}" />', p.read_text(), p)
        for p, who in ((_WEB / "pages" / "Feedback.tsx", '"web"'),
                       (_MOB / "app" / "feedback.tsx", '"phone"')):
            self.assertIn(f'failLine(send.error, "your report", {who}, "send")',
                          p.read_text(), p)


if __name__ == "__main__":
    unittest.main()
