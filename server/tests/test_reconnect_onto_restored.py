"""Reconnecting a bank after a restore adopts the accounts, not duplicates them.

A restored item has no access token, so it cannot be repaired — only
re-linked — and Plaid's ids are item-scoped, so the fresh link mints a new
account id for the same real account and a new transaction id for every
charge in its ~24-month backfill. Since both upserts key on the id, none
of it collides with what the restore brought back: the tenant ends up with
two of each account and two of most of their recent history.

What these protect:

  * an incoming account matching a restored one by institution, mask and
    type is ADOPTED — the restored row is renamed into the Plaid id, so
    the history, the bills, the holdings and the entity assignment all
    stay attached to the row that is now live;
  * ambiguity is never resolved by guessing: two candidates sharing a mask
    means no adoption at all, because welding a chequing history onto a
    savings feed is worse than an extra account;
  * the backfill that follows is filtered against what is already held —
    outright before the account's last restored transaction, and through
    the real one-to-one matcher in the overlapping days;
  * the guard is a ONE-SHOT. Left on it would start discarding Plaid's
    later corrections to old rows.
"""

import datetime as dt
import unittest
from unittest import mock

import psycopg

from oikonome.engine import budget, savings
from oikonome.sync import adopt

from .util import make_db, write_config


def _item(conn, iid, status, inst_id=None, inst_name=None, token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, "
        "institution_name, status, access_token) "
        "VALUES (%s,'plaid',%s,%s,%s,%s)",
        (iid, inst_id, inst_name, status, token))


def _account(conn, aid, iid, *, mask, type_="depository", name="Checking"):
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, mask) "
        "VALUES (%s,%s,%s,%s,%s)", (aid, iid, name, type_, mask))


def _txn(conn, tid, aid, date, amount, name="COFFEE"):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name, "
        "pending, removed) VALUES (%s,%s,%s,%s,%s,0,0)",
        (tid, aid, date, amount, name))


class _Txn:
    """The shape the sync path hands the guard."""

    def __init__(self, account_id, date, amount, name, id=None):
        self.account_id, self.date = account_id, date
        self.amount, self.name = amount, name
        self.merchant_name = None
        self.id = id


class AdoptionTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _txn(self.conn, "old-2", "old-acct", dt.date(2026, 6, 1), 20.0,
             "SAFEWAY STORE 123")
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")

    def _adopt(self, incoming):
        return adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme", incoming=incoming)

    def test_matching_account_is_adopted_and_history_follows(self):
        out = self._adopt([{"account_id": "new-acct", "mask": "1234",
                            "type": "depository"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_account_id"], "old-acct")
        self.assertEqual(out[0]["cutoff_date"], dt.date(2026, 6, 1))
        # exactly ONE account for this bank, under the Plaid id, on the
        # new item (make_db seeds unrelated fixture accounts)
        rows = self.conn.execute(
            "SELECT id, item_id FROM accounts WHERE item_id IN "
            "('old-item','new-item')").fetchall()
        self.assertEqual([(r["id"], r["item_id"]) for r in rows],
                         [("new-acct", "new-item")])
        # and the history came with it
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE account_id='new-acct'").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_the_emptied_connection_is_retired(self):
        """The shell must not linger: a connection with no accounts still
        wears its error and still holds a slot against the institution
        cap, which counts every non-archived item — so leftovers would
        refuse the NEXT reconnect."""
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        row = self.conn.execute(
            "SELECT status, archived_reason FROM items WHERE id='old-item'"
        ).fetchone()
        self.assertEqual(row["status"], "archived")
        self.assertIn("adopted", row["archived_reason"])

    def test_a_donor_retires_even_with_history_left_on_it(self):
        """A reconnect usually leaves something behind — a closed account,
        a lineage the new link no longer returns. Keeping the shell alive
        for those left it saying "sync failing" forever on a connection
        that has no credentials and never will again."""
        _account(self.conn, "old-acct-2", "old-item", mask="5678",
                 name="Closed savings")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='old-item'"
        ).fetchone()["status"], "archived")
        # the leftover history is still there, just quiet
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id='old-acct-2'").fetchone())

    def test_an_archived_donor_can_still_be_adopted_from_later(self):
        """Archiving must not strand what it holds: adoption keys on the
        missing token, so re-linking that bank later still claims it."""
        _account(self.conn, "old-acct-2", "old-item", mask="5678",
                 name="Closed savings")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        out = self._adopt([{"account_id": "new-acct-2", "mask": "5678",
                            "type": "depository"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_account_id"], "old-acct-2")

    def test_everything_pointing_at_the_account_follows_too(self):
        """A missed reference would orphan the rows it holds — a bill that
        silently stops matching is the expensive version of this bug."""
        self.conn.execute(
            "INSERT INTO bills (id, type, payee, amount, frequency, "
            "account_id) VALUES ('b1','expense','Rent',100,'monthly',"
            "'old-acct')")
        self.conn.execute(
            "INSERT INTO holdings (account_id, symbol, name, quantity, "
            "price) VALUES ('old-acct','VOO','Vanguard',1,1)")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM bills WHERE id='b1'"
        ).fetchone()["account_id"], "new-acct")
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM holdings WHERE symbol='VOO'"
        ).fetchone()["account_id"], "new-acct")

    def test_an_item_that_still_has_a_token_is_never_adopted_from(self):
        """A connection that can still sync is not a shell: stealing its
        accounts would break a working link to fix nothing."""
        self.conn.execute(
            "UPDATE items SET status='ok', access_token='live-token' "
            "WHERE id='old-item'")
        self.assertEqual(self._adopt([{"account_id": "new-acct",
                                       "mask": "1234",
                                       "type": "depository"}]), [])

    def _adopt_with_status(self, status):
        """A restored item wears status='restored' only until the next
        hourly sync tries it and stamps error:NO_TOKEN, so keying on the
        status string adopts nothing once a sweep has run. Tokenlessness is
        the real property."""
        self.conn.execute("UPDATE items SET status=%s WHERE id='old-item'",
                          (status,))
        return self._adopt([{"account_id": "new-acct", "mask": "1234",
                             "type": "depository"}])

    def test_the_status_a_real_restore_drifts_to_is_adopted(self):
        self.assertEqual(len(self._adopt_with_status("error:NO_TOKEN")), 1)

    def test_a_reaped_connection_is_adopted_too(self):
        """Same predicament from the other direction: the reaper
        disconnects after 30 days of failure, keeping the history and
        dropping the credentials, and the fix it offers is a fresh link."""
        self.assertEqual(len(self._adopt_with_status("archived")), 1)

    def test_a_second_login_at_one_bank_keeps_its_reconnect_prompt(self):
        """A household with two logins at one bank has two connections to
        re-link, and the link that repairs one is not the successor of the
        other: its accounts are nowhere in what this link returned.
        Archiving it anyway hides the very prompt telling them to reconnect
        it — the same harm the untouched-institution rule exists to avoid,
        one level down."""
        _item(self.conn, "other-shell", "error:NO_TOKEN", "ins_1", "Acme")
        _account(self.conn, "other-acct", "other-shell", mask="9999")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='other-shell'"
        ).fetchone()["status"], "error:NO_TOKEN")

    def test_a_shell_with_nothing_left_on_it_is_retired(self):
        """The other side of the same rule: a shell holding no account is
        the home of nothing, so the live link at its institution has
        replaced it. Left alone it nags "sync failing" for ever on
        credentials that are gone and holds a slot against the institution
        cap, which counts every non-archived item."""
        _item(self.conn, "empty-shell", "error:NO_TOKEN", "ins_1", "Acme")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        row = self.conn.execute(
            "SELECT status, archived_reason FROM items "
            "WHERE id='empty-shell'").fetchone()
        self.assertEqual(row["status"], "archived")
        self.assertIn("now serves", row["archived_reason"])

    def test_an_empty_shell_is_retired_with_nothing_to_adopt(self):
        """The successor question is open even when the link adopts
        nothing: a shell holding no account has no history to claim, so
        the candidate pool is empty — and the sweep must still run, or the
        shell nags forever."""
        self.conn.execute("DELETE FROM accounts WHERE item_id='old-item'")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='old-item'"
        ).fetchone()["status"], "archived")

    def test_a_shell_the_new_link_serves_is_retired(self):
        """And a shell whose remaining account the new link DOES return —
        the account arrived under an id we already held, so adoption had
        nothing to claim — is a connection this link replaced."""
        _item(self.conn, "other-shell", "error:NO_TOKEN", "ins_1", "Acme")
        _account(self.conn, "other-acct", "other-shell", mask="9999",
                 type_="credit")
        _account(self.conn, "known-card", "new-item", mask="9999",
                 type_="credit")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"},
                     {"account_id": "known-card", "mask": "9999",
                      "type": "credit"}])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='other-shell'"
        ).fetchone()["status"], "archived")

    def test_an_institution_with_no_live_link_is_left_flagged(self):
        """Globex Card never linked: archiving it would hide the very prompt
        telling the household to reconnect it."""
        _item(self.conn, "globex", "error:NO_TOKEN", "ins_99", "Globex Card")
        _account(self.conn, "globex-acct", "globex", mask="4242", type_="credit")
        self._adopt([{"account_id": "new-acct", "mask": "1234",
                      "type": "depository"}])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='globex'"
        ).fetchone()["status"], "error:NO_TOKEN")

    def test_a_renamed_institution_still_matches_on_its_id(self):
        """Plaid can return 'Acme Bank' where the restore carried 'Acme'; a
        name comparison would leave the shell behind, saying "sync failing"
        next to the working connection. The id is the stable identifier and
        wins whenever both sides have one."""
        self.conn.execute(
            "UPDATE items SET institution_name='Acme' WHERE id='old-item'")
        out = adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme Bank",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])
        self.assertEqual(len(out), 1)

    def test_a_different_institution_is_never_adopted_from(self):
        out = adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_999",
            institution_name="Chase",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])
        self.assertEqual(out, [])

    def test_a_shared_mask_refuses_to_guess(self):
        _account(self.conn, "old-acct-2", "old-item", mask="1234",
                 name="Savings")
        self.assertEqual(self._adopt([{"account_id": "new-acct",
                                       "mask": "1234",
                                       "type": "depository"}]), [])
        # both originals survive untouched
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE item_id='old-item'"
        ).fetchone()["n"], 2)

    def test_a_mismatched_type_is_not_the_same_account(self):
        self.assertEqual(self._adopt([{"account_id": "new-acct",
                                       "mask": "1234",
                                       "type": "credit"}]), [])

    def test_no_mask_means_no_safe_identity(self):
        self.assertEqual(self._adopt([{"account_id": "new-acct",
                                       "mask": None,
                                       "type": "depository"}]), [])


class BackfillGuardTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _txn(self.conn, "old-2", "old-acct", dt.date(2026, 6, 1), 20.0,
             "SAFEWAY STORE 123")
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])

    def test_the_backfill_we_already_hold_is_dropped(self):
        incoming = [_Txn("new-acct", dt.date(2026, 1, 5), 10.0, "COFFEE"),
                    _Txn("new-acct", dt.date(2026, 3, 3), 5.0, "OLDER")]
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", incoming)
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped_before_cutoff"], 2)

    def test_the_overlapping_day_is_matched_not_guessed(self):
        """On the cutoff day itself a genuine new charge and a restored one
        are told apart by the real matcher, not by the date."""
        dup = _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY")
        fresh = _Txn("new-acct", dt.date(2026, 6, 1), 77.0, "NEW SHOP")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct",
                                            [dup, fresh])
        self.assertEqual([t.name for t in kept], ["NEW SHOP"])
        self.assertEqual(stats["deduped_overlap"], 1)

    def test_anything_after_the_cutoff_passes_straight_through(self):
        later = [_Txn("new-acct", dt.date(2026, 7, 1), 9.0, "LATER")]
        kept, _ = adopt.filter_incoming(self.conn, "new-acct", later)
        self.assertEqual(kept, later)

    def test_a_pending_row_is_retired_because_nothing_can_post_it(self):
        """A pending transaction is a promise its own item will post it
        later. After a restore that item is gone, so the promise can never
        be kept — and when the bank posts the real charge, the fresh link
        delivers it under a NEW id that collides with nothing, so every
        recurring payee with a hold open appears twice."""
        c = self.conn
        c.execute("UPDATE transactions SET pending = 1 WHERE id = 'old-2'")
        # adopt a SECOND account so the fixture's adoption is not reused
        _account(c, "old-p", "old-item", mask="4321", name="Other")
        _txn(c, "pend-1", "old-p", dt.date(2026, 6, 2), 30.0, "VENMO")
        c.execute("UPDATE transactions SET pending = 1 WHERE id = 'pend-1'")
        adopt.adopt_accounts(
            c, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-p", "mask": "4321",
                       "type": "depository"}])
        self.assertEqual(c.execute(
            "SELECT removed FROM transactions WHERE id='pend-1'"
        ).fetchone()["removed"], 1)

    def test_the_guard_outlives_the_first_sync(self):
        """The guard must stay open past one pagination: Plaid keeps
        delivering for days, and a second sync arriving with the guard
        already shut would insert the duplicates."""
        adopt.mark_reconciled(self.conn, "new-item")   # explicit close
        self.assertIsNone(adopt.pending(self.conn, "new-acct"))
        # …but a fresh adoption stays open on its own clock
        self.conn.execute("UPDATE account_adoptions SET reconciled_at = NULL")
        self.assertIsNotNone(adopt.pending(self.conn, "new-acct"))
        self.conn.execute(
            "UPDATE account_adoptions SET adopted_at = now() - "
            "make_interval(days => %s)", (adopt.RECONCILE_DAYS + 1,))
        self.assertIsNone(adopt.pending(self.conn, "new-acct"),
                          "the guard must expire on its own")

    def test_a_row_just_under_the_cutoff_is_matched_not_waved_through(self):
        """Blind-dropping everything below the cutoff meant a row Plaid
        re-delivered just under the line was never examined."""
        near = _Txn("new-acct", dt.date(2026, 5, 30), 20.0, "SAFEWAY")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [near])
        # 2026-05-30 is inside the seam (cutoff 2026-06-01), so it is
        # dedup-checked; the restored SAFEWAY row at 20.00 claims it
        self.assertEqual(kept, [])
        self.assertEqual(stats["deduped_overlap"], 1)

    def test_the_guard_is_a_one_shot(self):
        self.assertEqual(adopt.mark_reconciled(self.conn, "new-item"), 1)
        # a correction to an old row must now be allowed through, or the
        # guard becomes the data loss it exists to prevent
        old = [_Txn("new-acct", dt.date(2026, 1, 5), 10.0, "COFFEE")]
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", old)
        self.assertEqual(kept, old)
        self.assertEqual(stats, {})

    def test_an_unadopted_account_is_untouched(self):
        _account(self.conn, "plain", "new-item", mask="9999")
        rows = [_Txn("plain", dt.date(2020, 1, 1), 1.0, "X")]
        kept, stats = adopt.filter_incoming(self.conn, "plain", rows)
        self.assertEqual(kept, rows)
        self.assertEqual(stats, {})


