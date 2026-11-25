"""An account is labelled with its institution said once, a type in words,
and a connection's sync outcome in the words its Accounts row uses. The
account picker read "Fabrikam Fabrikam (daily)" for an account the bank
names after itself, the Business account rows printed the feed's raw
"type/subtype", and the phone's Sync card printed raw status strings. The
helpers are exercised by the phone's pure-logic tests.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fn(src: str, name: str) -> str:
    at = src.index(f"export function {name}(")
    return re.sub(r"\s+", " ", src[at:src.index("\n}\n", at)])


class NamesSaidOnce(unittest.TestCase):
    def _read(self, rel):
        return (_ROOT / rel).read_text()

    def test_one_label_rule_on_both_clients(self):
        self.assertEqual(
            _fn(self._read("webapp/src/moneymath.ts"), "accountLabel"),
            _fn(self._read("mobile/src/lib/pure.ts"), "accountLabel"))

    def test_the_account_picker_uses_it(self):
        web = self._read("webapp/src/pages/Transactions.tsx")
        self.assertEqual(web.count("{accountLabel(a)}"), 2)
        self.assertFalse("{a.institution_name} {a.name}" in web)
        self.assertFalse("{a.institution_name} {a.name}"
                         in self._read("mobile/src/app/business.tsx"))

    def test_business_account_rows_name_the_type_in_words(self):
        self.assertFalse('<span className="mut">{a.kind}</span>'
                         in self._read("webapp/src/pages/Business.tsx"))
        self.assertTrue("{kindWords(a.kind)}"
                        in self._read("mobile/src/app/business.tsx"))

    def test_the_sync_card_prints_no_raw_status(self):
        mob = self._read("mobile/src/app/imports.tsx")
        self.assertFalse('${c.status ?? "ok"}' in mob)
        self.assertTrue("connectionStateWords(c)" in mob)


if __name__ == "__main__":
    unittest.main()
