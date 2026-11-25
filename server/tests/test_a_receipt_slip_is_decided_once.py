"""Which receipts are one slip is decided once, stored, and read back.

Two receipts are one slip for exactly two reasons: the same bytes, or a
person's answer. Two photos READ alike (store, day, total) are never
joined by a rule: both count, and the person is asked on the panel, the
waiting list and the Receipts page lines, never silently. The one
automatic case is the nightly release's hold-back: a stranded copy is
left on its retired row (counting nothing) while a look-alike sits on a
live charge of the same card and the matcher has no other charge for it,
judged afresh every night and never stored. Deciding never moves a
receipt and never overrules a person; the outcome does not depend on the
order the uploads, reads, retirements and releases came in. See
specs/receipts.md, "A receipt is one slip".
"""
import datetime as dt
import io
import json
import random
import unittest
import uuid
from unittest import mock
import zipfile

import httpx

from oikonome.engine import budget, receipt_identity, receipt_match, receipts
from oikonome.sync import adopt, export, reanchor, restore

from .util import add_txn, make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=adopt.RECONCILE_DAYS + 6)
LONG_AGO = adopt.RECONCILE_DAYS + 2


def _slip(store="Harbor Noodle House", total=40.00, desc="ramen bowl",
          day=DAY):
    return {"merchant": store, "date": day.isoformat(), "total": total,
            "tax": 3.00, "tip": None,
            "items": [{"description": desc, "qty": 1,
                       "amount": round(total - 3.00, 2)}]}


def _vision(payload):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]}))


class Household:
    """A household with the doors a person and the nightly use."""

    def __init__(self):
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        self._n = 0

    def photo(self, txn_id, image=None, payload=None, read=True):
        self._n += 1
        image = image or b"\x89PNG photo %d" % self._n
        rid = receipts.add(self.conn, txn_id, image, "image/png")
        if read:
            self.read(rid, payload or _slip())
        return rid

    def read(self, rid, payload=None):
        out = receipts.parse_one(self.conn, rid,
                                 transport=_vision(payload or _slip()))
        assert out["status"] == "parsed", out

    def retire(self, txn_id, days_ago=LONG_AGO):
        """The row left the ledger `days_ago`, and its receipts were on it
        at least that long."""
        self.conn.execute(
            "UPDATE transactions SET removed = 1 WHERE id = %s", (txn_id,))
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - make_interval("
            "days => %s) WHERE id = %s", (days_ago, txn_id))
        self.conn.execute(
            "UPDATE receipts SET created_at = now() - make_interval("
            "days => %s) WHERE txn_id = %s", (days_ago, txn_id))

    def nightly(self):
        reanchor.reanchor_stranded(self.conn)
        freed = receipt_match.release_unplaced(self.conn)
        receipt_match.match_waiting(self.conn)
        return freed

    def counted(self, q=""):
        """(receipt ids whose lines count, the item total)."""
        got = receipts.search_items(self.conn, q=q, group="merchant")
        return ({r["receipt_id"] for r in got["rows"] if r["counts"]},
                round(sum(g["total"] for g in got["groups"]), 2))

    def key(self, rid):
        return self.conn.execute(
            "SELECT COALESCE(slip_id, id)::text AS k FROM receipts"
            " WHERE id = %s::uuid", (rid,)).fetchone()["k"]

    def where(self, rid):
        r = self.conn.execute(
            "SELECT txn_id, match_method FROM receipts WHERE id = %s::uuid",
            (rid,)).fetchone()
        return r["txn_id"], r["match_method"]


