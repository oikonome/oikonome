"""A restore into a household that already linked its banks keeps what
the PERSON decided over what the destination's machinery derived.

A restore merges into the tenant. Linking first means the destination
already holds a merchant for every aggregator entity (under ids of its
own), seed and model category rules, derived aliases, bill pins and
pending offers. Where the archive holds the person's decision for the same
key, the decision must win:

- an archived merchant whose aggregator entity is already live is the
  same merchant: its rename and its merge land on the live row, and every
  pointer at it (rows, aliases, other merchants' merges, merge rejections)
  follows to the live id instead of being dropped as dangling;
- a person's category rule replaces a seed/model rule for that merchant;
- a person's alias replaces a derived one for that descriptor;
- an offer still pending on the destination takes the archived answer;
- a person's category on a row both sides hold replaces a bill's.

A machine row on both sides, or a person's row already here, is kept.
"""

import csv
import io
import unittest
import zipfile
from unittest import mock

from oikonome.engine import llm_categorize
from oikonome.engine.merchant_merge import _sig
from oikonome.sync import restore

from .util import make_db, write_config


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in files.items():
            s = io.StringIO()
            keys: list = []
            for r in rows:
                keys += [k for k in r if k not in keys]
            w = csv.DictWriter(s, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow(r)
            z.writestr(name, s.getvalue())
    return buf.getvalue()


LIVE = "11111111-1111-4111-8111-111111111111"
ARCH = "22222222-2222-4222-8222-222222222222"      # same entity as LIVE
SURVIVOR = "33333333-3333-4333-8333-333333333333"  # ARCH was merged into it
FOLLOWER = "44444444-4444-4444-8444-444444444444"  # merged into ARCH
OTHER = "55555555-5555-4555-8555-555555555555"     # a rejected pairing


def _merchant(mid, name, *, entity="", source="layer1", merged_into=""):
    return {"id": mid, "plaid_entity_id": entity, "name": name,
            "name_source": source, "kind": "merchant", "merged_into": merged_into}


def _txn(tid, merchant_id="", **over):
    row = {"id": tid, "account_id": "chk", "date": "2026-03-02",
           "amount": "12.50", "name": "SYNTHETIC HARDWARE 0042",
           "merchant_name": "Synthetic Hardware", "merchant_id": merchant_id}
    row.update(over)
    return row


class MerchantEntityAlreadyLiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        c = self.conn
        c.execute("""INSERT INTO merchants (id, plaid_entity_id, name,
                         name_source) VALUES (%s, 'ent-synthetic', 
                         'SYNTHETIC HDWE', 'plaid')""", (LIVE,))
        c.execute("""INSERT INTO transactions (id, account_id, date, amount,
                         name, merchant_name, merchant_id)
                     VALUES ('live-1', 'chk', '2026-04-01', 9.99,
                             'SYNTHETIC HARDWARE 0042', 'Synthetic Hardware',
                             %s)""", (LIVE,))

    def tearDown(self):
        self.conn.close()

    def _row(self, mid):
        return self.conn.execute(
            "SELECT name, name_source, merged_into::text AS merged_into "
            "FROM merchants WHERE id = %s", (mid,)).fetchone()

    def test_the_archived_merchant_folds_onto_the_live_one(self):
        data = _zip({
            "merchants.csv": [
                _merchant(ARCH, "Corner Hardware", entity="ent-synthetic",
                          source="manual", merged_into=SURVIVOR),
                _merchant(SURVIVOR, "Corner Hardware Co"),
                _merchant(FOLLOWER, "Corner Hdwe", merged_into=ARCH),
                _merchant(OTHER, "Synthetic Garden Supply")],
            "transactions.csv": [
                _txn("arch-1", ARCH),
                _txn("arch-2", OTHER, name="SYNTHETIC GARDEN 7",
                     merchant_name="Synthetic Garden Supply")],
            "merchant_canonical.csv": [
                {"raw_merchant": "SYNTHETIC HARDWARE 0042",
                 "canonical": "Corner Hardware", "method": "manual",
                 "merchant_id": ARCH}],
            "merchant_merge_proposals.csv": [
                {"id": _sig(ARCH, OTHER), "from_merchant_id": OTHER,
                 "into_merchant_id": ARCH, "status": "rejected",
                 "evidence": "{}", "decided_at": "2026-02-01T00:00:00+00:00"}],
        })
        # the post-restore identity pass prunes and re-points merchants by
        # its own rules; what is checked here is what the restore wrote
        with mock.patch("oikonome.engine.merchant_identity.resolve"):
            restore.restore_zip(self.conn, data)

        # the archive's copy was not inserted beside the live one
        self.assertIsNone(self._row(ARCH))
        live = self._row(LIVE)
        # the person's name and the person's merge reached the live row
        self.assertEqual((live["name"], live["name_source"]),
                         ("Corner Hardware", "manual"))
        self.assertEqual(live["merged_into"], SURVIVOR)
        # a merge that pointed at the archived row still lands, collapsed
        # to the terminal survivor rather than dropped as dangling
        self.assertEqual(self._row(FOLLOWER)["merged_into"], SURVIVOR)

        # the restored row and the alias point at the live merchant, never
        # at nothing
        mid = self.conn.execute(
            "SELECT merchant_id::text AS m FROM transactions WHERE id='arch-1'"
        ).fetchone()["m"]
        self.assertEqual(mid, LIVE)
        alias = self.conn.execute(
            "SELECT method, merchant_id::text AS m FROM merchant_canonical "
            "WHERE raw_merchant = 'SYNTHETIC HARDWARE 0042'").fetchone()
        self.assertEqual((alias["method"], alias["m"]), ("manual", LIVE))

        # the rejection is kept, under the id the proposer will compute for
        # the live pair, so the offer is not made again
        prop = self.conn.execute(
            "SELECT status, from_merchant_id::text AS f, "
            "       into_merchant_id::text AS i "
            "FROM merchant_merge_proposals WHERE id = %s",
            (_sig(LIVE, OTHER),)).fetchone()
        self.assertIsNotNone(prop)
        self.assertEqual(prop["status"], "rejected")
        self.assertEqual((prop["f"], prop["i"]), (OTHER, LIVE))

    def test_a_live_name_the_person_set_is_not_overwritten(self):
        self.conn.execute("UPDATE merchants SET name='Their Own Name', "
                          "name_source='manual' WHERE id=%s", (LIVE,))
        restore.restore_zip(self.conn, _zip({"merchants.csv": [
            _merchant(ARCH, "Archived Name", entity="ent-synthetic",
                      source="manual")]}))
        self.assertEqual(self._row(LIVE)["name"], "Their Own Name")

    def test_restoring_twice_changes_nothing_more(self):
        data = _zip({"merchants.csv": [
            _merchant(ARCH, "Corner Hardware", entity="ent-synthetic",
                      source="manual", merged_into=SURVIVOR),
            _merchant(SURVIVOR, "Corner Hardware Co")]})
        restore.restore_zip(self.conn, data)
        first = self._row(LIVE)
        report = restore.restore_zip(self.conn, data)
        self.assertEqual(self._row(LIVE), first)
        self.assertFalse(report.get("merchants"), report)


class PersonRulesBeatMachineRulesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_person_category_rule_replaces_a_seed_rule(self):
        c = self.conn
        c.execute("""INSERT INTO transactions (id, account_id, date, amount,
                         name, merchant_name, category_primary,
                         category_plaid)
                     VALUES ('live-c', 'chk', '2026-04-01', 80,
                             'COSTCO WHSE', 'Costco',
                             'GENERAL_MERCHANDISE', 'GENERAL_MERCHANDISE')""")
        llm_categorize.apply_seed(c)
        seeded = c.execute("SELECT merchant, category_primary FROM "
                           "merchant_categories WHERE source='seed'"
                           ).fetchall()
        self.assertTrue(seeded, "fixture: the seed pass wrote no rule")
        key = seeded[0]["merchant"]
        mine = "PERSONAL_CARE" if seeded[0]["category_primary"] != \
            "PERSONAL_CARE" else "ENTERTAINMENT"
        restore.restore_zip(c, _zip({"merchant_categories.csv": [
            {"merchant": key, "category_primary": mine, "source": "user",
             "classified_at": "2025-01-01T00:00:00+00:00",
             "disabled": "false"}]}))
        rule = c.execute("SELECT category_primary, source FROM "
                         "merchant_categories WHERE merchant=%s",
                         (key,)).fetchone()
        self.assertEqual((rule["category_primary"], rule["source"]),
                         (mine, "user"))

    def test_a_machine_rule_never_replaces_a_rule_already_here(self):
        c = self.conn
        c.execute("INSERT INTO merchant_categories (merchant, "
                  "category_primary, source) VALUES "
                  "('Synthetic Diner', 'FOOD_AND_DRINK', 'user')")
        c.execute("INSERT INTO merchant_categories (merchant, "
                  "category_primary, source) VALUES "
                  "('Synthetic Garage', 'TRANSPORTATION', 'model')")
        restore.restore_zip(c, _zip({"merchant_categories.csv": [
            {"merchant": "Synthetic Diner", "category_primary": "TRAVEL",
             "source": "user"},
            {"merchant": "Synthetic Garage", "category_primary": "TRAVEL",
             "source": "llm"}]}))
        got = {r["merchant"]: (r["category_primary"], r["source"])
               for r in c.execute("SELECT merchant, category_primary, source "
                                  "FROM merchant_categories")}
        self.assertEqual(got["Synthetic Diner"], ("FOOD_AND_DRINK", "user"))
        self.assertEqual(got["Synthetic Garage"], ("TRANSPORTATION", "model"))

    def test_a_person_alias_replaces_a_derived_alias(self):
        c = self.conn
        c.execute("INSERT INTO merchant_canonical (raw_merchant, canonical, "
                  "method) VALUES ('SYNTH PAYEE 77', 'Synth Payee', 'layer1'),"
                  " ('SYNTH OTHER 9', 'Kept Mine', 'manual')")
        restore.restore_zip(c, _zip({"merchant_canonical.csv": [
            {"raw_merchant": "SYNTH PAYEE 77", "canonical": "Childcare Co",
             "method": "manual"},
            {"raw_merchant": "SYNTH OTHER 9", "canonical": "Archived",
             "method": "manual"}]}))
        got = {r["raw_merchant"]: (r["canonical"], r["method"])
               for r in c.execute("SELECT raw_merchant, canonical, method "
                                  "FROM merchant_canonical")}
        self.assertEqual(got["SYNTH PAYEE 77"], ("Childcare Co", "manual"))
        self.assertEqual(got["SYNTH OTHER 9"], ("Kept Mine", "manual"))

    def test_a_pending_offer_takes_the_archived_answer(self):
        c = self.conn
        c.execute("INSERT INTO bill_proposals (id, payee, amount, status, "
                  "created_at) VALUES ('prop:synthetic', 'Synthetic Gym', "
                  "30, 'pending', now()), ('prop:answered', 'Synthetic Club',"
                  " 20, 'approved', now())")
        restore.restore_zip(c, _zip({"bill_proposals.csv": [
            {"id": "prop:synthetic", "payee": "Synthetic Gym",
             "amount": "30", "status": "rejected"},
            {"id": "prop:answered", "payee": "Synthetic Club",
             "amount": "20", "status": "rejected"}]}))
        got = {r["id"]: r["status"] for r in c.execute(
            "SELECT id, status FROM bill_proposals")}
        self.assertEqual(got["prop:synthetic"], "rejected")
        self.assertEqual(got["prop:answered"], "approved")

    def test_a_person_category_on_a_shared_row_replaces_a_bill_pin(self):
        c = self.conn
        c.execute("""INSERT INTO transactions (id, account_id, date, amount,
                         name, category_override, override_source)
                     VALUES ('shared-1', 'chk', '2026-04-03', 55,
                             'SYNTHETIC UTILITY', 'UTILITIES', 'bill')""")
        c.execute("INSERT INTO manual_categories (transaction_id, category, "
                  "bill_id) VALUES ('shared-1', 'UTILITIES', 'bill-1')")
        restore.restore_zip(c, _zip({
            "transactions.csv": [_txn("shared-1", name="SYNTHETIC UTILITY",
                                      merchant_name="", amount="55",
                                      category_override="RENT_AND_UTILITIES",
                                      override_source="user")],
            "manual_categories.csv": [
                {"transaction_id": "shared-1",
                 "category": "RENT_AND_UTILITIES", "bill_id": "",
                 "link_made": "false", "link_prior": ""}]}))
        t = c.execute("SELECT category_override, override_source FROM "
                      "transactions WHERE id='shared-1'").fetchone()
        self.assertEqual((t["category_override"], t["override_source"]),
                         ("RENT_AND_UTILITIES", "user"))
        m = c.execute("SELECT category, bill_id FROM manual_categories "
                      "WHERE transaction_id='shared-1'").fetchone()
        self.assertEqual((m["category"], m["bill_id"]),
                         ("RENT_AND_UTILITIES", None))


if __name__ == "__main__":
    unittest.main()
