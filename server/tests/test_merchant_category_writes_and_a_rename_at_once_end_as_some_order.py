"""A merchant-wide category write, or a bulk recategorize, beside a rename of
the same merchant ends as the two run one after the other
(specs/invariants-and-fuzzing.md, section 3, second scenario).

The rename moves the merchant's rows and then the category rules stored
under its old name; the category write files the rule and then the rows.
Run at once they must take turns on the identity lock: the household ends
with the rows and rules of one serial order, never a rule left under a
name no row carries, and neither side is killed as a deadlock.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import llm_categorize, merchant_dedup
from oikonome.web import api, api_ledger, data
from oikonome.web.fields import Fields

from .concurrency import ScenarioCase, Scenario, Seam
from .util import TODAY, add_txn

LINE = "SQ *LANTERN BOOKS"
IDS = ("lb-90", "lb-60", "lb-30")

IDENTITY = Seam("the identity lock", rank=3)
ROWS = Seam("a row lock", sql=r"\bFOR UPDATE\b(?!.*NOWAIT)")


def _setup(hh):
    for back, tid in zip((90, 60, 30), IDS):
        add_txn(hh.conn, TODAY - dt.timedelta(days=back), 40.0, LINE,
                merchant=None, txn_id=tid)
    merchant_dedup.apply(hh.conn)
    display = hh.conn.execute(
        f"SELECT DISTINCT {merchant_dedup.DISPLAY_MERCHANT} AS d"
        f"  FROM transactions t {merchant_dedup.MC_JOIN}"
        "  WHERE t.name = %s", (LINE,)).fetchone()["d"]
    llm_categorize.upsert_user_rule(hh.conn, display, "ENTERTAINMENT")
    llm_categorize.apply(hh.conn)
    return {"display": display}


def _rename(hh, ctx):
    conn = hh.connect()
    try:
        with conn.transaction():
            return merchant_dedup.rename(conn, ctx["display"],
                                         "Lantern Bookshop")
    finally:
        conn.close()


def _digest(hh, ctx):
    c = hh.conn
    rows = tuple((r["id"], r["d"], r["category_override"],
                  r["category_primary"]) for r in c.execute(
        f"SELECT t.id, {merchant_dedup.DISPLAY_MERCHANT} AS d,"
        "        t.category_override, t.category_primary"
        f"  FROM transactions t {merchant_dedup.MC_JOIN}"
        " WHERE t.id = ANY(%s) ORDER BY t.id", (list(IDS),)).fetchall())
    rules = tuple((r["merchant"], r["category_primary"], r["source"])
                  for r in c.execute(
        "SELECT merchant, category_primary, source FROM merchant_categories"
        " ORDER BY merchant").fetchall())
    return rows, rules


class CategoryWritesBesideARename(ScenarioCase, unittest.TestCase):
    def test_a_merchant_wide_category_write_and_a_rename(self):
        def category(hh, ctx):
            conn = hh.connect()
            try:
                return data.set_merchant_category(conn, ctx["display"],
                                                  "FOOD_AND_DRINK")
            finally:
                conn.close()

        self.check(Scenario(
            "a merchant-wide category write beside a rename",
            setup=_setup, actors={"category": category, "rename": _rename},
            seams=(IDENTITY, ROWS), digest=_digest))

    def test_a_bulk_recategorize_and_a_rename(self):
        def bulk(hh, ctx):
            return api.transactions_bulk(user=hh.user, body=Fields({
                "ids": list(IDS), "action": "category",
                "category": "FOOD_AND_DRINK"}))

        self.check(Scenario(
            "a bulk recategorize beside a rename",
            setup=_setup, actors={"bulk": bulk, "rename": _rename},
            seams=(IDENTITY, ROWS), digest=_digest,
            doors=frozenset({"bulk"}),
            patches=lambda: mock.patch.object(api_ledger, "_log_bulk")))


if __name__ == "__main__":
    unittest.main()
