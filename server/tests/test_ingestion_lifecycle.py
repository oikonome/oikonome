"""Transaction ingestion lifecycle: what must survive pending→posted
settlement, what a vanished pending means on a delta-less connector, and
what a removal delta that matches nothing has to leave behind.

The invariants here all guard the same money truth: every real charge
appears exactly once in the ledger. A settlement must not strand the
pending row's children (receipt, classification, category pin) on a row
every read hides; a full-pull connector must retire a pending its feed no
longer carries or it double-counts spend forever; a removal naming an
unknown id must be visible, because silence there is a phantom row; and
the reconnect guard's duplicate matcher must never treat rows its own
backfill just inserted as "existing history" to absorb later pages into.
"""

import datetime as dt
import unittest
from unittest import mock

import httpx

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.sync import adopt, base as sync_base, mx, simplefin
from oikonome.sync.base import Transaction
from oikonome.sync.dedup import Deduper

from .util import make_db, write_config

TODAY = dt.date.today()


class SettlementCarryTests(unittest.TestCase):
    """Pending→posted: children follow the settlement, predecessor retires."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_children_move_and_pending_retires(self):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id="pl:p1", account_id="chk", date=TODAY, amount=40.0,
            name="COFFEE HOLD", pending=True)])
        self.conn.execute(
            "INSERT INTO receipts (txn_id, image, mime) "
            "VALUES ('pl:p1', %s, 'image/png')", (b"\x89PNG",))
        self.conn.execute(
            "INSERT INTO business_txn_class (txn_id, bucket) "
            "VALUES ('pl:p1', 'operating')")
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category) "
            "VALUES ('pl:p1', 'FOOD_AND_DRINK')")

        sync_base.upsert_transactions(self.conn, [Transaction(
            id="pl:s1", account_id="chk", date=TODAY, amount=40.0,
            name="COFFEE", pending=False,
            raw={"pending_transaction_id": "pl:p1"})])

        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM receipts").fetchone()["txn_id"], "pl:s1")
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM business_txn_class").fetchone()["txn_id"],
            "pl:s1")
        self.assertEqual(self.conn.execute(
            "SELECT transaction_id FROM manual_categories").fetchone()[
                "transaction_id"], "pl:s1")
        old = self.conn.execute(
            "SELECT removed FROM transactions WHERE id='pl:p1'").fetchone()
        self.assertEqual(old["removed"], 1)

    def test_pending_retires_even_when_a_child_carry_fails(self):
        """The child moves are best-effort; retiring the pending row is
        not. A receipt UPDATE that fails must not leave the pending twin
        live next to its posted row — the same $40 would then count twice
        until an aggregator removal delta that may never arrive."""
        sync_base.upsert_transactions(self.conn, [Transaction(
            id="pl:p9", account_id="chk", date=TODAY, amount=40.0,
            name="COFFEE HOLD", pending=True)])
        real = self.conn.execute

        def flaky(sql, *a, **kw):
            if "UPDATE receipts" in sql:
                raise RuntimeError("constraint violated")
            return real(sql, *a, **kw)

        with mock.patch.object(self.conn, "execute", side_effect=flaky):
            sync_base.upsert_transactions(self.conn, [Transaction(
                id="pl:s9", account_id="chk", date=TODAY, amount=40.0,
                name="COFFEE", pending=False,
                raw={"pending_transaction_id": "pl:p9"})])
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='pl:p9'"
        ).fetchone()["removed"], 1)
        live = self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE id IN ('pl:p9','pl:s9') AND removed=0").fetchone()["n"]
        self.assertEqual(live, 1, "exactly one live row for one charge")

    def _settle_many(self, n: int, tag: str):
        """n pendings, each with a note and a receipt, then one sync that
        settles all of them. Returns the settling pairs."""
        pairs = [(f"{tag}:s{i}", f"{tag}:p{i}") for i in range(n)]
        sync_base.upsert_transactions(self.conn, [
            Transaction(id=old, account_id="chk", date=TODAY,
                        amount=10.0 + i, name="HOLD", pending=True)
            for i, (_new, old) in enumerate(pairs)])
        for i, (_new, old) in enumerate(pairs):
            self.conn.execute(
                "UPDATE transactions SET category_override='FOOD_AND_DRINK'"
                " WHERE id=%s", (old,))
            self.conn.execute(
                "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s)",
                (old, f"note {i}"))
            self.conn.execute(
                "INSERT INTO receipts (txn_id, image, mime) "
                "VALUES (%s, %s, 'image/png')", (old, b"\x89PNG"))
        sync_base.upsert_transactions(self.conn, [
            Transaction(id=new, account_id="chk", date=TODAY,
                        amount=10.0 + i, name="POSTED", pending=False,
                        raw={"pending_transaction_id": old})
            for i, (new, old) in enumerate(pairs)])
        return pairs

    def test_every_row_of_a_multi_row_settlement_carries_its_own_state(self):
        """A morning sync settles a whole day of card activity at once.
        Each posted row must inherit ITS OWN pending twin's override, note
        and receipt — a set-based carry that pairs the rows up wrongly
        hands one household's coffee receipt to another's rent."""
        pairs = self._settle_many(12, "many")
        notes = {r["txn_id"]: r["note"] for r in self.conn.execute(
            "SELECT txn_id, note FROM transaction_notes")}
        receipts = {r["txn_id"] for r in self.conn.execute(
            "SELECT txn_id FROM receipts")}
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, category_override, pending_transaction_id, removed "
            "FROM transactions")}
        for i, (new, old) in enumerate(pairs):
            # the note is COPIED (the retired twin keeps its own); the
            # receipt MOVES — each to the right successor, not the neighbour's
            self.assertEqual(notes.get(new), f"note {i}")
            self.assertIn(new, receipts)
            self.assertNotIn(old, receipts)
            self.assertEqual(rows[new]["category_override"], "FOOD_AND_DRINK")
            self.assertEqual(rows[new]["pending_transaction_id"], old)
            self.assertEqual(rows[new]["removed"], 0)
            self.assertEqual(rows[old]["removed"], 1, "predecessor retired")

    def test_settlement_carry_costs_the_same_whatever_the_page_size(self):
        """The carry is set-based: nine statements per settling row inside
        the path that delivers the money is a sync that slows down on the
        busiest days. Counted on the statements that only the carry
        issues, so the row upserts' own scaling is not measured."""
        marks = ("pending_transaction_id", "transaction_notes", "receipts",
                 "reimbursements", "reimburse_flags", "business_txn_class",
                 "equity_movement", "manual_categories", "SET removed=1")

        def _count(n: int, tag: str) -> int:
            seen = []
            real = self.conn.execute

            def spy(sql, *a, **kw):
                if any(m in sql for m in marks):
                    seen.append(sql)
                return real(sql, *a, **kw)

            with mock.patch.object(self.conn, "execute", side_effect=spy):
                self._settle_many(n, tag)
            # the fixture's own note/receipt inserts are counted too; they
            # are 2 per row by construction, so subtract them
            return len(seen) - 2 * n

        self.assertEqual(_count(1, "cost1"), _count(9, "cost9"))

    def test_carry_never_collides_with_posted_rows_own_children(self):
        """A replayed page must not clobber a class the posted row already
        has — the guard is NOT EXISTS, so the second run is a no-op."""
        sync_base.upsert_transactions(self.conn, [
            Transaction(id="pl:p2", account_id="chk", date=TODAY, amount=9.0,
                        name="HOLD", pending=True),
            Transaction(id="pl:s2", account_id="chk", date=TODAY, amount=9.0,
                        name="POSTED", pending=False)])
        self.conn.execute(
            "INSERT INTO business_txn_class (txn_id, bucket) "
            "VALUES ('pl:p2', 'startup_195')")
        self.conn.execute(
            "INSERT INTO business_txn_class (txn_id, bucket) "
            "VALUES ('pl:s2', 'operating')")
        # a modified re-delivery adds the settlement link after both rows
        # were classified — the carry must not overwrite the posted row's
        sync_base.upsert_transactions(self.conn, [Transaction(
            id="pl:s2", account_id="chk", date=TODAY, amount=9.0,
            name="POSTED", pending=False,
            raw={"pending_transaction_id": "pl:p2"})])
        row = self.conn.execute(
            "SELECT bucket FROM business_txn_class WHERE txn_id='pl:s2'"
        ).fetchone()
        self.assertEqual(row["bucket"], "operating")


