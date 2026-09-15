"""Business-entity money stays out of the household's own figures.

Hard separation: once an account is assigned to a business entity, its
rows are business money and every PERSONAL surface must leave them out
unless the combined toggle is on. The spend, income and net-worth paths
carry budget.PERSONAL_ONLY_SQL; without it an LLC's checking account
leaks into the household in five different ways:

* paycheck detection proposed the LLC's client deposits as a household
  paycheck (approving one seeds monthly income the household never has);
* the shortfall detector read an LLC → brokerage sweep as "sold
  investments to cover spending";
* cash-flow coverage called a year with only entity checking "covered",
  so household card spend became a fake negative savings rate;
* investment funding counted an LLC → Fidelity transfer as household
  money into investments;
* the 1099 vendor list used its own ownership rule instead of the P&L's,
  so a row assigned to ANOTHER entity in this entity's account, or a
  non-primary linked (shadow) row, could land on the wrong 1099 list.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget, entities, links, reporting, selfemploy

from .test_entity_money import _EntityFixture
from .util import TODAY, add_txn


class HouseholdFiguresExcludeBusinessTests(_EntityFixture):
    # ---- paycheck detection --------------------------------------------
    def _income_proposals(self, conn):
        return [p["payee"] for p in conn.execute(
            "SELECT payee FROM bill_proposals WHERE kind='add' "
            "AND (evidence->>'income')::bool IS TRUE").fetchall()]

    def test_paycheck_detection_ignores_entity_deposits(self):
        conn = self._conn()
        try:
            for i in range(6):
                d = TODAY - dt.timedelta(days=14 * i + 2)
                add_txn(conn, d, -3000.0, "BIGCO CLIENT ACH", primary="INCOME",
                        account="bizchk", txn_id=f"biz-pay-{i}")
                add_txn(conn, d, -2400.0, "ACME PAYROLL DIRECT DEP",
                        primary="INCOME", account="chk", txn_id=f"hh-pay-{i}")
            bills.run(conn, TODAY)
            payees = [p.lower() for p in self._income_proposals(conn)]
        finally:
            conn.close()
        self.assertTrue(any("acme" in p for p in payees),
                        "the household paycheck should still be proposed")
        self.assertFalse(any("bigco" in p for p in payees),
                         "LLC client deposits proposed as household income")

    # ---- shortfall detector --------------------------------------------
    def test_shortfall_detector_ignores_entity_inflows(self):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name) "
                "VALUES ('fid','test','Fidelity')")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('fidinv','fid','Brokerage','investment','brokerage',50000)")
            add_txn(conn, TODAY - dt.timedelta(days=2), -900.0,
                    "FIDELITY TRANSFER", account="bizchk", txn_id="biz-fund")
            add_txn(conn, TODAY - dt.timedelta(days=3), -400.0,
                    "FIDELITY TRANSFER", account="chk", txn_id="hh-fund")
            ev = budget.cash_events(conn, TODAY.replace(day=1),
                                    TODAY + dt.timedelta(days=1))
        finally:
            conn.close()
        self.assertEqual(ev["funding_mtd_total"], 400.0,
                         "LLC → brokerage sweep counted as household "
                         "investment-funded spending")

    # ---- cash-flow coverage + investment funding -------------------------
    def test_coverage_and_funding_ignore_entity_bank_rows(self):
        conn = self._conn()
        try:
            # a year with ONLY entity checking activity: household card
            # spend that year has UNKNOWN income, not $0
            add_txn(conn, dt.date(TODAY.year - 3, 3, 1), -5000.0,
                    "BIGCO CLIENT ACH", primary="INCOME", account="bizchk",
                    txn_id="biz-old-in")
            add_txn(conn, dt.date(TODAY.year - 3, 3, 5), 120.0,
                    "GROCER", account="card", txn_id="hh-old-spend")
            # LLC money into an investment provider is not household funding
            add_txn(conn, dt.date(TODAY.year - 3, 4, 1), 2500.0,
                    "FIDELITY INVESTMENTS", account="bizchk",
                    txn_id="biz-old-fund")
            covered = reporting._bank_coverage_years(conn)
            funding = dict(reporting._investment_funding(conn))
        finally:
            conn.close()
        self.assertNotIn(str(TODAY.year - 3), covered,
                         "a year with only entity bank rows reported as "
                         "covered household income")
        self.assertNotIn(str(TODAY.year - 3), funding,
                         "LLC → investment provider counted as household "
                         "money into investments")

    def test_coverage_and_funding_include_entities_when_combined(self):
        self._combine(True)
        conn = self._conn()
        try:
            add_txn(conn, dt.date(TODAY.year - 3, 3, 1), -5000.0,
                    "BIGCO CLIENT ACH", primary="INCOME", account="bizchk",
                    txn_id="biz-old-in")
            add_txn(conn, dt.date(TODAY.year - 3, 4, 1), 2500.0,
                    "FIDELITY INVESTMENTS", account="bizchk",
                    txn_id="biz-old-fund")
            covered = reporting._bank_coverage_years(conn)
            funding = dict(reporting._investment_funding(conn))
        finally:
            conn.close()
        self.assertIn(str(TODAY.year - 3), covered)
        self.assertEqual(funding.get(str(TODAY.year - 3)), 2500.0)

    # ---- 1099 vendor totals ----------------------------------------------
    def test_vendor_totals_follow_the_pnl_ownership_rule(self):
        conn = self._conn()
        try:
            other = entities.create_entity(conn, name="Other LLC",
                                           structure="sole_prop")
            add_txn(conn, TODAY, 700.0, "JANE CONTRACTOR", account="bizchk",
                    txn_id="v-mine")
            # in this entity's account but explicitly assigned elsewhere:
            # one payment must not appear on two 1099 lists
            add_txn(conn, TODAY, 650.0, "OTHER VENDOR", account="bizchk",
                    txn_id="v-other")
            conn.execute("UPDATE transactions SET entity_id = %s "
                         "WHERE id = 'v-other'", (other["id"],))
            mine = {v["merchant"]: v["paid"] for v in
                    selfemploy.vendor_totals(conn, self.ent["id"], TODAY.year)}
            theirs = {v["merchant"]: v["paid"] for v in
                      selfemploy.vendor_totals(conn, other["id"], TODAY.year)}
        finally:
            conn.close()
        self.assertEqual(mine, {"JANE CONTRACTOR": 700.0})
        self.assertEqual(theirs, {"OTHER VENDOR": 650.0})

    def test_vendor_totals_drop_shadow_rows(self):
        conn = self._conn()
        try:
            # the same business checking seen through a second aggregator
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name) "
                "VALUES ('sf','simplefin-org','Test Bank')")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('bizchk2','sf','Biz Checking','depository','checking',80000)")
            entities.assign_account(conn, "bizchk2", self.ent["id"])
            links.create(conn, ["bizchk", "bizchk2"])
            add_txn(conn, TODAY, 400.0, "JANE CONTRACTOR", account="bizchk",
                    txn_id="v-a")
            add_txn(conn, TODAY, 400.0, "JANE CONTRACTOR", account="bizchk2",
                    txn_id="v-b")
            conn.commit()
        finally:
            conn.close()
        conn = self._conn()          # fresh: app.shadow_ids recomputed
        try:
            vt = {v["merchant"]: v["paid"] for v in
                  selfemploy.vendor_totals(conn, self.ent["id"], TODAY.year)}
        finally:
            conn.close()
        self.assertEqual(vt.get("JANE CONTRACTOR"), 400.0,
                         "a dual-sourced payment doubled the vendor total")


if __name__ == "__main__":
    unittest.main()
