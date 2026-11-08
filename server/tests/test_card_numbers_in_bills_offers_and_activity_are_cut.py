"""A card number never stands whole in a bill, an offer or the activity log.

Detection names a new bill offer, and a fee to attach, in the ledger's own
words, and a feed row keeps the bank's whole number ("ACH 4000 1234 5678
9010 ACME"): approved, the offer made a bill named by the number, and the
activity log quoted it beside the ids the keying pass re-ids to hide it.
The pass and the log now cut what they write, and a migration cuts what
they stored, moving every reference that names a bill by its payee or its
id. What a person typed as a bill's name is theirs and stays. Every number
here is invented.
"""
import datetime as dt
import json
import pathlib
import unittest

from oikonome.engine import activity, bills, budget
from oikonome.sync import base, card_number

from .util import TODAY, add_bill, add_txn, make_db, write_config

MIGRATION = (pathlib.Path(base.__file__).parent.parent / "db" / "migrations"
             / "206_card_numbers_in_bills_offers_and_the_activity_log_are_cut.sql")
WHOLE = "ACH 4000 1234 5678 9010 FABRIKAM"
CUT = "ACH …9010 FABRIKAM"
DOTTED = "Visa 4000.1234.5678.9010"          # a layout 203/204 did not read
DOTTED_CUT = "Visa …9010"


def _months_back(n, day=5):
    y, m = TODAY.year, TODAY.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, day)


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _offers(self, kind="add"):
        return [r["payee"] for r in self.conn.execute(
            "SELECT payee FROM bill_proposals WHERE kind=%s", (kind,))]


class DetectionNamesNoWholeNumber(_Db):
    def test_a_series_the_feed_spells_with_a_number_is_offered_cut(self):
        for n in (4, 3, 2, 1):
            add_txn(self.conn, _months_back(n), 12.99, WHOLE)
        bills.run(self.conn, TODAY)
        offers = self._offers()
        self.assertIn(CUT, offers)
        self.assertFalse(any(card_number.holds_number(p) for p in offers),
                         offers)

    def test_approving_it_makes_a_bill_with_the_cut_name(self):
        for n in (4, 3, 2, 1):
            add_txn(self.conn, _months_back(n), 12.99, WHOLE)
        bills.run(self.conn, TODAY)
        pid = self.conn.execute(
            "SELECT id FROM bill_proposals WHERE kind='add' AND payee=%s",
            (CUT,)).fetchone()["id"]
        bills.apply_proposal(self.conn, pid, "approve")
        row = self.conn.execute("SELECT id, payee FROM bills").fetchone()
        self.assertEqual((row["id"], row["payee"]),
                         (bills._slug(CUT), CUT))
        # and the bill still matches the feed rows that spell it whole
        bills.run(self.conn, TODAY)
        self.assertEqual(self._offers(), [CUT])

    def test_a_plaid_streams_description_is_cut_in_the_evidence(self):
        bills.propose_from_plaid_streams(self.conn, [{
            "stream_id": "s-1", "direction": "outflow",
            "merchant": None, "description": WHOLE, "amount": 12.99,
            "frequency": "MONTHLY", "is_active": True, "status": "MATURE",
            "first_date": "2026-01-05", "last_date": "2026-07-05",
            "predicted_next_date": "2026-08-05",
            "transaction_ids": ["a", "b", "c"]}], TODAY)
        ev = self.conn.execute(
            "SELECT payee, evidence FROM bill_proposals").fetchone()
        self.assertEqual(ev["evidence"]["description"], CUT)
        self.assertFalse(card_number.holds_number(ev["payee"]))

    def test_an_offer_naming_a_merchant_keeps_its_stored_name(self):
        self.assertTrue(bills._insert_proposal(
            self.conn, pid="prop:m", kind="merchant", payee=DOTTED,
            amount=10, evidence={"bill_payee": DOTTED,
                                 "merchant_name": DOTTED}))
        p = self.conn.execute("SELECT payee, evidence FROM bill_proposals"
                              ).fetchone()
        self.assertEqual(p["payee"], DOTTED)
        self.assertEqual(p["evidence"]["merchant_name"], DOTTED)