class RemovalDeltaTests(unittest.TestCase):
    """A removal naming an id we don't hold must be counted and logged."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_missed_removal_is_reported(self):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id="pl:r1", account_id="chk", date=TODAY, amount=5.0,
            name="X", pending=True)])
        with self.assertLogs("oikonome.sync.base", level="WARNING") as cm:
            landed = sync_base.mark_removed(
                self.conn, ["pl:r1", "pl:never-held"])
        self.assertEqual(landed, 1)
        self.assertIn("pl:never-held", "\n".join(cm.output))
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='pl:r1'"
        ).fetchone()["removed"], 1)

    def test_log_sync_records_delta_counts(self):
        sync_base.log_sync(self.conn, "it1", 4, modified=2, removed=3)
        row = self.conn.execute(
            "SELECT added, modified, removed FROM sync_log "
            "WHERE item_id='it1' ORDER BY ran_at DESC LIMIT 1").fetchone()
        self.assertEqual((row["added"], row["modified"], row["removed"]),
                         (4, 2, 3))


class VanishedPendingTests(unittest.TestCase):
    """Full-pull connectors: a pending the feed dropped is retired; posted
    history and other sources' rows are never touched."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_only_vanished_own_prefix_pendings_retire(self):
        for tid, pending in (("sfx:a", 1), ("sfx:b", 1),
                             ("sfx:c", 0), ("csv:d", 1)):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed) VALUES (%s,'chk',%s,10,'T',%s,0)",
                (tid, TODAY - dt.timedelta(days=2), pending))
        retired = sync_base.reconcile_vanished_pendings(
            self.conn, ["chk"], {"sfx:b"}, "sfx:",
            TODAY - dt.timedelta(days=30))
        self.assertEqual([r["id"] for r in retired], ["sfx:a"])
        flags = {r["id"]: r["removed"] for r in self.conn.execute(
            "SELECT id, removed FROM transactions WHERE id LIKE '%%:%%'"
        ).fetchall()}
        self.assertEqual(flags["sfx:a"], 1)
        self.assertEqual(flags["sfx:b"], 0)
        self.assertEqual(flags["sfx:c"], 0)   # posted history is not a hold
        self.assertEqual(flags["csv:d"], 0)   # another source's row