class CarriedPlaidIdTests(unittest.TestCase):
    """A charge the guard drops still has to answer to Plaid afterwards.

    Plaid's `removed` and `modified` deltas name the transaction id the NEW
    link minted. The copy on the books came back from a backup under the
    id the OLD link minted, so unless the two identities are joined the
    delta arrives for an id this tenant has never stored and does nothing
    at all: a reversed charge stays on the books, a corrected one keeps its
    wrong amount.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _txn(self.conn, "old-2", "old-acct", dt.date(2026, 6, 1), 20.0,
             "SAFEWAY STORE 123")
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])

    def _claim(self, **kw):
        dup = _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY",
                   id=kw.get("id", "plaid-safeway"))
        return adopt.filter_incoming(self.conn, "new-acct", [dup])

    def test_the_restored_copy_takes_on_the_incoming_plaid_id(self):
        kept, stats = self._claim()
        self.assertEqual(kept, [])
        self.assertEqual(stats["deduped_overlap"], 1)
        self.assertEqual(stats["carried_plaid_ids"], 1)
        row = self.conn.execute(
            "SELECT name, amount, account_id FROM transactions "
            "WHERE id='plaid-safeway'").fetchone()
        # the restored row itself, under its new identity — not a copy of
        # the incoming payload, and not a second charge
        self.assertEqual(row["name"], "SAFEWAY STORE 123")
        self.assertEqual(row["account_id"], "new-acct")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='old-2'").fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE account_id='new-acct'").fetchone()["n"], 2)

    def test_a_removal_reaches_the_restored_copy(self):
        """A `removed` delta must retire the charge it names. Missing it is
        silent: the ledger keeps a charge the bank reversed, and nothing
        ever revisits it."""
        from oikonome.sync.base import mark_removed
        self._claim()
        mark_removed(self.conn, ["plaid-safeway"])
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='plaid-safeway'"
        ).fetchone()["removed"], 1)

    def test_everything_a_person_attached_follows_the_carried_id(self):
        """The row moves by copy-repoint-delete, and the children hang off
        it with ON DELETE CASCADE — so a reference the move forgets is not
        orphaned, it is DESTROYED along with the old row."""
        c = self.conn
        c.execute("INSERT INTO transaction_notes (txn_id, note) "
                  "VALUES ('old-2','ask about the double charge')")
        c.execute("INSERT INTO business_flags (txn_id) VALUES ('old-2')")
        c.execute("UPDATE transactions SET category_override='TRAVEL' "
                  "WHERE id='old-2'")
        self._claim()
        self.assertEqual(c.execute(
            "SELECT note FROM transaction_notes WHERE txn_id='plaid-safeway'"
        ).fetchone()["note"], "ask about the double charge")
        self.assertTrue(c.execute(
            "SELECT 1 FROM business_flags WHERE txn_id='plaid-safeway'"
        ).fetchone())
        self.assertEqual(c.execute(
            "SELECT category_override FROM transactions "
            "WHERE id='plaid-safeway'").fetchone()["category_override"],
            "TRAVEL")

    def test_a_correction_to_a_row_we_hold_is_never_dropped(self):
        """The guard drops DUPLICATES. A row Plaid re-sends under an id
        already on the books is that same row corrected, so dropping it —
        on the date rule, or through a matcher that happily matches the row
        against itself — loses the correction with no trace."""
        _txn(self.conn, "plaid-old", "new-acct", dt.date(2026, 1, 5), 10.0,
             "COFFEE")
        fix = _Txn("new-acct", dt.date(2026, 1, 5), 12.0, "COFFEE",
                   id="plaid-old")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [fix])
        self.assertEqual(kept, [fix])
        self.assertEqual(stats["dropped_before_cutoff"], 0)

    def test_two_charges_a_person_could_not_tell_apart_carry_nothing(self):
        """Which restored row the matcher consumed is unknowable when two
        are identical, and renaming the wrong one welds Plaid's future
        corrections onto the wrong charge. A missed delta is recoverable;
        a corrupted row is not."""
        _txn(self.conn, "old-2b", "new-acct", dt.date(2026, 6, 1), 20.0,
             "SAFEWAY STORE 123")
        kept, stats = self._claim()
        self.assertEqual(kept, [])
        self.assertEqual(stats["deduped_overlap"], 1)
        self.assertEqual(stats["carried_plaid_ids"], 0)
        # both originals still wear their own ids
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE id IN ('old-2','old-2b')").fetchone()["n"], 2)

    def test_a_sibling_pointing_at_the_renamed_row_follows_it(self):
        """`transactions.pending_transaction_id` is a plain TEXT
        self-reference — no foreign key — so the catalog scan the rename
        uses to find "everything pointing at this row" cannot see it, and
        restore.py round-trips the column from an export. A sibling left
        naming the id the rename deleted is a dangling pointer in a
        mechanism documented as exhaustive."""
        c = self.conn
        _txn(c, "old-posted", "new-acct", dt.date(2026, 6, 2), 20.0, "LATER")
        c.execute("UPDATE transactions SET pending_transaction_id='old-2' "
                  "WHERE id='old-posted'")
        self._claim()
        self.assertEqual(c.execute(
            "SELECT pending_transaction_id AS p FROM transactions "
            "WHERE id='old-posted'").fetchone()["p"], "plaid-safeway")

    def test_a_persons_category_pin_follows_the_renamed_row(self):
        """manual_categories.transaction_id is the same FK-less class as
        pending_transaction_id, and it is the pin that records "a person
        decided this category" — left behind on the old id, the renamed
        row would silently lose its protection against re-stamping."""
        c = self.conn
        c.execute("INSERT INTO manual_categories (transaction_id, category)"
                  " VALUES ('old-2', 'FOOD_AND_DRINK')")
        self._claim()
        self.assertEqual(c.execute(
            "SELECT category FROM manual_categories "
            "WHERE transaction_id='plaid-safeway'").fetchone()["category"],
            "FOOD_AND_DRINK")
        self.assertIsNone(c.execute(
            "SELECT 1 AS x FROM manual_categories "
            "WHERE transaction_id='old-2'").fetchone())

    def test_a_crash_partway_through_the_rename_leaves_the_row_whole(self):
        """Copy-repoint-delete is three statements and ONE move. A failure
        between them must leave the ledger exactly as it was — never the
        same charge under two ids, never children pointing at an id with
        no row behind it."""
        c = self.conn
        c.execute("INSERT INTO transaction_notes (txn_id, note) "
                  "VALUES ('old-2','ask about the double charge')")
        cols, refs = adopt._txn_shape(c)
        broken = refs + (("no_such_child_table", "txn_id"),)
        with mock.patch.object(adopt, "_txn_shape",
                               return_value=(cols, broken)):
            with self.assertRaises(psycopg.Error):
                adopt._rewrite_txn_id(c, "old-2", "plaid-safeway")
        self.assertIsNone(c.execute(
            "SELECT 1 FROM transactions WHERE id='plaid-safeway'").fetchone())
        self.assertEqual(c.execute(
            "SELECT name FROM transactions WHERE id='old-2'"
        ).fetchone()["name"], "SAFEWAY STORE 123")
        self.assertEqual(c.execute(
            "SELECT note FROM transaction_notes WHERE txn_id='old-2'"
        ).fetchone()["note"], "ask about the double charge")

    def test_an_edit_cannot_land_inside_the_rename_and_be_deleted(self):
        """The rename must leave no window: the copy is a SNAPSHOT and the
        delete drops whatever the row holds at the end, so an edit
        committing in between would be carried by neither and destroyed by
        the delete — silently, after the API already answered 200. The worker
        runs this while the household is using the app, so the window is
        real. Locking the row first makes the writer wait for the rename
        and then find no such id, which the API doors report as 404."""
        c2 = _second_connection(self, self.conn)
        c2.execute("SET statement_timeout = '750ms'")
        blocked = []

        def edit_from_the_web():
            try:
                c2.execute("UPDATE transactions SET category_override='TRAVEL' "
                           "WHERE id='old-2'")
                blocked.append(False)
            except psycopg.errors.QueryCanceled:
                blocked.append(True)

        proxy = _AfterTheCopy(self.conn, edit_from_the_web)
        self.assertTrue(adopt._rewrite_txn_id(proxy, "old-2",
                                              "plaid-safeway"))
        self.assertEqual(blocked, [True],
                         "the concurrent edit was not serialized against "
                         "the rename — it lands on a row about to be "
                         "deleted and is lost with no trace")
        # the rename itself completed, and the edit that could not land is
        # not silently sitting on a row nobody can reach any more
        self.assertIsNone(c2.execute(
            "SELECT 1 FROM transactions WHERE id='old-2'").fetchone())
        self.assertIsNone(c2.execute(
            "SELECT category_override AS c FROM transactions "
            "WHERE id='plaid-safeway'").fetchone()["c"])

    def test_more_history_than_the_cap_carries_nothing_rather_than_a_guess(self):
        """The candidate fetch is capped, and `LIMIT` with no `ORDER BY`
        returns whichever rows Postgres reached first. Matching against an
        arbitrary subset would rename a row on evidence that was never
        complete, so an overflowing window must carry NO ids at all — the
        guard still drops the duplicate, which is what it is for."""
        # inside the seam AND at or before the cutoff — the candidate set
        # is the restored history, so a row dated after the cutoff is not
        # fetched at all and would not push the count over the cap
        _txn(self.conn, "old-3", "new-acct", dt.date(2026, 5, 31), 33.0,
             "OTHER SHOP")
        with mock.patch("oikonome.sync.dedup.MAX_CANDIDATES", 1):
            kept, stats = self._claim()
        self.assertEqual(kept, [])
        self.assertEqual(stats["deduped_overlap"], 1)
        self.assertEqual(stats["carried_plaid_ids"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='old-2'").fetchone())


class ConfigPointersFollowTheAccountRenameTests(unittest.TestCase):
    """The settings document names accounts by plain string, with no
    foreign key to keep the two honest, so the rename has to carry those
    pointers itself.

    Every one of them fails OPEN and in silence. A stale
    `excluded_accounts` entry excludes nothing, so an account the household
    deliberately kept out of the budget starts counting toward spend again
    in Today, in the verdict and in every chart. A stale
    `checking_account_id` un-pins the primary account and lets the forecast
    fall back to whichever depository account it finds. A stale
    `savings_goals[].account_id` matches zero rows for ever, so a goal that
    was accumulating real progress reports none. A stale `link_dismissed`
    half stops suppressing a pairing the household already rejected.
    Nothing raises in any of the four cases.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-chk", "old-item", mask="1234")
        _account(self.conn, "old-side", "old-item", mask="5678",
                 name="Side business")
        _account(self.conn, "old-sav", "old-item", mask="9012",
                 name="Savings")
        _txn(self.conn, "side-1", "old-side", dt.date(2026, 6, 1), 40.0,
             "PRINTER INK")
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "category_primary, pending, removed) VALUES ('sav-1','old-sav',"
            "%s,-500,'TRANSFER TO SAVINGS','TRANSFER_IN',0,0)",
            (dt.date(2026, 6, 1),))
        write_config(
            self.conn,
            excluded_accounts=["old-side"],
            checking_account_id="old-chk",
            link_dismissed=["|".join(sorted(("old-side", "chk")))],
            savings_goals=[{"name": "trip fund", "target": 3000.0,
                            "monthly_plan": 250.0, "account_id": "old-sav",
                            "tokens": [], "start_balance": 0.0}])
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")

    def _adopt(self):
        return adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-chk", "mask": "1234",
                       "type": "depository"},
                      {"account_id": "new-side", "mask": "5678",
                       "type": "depository"},
                      {"account_id": "new-sav", "mask": "9012",
                       "type": "depository"}])

    def test_an_excluded_account_is_still_excluded_after_the_reconnect(self):
        self._adopt()
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["excluded_accounts"], ["new-side"])
        # and the exclusion still BITES: the budget filters on this list, so
        # an id naming no account silently re-admits the spend it was hiding
        rows = budget._spend_rows(self.conn, dt.date(2026, 6, 1),
                                  dt.date(2026, 6, 30),
                                  excluded=cfg.get("excluded_accounts"))
        self.assertEqual([r for r in rows if r["name"] == "PRINTER INK"], [])

    def test_the_primary_checking_pin_survives_the_reconnect(self):
        self._adopt()
        self.assertEqual(
            budget.load_config(self.conn)["checking_account_id"], "new-chk")

    def test_a_savings_goal_keeps_seeing_its_contributions(self):
        self._adopt()
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["savings_goals"][0]["account_id"], "new-sav")
        # the goal is what the household watches, so prove the money is
        # still attributed to it and not merely that a string moved
        p = savings.progress(self.conn, cfg, dt.date(2026, 6, 30))[0]
        self.assertEqual(p["saved"], 500.0)

    def test_a_rejected_link_suggestion_stays_rejected(self):
        self._adopt()
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["link_dismissed"],
                         ["|".join(sorted(("new-side", "chk")))])

    def test_a_pointer_at_an_account_that_was_not_adopted_is_left_alone(self):
        """Only the account that actually moved is rewritten — a rename
        that reached into every id-shaped string would repoint settings
        that name accounts this link never touched."""
        self.conn.execute("UPDATE items SET access_token='still-here' "
                          "WHERE id='old-item'")
        self._adopt()
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["excluded_accounts"], ["old-side"])
        self.assertEqual(cfg["checking_account_id"], "old-chk")


