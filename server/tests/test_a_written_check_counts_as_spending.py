"""A paper check the bank filed as a transfer out counts as spending, and the
ledger asks once whom it paid.

A bank often reports a written check as TRANSFER_OUT at low confidence: the
money left, nothing says to whom, so it counted nowhere and the month read
cheaper than it was. The categorization pass files such a check under
"Checks" (llm_categorize.check_classify) — spending by every predicate,
unchanged — while a person's word, a rule's verdict and a check deposit stay
as they are. The row then says it needs a payee until somebody answers; the
answer is written as its note and run through the check-image machinery.
specs/ledger-figures.md.

Every amount, payee and check number is invented.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import llm_categorize, reporting
from oikonome.sync import base, reanchor
from oikonome.web import data

from .util import (TODAY, _ensure_db, add_txn, make_db, seed_accounts,
                   write_config)


def _check(conn, tid, amount, number, *, primary="TRANSFER_OUT",
           source="plaid", override=None, override_source=None):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name,"
        " category_primary, category_source, category_override,"
        " override_source, check_number, removed, raw) VALUES"
        " (%s,'chk',%s,%s,%s,%s,%s,%s,%s,%s,0,'{}'::jsonb)",
        (tid, TODAY, amount, f"CHECK PAID #{number}", primary, source,
         override, override_source, number))
    return tid


class AWrittenCheckCountsAsSpending(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _row(self, tid):
        return data.transactions_by_ids(self.conn, [tid])[0]

    def test_a_check_filed_as_a_transfer_is_spending_under_checks(self):
        _check(self.conn, "c1", 140.00, "21")
        before = reporting.flows(self.conn, ids=["c1"])["out"]
        self.assertEqual(llm_categorize.check_classify(self.conn), 1)
        row = self._row("c1")
        self.assertEqual(before, 0.0)
        self.assertEqual(row["category"], "Checks")
        self.assertTrue(row["counts_as_spend"])
        self.assertEqual(reporting.flows(self.conn, ids=["c1"])["out"], 140.0)
        self.assertTrue(row["needs_payee"])

    def test_a_persons_word_a_rule_and_a_deposit_are_left_alone(self):
        _check(self.conn, "pin", 50.0, "22", override="TRANSFER_OUT",
               override_source="user")
        _check(self.conn, "cleared", 50.0, "23", override_source="user")
        _check(self.conn, "flow", 50.0, "24", source="flow")
        _check(self.conn, "dep", -50.0, "25", primary="TRANSFER_IN")
        self.assertEqual(llm_categorize.check_classify(self.conn), 0)
        for tid in ("pin", "cleared", "flow", "dep"):
            self.assertNotEqual(self._row(tid)["category"], "Checks", tid)

    def test_an_answer_stops_the_question_and_another_note_does_not(self):
        _check(self.conn, "noted", 75.0, "26")
        _check(self.conn, "answered", 75.0, "28")
        llm_categorize.check_classify(self.conn)
        data.set_note(self.conn, "noted", "garden club dues")
        data.set_note(self.conn, "answered", "paid to Fern Hollow Club")
        # a note that names nobody leaves the question open
        self.assertTrue(self._row("noted")["needs_payee"])
        self.assertFalse(self._row("answered")["needs_payee"])

    def _rule(self, payee, cat):
        self.conn.execute("INSERT INTO merchant_categories (merchant,"
                          " category_primary, source) VALUES (%s, %s, 'user')"
                          " ON CONFLICT DO NOTHING", (payee, cat))

    def _restamp(self, tid, number):
        """The feed re-reporting the check: Plaid's 'modified' upsert."""
        base.upsert_transactions(self.conn, [base.Transaction(
            id=tid, account_id="chk", date=TODAY, amount=95.0,
            name=f"CHECK PAID #{number}", category_primary="TRANSFER_OUT",
            category_plaid="TRANSFER_OUT", raw={"check_number": number})])

    def test_an_answer_survives_the_feed_re_stamping_the_check(self):
        self._rule("Cedar Row Lawn", "GENERAL_SERVICES")
        _check(self.conn, "resync", 95.0, "41")
        llm_categorize.check_classify(self.conn)
        data.set_note(self.conn, "resync", "paid to Cedar Row Lawn")
        llm_categorize.categorize_check_payee(self.conn, "resync",
                                              "Cedar Row Lawn")
        self._restamp("resync", "41")
        self.assertEqual(self._row("resync")["category_source"], "plaid")
        llm_categorize.categorize_new(self.conn, limit=0)
        row = self._row("resync")
        self.assertEqual(row["category"], "GENERAL SERVICES")
        self.assertFalse(row["needs_payee"])

    def test_an_answer_survives_a_re_anchor_onto_a_renumbered_twin(self):
        self._rule("Cedar Row Lawn", "GENERAL_SERVICES")
        _check(self.conn, "old", 95.0, "42")
        llm_categorize.check_classify(self.conn)
        data.set_note(self.conn, "old", "paid to Cedar Row Lawn")
        llm_categorize.categorize_check_payee(self.conn, "old",
                                              "Cedar Row Lawn")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id='old'")
        _check(self.conn, "new", 95.0, "42")
        self.assertEqual(reanchor.reanchor_stranded(self.conn)["moved"], 1)
        llm_categorize.categorize_new(self.conn, limit=0)
        row = self._row("new")
        self.assertEqual(row["note"], "paid to Cedar Row Lawn")
        self.assertEqual(row["category"], "GENERAL SERVICES")

    def test_an_answer_landing_after_a_re_stamp_still_files_the_check(self):
        # the door commits the note, a sync re-stamps the row, and only
        # then does the categorise run
        self._rule("Cedar Row Lawn", "GENERAL_SERVICES")
        _check(self.conn, "race", 95.0, "43")
        llm_categorize.check_classify(self.conn)
        data.set_note(self.conn, "race", "paid to Cedar Row Lawn")
        self._restamp("race", "43")
        self.assertEqual(llm_categorize.categorize_check_payee(
            self.conn, "race", "Cedar Row Lawn"), "GENERAL_SERVICES")
        self.assertEqual(self._row("race")["category"], "GENERAL SERVICES")

    def _account(self, aid, kind, subtype):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES (%s,'it1',%s,%s,%s,0)",
            (aid, f"Own {subtype}", kind, subtype))

    def test_a_check_into_the_households_own_account_stays_a_transfer(self):
        # a check written from checking to fund the household's own IRA,
        # deposited there a few days before it clears
        self._account("ira", "investment", "ira")
        _check(self.conn, "toira", 1000.0, "51")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1000.0,
                "CONTRIBUTION", primary=None, account="ira")
        llm_categorize.check_classify(self.conn)
        row = self._row("toira")
        self.assertNotEqual(row["category"], "Checks")
        self.assertFalse(row["counts_as_spend"])
        self.assertFalse(row["needs_payee"])
        self.assertEqual(reporting.flows(self.conn, ids=["toira"])["out"], 0.0)

    def test_a_check_filed_before_its_own_account_deposit_arrived_is_unfiled(self):
        self._account("sav", "depository", "savings")
        _check(self.conn, "tosav", 400.0, "52")
        llm_categorize.check_classify(self.conn)
        self.assertEqual(self._row("tosav")["category"], "Checks")
        add_txn(self.conn, TODAY, -400.0, "DEPOSIT", primary="TRANSFER_IN",
                account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertFalse(self._row("tosav")["counts_as_spend"])

    def test_a_same_size_paycheck_elsewhere_does_not_unfile_a_check(self):
        self._account("sav", "depository", "savings")
        _check(self.conn, "rent", 400.0, "53")
        add_txn(self.conn, TODAY, -400.0, "PAYROLL", primary="INCOME",
                account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertEqual(self._row("rent")["category"], "Checks")

    def test_a_deposit_its_own_transfer_already_pairs_takes_no_check(self):
        # rent paid by check, and the same sum moved to savings online that
        # week: the savings deposit pairs with its own transfer out, so the
        # rent check stays spending
        self._account("sav", "depository", "savings")
        _check(self.conn, "rent", 1500.0, "61")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 1500.0,
                "ONLINE TRANSFER TO SAVINGS", primary="TRANSFER_OUT",
                account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -1500.0,
                "ONLINE TRANSFER FROM CHECKING", primary="TRANSFER_IN",
                account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertEqual(self._row("rent")["category"], "Checks")
        self.assertTrue(self._row("rent")["counts_as_spend"])

    def test_an_uncategorised_bank_deposit_does_not_unfile_a_check(self):
        self._account("sav", "depository", "savings")
        _check(self.conn, "roof", 800.0, "62")
        add_txn(self.conn, TODAY, -800.0, "DEPOSIT", primary=None,
                account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertEqual(self._row("roof")["category"], "Checks")

    def test_an_answered_check_is_never_read_as_a_transfer(self):
        self._account("sav", "depository", "savings")
        _check(self.conn, "contractor", 900.0, "63")
        data.set_note(self.conn, "contractor", "paid to Fourth Street Roofing")
        add_txn(self.conn, TODAY, -900.0, "DEPOSIT", primary="TRANSFER_IN",
                account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertTrue(self._row("contractor")["counts_as_spend"])

    def test_a_check_read_as_a_transfer_goes_back_when_its_deposit_leaves(self):
        self._account("sav", "depository", "savings")
        _check(self.conn, "back", 450.0, "64")
        dep = add_txn(self.conn, TODAY, -450.0, "DEPOSIT",
                      primary="TRANSFER_IN", account="sav")
        llm_categorize.check_classify(self.conn)
        self.assertFalse(self._row("back")["counts_as_spend"])
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (dep,))
        llm_categorize.check_classify(self.conn)
        self.assertEqual(self._row("back")["category"], "Checks")

    def test_an_answer_naming_a_transfer_takes_the_check_out_of_spending(self):
        _check(self.conn, "xfer", 300.0, "54")
        llm_categorize.check_classify(self.conn)
        data.set_note(self.conn, "xfer", "paid to Transfer to savings")
        got = llm_categorize.categorize_check_payee(self.conn, "xfer",
                                                    "Transfer to savings")
        # the door's reply names what the answer made of the check
        self.assertTrue(got and got.startswith("TRANSFER"), got)
        llm_categorize.check_classify(self.conn)
        self.assertFalse(self._row("xfer")["counts_as_spend"])

    def test_a_read_check_image_names_the_payee_and_finds_the_check(self):
        _check(self.conn, "img", 210.0, "27")
        self.conn.execute(
            "INSERT INTO receipts (txn_id, image, mime, kind, status, parsed,"
            " parsed_at) VALUES ('img', '\\x00', 'image/png', 'check',"
            " 'parsed', '{\"payee\": \"Birch Lane Garage\"}'::jsonb, now())")
        llm_categorize.check_classify(self.conn)
        row = self._row("img")
        self.assertEqual(row["check_payee"], "Birch Lane Garage")
        # the image answered the question
        self.assertFalse(row["needs_payee"])
        rows = data.search_transactions(self.conn, "birch lane")[0]
        self.assertEqual([r["id"] for r in rows], ["img"])


class TheCheckPayeeDoor(unittest.TestCase):
    """POST /api/transactions/{id}/check-payee."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup", data={
            "email": f"checks-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _conn(self):
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        return conn

    def test_the_answer_is_the_note_and_the_question_is_not_asked_again(self):
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 90.0, "31")
        llm_categorize.check_classify(conn)
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Maple Street Music"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["note"], "paid to Maple Street Music")
        row = data.transactions_by_ids(conn, [tid])[0]
        self.assertEqual(row["note"], "paid to Maple Street Music")
        self.assertFalse(row["needs_payee"])

    def test_an_answer_goes_ahead_of_a_note_the_check_already_had(self):
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 45.0, "33")
        llm_categorize.check_classify(conn)
        data.set_note(conn, tid, "spring recital")
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Maple Street Music"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["note"],
                         "paid to Maple Street Music\nspring recital")
        self.assertFalse(data.transactions_by_ids(conn, [tid])[0]
                         ["needs_payee"])

    def test_a_payee_with_a_rule_files_the_check_under_its_category(self):
        conn = self._conn()
        conn.execute("INSERT INTO merchant_categories (merchant,"
                     " category_primary, source) VALUES"
                     " ('Willow Tutoring', 'GENERAL_SERVICES', 'user')"
                     " ON CONFLICT DO NOTHING")
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 60.0, "32")
        llm_categorize.check_classify(conn)
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Willow Tutoring"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["category"], "GENERAL_SERVICES")
        self.assertEqual(data.transactions_by_ids(conn, [tid])[0]["category"],
                         "GENERAL SERVICES")

    def test_a_new_answer_replaces_the_doors_line_and_keeps_the_rest(self):
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 45.0, "34")
        llm_categorize.check_classify(conn)
        data.set_note(conn, tid, "spring recital")
        self.client.post(f"/api/transactions/{tid}/check-payee",
                         json={"payee": "Maple Street Musc"})
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Maple Street Music"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["note"],
                         "paid to Maple Street Music\nspring recital")

    def test_a_hand_written_paid_to_line_is_kept_under_the_answer(self):
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 45.0, "35")
        llm_categorize.check_classify(conn)
        data.set_note(conn, tid, "Paid to Jane for May rent, unit 2")
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Jane Rowan"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["note"], "paid to Jane Rowan\n"
                         "Paid to Jane for May rent, unit 2")

    def test_an_answer_that_would_overflow_the_note_is_refused_whole(self):
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 45.0, "36")
        llm_categorize.check_classify(conn)
        long = "x" * (data.NOTE_MAX - 5)
        data.set_note(conn, tid, long)
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Maple Street Music"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(data.transactions_by_ids(conn, [tid])[0]["note"],
                         long)

    def test_a_note_saved_while_the_answer_is_written_is_kept(self):
        import threading
        import time
        conn = self._conn()
        tid = _check(conn, f"k-{uuid.uuid4().hex[:6]}", 45.0, "37")
        llm_categorize.check_classify(conn)
        other = tenancy.tenant_connect(self.tid)
        self.addCleanup(other.close)
        out = {}
        with other.transaction():
            # the note door's write, not yet committed
            data.set_note(other, tid, "May rent, unit 2")
            t = threading.Thread(target=lambda: out.setdefault(
                "r", self.client.post(f"/api/transactions/{tid}/check-payee",
                                      json={"payee": "Jane Rowan"})))
            t.start()
            time.sleep(0.5)
        t.join(10)
        self.assertEqual(out["r"].status_code, 200, out["r"].text)
        self.assertEqual(data.transactions_by_ids(conn, [tid])[0]["note"],
                         "paid to Jane Rowan\nMay rent, unit 2")

    def test_a_row_that_is_not_a_check_is_refused(self):
        conn = self._conn()
        tid = f"n-{uuid.uuid4().hex[:6]}"
        conn.execute("INSERT INTO transactions (id, account_id, date, amount,"
                     " name, removed, raw) VALUES"
                     " (%s,'chk',%s,10,'SHOP',0,'{}'::jsonb)", (tid, TODAY))
        r = self.client.post(f"/api/transactions/{tid}/check-payee",
                             json={"payee": "Someone"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