class ASlipIsDecidedOnceTests(unittest.TestCase):
    def setUp(self):
        self.h = Household()
        self.conn = self.h.conn
        self.addCleanup(self.conn.close)

    def asked(self, a, b):
        """Is each of the two receipts put to the person about the other?"""
        got = receipt_identity.suspected(self.conn, [a, b])
        return b in got.get(a, ()) and a in got.get(b, ())

    def lines(self, q=""):
        return receipts.search_items(self.conn, q=q, group="merchant")

    def test_two_identical_purchases_on_two_cards_are_two(self):
        # the same meal bought twice, one on each card; the first card's
        # hold was dropped with no posted twin
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-a")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 40.00,
                "HARBOR NOODLE HOUSE", account="chk", txn_id="posted-b")
        first = self.h.photo("hold-a")
        self.h.retire("hold-a")
        second = self.h.photo("posted-b")
        self.assertNotEqual(self.h.key(first), self.h.key(second))
        # another card's charge never holds a copy back
        self.assertEqual(self.h.nightly(), 1)
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertTrue(self.asked(first, second))

    def _stranded_and_posted(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 49.00,
                "UNRELATED NAME", txn_id="posted-1")
        first = self.h.photo("hold-1")
        self.h.retire("hold-1")
        return first

    def test_a_reading_never_joins_two_receipts(self):
        # no order of photo and reading joins two receipts: the second
        # photo read after the release, taken after it, or read alike on
        # one charge
        first = self._stranded_and_posted()
        second = self.h.photo("posted-1", read=False)
        self.assertEqual(self.h.nightly(), 1)
        self.h.read(second)
        third = self.h.photo("posted-1")
        keys = {self.h.key(r) for r in (first, second, third)}
        self.assertEqual(len(keys), 3)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM receipts WHERE slip_id IS NOT NULL"
        ).fetchone()["n"], 0)

    def test_a_second_photo_after_the_release_is_asked_and_flagged(self):
        first = self._stranded_and_posted()
        self.assertEqual(self.h.nightly(), 1)
        self.assertEqual(self.h.where(first)[0], None)
        second = self.h.photo("posted-1")
        # both count, never silently: the panel, the waiting list and
        # every line of either say so, and so does the total
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertTrue(self.asked(first, second))
        [panel] = receipts.for_txn(self.conn, "posted-1")
        self.assertEqual([t["receipt_id"] for t in panel["suspected"]],
                         [first])
        [w] = receipt_match.waiting(self.conn)["waiting"]
        self.assertEqual([t["receipt_id"] for t in w["suspected"]], [second])
        got = self.lines()
        self.assertEqual({(r["receipt_id"], r["suspected"], r["note"])
                          for r in got["rows"]},
                         {(first, True, receipt_identity.LINE_DOUBLED),
                          (second, True, receipt_identity.LINE_DOUBLED)})
        self.assertIn("may hold a purchase twice", got["groups"][0]["note"])
        # the person's answer settles it, and every pass keeps it
        receipt_identity.same_slip(self.conn, second, first, request=False)
        self.h.nightly()
        self.assertEqual(self.h.counted(), ({second}, 37.0))
        got = self.lines()
        self.assertEqual({(r["receipt_id"], r["note"]) for r in got["rows"]},
                         {(second, None),
                          (first, receipt_identity.LINE_SAME_SLIP)})
        self.assertIsNone(got["groups"][0]["note"])

    def test_lines_off_the_ledger_say_why_they_do_not_count(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        rid = self.h.photo("t-1")
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = 't-1'")
        rows = receipts.search_items(self.conn, q="ramen")["rows"]
        self.assertEqual([(r["receipt_id"], r["counts"], r["why"], r["note"])
                          for r in rows],
                         [(rid, False, "off_ledger",
                           receipt_identity.LINE_OFF_LEDGER)])

    def test_the_same_file_on_two_charges_counts_once(self):
        # one slip paid with two cards: the same file on both charges
        add_txn(self.conn, DAY, 20.00, "HARBOR NOODLE HOUSE", txn_id="part-a")
        add_txn(self.conn, DAY, 20.00, "HARBOR NOODLE HOUSE",
                account="chk", txn_id="part-b")
        a = self.h.photo("part-a", image=b"\x89PNG one slip")
        self.h.photo("part-b", image=b"\x89PNG one slip")
        self.assertEqual(self.h.counted(), ({a}, 37.0))
        [pa] = receipts.for_txn(self.conn, "part-a")
        [pb] = receipts.for_txn(self.conn, "part-b")
        self.assertEqual((len(pa["items"]), pa["duplicate_of"]), (1, None))
        self.assertEqual((pb["items"], pb["duplicate_of"],
                          len(pb["listed_items"])), ([], a, 1))
        self.assertEqual([t["receipt_id"] for t in pb["slip_twins"]], [a])
        self.assertTrue(pb["slip_twins"][0]["counted"])

    def test_deleting_the_counted_copy_promotes_the_next(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1", image=b"\x89PNG same")
        b = self.h.photo(None, image=b"\x89PNG same")
        self.assertEqual(self.h.counted(), ({a}, 37.0))
        receipts.delete(self.conn, a)
        self.assertEqual(self.h.counted(), ({b}, 37.0))

    def test_a_corrected_reading_ends_the_hold_back(self):
        first = self._stranded_and_posted()
        second = self.h.photo("posted-1")
        self.assertEqual(self.h.nightly(), 0)
        self.assertEqual(self.h.where(first)[0], "hold-1")
        # the second photo was misread: a person types the real total
        receipts.set_details(self.conn, second, total=52.0, date=DAY,
                             merchant="Harbor Noodle House")
        self.assertFalse(self.asked(first, second))
        self.assertEqual(self.h.nightly(), 1)
        self.assertEqual(self.h.counted()[0], {first, second})

    def test_a_persons_two_purchases_is_never_merged_again(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-2")
        a = self.h.photo("t-1", image=b"\x89PNG lookalike")
        b = self.h.photo("t-2", image=b"\x89PNG lookalike")
        self.assertEqual(self.h.key(a), self.h.key(b))
        receipt_identity.two_purchases(self.conn, b, a, request=False)
        self.assertEqual(self.h.counted()[0], {a, b})
        for rid in (a, b):
            receipt_identity.decide(self.conn, rid)
        # a third copy of the same file joins one of them, never both
        c = self.h.photo(None, image=b"\x89PNG lookalike")
        self.assertNotEqual(self.h.key(a), self.h.key(b))
        self.assertIn(self.h.key(c), {self.h.key(a), self.h.key(b)})
        self.assertEqual(self.h.counted()[0], {a, b})
        # and the panel never asks about them again
        self.assertEqual(receipts.for_txn(self.conn, "t-1")[0]["suspected"],
                         [])

    def test_a_persons_same_slip_is_never_split_by_a_rule(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1")
        # read alike when the person is asked; read apart after
        b = self.h.photo(None)
        self.assertEqual(self.h.counted()[0], {a, b})
        receipt_identity.same_slip(self.conn, b, a, request=False)
        self.assertEqual(self.h.counted()[0], {a})
        receipts.set_details(self.conn, b, total=60.0, date=DAY)
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        receipt_identity.decide_backlog(self.conn)
        self.h.nightly()
        self.assertEqual(self.h.key(a), self.h.key(b))
        self.assertEqual(self.h.counted()[0], {a})

    def test_two_coffees_waiting_are_two_and_the_person_is_asked(self):
        a = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.50,
                                            desc="latte"))
        b = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.50,
                                            desc="latte"))
        self.assertEqual(self.h.counted()[0], {a, b})
        listed = {w["id"]: w for w in
                  receipt_match.waiting(self.conn)["waiting"]}
        self.assertEqual([t["receipt_id"] for t in listed[a]["suspected"]],
                         [b])
        receipt_identity.same_slip(self.conn, a, b, request=False)
        self.assertEqual(len(self.h.counted()[0]), 1)
        listed = {w["id"]: w for w in
                  receipt_match.waiting(self.conn)["waiting"]}
        self.assertEqual(listed[a]["suspected"], [])
        self.assertEqual(sorted([listed[a]["counts"], listed[b]["counts"]]),
                         [False, True])

    def test_deciding_a_slip_never_moves_a_receipt(self):
        first = self._stranded_and_posted()
        self.h.photo("posted-1")
        self.h.photo(None, image=b"\x89PNG photo 1")
        before = self.conn.execute(
            "SELECT id, txn_id, match_method FROM receipts ORDER BY id"
        ).fetchall()
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        receipt_identity.decide_backlog(self.conn)
        for r in before:
            receipt_identity.decide(self.conn, str(r["id"]))
        self.assertEqual(self.conn.execute(
            "SELECT id, txn_id, match_method FROM receipts ORDER BY id"
        ).fetchall(), before)
        self.assertIsNotNone(first)

    def test_moving_retiring_or_reading_a_receipt_changes_no_slip(self):
        # neither rule reads where a receipt is or what it reads as: a
        # receipt is decided once, and nothing un-decides it
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 49.00,
                "UNRELATED NAME", txn_id="posted-1")
        a = self.h.photo("hold-1", image=b"\x89PNG kept")
        b = self.h.photo(None, image=b"\x89PNG kept")
        c = self.h.photo("posted-1")
        before = self.conn.execute(
            "SELECT id, slip_id, slip_source, slip_decided_at FROM receipts"
            " ORDER BY id").fetchall()
        self.h.retire("hold-1")
        self.conn.execute("UPDATE receipts SET txn_id = 'posted-1'"
                          " WHERE id = %s::uuid", (b,))
        receipts.set_details(self.conn, c, total=52.0, date=DAY)
        self.h.read(a, _slip(total=12.0))
        self.assertFalse(receipt_identity.has_pending(self.conn))
        self.h.nightly()
        self.assertEqual(self.conn.execute(
            "SELECT id, slip_id, slip_source, slip_decided_at FROM receipts"
            " ORDER BY id").fetchall(), before)

    def test_a_retired_copy_is_held_only_while_its_slip_counts_elsewhere(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 49.00,
                "UNRELATED NAME", txn_id="posted-1")
        first = self.h.photo("hold-1", image=b"\x89PNG the slip")
        self.h.retire("hold-1")
        second = self.h.photo("posted-1", image=b"\x89PNG the slip")
        self.assertEqual(self.h.nightly(), 0)
        self.assertEqual(self.h.where(first)[0], "hold-1")
        receipts.delete(self.conn, second)
        self.assertEqual(self.h.nightly(), 1)
        self.assertEqual(self.h.counted()[0], {first})

    def test_a_restore_without_slip_columns_decides_them_again(self):
        add_txn(self.conn, DAY, 20.00, "HARBOR NOODLE HOUSE", txn_id="part-a")
        add_txn(self.conn, DAY, 20.00, "HARBOR NOODLE HOUSE",
                account="chk", txn_id="part-b")
        a = self.h.photo("part-a", image=b"\x89PNG one slip")
        b = self.h.photo("part-b", image=b"\x89PNG one slip")
        c = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.5))
        d = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.5))
        receipt_identity.two_purchases(self.conn, c, d, request=False)
        buf = io.BytesIO()
        export.build_zip(self.conn, out=buf)
        # the same archive as an older build wrote it: no slip columns
        old = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z, \
                zipfile.ZipFile(old, "w") as w:
            for info in z.infolist():
                body = z.read(info.filename)
                if info.filename == "receipts.csv":
                    import csv
                    rows = list(csv.DictReader(io.StringIO(body.decode())))
                    self.assertNotIn("image_sha256", rows[0])
                    self.assertIn("slip_decided_at", rows[0])
                    keep = [k for k in rows[0] if not k.startswith(
                        ("slip_", "not_same"))]
                    out = io.StringIO()
                    cw = csv.DictWriter(out, fieldnames=keep,
                                        extrasaction="ignore")
                    cw.writeheader()
                    cw.writerows(rows)
                    body = out.getvalue().encode()
                w.writestr(info.filename, body)
        for data, two in ((old.getvalue(), False), (buf.getvalue(), True)):
            dest = make_db()
            self.addCleanup(dest.close)
            restore.restore_zip(dest, data)
            self.assertEqual(dest.execute(
                "SELECT count(*) AS n FROM receipts"
                " WHERE slip_decided_at IS NULL").fetchone()["n"], 0)
            k = {r["id"]: r["k"] for r in dest.execute(
                "SELECT id::text AS id, COALESCE(slip_id, id)::text AS k"
                " FROM receipts").fetchall()}
            self.assertEqual(k[a], k[b])
            self.assertNotEqual(k[c], k[d])
            # the person's answer travels with an archive that carries it;
            # without it, the two are asked about again, never joined
            self.assertEqual(bool(receipt_identity.suspected(dest, [c])),
                             not two)

    def test_hold_with_tip_then_a_second_photo_counts_once(self):
        # the posted charge (with a tip) arrives while the bank still lists
        # the hold; the person photographs the slip again for it; the hold
        # is withdrawn later and the re-anchor names no twin
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 52.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        first = self.h.photo("hold-1")
        second = self.h.photo("posted-1")
        self.assertTrue(self.asked(first, second))
        self.h.retire("hold-1")
        for _ in range(3):
            self.h.nightly()
            self.assertEqual(self.h.where(first)[0], "hold-1")
            self.assertEqual(self.h.counted(), ({second}, 37.0))
        # never silent: the live copy's panel asks about the held one
        [panel] = receipts.for_txn(self.conn, "posted-1")
        [t] = panel["suspected"]
        self.assertEqual((t["receipt_id"], t["off_ledger"], t["counted"]),
                         (first, True, False))
        # a row retired before its retirement time was stored carries the
        # migration's time: no time is read, so the same
        self.conn.execute("UPDATE transactions SET removed_at = now()"
                          " - make_interval(days => %s) WHERE id = 'hold-1'",
                          (LONG_AGO + 30,))
        self.h.nightly()
        self.assertEqual(self.h.counted(), ({second}, 37.0))

    def test_a_held_copy_answers_through_both_doors(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 52.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        first = self.h.photo("hold-1")
        second = self.h.photo("posted-1")
        self.h.retire("hold-1")
        self.assertEqual(self.h.nightly(), 0)
        # "Two purchases": back to waiting at once, and both count
        self.assertEqual(receipt_identity.two_purchases(
            self.conn, second, first, request=False), [first])
        self.assertEqual(self.h.where(first)[0], None)
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertEqual(self.h.nightly(), 0)
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertFalse(self.asked(first, second))
        # "Same receipt" (on a fresh pair): one slip, held by the slip rule
        add_txn(self.conn, DAY, 30.00, "QUILL STATIONERS", pending=1,
                txn_id="hold-2")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 39.00,
                "QUILL STATIONERS", txn_id="posted-2")
        pen = _slip(store="Quill Stationers", total=30.0, desc="pen")
        third = self.h.photo("hold-2", payload=pen)
        fourth = self.h.photo("posted-2", payload=pen)
        self.h.retire("hold-2")
        receipt_identity.same_slip(self.conn, third, fourth, request=False)
        self.assertEqual(self.h.nightly(), 0)
        self.assertEqual(self.h.where(third)[0], "hold-2")
        self.assertEqual(self.h.counted("pen"), ({fourth}, 27.0))
        self.assertFalse(self.asked(third, fourth))

    def test_the_release_decides_the_backlog_before_it_acts(self):
        # the first nightly after the slip columns: every receipt is
        # undecided, more than one pass takes
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 49.00,
                "UNRELATED NAME", txn_id="posted-1")
        for i in range(3):
            self.h.photo(None, image=b"\x89PNG older %d" % i,
                         payload=_slip(store=f"Other {i}", desc="tea"))
        first = self.h.photo("hold-1", image=b"\x89PNG the slip")
        self.h.retire("hold-1")
        self.h.photo("posted-1", image=b"\x89PNG the slip")
        self.conn.execute("UPDATE receipts SET slip_id = NULL,"
                          " slip_source = NULL, slip_decided_at = NULL")
        real = receipt_identity.decide_pending
        with mock.patch.object(receipt_identity, "decide_pending",
                               lambda conn, limit=1: real(conn, limit=1)):
            self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(self.h.where(first)[0], "hold-1")
        self.assertEqual(self.h.counted("ramen")[1], 37.0)

    def test_a_receipt_left_undecided_holds_its_row(self):
        # a backlog bigger than the release's bound: the row of an
        # undecided receipt waits a night rather than being released
        # beside a copy of its own image
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 49.00,
                "UNRELATED NAME", txn_id="posted-1")
        first = self.h.photo("hold-1", image=b"\x89PNG the slip")
        self.h.retire("hold-1")
        self.h.photo("posted-1", image=b"\x89PNG the slip")
        self.conn.execute("UPDATE receipts SET slip_id = NULL,"
                          " slip_source = NULL, slip_decided_at = NULL")
        with mock.patch.object(receipt_identity, "decide_backlog",
                               lambda conn, passes=0: 0):
            self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(self.h.where(first)[0], "hold-1")

    def test_two_purchases_from_a_slip_of_three_is_asked_once(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1", image=b"\x89PNG same")
        b = self.h.photo(None, image=b"\x89PNG same")
        c = self.h.photo(None, image=b"\x89PNG same")
        self.assertEqual(len({self.h.key(r) for r in (a, b, c)}), 1)
        receipt_identity.two_purchases(self.conn, c, a, request=False)
        self.assertEqual(self.h.counted()[0], {a, c})
        listed = {w["id"]: w for w in
                  receipt_match.waiting(self.conn)["waiting"]}
        self.assertEqual(listed[c]["suspected"], [])
        self.assertEqual(listed[b]["suspected"], [])
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        receipt_identity.decide_backlog(self.conn)
        self.h.nightly()
        self.assertNotEqual(self.h.key(c), self.h.key(b))
        self.assertEqual(self.h.key(a), self.h.key(b))
        # and the Receipts page flags neither as counted twice
        self.assertFalse(any(r["suspected"] for r in self.lines()["rows"]))

    def test_a_look_alike_slip_of_two_copies_is_asked_about_once(self):
        a = self.h.photo(None, image=b"\x89PNG one",
                         payload=_slip(store="Corner Cafe", total=4.5))
        self.h.photo(None, image=b"\x89PNG one",
                     payload=_slip(store="Corner Cafe", total=4.5))
        c = self.h.photo(None, image=b"\x89PNG other",
                         payload=_slip(store="Corner Cafe", total=4.5))
        listed = {w["id"]: w for w in
                  receipt_match.waiting(self.conn)["waiting"]}
        self.assertEqual([t["receipt_id"] for t in listed[c]["suspected"]],
                         [a])

    def test_a_large_household_stays_fast(self):
        # 1,500 read receipts in groups of five read alike, a third
        # waiting, a third on retired rows past the wait, a person's
        # "two purchases" on some, every one undecided: the backlog, the
        # nightly release, the waiting list, a panel and the Receipts page
        # each read the look-alikes without comparing every receipt with
        # every other
        import time
        from oikonome.engine.compat import jsonb
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        for j in range(50):
            add_txn(self.conn, DAY, 4.5, f"STORE {j}", pending=1,
                    txn_id=f"gone-{j}")
            self.h.retire(f"gone-{j}")
        ids = []
        for i in range(1500):
            on = (None if i % 3 == 1 else "t-1" if i % 3 == 0
                  else f"gone-{i % 50}")
            ids.append(self.conn.execute(
                "INSERT INTO receipts (txn_id, image, mime, kind, status,"
                " parsed, created_at) VALUES (%s, %s, 'image/png',"
                " 'receipt', 'parsed', %s, now() - interval '40 days')"
                " RETURNING id::text AS id",
                (on, b"\x89PNG bulk %d" % (i % 1400),
                 jsonb(_slip(store=f"Store {i // 5}", total=4.5)))
            ).fetchone()["id"])
            self.conn.execute(
                "INSERT INTO receipt_items (receipt_id, line, description,"
                " qty, amount) VALUES (%s::uuid, 0, 'bulk line', 1, 1.5)",
                (ids[-1],))
        t0 = time.monotonic()
        receipt_identity.decide_backlog(self.conn)
        self.assertFalse(receipt_identity.has_pending(self.conn))
        receipt_match.release_unplaced(self.conn)
        self.assertLess(time.monotonic() - t0, 20.0)
        for a, b in zip(ids[0:300:5], ids[1:300:5]):
            receipt_identity.two_purchases(self.conn, a, b, request=False)
        best = []
        for _ in range(2):
            t0 = time.monotonic()
            receipt_match.waiting(self.conn)
            receipt_identity.twins(self.conn, ids[:200])
            best.append(time.monotonic() - t0)
        self.assertLess(min(best), 1.0)
        best = []
        for _ in range(2):
            t0 = time.monotonic()
            receipts.search_items(self.conn, q="", group="merchant")
            best.append(time.monotonic() - t0)
        self.assertLess(min(best), 3.0)

    def test_two_purchases_tapped_on_the_label_holder_survives_every_pass(self):
        # the counted copy's own panel offers "Two purchases" against the
        # other copy; the label of the slip is the counted copy's id
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-2")
        a = self.h.photo("t-1", image=b"\x89PNG same")
        b = self.h.photo("t-2", image=b"\x89PNG same")
        self.assertEqual(self.h.key(a), a)
        [pa] = receipts.for_txn(self.conn, "t-1")
        other = pa["slip_twins"][0]["receipt_id"]
        receipt_identity.two_purchases(self.conn, a, other, request=False)
        for rid in (a, b):
            receipt_identity.decide(self.conn, rid)
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        receipt_identity.decide_backlog(self.conn)
        self.h.nightly()
        receipt_match.match_waiting(self.conn)
        self.assertNotEqual(self.h.key(a), self.h.key(b))
        self.assertEqual(self.h.counted()[0], {a, b})
        self.assertEqual(receipts.for_txn(self.conn, "t-2")[0]["suspected"],
                         [])
        self.assertFalse(any(r["suspected"] for r in self.lines()["rows"]))

    def test_two_purchases_tapped_on_a_held_copy_survives_every_pass(self):
        # the same answer about a look-alike the release holds back: the
        # hold ends for good, whichever of the two the person tapped on
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 52.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        first = self.h.photo("hold-1")
        second = self.h.photo("posted-1")
        self.h.retire("hold-1")
        receipt_identity.two_purchases(self.conn, first, second,
                                       request=False)
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        for _ in range(3):
            self.h.nightly()
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertNotEqual(self.h.key(first), self.h.key(second))

    def _two_coffees(self):
        coffee = _slip(store="Corner Cafe", total=4.50, desc="latte")
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", pending=1, txn_id="h1")
        a = self.h.photo("h1", payload=coffee)
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", txn_id="p2")
        b = self.h.photo("p2", payload=coffee)
        self.h.retire("h1")
        return a, b

    def _no_join_stored(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM receipts WHERE slip_id IS NOT NULL"
        ).fetchone()["n"] == 0

    def test_two_coffees_one_card_the_first_posting_before_the_hold_drops(self):
        # two coffees on one card; the first one's hold is dropped after
        # its own posted charge is here: the re-anchor sees two charges it
        # could be and leaves the choice to the person. Nothing is joined;
        # the held copy counts nothing and is asked about on the other
        a, b = self._two_coffees()
        add_txn(self.conn, DAY + dt.timedelta(days=1), 4.50, "CORNER CAFE",
                txn_id="p1")
        for _ in range(2):
            self.h.nightly()
        self.assertTrue(self._no_join_stored())
        self.assertEqual(self.h.counted("latte")[0], {b})
        self.assertTrue(self.asked(a, b))

    def test_two_coffees_one_card_the_first_posting_after_the_hold_drops(self):
        # the same, the first coffee's posted charge arriving only after a
        # night has passed: whatever the re-anchor did with the stranded
        # copy, nothing stored joins the two, both count or are asked, and
        # the person's "Two purchases" ends the question for good
        a, b = self._two_coffees()
        self.h.nightly()
        self.assertTrue(self._no_join_stored())
        got = self.h.counted("latte")[0]
        self.assertIn(b, got)
        self.assertTrue(self.asked(a, b))
        add_txn(self.conn, DAY + dt.timedelta(days=1), 4.50, "CORNER CAFE",
                txn_id="p1")
        self.h.nightly()
        self.assertTrue(self._no_join_stored())
        self.assertTrue(self.asked(a, b))
        receipt_identity.two_purchases(self.conn, b, a, request=False)
        for _ in range(2):
            self.h.nightly()
        self.assertEqual(self.h.counted("latte")[0], {a, b})
        self.assertFalse(self.asked(a, b))

    def test_two_purchases_releases_a_copy_the_re_anchor_cannot_place(self):
        # two coffees on one card, both posted; the first one's hold is
        # dropped and the re-anchor sees two charges it could be, so it
        # leaves the copy on the retired row. Before the answer the live
        # copy asks about it; "Two purchases" sends it to waiting at once,
        # where it counts and can be placed: never hidden with the
        # question gone
        coffee = _slip(store="Corner Cafe", total=4.50, desc="latte")
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", pending=1, txn_id="h1")
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", txn_id="p1")
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", txn_id="p2")
        a = self.h.photo("h1", payload=coffee)
        b = self.h.photo("p2", payload=coffee)
        self.h.retire("h1")
        for _ in range(2):
            self.h.nightly()
        self.assertEqual(self.h.where(a)[0], "h1")
        self.assertEqual(self.h.counted("latte")[0], {b})
        self.assertTrue(self.asked(a, b))
        self.assertEqual(receipt_identity.two_purchases(
            self.conn, b, a, request=False), [a])
        for _ in range(3):
            self.h.nightly()
        self.assertEqual(self.h.counted("latte"), ({a, b}, 3.0))
        self.assertNotEqual(self.h.where(a)[0], "h1")
        self.assertFalse(self.asked(a, b))

    def test_two_purchases_leaves_a_hand_retired_row_and_a_counted_slip(self):
        # a row the person retired keeps what is on it, and a copy whose
        # same file counts on the ledger stays where it is
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 52.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 45.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-2")
        first = self.h.photo("hold-1", image=b"\x89PNG file one")
        self.h.photo("posted-2", image=b"\x89PNG file one")
        second = self.h.photo("posted-1")
        self.h.retire("hold-1")
        self.assertEqual(receipt_identity.two_purchases(
            self.conn, second, first, request=False), [])
        self.assertEqual(self.h.where(first)[0], "hold-1")
        self.conn.execute("UPDATE transactions SET retired_at = now()"
                          " WHERE id = 'hold-1'")
        receipt_identity.same_slip(self.conn, first, second, request=False)
        self.assertEqual(receipt_identity.two_purchases(
            self.conn, first, second, request=False), [])
        self.assertEqual(self.h.where(first)[0], "hold-1")

    def test_a_restored_copy_beside_a_local_copy_of_the_file_counts_once(self):
        # an archive (with the slip columns) restored into a household
        # that already holds the same file: one slip, counted once; a
        # person's "Two purchases" in the archive is kept
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        self.h.photo("t-1", image=b"\x89PNG shared slip")
        c = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.5,
                                             desc="latte"))
        d = self.h.photo(None, payload=_slip(store="Corner Cafe", total=4.5,
                                             desc="latte"))
        receipt_identity.two_purchases(self.conn, c, d, request=False)
        buf = io.BytesIO()
        export.build_zip(self.conn, out=buf)
        dest = Household()
        self.addCleanup(dest.conn.close)
        add_txn(dest.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        local = dest.photo(None, image=b"\x89PNG shared slip")
        restore.restore_zip(dest.conn, buf.getvalue())
        self.assertFalse(receipt_identity.has_pending(dest.conn))
        mine = [r["id"] for r in dest.conn.execute(
            "SELECT id::text AS id FROM receipts WHERE image = %s",
            (b"\x89PNG shared slip",)).fetchall()]
        self.assertEqual(len(mine), 2)
        self.assertIn(local, mine)
        self.assertEqual(len({dest.key(r) for r in mine}), 1)
        self.assertEqual(dest.counted("ramen")[1], 37.0)
        self.assertNotEqual(dest.key(c), dest.key(d))
        self.assertFalse(receipt_identity.suspected(dest.conn, [c]))

    def test_a_look_alike_holds_nothing_back_while_the_matcher_has_a_charge(self):
        # a second meal at the same price on the same card posts free: the
        # stranded copy may be that one, so it is released, waits, and
        # counts beside its look-alike, asked about
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 52.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        first = self.h.photo("hold-1")
        second = self.h.photo("posted-1")
        self.h.retire("hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 45.00,
                "HARBOR NOODLE HOUSE", txn_id="another-meal")
        self.assertEqual(receipt_match.release_unplaced(self.conn), 1)
        self.assertEqual(self.h.counted(), ({first, second}, 74.0))
        self.assertTrue(self.asked(first, second))

    def test_a_dangling_slip_label_counts_as_its_own_slip(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1")
        self.conn.execute("UPDATE receipts SET slip_id = %s::uuid"
                          " WHERE id = %s::uuid", (str(uuid.uuid4()), a))
        self.assertEqual(self.h.counted(), ({a}, 37.0))

    def test_a_receipt_a_writer_that_does_not_decide_stored_is_decided(self):
        # an older build, or a writer that copies a row whole, inserts a
        # copy without deciding it: the next matcher pass does
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1", image=b"\x89PNG kept")
        b = self.conn.execute(
            "INSERT INTO receipts (txn_id, image, mime) VALUES"
            " (NULL, %s, 'image/png') RETURNING id::text AS id",
            (b"\x89PNG kept",)).fetchone()["id"]
        self.assertTrue(receipt_identity.has_pending(self.conn))
        receipt_match.match_waiting(self.conn)
        self.assertEqual(self.h.key(a), self.h.key(b))

    def test_two_photos_read_alike_on_one_charge_are_asked_not_joined(self):
        # a re-anchor or the matcher can put a stranded copy on a
        # look-alike purchase's charge: the two count, and the person says
        add_txn(self.conn, DAY, 4.50, "CORNER CAFE", txn_id="t-1")
        a = self.h.photo("t-1", payload=_slip(store="Corner Cafe",
                                              total=4.5))
        b = self.h.photo("t-1", payload=_slip(store="Corner Cafe",
                                              total=4.5))
        self.assertNotEqual(self.h.key(a), self.h.key(b))
        self.assertEqual(self.h.counted()[0], {a, b})
        self.assertEqual([t["receipt_id"] for t in
                          receipts.for_txn(self.conn, "t-1")[0]["suspected"]],
                         [b])

    def test_the_tag_report_says_when_its_total_may_hold_a_purchase_twice(self):
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", txn_id="t-1")
        a = self.h.photo("t-1")
        b = self.h.photo(None)
        report = lambda: receipts.report(  # noqa: E731
            self.conn, "work", DAY.year, DAY.month)
        self.assertEqual(receipts.set_tag(self.conn, a, 0, "work"), 1)
        # the look-alike counts on the Receipts page but carries no tag:
        # the report holds the purchase once
        got = report()
        self.assertEqual((got["total"], got["note"]), (37.0, None))
        self.assertFalse(got["rows"][0]["suspected"])
        self.assertEqual(receipts.set_tag(self.conn, b, 0, "work"), 1)
        got = report()
        self.assertEqual(got["total"], 74.0)
        self.assertTrue(all(r["suspected"] for r in got["rows"]))
        self.assertIn("may hold a purchase twice", got["note"])
        receipt_identity.same_slip(self.conn, a, b, request=False)
        got = report()
        self.assertEqual((got["total"], got["note"]), (37.0, None))

    def test_adopting_an_account_keeps_a_released_and_a_held_receipt(self):
        # one receipt released from a retired row of the card, another
        # held on one by its look-alike; the card is re-linked under a new
        # id: nothing stored names the old account, the held one stays
        # held and the released one stays counted and asked about
        first = self._stranded_and_posted()
        self.assertEqual(self.h.nightly(), 1)
        add_txn(self.conn, DAY, 30.00, "QUILL STATIONERS", pending=1,
                txn_id="hold-2")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 39.00,
                "QUILL STATIONERS", txn_id="posted-2")
        pen = _slip(store="Quill Stationers", total=30.0, desc="pen")
        held = self.h.photo("hold-2", payload=pen)
        live = self.h.photo("posted-2", payload=pen)
        self.h.retire("hold-2")
        self.assertEqual(self.h.nightly(), 0)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('it2','test','Test Bank','tok-2')")
        with self.conn.transaction():
            self.assertTrue(adopt._rewrite_account_id(
                self.conn, "card", "card-new", "it2"))
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM transactions WHERE id = 'hold-2'"
        ).fetchone()["account_id"], "card-new")
        self.assertEqual(self.h.nightly(), 0)
        self.assertEqual(self.h.where(held)[0], "hold-2")
        self.assertEqual(self.h.counted("pen"), ({live}, 27.0))
        second = self.h.photo("posted-1")
        self.assertEqual(self.h.counted("ramen")[0], {first, second})
        self.assertTrue(self.asked(first, second))

