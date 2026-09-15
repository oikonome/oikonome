"""A rename must move every spelling the merchant had when the rename
committed — not only the ones it had when it started looking.

`rename` reads the merchant's member set, then locks the alias rows it
found. Nothing can lock a row that has not been inserted yet, so a
nightly layer-1 pass folding a newly-synced spelling under the same
display name inside that window is easily missed: those rows keep the old
name while their siblings move, splitting one payee in two with nothing
anywhere reporting it.
"""

import unittest

from oikonome.engine import merchant_dedup

from .util import add_txn, make_db


class RenameCoversLateArrivals(unittest.TestCase):

    def test_a_spelling_that_joins_mid_rename_is_renamed_too(self):
        conn = make_db()
        self.addCleanup(conn.close)
        add_txn(conn, "2026-07-01", 10.0, "BIGBOX WHSE #0007",
                merchant="BIGBOX WHSE #0007")
        merchant_dedup.apply(conn)
        display = conn.execute(
            "SELECT canonical FROM merchant_canonical "
            "WHERE raw_merchant='BIGBOX WHSE #0007'").fetchone()["canonical"]

        # a second spelling lands under the same display name after the
        # member set has been read once — exactly the nightly pass writing
        # while someone renames
        real = merchant_dedup.raw_strings_for
        calls = []

        def racing(c, d):
            got = real(c, d)
            if not calls:
                calls.append(1)
                add_txn(conn, "2026-07-02", 12.0, "BIGBOX WHSE #0088",
                        merchant="BIGBOX WHSE #0088")
                conn.execute(
                    "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                    "method, as_of) VALUES ('BIGBOX WHSE #0088',%s,'layer1',now())",
                    (d,))
            return got

        merchant_dedup.raw_strings_for = racing
        try:
            with conn.transaction():
                merchant_dedup.rename(conn, display, "Bigbox Wholesale")
        finally:
            merchant_dedup.raw_strings_for = real

        moved = {r["raw_merchant"]: r["canonical"] for r in conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical").fetchall()}
        self.assertEqual(moved["BIGBOX WHSE #0007"], "Bigbox Wholesale")
        self.assertEqual(moved["BIGBOX WHSE #0088"], "Bigbox Wholesale")


if __name__ == "__main__":
    unittest.main()
