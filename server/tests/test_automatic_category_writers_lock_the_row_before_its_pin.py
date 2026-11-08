"""The automatic category writers take a charge's row before its pin, the
order every person-facing writer keeps, so they wait for each other
instead of deadlocking.

A person's edit (one row, a bulk selection, a mailed tap) locks the
`transactions` row, then writes its `manual_categories` pin. The bill
pass and an unlink that hands a person's earlier pin back wrote the pin
first and updated the row after. With the two orders crossed on one
charge, each side held what the other wanted and Postgres killed one:
the bill pass's whole stamp transaction was lost (a server error on a
bill save), or the person's edit was."""

import datetime as dt
import threading
import time
import unittest

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.web import data

from .util import TODAY, add_bill, add_txn, make_db, write_config

PIN = """INSERT INTO manual_categories (transaction_id, category, set_at,
                                        bill_id)
         VALUES (%s, %s, now(), NULL)
         ON CONFLICT (tenant_id, transaction_id)
         DO UPDATE SET category = EXCLUDED.category, bill_id = NULL,
                       link_made = FALSE, link_prior = NULL"""


class AutomaticWritersLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _while_a_person_edits(self, row: str, writer):
        """A person's edit locks `row` the way set_category does; `writer`
        starts on its own connection, and once it has had time to take
        whatever it takes first the person writes the row's pin and
        commits. Held past Postgres's deadlock_timeout (1 s by default)
        so a crossed order is caught."""
        errors = {}
        person = tenancy.tenant_connect(self.tid)
        self.addCleanup(person.close)

        def run():
            other = tenancy.tenant_connect(self.tid)
            try:
                with other.transaction():
                    writer(other)
            except Exception as e:                       # noqa: BLE001
                errors["writer"] = type(e).__name__
            finally:
                other.close()

        th = threading.Thread(target=run)
        try:
            with person.transaction():
                person.execute("SELECT 1 FROM transactions WHERE id=%s "
                               "FOR UPDATE", (row,))
                th.start()
                time.sleep(0.5)
                person.execute(PIN, (row, "GENERAL_MERCHANDISE"))
                person.execute(
                    "UPDATE transactions SET category_override=%s, "
                    "override_source='user' WHERE id=%s",
                    ("GENERAL_MERCHANDISE", row))
                time.sleep(1.5)
        except Exception as e:                           # noqa: BLE001
            errors["person"] = type(e).__name__
        th.join(30)
        return errors

    def _row(self, tid):
        return self.conn.execute(
            """SELECT t.category_override, t.override_source, m.bill_id
                 FROM transactions t
                 LEFT JOIN manual_categories m ON m.transaction_id = t.id
                WHERE t.id=%s""", (tid,)).fetchone()

    def _bill(self):
        add_bill(self.conn, "Pinewood Telecom", 80.00, frequency="MONTHLY",
                 next_due=TODAY, merchant="pinewood telecom",
                 txn_category="RENT_AND_UTILITIES",
                 merchants=["Pinewood Telecom"])

    def _charge(self, days_ago=3):
        tid = add_txn(self.conn, TODAY - dt.timedelta(days=days_ago), 80.00,
                      "PINEWOOD TELECOM", merchant="Pinewood Telecom",
                      primary="GENERAL_SERVICES")
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) "
            "VALUES ('Pinewood Telecom','layer1') RETURNING id").fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (mid, tid))
        return tid

    def test_the_bill_pass_stamping_a_new_charge_waits_for_a_persons_edit(self):
        self._bill()
        charge = self._charge()
        # not stamped yet: the pass under test is the one that stamps it
        self.assertEqual(self._row(charge)["override_source"], None)
        errors = self._while_a_person_edits(
            charge, lambda c: bills.apply_txn_categories(c, days=120))
        self.assertEqual(errors, {})
        # the person's answer stands; the pass left it alone
        r = self._row(charge)
        self.assertEqual((r["category_override"], r["override_source"],
                          r["bill_id"]), ("GENERAL_MERCHANDISE", "user", None))

    def test_the_bill_pass_reverting_a_stamp_waits_for_a_persons_edit(self):
        self._bill()
        charge = self._charge()
        bills.apply_txn_categories(self.conn)
        self.assertEqual(self._row(charge)["override_source"], "bill")
        # the bill's category is cleared: the next pass takes its stamp back
        self.conn.execute(
            "UPDATE bills SET raw = raw - 'txn_category'")
        errors = self._while_a_person_edits(
            charge, lambda c: bills.apply_txn_categories(c))
        self.assertEqual(errors, {})
        r = self._row(charge)
        self.assertEqual((r["category_override"], r["override_source"],
                          r["bill_id"]), ("GENERAL_MERCHANDISE", "user", None))

    def test_an_unlink_handing_back_a_persons_pin_waits_for_their_edit(self):
        charge = add_txn(self.conn, TODAY.replace(day=5), 40.00, "CLINIC")
        dep = add_txn(self.conn, TODAY.replace(day=6), -40.00, "INSURER")
        data.set_category(self.conn, charge, "MEDICAL")
        out = data.link_reimbursement(self.conn, charge, dep)
        self.assertNotIn("error", out, out)
        self.assertEqual(self._row(charge)["category_override"],
                         "TRANSFER_OUT")
        errors = self._while_a_person_edits(
            charge, lambda c: data._unlink_locked(c, charge, dep))
        self.assertEqual(errors, {})
        # the edit after the link is the person's: the unlink keeps it
        self.assertEqual(self._row(charge)["category_override"],
                         "GENERAL_MERCHANDISE")


if __name__ == "__main__":
    unittest.main()