class TheLogQuotesNoWholeNumber(_Db):
    def test_label_and_sentence_are_cut_and_the_ids_stay(self):
        # a bill's act is targeted by its payee, as every door gives it: a
        # name, cut with the label; the ids its detail lists stay
        rid = activity.record(
            self.conn, "sam@example.test", "bill", "edited",
            target=WHOLE, label=WHOLE,
            detail={"before": {"payee": WHOLE, "amount": 12.99},
                    "after": {"payee": "Fabrikam", "amount": 12.99},
                    "ids": ["csv:4000123456789010"]})
        row = self.conn.execute(
            "SELECT label, summary, target, detail FROM activity_log"
            " WHERE id=%s", (rid,)).fetchone()
        self.assertEqual(row["label"], CUT)
        self.assertFalse(card_number.holds_number(row["summary"]),
                         row["summary"])
        self.assertEqual(row["target"], CUT)
        self.assertEqual(row["detail"]["before"]["payee"], CUT)
        self.assertEqual(row["detail"]["ids"], ["csv:4000123456789010"])

    def test_a_transactions_id_target_stays_whole(self):
        # a transaction is targeted by its id, which the keying pass finds
        # a re-id'd row by
        rid = activity.record(
            self.conn, "sam@example.test", "category", "set",
            target="csv:4000123456789010", label=WHOLE,
            detail={"before": None, "after": "SHOPPING",
                    "merchant": WHOLE})
        row = self.conn.execute(
            "SELECT target, detail FROM activity_log WHERE id=%s",
            (rid,)).fetchone()
        self.assertEqual(row["target"], "csv:4000123456789010")
        self.assertEqual(row["detail"]["merchant"], CUT)

    def test_a_number_a_quote_would_clip_is_cut_before_it_is_clipped(self):
        # clipped whole it would read "…Visa 4000.1234.5678.9…": fourteen
        # digits no longer in a card's layout
        long_name = "Fabrikam Warranty " + DOTTED
        rid = activity.record(
            self.conn, "sam@example.test", "bill", "edited",
            label="Fabrikam",
            detail={"before": {"payee": long_name, "amount": 12.99},
                    "after": {"payee": "Fabrikam", "amount": 12.99}})
        summary = self.conn.execute(
            "SELECT summary FROM activity_log WHERE id=%s",
            (rid,)).fetchone()["summary"]
        self.assertIn("renamed from", summary)
        self.assertNotIn("4000.1234", summary)


