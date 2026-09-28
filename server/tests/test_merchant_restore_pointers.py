"""A restored archive must never plant a merchant pointer that names
nothing.

An export can carry transactions and aliases whose `merchant_id` refers to
a merchant the archive does not contain — a partial export, a hand-edited
CSV, a ZIP assembled across versions. Copied in verbatim, those ids are
worse than no id at all: the identity pass reads the merchant's name off a
row that is not there and aborts, so the household's merchants stop being
resolved on every subsequent nightly run.

The same goes for merchant-to-merchant pointers: a `merged_into` chain must
arrive collapsed to its terminal survivor, a chain leaving the archive must
arrive as no pointer, and a cycle must not survive at all.
"""

import csv
import io
import unittest
import uuid
import zipfile

from oikonome.sync import restore

from .util import TODAY, add_txn, make_db


def _zip(tables: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, (cols, rows) in tables.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c))
                            for c in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


M_COLS = ["id", "name", "name_source", "kind", "parent_id", "merged_into"]
A_COLS = ["raw_merchant", "canonical", "method", "merchant_id"]


def _anchors(merchants):
    """One alias per merchant, so the post-restore prune (which deletes
    merchants nothing points at) leaves the graph under test alone."""
    return (A_COLS, [{"raw_merchant": f"RAW {m['name']}",
                      "canonical": m["name"], "method": "manual",
                      "merchant_id": m["id"]} for m in merchants])


class RestoredMerchantPointers(unittest.TestCase):

    def test_pointers_at_merchants_the_archive_omitted_land_as_null(self):
        conn = make_db()
        self.addCleanup(conn.close)
        real = str(uuid.uuid4())
        ghost = str(uuid.uuid4())
        data = _zip({
            "merchants": (M_COLS, [
                {"id": real, "name": "Northwind Club", "name_source": "plaid",
                 "kind": "merchant"}]),
            "accounts": (["id", "item_id", "name", "type", "subtype"], [
                {"id": "chk", "item_id": "it1", "name": "Test Checking",
                 "type": "depository", "subtype": "checking"}]),
            "transactions": (
                ["id", "account_id", "date", "amount", "name", "merchant_id"], [
                    {"id": "keeps", "account_id": "chk", "date": str(TODAY),
                     "amount": "10.00", "name": "NORTHWIND CLUB WHSE",
                     "merchant_id": real},
                    {"id": "orphan", "account_id": "chk", "date": str(TODAY),
                     "amount": "20.00", "name": "MYSTERY CO",
                     "merchant_id": ghost}]),
            "merchant_canonical": (
                ["raw_merchant", "canonical", "method", "merchant_id"], [
                    {"raw_merchant": "NORTHWIND CLUB WHSE", "canonical": "Northwind Club",
                     "method": "manual", "merchant_id": real},
                    {"raw_merchant": "MYSTERY CO", "canonical": "Mystery Co",
                     "method": "manual", "merchant_id": ghost}]),
        })

        restore.restore_zip(conn, data)

        self.assertEqual(conn.execute(
            "SELECT merchant_id FROM transactions WHERE id='keeps'"
        ).fetchone()["merchant_id"], uuid.UUID(real))
        # nulled, then given a real merchant by the post-restore resolve
        got = conn.execute(
            "SELECT merchant_id FROM transactions WHERE id='orphan'"
        ).fetchone()["merchant_id"]
        self.assertNotEqual(got, uuid.UUID(ghost))
        self.assertEqual(conn.execute(
            """SELECT count(*) AS n FROM merchant_canonical a
                WHERE a.merchant_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM merchants m
                                   WHERE m.id = a.merchant_id)"""
        ).fetchone()["n"], 0)

    def test_a_restored_merge_chain_arrives_collapsed(self):
        conn = make_db()
        self.addCleanup(conn.close)
        a, b, c = (str(uuid.uuid4()) for _ in range(3))
        outside = str(uuid.uuid4())
        gone = str(uuid.uuid4())
        rows = [
            {"id": a, "name": "A", "merged_into": b},
            {"id": b, "name": "B", "merged_into": c},
            {"id": c, "name": "C"},
            # a chain that walks out of the archive is no chain
            {"id": outside, "name": "Outside", "merged_into": gone},
        ]
        data = _zip({"merchants": (M_COLS, rows),
                     "merchant_canonical": _anchors(rows)})

        restore.restore_zip(conn, data)

        rows = {str(r["id"]): r["merged_into"] for r in conn.execute(
            "SELECT id, merged_into FROM merchants").fetchall()}
        self.assertEqual(rows[a], uuid.UUID(c))
        self.assertEqual(rows[b], uuid.UUID(c))
        self.assertIsNone(rows[c])
        self.assertIsNone(rows[outside])

    def test_a_restored_merge_cycle_is_broken(self):
        conn = make_db()
        self.addCleanup(conn.close)
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        rows = [{"id": a, "name": "A", "merged_into": b},
                {"id": b, "name": "B", "merged_into": a}]
        data = _zip({"merchants": (M_COLS, rows),
                     "merchant_canonical": _anchors(rows)})

        restore.restore_zip(conn, data)

        merged = [r["merged_into"] for r in conn.execute(
            "SELECT merged_into FROM merchants").fetchall()]
        self.assertEqual(merged, [None, None])

    def test_a_parent_the_archive_omitted_lands_as_null(self):
        conn = make_db()
        self.addCleanup(conn.close)
        child = str(uuid.uuid4())
        rows = [{"id": child, "name": "Northwind Club Gas",
                 "parent_id": str(uuid.uuid4())}]
        data = _zip({"merchants": (M_COLS, rows),
                     "merchant_canonical": _anchors(rows)})

        restore.restore_zip(conn, data)

        self.assertIsNone(conn.execute(
            "SELECT parent_id FROM merchants WHERE id=%s",
            (child,)).fetchone()["parent_id"])


