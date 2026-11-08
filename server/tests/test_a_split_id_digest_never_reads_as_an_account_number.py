"""A split account's digest id is never taken for one that spells a number.

A source whose slug loses what tells it apart (a letter the slug cannot
spell, a name past the cut) gets an id that carries a short digest of the
source. Restore moves every split account whose id holds eight digits in
a row, because an earlier release spelled full account numbers that way.
Spelled in hex, the digest is all decimal about one time in forty-four:
restore then moved an account that held no number, the next import minted
the digest id again beside it, and one source's ledger was split across
two accounts, growing by one more on every restore.
"""
import hashlib
import unittest
from unittest import mock

from oikonome.sync import adopt, base, qifimport

from .util import make_db, write_config


def _all_decimal_source() -> str:
    """An invented source name the slug cannot spell whose digest, in hex,
    is all decimal digits."""
    for i in range(10_000):
        name = f"Épargne Fabrikam {i}"
        hexed = hashlib.sha1(base._split_source(name).encode()).hexdigest()
        if hexed[:8].isdigit():
            return name
    raise AssertionError("no all-decimal digest in range")


def _readable_ensure(conn, prefix, name, *, key=None, type_=None,
                     subtype=None, lines=None):
    """The split account under the id a build before opaque ids derived
    (base.split_account_id)."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    aid = base.split_account_id(prefix, key or name)
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype)"
        " VALUES (%s,'manual',%s,'depository','checking')"
        " ON CONFLICT (tenant_id, id) DO NOTHING", (aid, name))
    return aid


def _qif(*accounts):
    out = []
    for name, rows in accounts:
        out += ["!Account", f"N{name}", "TBank", "^", "!Type:Bank"]
        for day, amount, payee in rows:
            out += [f"D03/{day:02d}/2026", f"T{amount:.2f}", f"P{payee}",
                    "^"]
    return "\n".join(out) + "\n"


ROWS = [(4, -61.20, "SPRINGFIELD GROCER"), (5, -5.00, "SERVICE FEE")]
MORE = ROWS + [(9, -14.00, "CONTOSO STREAM")]
OTHER = [(6, -23.00, "NORTHWIND FUEL")]


class DigestIdTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.name = _all_decimal_source()

    def _split(self):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%' GROUP BY account_id")}

    def test_the_digest_holds_no_digit_run_and_is_not_rekeyed(self):
        aid = base.split_account_id("qif", self.name)
        self.assertIsNone(base.NUMBERED_SPLIT_ID_RE.search(aid), aid)
        self.assertIsNone(base.LONG_DIGITS_RE.search(aid), aid)
        taken = base.split_account_id("qif", self.name, taken=True)
        self.assertIsNone(base.NUMBERED_SPLIT_ID_RE.search(taken), taken)

    def test_a_restore_then_a_reimport_keeps_one_account(self):
        qifimport.import_qif(self.conn, "chk", _qif(
            (self.name, ROWS), ("Harbor Savings", OTHER)))
        before = self._split()
        # what every restore runs over the household's accounts
        self.assertEqual(base.rekey_numbered_split_accounts(self.conn), {})
        self.assertEqual(self._split(), before)
        qifimport.import_qif(self.conn, "chk", _qif(
            (self.name, MORE), ("Harbor Savings", OTHER)))
        got = self._split()
        self.assertEqual(len(got), 2, got)
        self.assertEqual(sorted(got.values()), [len(OTHER), len(MORE)])

    def test_a_reimport_follows_an_account_moved_off_its_current_id(self):
        """The current id's account was moved (and the move recorded): the
        import lands on the moved account rather than minting the id
        again, and adds none of the rows it made before."""
        text = _qif((self.name, ROWS), ("Harbor Savings", OTHER))
        # the accounts as a build before opaque ids made them: under the
        # readable id, their rows hashed over it, no account_sources row
        with mock.patch.object(qifimport, "ensure_split_account",
                               _readable_ensure):
            qifimport.import_qif(self.conn, "chk", text)
        aid = base.split_account_id("qif", self.name)
        self.assertIn(aid, self._split())
        moved = "manual:qif-epargne-fabrikam"
        self.assertTrue(adopt._rewrite_account_id(self.conn, aid, moved,
                                                  "manual"))
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id, reconciled_at) VALUES (%s, %s, 'manual', now())",
            (moved, aid))
        before = self._split()
        qifimport.import_qif(self.conn, "chk", _qif(
            (self.name, MORE), ("Harbor Savings", OTHER)))
        got = self._split()
        self.assertNotIn(aid, got)
        self.assertEqual(got[moved], len(MORE))
        self.assertEqual(set(got), set(before))


if __name__ == "__main__":
    unittest.main()
