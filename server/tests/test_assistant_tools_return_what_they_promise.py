"""Every assistant tool returns the values its description promises.

The model can only quote figures a tool hands it. A tool that reads a
field the engine does not produce (the bills tool once read `amount`
from the health verdict, which has none) answers null for every row, and
the model then says it cannot tell what anything costs. So every
registered tool runs here against one seeded household and every value
it declares in TOOL_PROMISES must be present and non-null.

The tool arguments the model sends are untrusted JSON: they are coerced
to each tool's declared schema by one helper before any tool sees them.
"""

import datetime as dt
import unittest

from oikonome.engine import assistant, bills

from .util import add_txn, make_db


def _values_at(obj, path):
    """The values `path` names in `obj`: `a.b` walks maps, `a[]` every
    item of a list."""
    cur = [obj]
    for part in path.split("."):
        nxt = []
        for o in cur:
            key, each = (part[:-2], True) if part.endswith("[]") else (part,
                                                                       False)
            if not isinstance(o, dict) or key not in o:
                nxt.append(None)
                continue
            v = o[key]
            if each:
                if not isinstance(v, list) or not v:
                    nxt.append(None)
                else:
                    nxt.extend(v)
            else:
                nxt.append(v)
        cur = nxt
    return cur


class AssistantToolsKeepTheirPromises(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        today = dt.date.today()
        for i, (days, amt, name, cat) in enumerate([
                (5, 15.99, "STREAMLY VIDEO", "ENTERTAINMENT"),
                (35, 15.99, "STREAMLY VIDEO", "ENTERTAINMENT"),
                (9, 84.20, "GREENLEAF GROCER", "FOOD_AND_DRINK"),
                (400, 61.10, "GREENLEAF GROCER", "FOOD_AND_DRINK")]):
            add_txn(self.conn, (today - dt.timedelta(days=days)).isoformat(),
                    amt, name, merchant=name.title(), primary=cat)
        bills.save_bill(self.conn, payee="Streamly", amount=15.99,
                        frequency="MONTHLY",
                        next_due=today + dt.timedelta(days=25),
                        merchant="streamly")
        bills.save_bill(self.conn, payee="Lakeview Water", amount=120.0,
                        frequency="MONTHLY", interval=3,
                        next_due=today + dt.timedelta(days=40),
                        merchant="lakeview water")
        # a paycheck is an income series, not a bill
        bills.save_bill(self.conn, payee="Acme Payroll", amount=2400.0,
                        frequency="WEEKLY", interval=2, income=True,
                        next_due=today + dt.timedelta(days=4),
                        merchant="acme payroll")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_every_registered_tool_declares_its_promises(self):
        self.assertEqual(set(assistant.TOOL_PROMISES),
                         set(assistant._TOOLS_IMPL))
        self.assertEqual({t["function"]["name"] for t in assistant.TOOLS},
                         set(assistant._TOOLS_IMPL))

    def test_every_promised_value_is_there(self):
        args = {"spend_lookup": {"merchant": "greenleaf"}}
        for name, paths in assistant.TOOL_PROMISES.items():
            out = assistant._run_tool(self.conn, name, args.get(name, {}))
            self.assertNotIn("error", out, name)
            for path in paths:
                vals = _values_at(out, path)
                self.assertNotIn(None, vals, f"{name}: {path} in {out}")

    def test_bills_carry_their_cost_and_leave_income_out(self):
        out = assistant._run_tool(self.conn, "recurring_bills", {})
        by = {b["payee"]: b for b in out["bills"]}
        self.assertEqual(set(by), {"Streamly", "Lakeview Water"})
        self.assertEqual(out["count"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual(by["Streamly"]["amount"], 15.99)
        self.assertEqual(by["Streamly"]["monthly"], 15.99)
        self.assertEqual(by["Lakeview Water"]["amount"], 120.0)
        self.assertEqual(by["Lakeview Water"]["monthly"], 40.0)
        self.assertEqual(by["Lakeview Water"]["cadence"], "every 3 months")

    def test_a_one_time_bill_is_evened_over_its_year(self):
        """A one-time $2,400 bill due this year is $200 a month, as the
        Bills headline evens it — never $2,400 every month, and never
        ranked first for it."""
        today = dt.date.today()
        bills.save_bill(self.conn, payee="Ridgeline County Tax",
                        amount=2400.0, frequency=None,
                        next_due=today.replace(month=12, day=1))
        self.conn.commit()
        out = assistant._run_tool(self.conn, "recurring_bills", {})
        by = {b["payee"]: b for b in out["bills"]}
        self.assertEqual(by["Ridgeline County Tax"]["monthly"], 200.0)
        self.assertEqual(out["bills"][0]["monthly"],
                         max(b["monthly"] for b in out["bills"]))

    def test_a_long_bill_list_says_it_was_cut(self):
        today = dt.date.today()
        for i in range(assistant.BILLS_LISTED + 2):
            bills.save_bill(self.conn, payee=f"Service {i:02d}", amount=5.0,
                            frequency="MONTHLY",
                            next_due=today + dt.timedelta(days=10))
        self.conn.commit()
        out = assistant._run_tool(self.conn, "recurring_bills", {})
        self.assertEqual(len(out["bills"]), assistant.BILLS_LISTED)
        self.assertEqual(out["count"], assistant.BILLS_LISTED + 4)
        self.assertTrue(out["truncated"])


class ToolArgumentsFollowTheSchema(unittest.TestCase):

    def test_arguments_are_coerced_to_the_declared_types(self):
        c = assistant._tool_args
        self.assertEqual(c("spend_lookup", {"year": "2024"}), {"year": 2024})
        self.assertEqual(c("spend_lookup", {"year": 2024.0}), {"year": 2024})
        # a year that is not a whole number, or not a year, is dropped
        for bad in ("last year", True, 20245, 2024.5, [2024], None):
            self.assertEqual(c("spend_lookup", {"year": bad}), {}, bad)
        self.assertEqual(c("spend_lookup", {"merchant": 7}),
                         {"merchant": "7"})
        self.assertEqual(c("spend_lookup", {"merchant": "  x  "}),
                         {"merchant": "x"})
        self.assertEqual(len(c("spend_lookup",
                               {"merchant": "m" * 500})["merchant"]), 100)
        self.assertEqual(c("spend_lookup", {"merchant": {"a": 1}}), {})
        # undeclared keys and non-object arguments never reach a tool
        self.assertEqual(c("spend_lookup", {"sql": "drop"}), {})
        self.assertEqual(c("net_worth", ["x"]), {})
        self.assertEqual(c("spend_lookup", "merchant"), {})

    def test_a_list_of_arguments_is_an_answer_not_a_crash(self):
        conn = make_db()
        try:
            out = assistant._run_tool(conn, "spend_lookup", ["greenleaf"])
            self.assertEqual(out, {"error": "give at least one of merchant, "
                                            "category, year"})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