class SeamCarryOnlyOnProvableMatchTests(unittest.TestCase):
    """The carry moves a LIVE charge's identity, so it may only happen when
    the row it moves onto is provably the one the matcher consumed.

    Getting it wrong is not a missed repair: the row that loses its Plaid
    id can never again be reached by a `removed` or `modified` delta, and
    the charge that gains one answers for a transaction it is not. The
    guard stays open for a fortnight after any reconnect, so "the row the
    matcher consumed" has to be told apart from every ordinary charge that
    lands in the meantime.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-2", "old-acct", dt.date(2026, 6, 1), 20.0,
             "SAFEWAY STORE 123")
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])

    def test_a_charge_from_after_the_backup_keeps_its_own_plaid_id(self):
        """Nothing the restore holds is dated after the cutoff — the cutoff
        IS the newest restored date — so a live row past it cannot be a row
        the restore brought back, and must never be picked as the one the
        matcher consumed. Without that bound a charge that arrived days
        AFTER the reconnect could have its Plaid id overwritten by the next
        similar charge, and every later correction to either one lands on
        the wrong charge or on nothing."""
        _txn(self.conn, "plaid-target-1", "new-acct", dt.date(2026, 6, 2),
             15.0, "TARGET")
        later = _Txn("new-acct", dt.date(2026, 6, 3), 15.0, "TARGET",
                     id="plaid-target-2")
        _, stats = adopt.filter_incoming(self.conn, "new-acct", [later])
        self.assertEqual(stats["carried_plaid_ids"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='plaid-target-1'"
        ).fetchone(), "a live charge lost the id Plaid addresses it by")

    def test_a_charge_past_the_seam_is_never_matched_at_all(self):
        """The seam has a far edge: the matcher reaches WINDOW_DAYS, so a
        row dated further past the cutoff than that cannot duplicate
        anything the restore holds. Without the edge every ordinary charge
        of the guard's fortnight was run through a duplicate matcher
        against the household's own live history — two same-price coffees a
        few days apart, and the second is claimed as a copy of the
        first."""
        _txn(self.conn, "plaid-coffee-1", "new-acct", dt.date(2026, 6, 20),
             4.75, "COFFEE HOUSE")
        second = _Txn("new-acct", dt.date(2026, 6, 23), 4.75, "COFFEE HOUSE",
                      id="plaid-coffee-2")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [second])
        self.assertEqual(kept, [second], "a genuine new charge was dropped "
                                         "as a duplicate of an older one")
        self.assertEqual(stats["deduped_overlap"], 0)
        self.assertEqual(stats["carried_plaid_ids"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='plaid-coffee-1'"
        ).fetchone())

    def test_a_match_with_no_shared_name_carries_nothing(self):
        """The matcher will also claim on same-day amount alone when the
        two names merely fail to contradict each other — an opaque bank
        descriptor is no evidence either way. That is the right rule for
        DROPPING a duplicate and the wrong one for deciding whose identity
        to move, because it rests on the absence of evidence rather than
        the presence of any."""
        opaque = _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "POS 8829174",
                      id="plaid-opaque")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [opaque])
        self.assertEqual(kept, [])
        self.assertEqual(stats["deduped_overlap"], 1)
        self.assertEqual(stats["carried_plaid_ids"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='old-2'").fetchone())

    def test_a_row_already_wearing_an_incoming_id_is_never_renamed_again(self):
        """An earlier page of the same backfill already carried an id onto
        that row, and Plaid has been addressing it by that id ever since.
        Renaming it a second time strips an identity that is already live."""
        first = _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY",
                     id="plaid-safeway")
        adopt.filter_incoming(self.conn, "new-acct", [first])
        again = _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY",
                     id="plaid-safeway-2")
        _, stats = adopt.filter_incoming(
            self.conn, "new-acct",
            [_Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY",
                  id="plaid-safeway"), again])
        self.assertEqual(stats["carried_plaid_ids"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='plaid-safeway'").fetchone())


class AdoptionIsOneMoveWithItsBookkeepingTests(unittest.TestCase):
    """The rename and the row that RECORDS it are one move.

    The connection is autocommit, so with the bookkeeping outside the
    rename's transaction a crash in the gap (deploy restart, OOM, dropped
    connection) left the account already wearing the incoming Plaid id with
    no `account_adoptions` row behind it. That state is unrecoverable, not
    untidy: candidates are only ever sought on TOKENLESS items, so the
    renamed account now sits on the live item and is never revisited, while
    the backfill guard keys entirely off the adoption row and short-circuits
    when there is none. The next page of the same backfill then inserts two
    years of Plaid history alongside the restored copy of it, under ids
    that collide with nothing — a duplicate ledger, and every money
    aggregate wrong with it.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _txn(self.conn, "old-p", "old-acct", dt.date(2026, 1, 6), 11.0,
             "VENMO")
        self.conn.execute(
            "UPDATE transactions SET pending = 1 WHERE id = 'old-p'")
        write_config(self.conn, excluded_accounts=["old-acct"])
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")

    def test_a_crash_before_the_bookkeeping_lands_undoes_the_rename(self):
        proxy = _CrashBefore(self.conn, "INSERT INTO account_adoptions")
        with self.assertRaises(RuntimeError):
            adopt.adopt_accounts(
                proxy, item_id="new-item", institution_id="ins_1",
                institution_name="Acme",
                incoming=[{"account_id": "new-acct", "mask": "1234",
                           "type": "depository"}])
        c = self.conn
        # the account never moved, so the retry still sees it on a
        # tokenless item and can adopt it properly
        self.assertIsNone(c.execute(
            "SELECT 1 FROM accounts WHERE id='new-acct'").fetchone())
        self.assertEqual(c.execute(
            "SELECT item_id FROM accounts WHERE id='old-acct'"
        ).fetchone()["item_id"], "old-item")
        self.assertIsNone(c.execute(
            "SELECT 1 FROM account_adoptions").fetchone())
        # and nothing the rename does on the way rode out on its own
        self.assertEqual(c.execute(
            "SELECT removed FROM transactions WHERE id='old-p'"
        ).fetchone()["removed"], 0)
        self.assertEqual(
            budget.load_config(c)["excluded_accounts"], ["old-acct"])

    def test_the_retry_after_such_a_crash_adopts_cleanly(self):
        """The point of rolling back is that the next attempt works — a
        half-done adoption is the one state nothing can repair."""
        proxy = _CrashBefore(self.conn, "INSERT INTO account_adoptions")
        with self.assertRaises(RuntimeError):
            adopt.adopt_accounts(
                proxy, item_id="new-item", institution_id="ins_1",
                institution_name="Acme",
                incoming=[{"account_id": "new-acct", "mask": "1234",
                           "type": "depository"}])
        out = adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(adopt.pending(self.conn, "new-acct"))
        self.assertEqual(
            budget.load_config(self.conn)["excluded_accounts"], ["new-acct"])