# ---- the property: one slip counts once, whatever the order -------------

class Scenario:
    """A generated household: purchases, each with its photos, on one of
    two cards, some read alike. The generator knows which photos are of
    which purchase. Until the person answers, a purchase may count twice
    or be held back only where it is put to them; once they have answered
    every question as the purchases really were, each purchase with a
    read photo counts exactly once."""

    FLOWS = ("posted", "settled", "stranded", "waiting")

    def __init__(self, seed: int):
        rnd = random.Random(seed)
        self.purchases = []
        for k in range(rnd.randint(1, 4)):
            flow = rnd.choice(self.FLOWS)
            card = rnd.choice(["card", "chk"])
            # each purchase its own store, named so no two read alike
            store = ("Harbor Noodle", "Juniper Market", "Quill Stationers",
                     "Fernhill Grocer")[k]
            total = 10.0 + 7 * k
            # a look-alike of an earlier purchase (same store, day and
            # total) put straight on its charge, on either card: two
            # purchases read alike, which only the person can tell apart
            twin = [q for q in self.purchases if q["flow"] in
                    ("posted", "settled")]
            if flow in ("posted", "settled") and twin and rnd.random() < 0.5:
                q = rnd.choice(twin)
                store, total = q["payload"]["merchant"], q["payload"]["total"]
            self.purchases.append({
                "k": k, "card": card, "flow": flow,
                "copies": rnd.randint(1, 3),
                "same_bytes": rnd.random() < 0.5,
                "payload": _slip(store=store, total=total, desc=f"item {k}"),
            })

    def steps(self, rnd):
        """The events in an order: each purchase's own sequence is fixed
        where the design makes it matter (a second photo is taken after
        the hold left the ledger); reads land anywhere after their upload,
        and the nightly runs at random points."""
        per = []
        for p in self.purchases:
            k = p["k"]
            seq = [("setup", k), ("photo", k, 0)]
            if p["flow"] == "settled":
                seq.append(("settle", k))
            if p["flow"] == "stranded":
                seq.append(("retire", k))
            if p["flow"] == "waiting":
                seq.append(("pair", k))
            for i in range(1, p["copies"]):
                seq.append(("photo", k, i))
            per.append(seq)
        order = []
        reads = []
        while any(per):
            seq = rnd.choice([s for s in per if s])
            ev = seq.pop(0)
            order.append(ev)
            if ev[0] == "photo":
                reads.append(("read", ev[1], ev[2]))
            if reads and rnd.random() < 0.5:
                order.append(reads.pop(rnd.randrange(len(reads))))
            if rnd.random() < 0.3:
                order.append(("nightly",))
        rnd.shuffle(reads)
        order += reads + [("nightly",)]
        return order


