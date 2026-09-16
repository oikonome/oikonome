"""Workplace-plan CSV importer: window-replace idempotency, origin id scheme,
sign flip at the boundary, holdings recompute (dust rule), reporting
integration (contribution-anchored P/L)."""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.sync import plan_csv

from .util import make_db

HEADER = ("Trade Date,Investments,Ticker,Transaction,"
          "Transaction Amount,Share Price,Total shares")

CSV = f"""{HEADER}
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,"1,000.00",100.00,10.000
06/15/2025,Vanguard 500 Idx,VFIAX,Contribution,500.00,100.00,5.000
06/25/2025,Vanguard 500 Idx,VFIAX,Fee,-5.00,100.00,-0.050
06/20/2025,Closed Fund,FXNAX,Purchase,300.01,10.00,30.001
06/21/2025,Closed Fund,FXNAX,Sale,-300.03,10.00,-30.003
Totals as of some date,,,,,,
"""


class PlanCsvImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE id LIKE 'plan:%' "
            "ORDER BY date").fetchall()

    def test_import_counts_span_holdings(self):
        res = plan_csv.import_csv(self.conn, "401a", CSV)
        self.assertEqual(res["plan"], "401A")
        self.assertEqual(res["rows"], 5)             # junk/header lines skipped
        self.assertEqual(res["inserted"], 5)
        self.assertEqual(res["span"], "2025-06-15..2025-07-01")
        # VFIAX nets 14.95 shares × last price 100.00; FXNAX dust skipped
        self.assertEqual(res["holdings_value"], 1495.0)
        self.assertEqual(res["balance"], 1495.0)     # defaults to holdings

    def test_sign_flip_and_categories(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        rows = {r["raw"]["type"] + r["raw"]["date"]: r for r in self._rows()}
        contrib = rows["Contribution2025-07-01"]
        # CSV positive = money INTO the plan → engine convention flips
        self.assertEqual(contrib["amount"], -1000.0)
        self.assertEqual(contrib["category_primary"], "TRANSFER_IN")
        self.assertEqual(contrib["category_detailed"], "PLAN_CONTRIBUTION")
        self.assertEqual(contrib["merchant_name"], "Workplace plan")
        self.assertIn("Workplace plan 401A: Contribution", contrib["name"])
        # raw preserves the ORIGINAL sign for _retirement_pl/_security
        self.assertEqual(contrib["raw"]["amount"], 1000.0)
        fee = rows["Fee2025-06-25"]
        self.assertEqual(fee["amount"], 5.0)
        self.assertEqual(fee["category_primary"], "TRANSFER_OUT")

    def test_window_replace_idempotent(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        ids1 = {r["id"] for r in self._rows()}
        plan_csv.import_csv(self.conn, "401A", CSV)   # daily refresh replay
        ids2 = {r["id"] for r in self._rows()}
        self.assertEqual(ids1, ids2)                 # deterministic origin ids
        self.assertEqual(len(ids2), 5)
        h = self.conn.execute(
            "SELECT COUNT(*) n FROM holdings WHERE account_id='plan-401a'"
        ).fetchone()["n"]
        self.assertEqual(h, 1)                       # VFIAX only, no creep

    def test_identical_rows_get_distinct_occurrence_ids(self):
        dup = f"""{HEADER}
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,500.00,100.00,5.000
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,500.00,100.00,5.000
"""
        res = plan_csv.import_csv(self.conn, "401A", dup)
        self.assertEqual(res["inserted"], 2)
        self.assertEqual(len(self._rows()), 2)       # both survived the upsert

    def test_dust_positions_skipped(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        syms = {r["symbol"] for r in self.conn.execute(
            "SELECT symbol FROM holdings WHERE account_id='plan-401a'")}
        self.assertEqual(syms, {"VFIAX"})            # FXNAX nets -0.002 → dust
        vf = self.conn.execute(
            "SELECT quantity, price, value FROM holdings "
            "WHERE account_id='plan-401a' AND symbol='VFIAX'").fetchone()
        self.assertEqual(vf["quantity"], 14.95)
        self.assertEqual(vf["price"], 100.0)
        self.assertEqual(vf["value"], 1495.0)

    def test_balance_override(self):
        res = plan_csv.import_csv(self.conn, "401A", CSV, balance=1500.25)
        self.assertEqual(res["balance"], 1500.25)
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='plan-401a'"
        ).fetchone()["balance_current"], 1500.25)

    def test_retirement_pl_reads_contributions(self):
        """reporting._retirement_pl anchors P/L to raw type='Contribution'
        rows on plan-CSV accounts — the port must feed it as-is."""
        plan_csv.import_csv(self.conn, "401A", CSV)
        pl = reporting._retirement_pl(self.conn)
        self.assertEqual(len(pl), 1)
        self.assertEqual(pl[0]["contributed"], 1500.0)   # the two contributions
        self.assertEqual(pl[0]["value"], 1495.0)
        self.assertEqual(pl[0]["pl"], -5.0)

    def test_empty_and_unknown_plan_raise_value_error(self):
        # an empty CSV with NO balance is still nothing at all
        with self.assertRaises(ValueError):
            plan_csv.import_csv(self.conn, "401A", HEADER + "\n")
        with self.assertRaises(ValueError):
            plan_csv.import_csv(self.conn, "999Z", CSV)

    def test_a_dormant_plan_still_updates_its_balance(self):
        """No activity plus a balance is the normal state of a plan nobody
        contributes to — not an error. Refusing the push threw the balance
        away, so such an account froze at the date of its last
        contribution while the portal had the current figure all along."""
        out = plan_csv.import_csv(self.conn, "401A", HEADER + "\n",
                                 balance=50000.0)
        self.assertEqual(out["rows"], 0)
        self.assertTrue(out["balance_only"])
        self.assertEqual(out["balance"], 50000.0)
        bal = self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id LIKE 'plan%'"
        ).fetchone()["balance_current"]
        self.assertEqual(bal, 50000.0)

    def test_a_dormant_push_does_not_disturb_existing_rows(self):
        plan_csv.import_csv(self.conn, "401A", CSV, balance=1000.0)
        before = [dict(r) for r in self._rows()]
        plan_csv.import_csv(self.conn, "401A", HEADER + "\n", balance=2000.0)
        self.assertEqual([dict(r) for r in self._rows()], before)
        bal = self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id LIKE 'plan%'"
        ).fetchone()["balance_current"]
        self.assertEqual(bal, 2000.0)

    def test_mask_lands_and_pairs_with_an_aggregator_account(self):
        # the plan number's digits become the account mask, an existing
        # row gets it too, and a Plaid account at the same plan (balance
        # only, same mask + type) is then proposed as the same account
        from oikonome.engine import links
        plan_csv.import_csv(self.conn, "401A", CSV)
        self.assertIsNone(self.conn.execute(
            "SELECT mask FROM accounts WHERE id='plan-401a'").fetchone()["mask"])
        plan_csv.import_csv(self.conn, "401A", CSV, mask="2468")
        self.assertEqual(self.conn.execute(
            "SELECT mask FROM accounts WHERE id='plan-401a'").fetchone()["mask"],
            "2468")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('pl-plan','plaid','Northwind Plan Services')")
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                     balance_current, updated_at)
               VALUES ('pl-401a','pl-plan','401A RET. PLAN','investment',
                       '401k','2468',1234.5,now())""")
        pairs = {(p["a"]["id"], p["b"]["id"]) for p in links.suggestions(self.conn)}
        self.assertIn(("plan-401a", "pl-401a"), pairs | {tuple(reversed(x)) for x in pairs})
        self.assertEqual(links.default_order(self.conn, ["pl-401a", "plan-401a"]),
                         ["plan-401a", "pl-401a"])

    def test_looks_like_plan_activity(self):
        self.assertTrue(plan_csv.looks_like_plan_activity(HEADER.split(",")))
        self.assertFalse(plan_csv.looks_like_plan_activity(
            ["Date", "Description", "Amount"]))

    def test_migrated_rows_preserved(self):
        """Converter-carried item/account rows survive re-import (ON
        CONFLICT DO NOTHING) — only the balance updates."""
        self.conn.execute(
            """INSERT INTO items (id, aggregator, institution_name, status)
               VALUES ('plan', 'csv', 'Workplace plan', 'archived')""")
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, display_name, type, subtype)
               VALUES ('plan-401a', 'plan', 'Acme Corp 401A',
                       'My 401a', 'investment', '401a')""")
        plan_csv.import_csv(self.conn, "401A", CSV)
        row = self.conn.execute(
            "SELECT display_name, balance_current FROM accounts "
            "WHERE id='plan-401a'").fetchone()
        self.assertEqual(row["display_name"], "My 401a")
        self.assertEqual(row["balance_current"], 1495.0)

    def test_rollback(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        n = plan_csv.rollback(self.conn, "401A")
        self.assertEqual(n, 5)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM holdings WHERE account_id='plan-401a'"
        ).fetchone()["n"], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id='plan-401a'").fetchone())
        # plan-scoped rollback keeps the shared item
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id='plan'").fetchone())
        plan_csv.rollback(self.conn)                  # full rollback drops it
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id='plan'").fetchone())

    def test_dates_parsed_and_stored_as_dates(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        dates = [r["date"] for r in self._rows()]
        self.assertEqual(dates[0], dt.date(2025, 6, 15))
        self.assertEqual(dates[-1], dt.date(2025, 7, 1))


if __name__ == "__main__":
    unittest.main()

    def test_provider_and_institution_name_the_item_and_its_ids(self):
        """A collector names the plan administrator: its slug is the item id
        and the id namespace, the institution the display name — so two
        administrators' plans never share an account row."""
        res = plan_csv.import_csv(self.conn, "401A", CSV, provider="acme-ret",
                                  institution="Acme Retirement Services")
        self.assertEqual(res["rows"], 5)
        item = self.conn.execute(
            "SELECT aggregator, institution_name FROM items WHERE id='acme-ret'"
        ).fetchone()
        self.assertEqual((item["aggregator"], item["institution_name"]),
                         ("plan_csv", "Acme Retirement Services"))
        acct = self.conn.execute(
            "SELECT name FROM accounts WHERE id='acme-ret-401a'").fetchone()
        self.assertEqual(acct["name"], "Acme Retirement Services 401A")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions WHERE id LIKE 'acme-ret:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 5)
        with self.assertRaises(ValueError):
            plan_csv.import_csv(self.conn, "401A", CSV, provider="Not A Slug!")

    def test_columns_are_mapped_by_header_when_the_portal_reorders_them(self):
        reordered = ("Transaction,Trade Date,Total shares,Ticker,Investments,"
                     "Share Price,Transaction Amount\n"
                     'Contribution,07/01/2025,10.000,VFIAX,Vanguard 500 Idx,100.00,"1,000.00"\n')
        res = plan_csv.import_csv(self.conn, "401A", reordered)
        self.assertEqual(res["rows"], 1)
        row = self._rows()[0]
        self.assertEqual(row["amount"], -1000.0)
        self.assertEqual(row["raw"]["symbol"], "VFIAX")
        self.assertEqual(row["raw"]["shares"], 10.0)