class _CrashBefore:
    """A connection stand-in that dies the instant it is asked to run a
    given statement — the worker killed (deploy restart, OOM, a dropped
    connection) at exactly the point the sequence is most exposed."""

    def __init__(self, conn, before):
        self._conn, self._before = conn, before

    def execute(self, sql, params=None):
        if isinstance(sql, str) and sql.lstrip().startswith(self._before):
            raise RuntimeError("worker killed mid-adoption")
        return (self._conn.execute(sql) if params is None
                else self._conn.execute(sql, params))

    def transaction(self):
        return self._conn.transaction()


def _second_connection(test, conn):
    """A SECOND connection into the same tenant — the web request the
    worker is racing. Pooled, so its session settings are reset before
    the next borrower sees it."""
    from oikonome.db import tenancy
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    c = tenancy.tenant_connect(tid)

    def _done():
        try:
            c.execute("RESET statement_timeout")
        finally:
            c.close()
    test.addCleanup(_done)
    return c


class _AfterTheCopy:
    """A connection stand-in that runs `hook` the instant the snapshot copy
    lands — the moment a concurrent web edit could slip in between the copy
    and the delete if the row were not locked."""

    def __init__(self, conn, hook, after="INSERT INTO transactions"):
        self._conn, self._hook, self._after = conn, hook, after

    def execute(self, sql, params=None):
        cur = (self._conn.execute(sql) if params is None
               else self._conn.execute(sql, params))
        if self._hook and isinstance(sql, str) and \
                sql.startswith(self._after):
            hook, self._hook = self._hook, None
            hook()
        return cur

    def transaction(self):
        return self._conn.transaction()