def _run(sc: Scenario, order, check):
    h = Household()
    photos: dict[tuple, str] = {}
    try:
        for ev in order:
            kind, k = ev[0], (ev[1] if len(ev) > 1 else None)
            p = sc.purchases[k] if k is not None else None
            if kind == "setup":
                name = p["payload"]["merchant"].upper()
                if p["flow"] in ("settled", "stranded"):
                    add_txn(h.conn, DAY, p["payload"]["total"], name,
                            account=p["card"], pending=1, txn_id=f"hold-{k}")
                # a stranded hold posts with a tip and no link
                add_txn(h.conn, DAY + dt.timedelta(days=2),
                        p["payload"]["total"] + (9.0 if p["flow"] == "stranded"
                                                 else 0.0),
                        name, account=p["card"], txn_id=f"posted-{k}")
            elif kind == "photo":
                i = ev[2]
                on = {"posted": f"posted-{k}", "settled": f"hold-{k}",
                      "stranded": f"hold-{k}", "waiting": None}[p["flow"]]
                if i > 0:
                    # a later copy goes on the posted charge (a settled
                    # hold's receipts are there already)
                    on = f"posted-{k}"
                base = b"\x89PNG purchase %d" % k
                image = base if p["same_bytes"] else base + b" copy %d" % i
                if i == 0:
                    image = base
                photos[(k, i)] = h.photo(on, image=image, read=False)
            elif kind == "read":
                h.read(photos[(k, ev[2])], p["payload"])
            elif kind == "settle":
                # the feed names the posted row the hold became: the
                # carry moves its receipts, then the hold is retired
                h.conn.execute("UPDATE receipts SET txn_id = %s"
                               " WHERE txn_id = %s",
                               (f"posted-{k}", f"hold-{k}"))
                h.conn.execute("UPDATE transactions SET removed = 1"
                               " WHERE id = %s", (f"hold-{k}",))
            elif kind == "retire":
                h.retire(f"hold-{k}")
            elif kind == "pair":
                receipt_match.match_by_hand(h.conn, photos[(k, 0)],
                                            f"posted-{k}")
            elif kind == "nightly":
                h.nightly()
            check(h, photos, after_nightly=kind == "nightly")
        receipt_identity.decide_backlog(h.conn)
        h.nightly()
        check(h, photos, after_nightly=True)
        # the person answers every question as the purchases really were
        owner = {rid: k for (k, _i), rid in photos.items()}
        for _ in range(3):
            asked = receipt_identity.twins(h.conn, list(owner))
            for rid, t in asked.items():
                for other in t["suspected"]:
                    o = other["receipt_id"]
                    if owner.get(o) == owner[rid]:
                        receipt_identity.same_slip(h.conn, rid, o,
                                                   request=False)
                    else:
                        receipt_identity.two_purchases(h.conn, rid, o,
                                                       request=False)
        h.nightly()
        check(h, photos, answered=True, after_nightly=True)
        groups = {}
        for (k, i), rid in photos.items():
            groups.setdefault(h.key(rid), set()).add(k)
        partition = sorted(sorted({(k, i) for (k, i), rid in photos.items()
                                   if h.key(rid) == key})
                           for key in groups)
        counted, _ = h.counted()
        per = sorted(sum(1 for (kk, _i), rid in photos.items()
                         if kk == k and rid in counted)
                     for k in range(len(sc.purchases)))
        return partition, per
    finally:
        h.conn.close()


