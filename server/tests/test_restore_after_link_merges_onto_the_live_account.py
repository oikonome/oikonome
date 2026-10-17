"""Restoring an export AFTER linking the same bank leaves one account.

The natural order on a fresh hosted household is: sign up, link the banks,
then restore the archive from the old install. The link has already
created each account under its new Plaid id, and the restore brings the
same real account back under its OLD Plaid id on a tokenless shell. Plaid
ids are scoped to the Item, so nothing collides: without reconciliation the
household holds every account twice and the ~24 months both copies cover
are counted twice in spend, budget and net worth.

The link-side adoption never sees this order — it only adopts an incoming
account it does not hold yet, and here it already holds it. So the restore
reconciles: a restored account whose bank is live here and whose mask and
type pick out exactly one live account is merged into it. The history from
before the link moves across, the days both copies cover keep the live row
(it carries the id the bank's later corrections name) with everything a
person put on the restored copy, and the ambiguous case merges nothing and
says so.
"""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import adopt, export, restore

from .util import make_db

D = dt.date


def _item(conn, iid, *, token, status="ok", inst="ins_77",
          update_status=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, institution_name,"
        " status, access_token, tx_update_status)"
        " VALUES (%s,'plaid',%s,'Example Credit Union',%s,%s,%s)",
        (iid, inst, status, token, update_status))


def _account(conn, aid, iid, mask, type_="depository", name="Checking"):
    conn.execute("INSERT INTO accounts (id, item_id, name, type, mask)"
                 " VALUES (%s,%s,%s,%s,%s)", (aid, iid, name, type_, mask))


def _txn(conn, tid, aid, date, amount, name, override=None):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name,"
        " pending, removed, category_override)"
        " VALUES (%s,%s,%s,%s,%s,0,0,%s)",
        (tid, aid, date, amount, name, override))


def _fresh_household(label):
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, label)
    finally:
        admin.close()
    return tenancy.tenant_connect(tid)


class _Base(unittest.TestCase):
    """The old install: one checking account, history from 2024 to
    2026-06-01, a note and a category on a row inside the overlap."""

    masks = ("1234",)

    def setUp(self):
        a = make_db()
        _item(a, "old-item", token="tok-old")
        for i, mask in enumerate(self.masks):
            _account(a, f"old-acct-{i}", "old-item", mask)
        _txn(a, "old-pre", "old-acct-0", D(2024, 3, 1), 500.0,
             "LANDLORD RENT")
        _txn(a, "old-overlap", "old-acct-0", D(2026, 5, 20), 42.5,
             "POS DEBIT GROCERYMART 0042", override="GROCERIES")
        _txn(a, "old-last", "old-acct-0", D(2026, 6, 1), 9.99,
             "STREAMCO SUBSCRIPTION")
        a.execute("INSERT INTO transaction_notes (txn_id, note)"
                  " VALUES ('old-overlap', 'birthday party food')")
        self.zip = export.build_zip(a)
        a.close()
        self.b = _fresh_household("restore-after-link")
        self.addCleanup(self.b.close)

    def _link(self, *, update_status="HISTORICAL_UPDATE_COMPLETE"):
        """The new household linked the same bank before restoring: its
        backfill reaches back to 2025-06 and covers the last restored
        days under its own ids and cleaner names."""
        b = self.b
        _item(b, "new-item", token="tok-new", update_status=update_status)
        _account(b, "new-acct", "new-item", "1234")
        _txn(b, "live-overlap", "new-acct", D(2026, 5, 20), 42.5,
             "Grocerymart")
        _txn(b, "live-last", "new-acct", D(2026, 6, 1), 9.99, "StreamCo")
        _txn(b, "live-after", "new-acct", D(2026, 7, 4), 15.0, "Cafe Corner")

    def _live(self, aid):
        return {r["id"]: dict(r) for r in self.b.execute(
            "SELECT id, date, amount, category_override FROM transactions"
            " WHERE account_id = %s AND removed = 0", (aid,)).fetchall()}