class AccountRenameIsOneMoveTests(unittest.TestCase):
    """Moving an account to the id the fresh link minted is eleven
    statements on an AUTOCOMMIT connection, so without a transaction each
    one lands on its own. A crash partway (deploy, OOM, dropped connection)
    then leaves the tenant holding BOTH accounts with the children split
    across them — double-counted or missing in every money aggregate, with
    nothing raised anywhere."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")

    def test_a_crash_partway_leaves_exactly_one_account(self):
        c = self.conn
        real = adopt.ACCOUNT_REFS
        with mock.patch.object(adopt, "ACCOUNT_REFS",
                               real + (("no_such_ref_table", "account_id"),)):
            with self.assertRaises(psycopg.Error):
                adopt._rewrite_account_id(c, "old-acct", "new-acct",
                                          "new-item")
        self.assertIsNone(c.execute(
            "SELECT 1 FROM accounts WHERE id='new-acct'").fetchone())
        self.assertEqual(c.execute(
            "SELECT item_id FROM accounts WHERE id='old-acct'"
        ).fetchone()["item_id"], "old-item")
        self.assertEqual(c.execute(
            "SELECT account_id FROM transactions WHERE id='old-1'"
        ).fetchone()["account_id"], "old-acct")

    def test_an_edit_cannot_land_inside_the_rename_and_be_deleted(self):
        """Atomicity is not the whole hazard: the copy is a SNAPSHOT and the
        delete drops whatever the row holds at the end, so a web edit
        committing in between is carried by neither and destroyed by the
        delete — silently, after the API already answered 200. Adoption runs
        on the synchronous link-exchange path while the household is using
        the app, so the window is real, and everything an account carries is
        hand-set: the display name, the owner label, the type override.

        Locking the row first makes that writer wait for the rename and then
        find no such id. What it is told still differs by door — POST
        /api/accounts/{id}/owner checks its rowcount and answers 404, while
        /api/accounts/rename and /api/accounts/classify answer 200 for an id
        that no longer exists (a web-layer gap, not this one). Either way the
        lock is what stops the edit landing on a row already condemned.
        """
        c2 = _second_connection(self, self.conn)
        c2.execute("SET statement_timeout = '750ms'")
        blocked = []

        def rename_from_the_web():
            try:
                c2.execute("UPDATE accounts SET display_name='Joint chequing' "
                           "WHERE id='old-acct'")
                blocked.append(False)
            except psycopg.errors.QueryCanceled:
                blocked.append(True)

        proxy = _AfterTheCopy(self.conn, rename_from_the_web,
                              after="INSERT INTO accounts")
        self.assertTrue(adopt._rewrite_account_id(proxy, "old-acct",
                                                  "new-acct", "new-item"))
        self.assertEqual(blocked, [True],
                         "the concurrent edit was not serialized against "
                         "the rename — it lands on a row about to be "
                         "deleted and is lost with no trace")
        # the rename itself completed, and the rename that could not land is
        # not silently sitting on a row nobody can reach any more
        self.assertIsNone(c2.execute(
            "SELECT 1 FROM accounts WHERE id='old-acct'").fetchone())
        self.assertIsNone(c2.execute(
            "SELECT display_name AS d FROM accounts "
            "WHERE id='new-acct'").fetchone()["d"])

    def test_an_account_removed_before_the_lock_adopts_nothing(self):
        """The candidate list is read on an autocommit connection, so the
        row can be gone by the time the rename takes its lock. Nothing moved
        means no adoption — recording one for an account that does not exist
        would hold the one-shot backfill guard open on a phantom id."""
        self.assertFalse(adopt._rewrite_account_id(
            self.conn, "no-such-acct", "new-acct", "new-item"))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id='new-acct'").fetchone())


if __name__ == "__main__":
    unittest.main()
