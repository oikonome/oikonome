"""A failed background refetch never replaces data a page already shows.

In TanStack Query v5 a refetch that fails after a success sets the query's
status to "error" and KEEPS its data, so `isError` is true while `data` is
still there. A page that returned "Couldn't load" on `isError` threw away a
working screen whenever a focus refetch failed — typically a phone whose
radio was still waking. Only a first load with nothing to show
(`isLoadingError`) may take the page over. The app shell's /me check is
exempt: it handles sign-out and lockouts itself and already lets a
transient failure through.
"""

import pathlib
import re
import unittest

import oikonome

_SRC = pathlib.Path(oikonome.__file__).parent.parent.parent / "webapp" / "src"

# an early return or a render-nothing gate on a bare isError
_BAD = re.compile(r"if \(\s*(\w+)\.isError\s*\)|"
                  r"(\w+)\.isPending \|\| \2\.isError\b")


class WebPagesKeepLoadedData(unittest.TestCase):
    def test_no_page_gates_on_a_bare_isError(self):
        if not _SRC.exists():
            self.skipTest("webapp/ not present")
        bad = []
        for p in sorted(_SRC.rglob("*.tsx")):
            if p.name == "App.tsx":
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if _BAD.search(line):
                    bad.append(f"{p.relative_to(_SRC)}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "gate on isLoadingError instead:\n"
                         + "\n".join(bad))

    def test_the_named_pages_gate_on_isLoadingError(self):
        for rel in ("pages/Today.tsx", "pages/Accounts.tsx",
                    "pages/Reimburse.tsx", "pages/Business.tsx",
                    "pages/Rules.tsx"):
            p = _SRC / rel
            if not p.exists():
                self.skipTest(f"{rel} not present")
            self.assertIn(".isLoadingError", p.read_text(), rel)


if __name__ == "__main__":
    unittest.main()