class TheMigrationCutsWhatIsStored(_Db):
    def _migrate(self):
        self.conn.execute(MIGRATION.read_text())

    def _store_offer(self, pid, kind, payee, evidence):
        # written past the pass, as an offer filed before it cut was
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, evidence,"
            " status) VALUES (%s,%s,%s,12.99,%s::jsonb,'pending')",
            (pid, kind, payee, json.dumps(evidence)))

    def _detected_bill(self, payee):
        add_bill(self.conn, payee, 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        return bills._slug(payee)

    def _snapshot(self):
        def rows(sql):
            return sorted(tuple(sorted((k, str(v)) for k, v in dict(r).items()
                                       if k not in ("synced_at",)))
                          for r in self.conn.execute(sql))
        return (rows("SELECT id, payee FROM bills"),
                rows("SELECT id, kind, payee, evidence FROM bill_proposals"),
                rows("SELECT transaction_id, bill_id FROM manual_categories"),
                rows("SELECT config FROM tenant_settings"),
                rows("SELECT label, summary, target, detail FROM activity_log"))

    def test_a_detected_bill_moves_with_everything_that_names_it(self):
        old_id = self._detected_bill(DOTTED)
        txn = add_txn(self.conn, _months_back(1), 12.99, DOTTED)
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category, bill_id)"
            " VALUES (%s, 'SUBSCRIPTIONS', %s)", (txn, old_id))
        self._store_offer("prop:drift", "amount_drift", DOTTED, {})
        self._store_offer("prop:fee", "attach", "LATE FEE",
                          {"bill_payee": DOTTED})
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = [DOTTED, "Other"]
            cfg["occurrence_caps"] = {DOTTED: 1, "Other": 2}
        self._migrate()
        new_id = bills._slug(DOTTED_CUT)
        self.assertEqual(
            [tuple(r.values()) for r in self.conn.execute(
                "SELECT id, payee FROM bills")], [(new_id, DOTTED_CUT)])
        self.assertEqual(self.conn.execute(
            "SELECT bill_id FROM manual_categories").fetchone()["bill_id"],
            new_id)
        offers = {r["id"]: r for r in self.conn.execute(
            "SELECT id, payee, evidence FROM bill_proposals")}
        self.assertEqual(offers["prop:drift"]["payee"], DOTTED_CUT)
        self.assertEqual(offers["prop:fee"]["evidence"]["bill_payee"],
                         DOTTED_CUT)
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["disabled_bills"], [DOTTED_CUT, "Other"])
        self.assertEqual(cfg["occurrence_caps"], {DOTTED_CUT: 1, "Other": 2})
        # the bill is the same bill to every door that looks it up
        self.assertIsNotNone(bills.get_bill(self.conn, DOTTED_CUT))

    def test_a_name_a_person_typed_stays(self):
        add_bill(self.conn, DOTTED, 12.99, source="manual",
                 next_due=TODAY + dt.timedelta(days=10))
        self._store_offer("prop:drift", "amount_drift", DOTTED, {})
        self._migrate()
        self.assertEqual(self.conn.execute(
            "SELECT payee FROM bills").fetchone()["payee"], DOTTED)
        self.assertEqual(self.conn.execute(
            "SELECT payee FROM bill_proposals").fetchone()["payee"], DOTTED)

    def test_a_cut_name_another_bill_holds_is_left_as_it_is(self):
        self._detected_bill(DOTTED)
        self._detected_bill(DOTTED_CUT)
        self._migrate()
        self.assertEqual(sorted(r["payee"] for r in self.conn.execute(
            "SELECT payee FROM bills")), sorted([DOTTED, DOTTED_CUT]))

    def test_offers_own_labels_are_cut(self):
        self._store_offer("prop:add", "add", WHOLE,
                          {"description": WHOLE, "source": "plaid_recurring"})
        self._store_offer("prop:att", "attach", "FEE " + DOTTED,
                          {"bill_payee": "Gone " + DOTTED})
        self._migrate()
        offers = {r["id"]: r for r in self.conn.execute(
            "SELECT id, payee, evidence FROM bill_proposals")}
        self.assertEqual(offers["prop:add"]["payee"], CUT)
        self.assertEqual(offers["prop:add"]["evidence"]["description"], CUT)
        self.assertEqual(offers["prop:att"]["payee"], "FEE " + DOTTED_CUT)
        self.assertEqual(offers["prop:att"]["evidence"]["bill_payee"],
                         "Gone " + DOTTED_CUT)

    def test_a_merchant_offer_follows_its_merchant_only(self):
        self.conn.execute("INSERT INTO merchants (name) VALUES (%s)",
                          (DOTTED_CUT,))
        # a person named this one, number and all: 204 leaves it
        self.conn.execute("INSERT INTO merchants (name, name_source)"
                          " VALUES (%s, 'manual')",
                          ("Pump 5500.0000.0000.0004",))
        self._store_offer("prop:m1", "merchant", DOTTED,
                          {"bill_payee": "X", "merchant_name": DOTTED})
        self._store_offer("prop:m2", "merchant", "Pump 5500.0000.0000.0004",
                          {"bill_payee": "X",
                           "merchant_name": "Pump 5500.0000.0000.0004"})
        self._migrate()
        offers = {r["id"]: r for r in self.conn.execute(
            "SELECT id, payee, evidence FROM bill_proposals")}
        self.assertEqual(offers["prop:m1"]["payee"], DOTTED_CUT)
        self.assertEqual(offers["prop:m1"]["evidence"]["merchant_name"],
                         DOTTED_CUT)
        # a person's merchant still holds the whole name: the offer keeps it
        self.assertEqual(offers["prop:m2"]["payee"],
                         "Pump 5500.0000.0000.0004")

    def test_the_activity_logs_label_and_sentence_are_cut(self):
        self.conn.execute(
            "INSERT INTO activity_log (actor, kind, action, target, label,"
            " summary, detail) VALUES ('sam@example.test', 'bill', 'edited',"
            " 'csv:4000123456789010', %s, %s, %s::jsonb)",
            (WHOLE, f"edited the bill {WHOLE}",
             '{"ids": ["csv:4000123456789010"]}'))
        self._migrate()
        row = self.conn.execute(
            "SELECT label, summary, target, detail FROM activity_log"
        ).fetchone()
        self.assertEqual((row["label"], row["summary"]),
                         (CUT, f"edited the bill {CUT}"))
        self.assertEqual(row["target"], "csv:4000123456789010")
        self.assertEqual(row["detail"]["ids"], ["csv:4000123456789010"])

    def test_a_layout_the_frozen_cut_missed_is_cut_on_a_merchant(self):
        self.conn.execute("INSERT INTO merchants (name) VALUES (%s)",
                          ("Venmo — 4000/1234/5678/9010",))
        self._migrate()
        self.assertEqual(self.conn.execute(
            "SELECT name FROM merchants").fetchone()["name"], "Venmo — …9010")

    def test_a_layout_the_frozen_cut_missed_is_cut_on_a_file_row(self):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " merchant_name, removed, raw) VALUES ('csv:abc', 'chk',"
            " '2026-03-10', -50, %s, %s, 0, %s::jsonb)",
            (DOTTED, DOTTED, '{"memo": "%s"}' % DOTTED))
        self._migrate()
        row = self.conn.execute(
            "SELECT name, merchant_name, raw FROM transactions"
            " WHERE id='csv:abc'").fetchone()
        self.assertEqual((row["name"], row["merchant_name"],
                          row["raw"]["memo"]),
                         (DOTTED_CUT, DOTTED_CUT, DOTTED_CUT))

    def test_applied_twice_it_changes_nothing(self):
        self._detected_bill(DOTTED)
        self._store_offer("prop:add", "add", WHOLE, {"description": WHOLE})
        self.conn.execute(
            "INSERT INTO activity_log (actor, kind, action, label, summary)"
            " VALUES ('sam@example.test', 'bill', 'edited', %s, %s)",
            (WHOLE, WHOLE))
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = [DOTTED]
        self._migrate()
        once = self._snapshot()
        self._migrate()
        self.assertEqual(self._snapshot(), once)


if __name__ == "__main__":
    unittest.main()