class MergeOffersSurviveARestore(unittest.TestCase):
    """A rejected merge offer is the household saying "these two are
    different businesses". The proposer's id is deterministic per pair, so
    that row is the ONLY thing keeping the pair from being offered again —
    left out of the archive, every pair the person has already turned down
    comes back the first night after a restore."""

    def test_decisions_and_offers_round_trip_and_a_rejection_still_holds(self):
        from oikonome.engine import merchant_merge as mm
        from oikonome.sync import export

        src, dest = make_db(), make_db()
        self.addCleanup(src.close)
        self.addCleanup(dest.close)
        ids = {}
        for name, raw, n in (("Riverton Pantry", "RIVERTON PANTRY", 5),
                             ("Riverton Pant", "RIVERTON PANT", 8),
                             ("Riverton Pantr", "RIVERTON PANTR", 3)):
            mid = src.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            ids[name] = mid
            for i in range(n):
                tid = add_txn(src, TODAY, 24.5, raw, merchant=raw.title(),
                              primary="FOOD_AND_DRINK")
                src.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                            (mid, tid))
        mm.run(src)
        rejected = next(o for o in mm.pending(src) if o["from"] == "Riverton Pantr")
        mm.decide(src, rejected["id"], "reject")

        restore.restore_zip(dest, export.build_zip(src))

        rows = {r["id"]: r for r in dest.execute(
            "SELECT id, status, from_merchant_id::text AS f, "
            "into_merchant_id::text AS i, evidence FROM merchant_merge_proposals"
        ).fetchall()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[rejected["id"]]["status"], "rejected")
        # the ids travelled, so the row still names the same two merchants
        self.assertEqual(rows[rejected["id"]]["f"], ids["Riverton Pantr"])
        self.assertEqual(rows[rejected["id"]]["i"], ids["Riverton Pantry"])
        self.assertTrue(rows[rejected["id"]]["evidence"]["signals"])
        # and the proposer on the destination does not offer it again
        self.assertEqual(mm.run(dest)["inserted"], 0)
        self.assertNotIn("Riverton Pantr", {o["from"] for o in mm.pending(dest)})

    def test_an_offer_about_a_merchant_the_archive_omitted_is_dropped(self):
        """Both columns are NOT NULL, so unlike an alias there is no
        resolve-it-later form of the row: an offer about a merchant that is
        not here is about nothing."""
        conn = make_db()
        self.addCleanup(conn.close)
        real, ghost = str(uuid.uuid4()), str(uuid.uuid4())
        rows = [{"id": real, "name": "Riverton Pantry",
                 "name_source": "layer1", "kind": "merchant"}]
        data = _zip({
            "merchants": (M_COLS, rows),
            "merchant_canonical": _anchors(rows),
            "merchant_merge_proposals": (
                ["id", "from_merchant_id", "into_merchant_id", "evidence",
                 "status", "created_at", "decided_at"],
                [{"id": "mm:ghost", "from_merchant_id": ghost,
                  "into_merchant_id": real, "evidence": "{}",
                  "status": "rejected"}])})

        restore.restore_zip(conn, data)

        self.assertEqual(conn.execute(
            "SELECT count(*) AS n FROM merchant_merge_proposals"
        ).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