class DeduperCutoffTests(unittest.TestCase):
    """The reconnect guard's matcher must not see rows dated past the
    restore cutoff — those are the backfill's own inserts, not history."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_max_date_excludes_post_cutoff_candidates(self):
        cutoff = TODAY - dt.timedelta(days=10)
        after = cutoff + dt.timedelta(days=1)
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed) VALUES ('pl:new','chk',%s,55.0,'LUNCH',0,0)",
            (after,))
        unbounded = Deduper(self.conn, "chk", "__none__", [after])
        self.assertTrue(unbounded.claim(after, 55.0, "LUNCH"))
        bounded = Deduper(self.conn, "chk", "__none__", [after],
                          max_date=cutoff)
        self.assertFalse(bounded.claim(after, 55.0, "LUNCH"))


class AdoptionGuardCloseTests(unittest.TestCase):
    """The guard closes on a demonstrably settled seam — but never in its
    first day, which is exactly the re-delivery window it exists for."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _adoption(self, item_id: str, age_hours: int):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type) "
            "VALUES (%s,'it1','A','depository') "
            "ON CONFLICT (tenant_id, id) DO NOTHING", (f"acct-{item_id}",))
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id, "
            "item_id, cutoff_date, adopted_at) "
            "VALUES (%s,'old-x',%s,%s, now() - make_interval(hours => %s))",
            (f"acct-{item_id}", item_id, TODAY, age_hours))

    def test_settled_seam_closes_after_age_floor(self):
        self._adoption("it-old", 72)
        self._adoption("it-new", 24)
        self.assertEqual(
            adopt.mark_reconciled_if_settled(self.conn, "it-old"), 1)
        self.assertEqual(
            adopt.mark_reconciled_if_settled(self.conn, "it-new"), 0)
        rows = {r["item_id"]: r["reconciled_at"] for r in self.conn.execute(
            "SELECT item_id, reconciled_at FROM account_adoptions"
        ).fetchall()}
        self.assertIsNotNone(rows["it-old"])
        self.assertIsNone(rows["it-new"])