class RestoreAfterLink(_Base):

    def test_one_account_and_no_doubled_overlap(self):
        self._link()
        counts = restore.restore_zip(self.b, self.zip)
        accts = [r["id"] for r in self.b.execute(
            "SELECT id FROM accounts WHERE mask = '1234'").fetchall()]
        self.assertEqual(accts, ["new-acct"])
        live = self._live("new-acct")
        # the pre-link history came across; the overlap is held once,
        # under the live ids
        self.assertEqual(set(live),
                         {"old-pre", "live-overlap", "live-last",
                          "live-after"})
        self.assertFalse(counts.get("_notes") and any(
            "twice" in n for n in counts["_notes"]))

    def test_what_a_person_put_on_the_restored_copy_survives(self):
        self._link()
        restore.restore_zip(self.b, self.zip)
        live = self._live("new-acct")
        self.assertEqual(live["live-overlap"]["category_override"],
                         "GROCERIES")
        note = self.b.execute(
            "SELECT note FROM transaction_notes WHERE txn_id='live-overlap'"
        ).fetchone()
        self.assertEqual(note["note"], "birthday party food")

    def test_the_shell_is_retired_and_the_lineage_recorded(self):
        self._link()
        restore.restore_zip(self.b, self.zip)
        self.assertEqual(self.b.execute(
            "SELECT status FROM items WHERE id='old-item'"
        ).fetchone()["status"], "archived")
        row = self.b.execute(
            "SELECT old_account_id, cutoff_date, reconciled_at"
            " FROM account_adoptions WHERE account_id='new-acct'").fetchone()
        self.assertEqual(row["old_account_id"], "old-acct-0")
        self.assertEqual(row["cutoff_date"], D(2026, 6, 1))
        # the live backfill was already complete: nothing left to guard
        self.assertIsNotNone(row["reconciled_at"])
        self.assertIsNone(adopt.pending(self.b, "new-acct"))

    def test_a_backfill_still_arriving_is_guarded(self):
        """Restored minutes after the link, the rest of Plaid's two-year
        replay is still on its way; the guard catches what is left of it."""
        self._link(update_status="INITIAL_UPDATE_COMPLETE")
        restore.restore_zip(self.b, self.zip)
        self.assertIsNotNone(adopt.pending(self.b, "new-acct"))
        later = [{"id": "live-old", "date": D(2024, 3, 1), "amount": 500.0,
                  "name": "Landlord Rent", "merchant_name": None}]
        kept, _ = adopt.filter_incoming(self.b, "new-acct", later)
        self.assertEqual(kept, [])
        self.assertEqual(set(self._live("new-acct")),
                         {"old-pre", "live-overlap", "live-last",
                          "live-after"})

    def test_restoring_twice_changes_nothing_more(self):
        self._link()
        restore.restore_zip(self.b, self.zip)
        before = self._live("new-acct")
        restore.restore_zip(self.b, self.zip)
        self.assertEqual(self._live("new-acct"), before)
        self.assertEqual([r["id"] for r in self.b.execute(
            "SELECT id FROM accounts WHERE mask='1234'").fetchall()],
            ["new-acct"])


class RestoreFirstStillAdopts(_Base):

    def test_restore_then_link_adopts_as_before(self):
        restore.restore_zip(self.b, self.zip)
        _item(self.b, "new-item", token="tok-new")
        out = adopt.adopt_accounts(
            self.b, item_id="new-item", institution_id="ins_77",
            institution_name="Example Credit Union",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])
        self.assertEqual([o["old_account_id"] for o in out], ["old-acct-0"])


class AmbiguousMaskMergesNothing(_Base):
    """Two restored accounts at the bank share the live account's mask and
    type: either could be it, so neither is merged — and the person is
    told which accounts may now appear twice."""

    masks = ("1234", "1234")

    def test_nothing_merged_and_the_restore_says_so(self):
        self._link()
        counts = restore.restore_zip(self.b, self.zip)
        ids = sorted(r["id"] for r in self.b.execute(
            "SELECT id FROM accounts WHERE mask='1234'").fetchall())
        self.assertEqual(ids, ["new-acct", "old-acct-0", "old-acct-1"])
        self.assertIn("live-overlap", self._live("new-acct"))
        self.assertIn("old-overlap", self._live("old-acct-0"))
        notes = " ".join(counts.get("_notes") or [])
        self.assertIn("••1234", notes)
        self.assertIn("twice", notes)


class OverlapPairsOneToOne(unittest.TestCase):
    """Two real same-price coffees on one day are held by both lineages:
    they pair off as two twins and two charges stay, not one. A charge only
    the restore holds stays too, and a budget exclusion follows the
    account into the live one."""

    def setUp(self):
        a = make_db()
        _item(a, "old-item", token="tok-old")
        _account(a, "old-acct", "old-item", "4321")
        for tid in ("c1", "c2"):
            _txn(a, f"old-{tid}", "old-acct", D(2026, 5, 2), 4.5,
                 "BEANHOUSE 0042")
        _txn(a, "old-only", "old-acct", D(2026, 5, 3), 77.0, "HARDWARE BARN")
        with budget.config_txn(a) as c:
            c["excluded_accounts"] = ["old-acct"]
        self.zip = export.build_zip(a)
        a.close()
        self.b = _fresh_household("restore-after-link-pairs")
        self.addCleanup(self.b.close)
        _item(self.b, "new-item", token="tok-new",
              update_status="HISTORICAL_UPDATE_COMPLETE")
        _account(self.b, "new-acct", "new-item", "4321")
        for tid in ("c1", "c2"):
            _txn(self.b, f"live-{tid}", "new-acct", D(2026, 5, 2), 4.5,
                 "Beanhouse")
        restore.restore_zip(self.b, self.zip)

    def test_each_restored_row_pairs_with_one_live_row(self):
        live = {r["id"] for r in self.b.execute(
            "SELECT id FROM transactions WHERE account_id='new-acct'"
            " AND removed = 0").fetchall()}
        self.assertEqual(live, {"live-c1", "live-c2", "old-only"})

    def test_the_exclusion_follows_into_the_live_account(self):
        cfg = budget.load_config(self.b)
        self.assertEqual(cfg.get("excluded_accounts"), ["new-acct"])


if __name__ == "__main__":
    unittest.main()