class OrderOfEventsTests(unittest.TestCase):
    def _check(self, sc):
        def check(h, photos, answered=False, after_nightly=False):
            counted, _ = h.counted()
            by_purchase = {}
            for (k, _i), rid in photos.items():
                if rid in counted:
                    by_purchase.setdefault(k, []).append(rid)
            # I1: one slip counts once. Before the person has answered, a
            # purchase may count twice only where the two copies are put
            # to the person as look-alikes, never silently
            for k, rids in by_purchase.items():
                if answered or len(rids) <= 1:
                    self.assertLessEqual(len(rids), 1,
                                         f"purchase {k} counted {len(rids)}x")
                    continue
                asked = receipt_identity.suspected(h.conn, rids)
                for a in rids:
                    for b in rids:
                        if a < b:
                            self.assertTrue(
                                b in asked.get(a, ()) or a in asked.get(b, ()),
                                f"purchase {k} counted twice, not asked")
            # I2: a read photo on the ledger or waiting is never hidden
            on = {r["id"]: r for r in h.conn.execute(
                """SELECT r.id::text AS id, r.status,
                          r.txn_id IS NULL OR t.removed = 0 AS on_ledger
                     FROM receipts r
                     LEFT JOIN transactions t ON t.id = r.txn_id"""
            ).fetchall()}
            for k in range(len(sc.purchases)):
                mine = [rid for (kk, _i), rid in photos.items() if kk == k]
                if any(on[r]["on_ledger"] and on[r]["status"] == "parsed"
                       for r in mine):
                    self.assertGreaterEqual(len(by_purchase.get(k, [])), 1,
                                            f"purchase {k} hidden")
            # never silent: once the nightly has had its say, a purchase
            # with a read photo that counts nowhere (held back on a
            # retired row) is put to the person beside a counted
            # look-alike, until they answer; after the answer it counts
            if not after_nightly:
                return
            for k in range(len(sc.purchases)):
                mine = [rid for (kk, _i), rid in photos.items()
                        if kk == k and on[rid]["status"] == "parsed"]
                if not mine or by_purchase.get(k):
                    continue
                self.assertFalse(answered, f"purchase {k} hidden after"
                                 " the person answered")
                asked = receipt_identity.suspected(h.conn, mine)
                self.assertTrue(any(o in counted for r in mine
                                    for o in asked.get(r, ())),
                                f"purchase {k} hidden, not asked")
        return check

    def test_one_slip_counts_once_whatever_the_order_of_events(self):
        for seed in range(24):
            sc = Scenario(seed)
            with self.subTest(seed=seed, purchases=sc.purchases):
                first = _run(sc, sc.steps(random.Random(seed)),
                             self._check(sc))
                # I4: the same events in another order end the same
                again = _run(sc, sc.steps(random.Random(seed + 1000)),
                             self._check(sc))
                self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
