"""The account editor offers the forecast anchor and the budget exclusion
where the server says each applies (the accounts reply's `can_pin` and
`can_exclude`), and names an account's type the way the list does. Every account — a card, a retirement
plan, one closed years ago — showed both switches and the feed's raw
"type/subtype" pair. The rule itself is exercised by the phone's pure-logic
tests (`accountSwitches`, `kindWords`); this pins both clients to it.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fn(src: str, name: str) -> str:
    """The body of an exported function, whitespace collapsed."""
    at = src.index(f"export function {name}(")
    return re.sub(r"\s+", " ", src[at:src.index("\n}\n", at)])


class EditorOffersWhatApplies(unittest.TestCase):
    def setUp(self):
        self.web = (_ROOT / "webapp/src/pages/Accounts.tsx").read_text()
        self.mob = (_ROOT / "mobile/src/app/(tabs)/accounts.tsx").read_text()
        self.web_lib = (_ROOT / "webapp/src/moneymath.ts").read_text()
        self.mob_lib = (_ROOT / "mobile/src/lib/pure.ts").read_text()

    def test_the_two_clients_share_one_rule(self):
        for name in ("accountSwitches", "kindWords"):
            self.assertEqual(_fn(self.web_lib, name), _fn(self.mob_lib, name),
                             name)

    def test_each_switch_is_drawn_only_when_offered(self):
        for src in (self.web, self.mob):
            self.assertTrue("accountSwitches({" in src)
            self.assertTrue("{offered.primary && (" in src)
            self.assertTrue("{offered.excluded && (" in src)
            # the server's flags decide, never the type read on the client
            self.assertTrue("can_pin: a.can_pin, can_exclude: a.can_exclude"
                            in src)
            self.assertFalse("kind: a.kind, pinned" in src)
        for lib in (self.web_lib, self.mob_lib):
            rule = _fn(lib, "accountSwitches")
            self.assertTrue("a.excluded || (a.can_exclude ?? true)" in rule)
            self.assertTrue("a.pinned || (a.can_pin ?? true)" in rule)
            self.assertFalse('"depository"' in rule)

    def test_no_raw_type_pair_is_printed(self):
        self.assertFalse("<option value={a.kind}>{a.kind}</option>"
                         in self.web)
        self.assertFalse("{a.kind}{a.owner" in self.mob)
        self.assertTrue("{kindWords(a.kind)}{a.owner" in self.mob)


if __name__ == "__main__":
    unittest.main()