class MxResilienceTests(unittest.TestCase):
    """One unparseable MX row is skipped, never a whole-member abort."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["mx_client_id"] = "cid"
        cfg["mx_api_key"] = crypto.encrypt(self.conn, "key")
        cfg["mx_env"] = "sandbox"
        budget.save_config(self.conn, cfg)

    def tearDown(self):
        self.conn.close()

    def _transport(self, txns):
        payload = {
            "GET /users": {"users": [], "pagination": {"total_pages": 1}},
            "POST /users": {"user": {"guid": "USR-1"}},
            "GET /users/USR-1/members": {
                "members": [{"guid": "MBR-1", "name": "Demo CU"}],
                "pagination": {"total_pages": 1}},
            "GET /users/USR-1/accounts": {"accounts": [
                {"guid": "ACT-1", "member_guid": "MBR-1", "name": "Checking",
                 "type": "CHECKING", "balance": 1000.0,
                 "currency_code": "USD"}],
                "pagination": {"total_pages": 1}},
            "GET /users/USR-1/transactions": {
                "transactions": txns, "pagination": {"total_pages": 1}},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            body = payload.get(key)
            if body is None:
                return httpx.Response(404, json={"missing": key})
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)

    def test_malformed_date_skips_row_not_sync(self):
        good = {"guid": "TRN-OK", "account_guid": "ACT-1", "amount": 12.5,
                "type": "DEBIT", "transacted_at": "2026-07-10T12:00:00Z",
                "description": "COFFEE", "status": "POSTED"}
        bad = {"guid": "TRN-BAD", "account_guid": "ACT-1", "amount": 3.0,
               "type": "DEBIT", "description": "GLITCH", "status": "POSTED"}
        with self.assertLogs("oikonome.sync.mx", level="WARNING"):
            mx.sync(self.conn, transport=self._transport([bad, good]))
        held = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'mx:%'").fetchall()}
        self.assertIn("mx:TRN-OK", held)
        self.assertNotIn("mx:TRN-BAD", held)

    def test_vanished_pending_retired_on_refresh(self):
        pend = {"guid": "TRN-P", "account_guid": "ACT-1", "amount": 8.0,
                "type": "DEBIT",
                "transacted_at": f"{TODAY - dt.timedelta(days=3)}T12:00:00Z",
                "description": "HOLD", "status": "PENDING"}
        mx.sync(self.conn, transport=self._transport([pend]))
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='mx:TRN-P'"
        ).fetchone()["removed"], 0)
        res = mx.sync(self.conn, transport=self._transport([]))
        self.assertEqual(res["removed"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='mx:TRN-P'"
        ).fetchone()["removed"], 1)

    def test_unparseable_redelivery_does_not_retire_its_pending(self):
        """A pending that comes back with one glitched field is still
        PRESENT at the source — the skip must not read as vanished."""
        pend = {"guid": "TRN-P2", "account_guid": "ACT-1", "amount": 8.0,
                "type": "DEBIT",
                "transacted_at": f"{TODAY - dt.timedelta(days=3)}T12:00:00Z",
                "description": "HOLD", "status": "PENDING"}
        mx.sync(self.conn, transport=self._transport([pend]))
        broken = dict(pend)
        del broken["transacted_at"]          # transient feed glitch
        with self.assertLogs("oikonome.sync.mx", level="WARNING"):
            res = mx.sync(self.conn, transport=self._transport([broken]))
        self.assertEqual(res["removed"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='mx:TRN-P2'"
        ).fetchone()["removed"], 0)


SFIN_PAYLOAD = {
    "errors": [],
    "accounts": [
        {"id": "acc-chk", "name": "Everyday Checking", "currency": "USD",
         "balance": "1500.00",
         "org": {"name": "Demo Bank"},
         "transactions": [
             {"id": "t1", "posted": 1752537600, "amount": "-42.50",
              "description": "SAFEWAY", "payee": "Safeway"},
             {"id": "tp", "posted": int(dt.datetime.combine(
                  TODAY - dt.timedelta(days=2), dt.time(12),
                  dt.timezone.utc).timestamp()),
              "amount": "-9.99", "description": "HOLD", "payee": "Cafe",
              "pending": True},
         ]},
    ],
}


def _sfin_transport(payload):
    import json as _json

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200,
                                  text="https://u:p@bridge.test/simplefin")
        return httpx.Response(200, text=_json.dumps(payload))
    return httpx.MockTransport(handler)


class SimpleFinLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _sync(self, payload):
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_sfin_transport(payload))

    def test_undated_row_skipped_not_epoch_dated(self):
        import copy
        payload = copy.deepcopy(SFIN_PAYLOAD)
        payload["accounts"][0]["transactions"].append(
            {"id": "t-undated", "amount": "-1.00",
             "description": "NO DATE", "pending": True})
        with self.assertLogs("oikonome.sync.simplefin", level="WARNING"):
            self._sync(payload)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id LIKE '%%t-undated%%'"
        ).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE date = '1970-01-01'"
        ).fetchone())

    def test_pending_that_loses_its_date_is_not_retired(self):
        """A known pending re-delivered WITHOUT a date is still present at
        the source — skipping its parse must not read as vanished."""
        import copy
        self._sync(SFIN_PAYLOAD)
        payload = copy.deepcopy(SFIN_PAYLOAD)
        for t in payload["accounts"][0]["transactions"]:
            if t["id"] == "tp":
                del t["posted"]              # transient feed glitch
        with self.assertLogs("oikonome.sync.simplefin", level="WARNING"):
            res = self._sync(payload)
        self.assertEqual(res["removed"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='sfin:acc-chk:tp'"
        ).fetchone()["removed"], 0)

    def test_vanished_pending_retired_posted_history_kept(self):
        import copy
        self._sync(SFIN_PAYLOAD)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='sfin:acc-chk:tp'"
        ).fetchone()["removed"], 0)
        payload = copy.deepcopy(SFIN_PAYLOAD)
        payload["accounts"][0]["transactions"] = [
            t for t in payload["accounts"][0]["transactions"]
            if t["id"] != "tp"]
        res = self._sync(payload)
        self.assertEqual(res["removed"], 1)
        flags = {r["id"]: r["removed"] for r in self.conn.execute(
            "SELECT id, removed FROM transactions WHERE id LIKE 'sfin:%'"
        ).fetchall()}
        self.assertEqual(flags["sfin:acc-chk:tp"], 1)
        self.assertEqual(flags["sfin:acc-chk:t1"], 0)


if __name__ == "__main__":
    unittest.main()
