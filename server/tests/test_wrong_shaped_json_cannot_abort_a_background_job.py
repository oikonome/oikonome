"""A JSONB value of the wrong shape must not take a whole job off the air.

`jsonb_array_elements` and `jsonb_array_length` do not skip a row whose
value is an object or a number — they raise, and the raise aborts the
statement that asked. `COALESCE(x, '[]'::jsonb)` reads like a guard for
that and is not one: it substitutes for a MISSING value, never for a
wrong-shaped one, and `raw ? 'key'` and `raw @> …` say nothing about shape
either.

The ledger search was hardened against this first, because a search that
answers nothing is a defect a household reports the same day. The jobs
here are the quiet half of the same seam, and quiet is worse: a nightly
email that dies on one bill just stops arriving, and nobody can tell that
from a calm week. Each test writes the shape a restore or a hand-edit can
still leave behind — the writers are validated now, so this is what an
older instance's rows look like — and asks the job to run over it.

The rule these protect is that the row with the bad value loses only that
value: the bill still appears with no companion fees, the transaction is
still considered and simply matches nothing, the import still lists.
"""

import unittest

from oikonome.engine import llm_categorize
from oikonome.engine.compat import as_date, jsonb
from oikonome.sync import batches
from oikonome.web import report

from .util import TODAY, make_db


class TheDailyEmailSurvivesABillWithAMalformedCompanionList(unittest.TestCase):
    """The due-soon block sums each bill's companion fees. One bill whose
    companion list is an object must not silence the household's mail."""

    def setUp(self):
        self.conn = make_db()
        self._bill("rec:bad", "Northwind Fiber", -90.00, TODAY,
                   {"companions": {"tokens": "equipment", "amount": 12.0}})
        self._bill("rec:good", "Riverton Power", -140.00, TODAY,
                   {"companions": [{"tokens": "service fee", "amount": 5.0}]})

    def tearDown(self):
        self.conn.close()

    def _bill(self, bill_id, payee, amount, due_on, raw):
        self.conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                   monthly_amount, due_on, category, active, is_completed, raw)
               VALUES (%s,'BILL',%s,%s,'MONTHLY',%s,%s,'RENT_AND_UTILITIES',
                       1,0,%s)""",
            (bill_id, payee, amount, amount, as_date(due_on), jsonb(raw)))

    def test_the_gather_answers_over_a_bill_whose_companions_are_an_object(self):
        due = {r["payee"]: r for r in report.gather(self.conn, TODAY)["due_soon"]}
        # the bad bill is still due, and is simply carrying no fee
        self.assertIn("Northwind Fiber", due)
        self.assertEqual(float(due["Northwind Fiber"]["fee"]), 0.0)
        # and the bill next to it keeps the fee it really has
        self.assertEqual(float(due["Riverton Power"]["fee"]), 5.0)

    def test_the_email_still_renders(self):
        # the whole point: the household's mail goes out. A gather that
        # raises here does not degrade the email, it cancels it.
        subject, plain, html = report.build(report.gather(self.conn, TODAY))
        self.assertTrue(subject)
        self.assertTrue(plain.strip())
        self.assertIn("daily budget", html.lower())


class TheOwnTransferPassSurvivesAMalformedCounterpartyList(unittest.TestCase):
    """The own-transfer pass runs inside every sync. A single transaction
    whose counterparty list is an object must not stop it classifying the
    rest — it is one UPDATE, so the raise loses the whole pass."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status) "
            "VALUES ('i_inv','plaid','Northwind Brokerage','ok')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) "
            "VALUES ('inv','i_inv','Northwind Autoportfolio',"
            "'investment','brokerage')")
        self._txn("bad", -1000, "NORTHWIND BRKRG ACH-XFER-0001",
                  {"counterparties": {"name": "Northwind Brokerage",
                                      "type": "financial_institution"}})
        self._txn("good", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  {"counterparties": [{"name": "Northwind Brokerage",
                                       "type": "financial_institution",
                                       "confidence_level": "VERY_HIGH"}]})

    def tearDown(self):
        self.conn.close()

    def _txn(self, tid, amount, name, raw):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES (%s,'chk',%s,%s,%s,%s,'INCOME',0,0,%s)""",
            (tid, as_date("2026-07-30"), amount, name, name, jsonb(raw)))

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_primary"]

    def test_the_pass_classifies_the_rest_of_the_ledger(self):
        self.assertEqual(1, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("TRANSFER_IN", self._cat("good"))
        # the unreadable row simply matched nothing; it was not corrupted
        self.assertEqual("INCOME", self._cat("bad"))


class TheImportLedgerSurvivesAMalformedBatchList(unittest.TestCase):
    """Batch ownership rides in `raw._batches`. The Import page counts what
    a rollback would take with it, and rollback walks the same key — both
    over every row in the household, so one bad value takes out the page
    and the rollback together."""

    def setUp(self):
        self.conn = make_db()
        self.batch = batches.create(self.conn, "csv", "chk", "june.csv")
        self._txn("owned", {"_batches": [self.batch], "_batch": self.batch})
        # the single id stored bare instead of in a one-element array —
        # `?|` and `@>` both accept a scalar here without complaint, so
        # nothing before the array read notices
        self._txn("bad", {"_batches": self.batch})
        batches.finish(self.conn, self.batch, 1)

    def tearDown(self):
        self.conn.close()

    def _txn(self, tid, raw):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES (%s,'chk',%s,-25,%s,%s,'GENERAL_MERCHANDISE',0,0,%s)""",
            (tid, as_date("2026-06-10"), tid, tid, jsonb(raw)))

    def test_the_import_list_still_counts_what_a_rollback_would_remove(self):
        rows = batches.recent(self.conn)
        self.assertEqual([self.batch], [r["id"] for r in rows])
        self.assertEqual(0, rows[0]["annotations"])

    def test_rollback_removes_its_own_rows_and_leaves_the_bad_one(self):
        batches.rollback(self.conn, self.batch)
        left = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions").fetchall()}
        self.assertEqual({"bad"}, left)

    def test_the_overlap_warning_walks_the_ledger_without_raising(self):
        later = batches.create(self.conn, "csv", "chk", "june-again.csv")
        self.conn.execute(
            "UPDATE import_batches SET created_at = now() + INTERVAL '1 day' "
            "WHERE id=%s", (later,))
        self._txn("shared", {"_batches": [self.batch, later],
                             "_batch": later})
        warning = batches._overlap_warning(self.conn, self.batch)
        self.assertIsNotNone(warning)
        self.assertIn("re-import", warning)


if __name__ == "__main__":
    unittest.main()
