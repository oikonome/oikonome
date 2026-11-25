"""A card's statement due date is a fact about the card, so every view of
the cash timeline marks it (specs/today-figures.md, "Every view marks the
cards' due dates"): one mark per card per due date inside the chart, on
the real date (a due date on a weekend is marked on it; the payment rolls
to the business day after), labelled "<card> due", with each view's own
words for what is still owed that day ("paid" when the view has paid it
already). Households are generated; figures and names are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine.compat import jsonb
from oikonome.web import report, todayview

from . import props
from .test_each_card_is_paid_its_own_way import _card, _credit, _household
from .util import TODAY, add_txn

D = dt.date
# a Saturday: the mark stays on it, the payment lands the Monday after
WEEKEND_DUE = D(2026, 8, 8)


def _three_cards(seed):
    conn = _household(seed, checking=12000)
    rng = random.Random(seed * 7)
    _card(conn, "lag", "Cove card")
    # paid about two weeks after each charge, in weekly payments
    charges = []
    d = TODAY - dt.timedelta(days=200)
    while d < TODAY:
        amt = float(rng.randint(15, 45))
        add_txn(conn, d, amt, f"PANTRY {rng.randint(1, 5)}", account="lag")
        charges.append((d, amt))
        if d.weekday() == 0:
            owed = sum(a for c, a in charges
                       if d - dt.timedelta(days=21) <= c
                       < d - dt.timedelta(days=14))
            if owed:
                _credit(conn, "lag", d, owed)
        d += dt.timedelta(days=1)
    bal = round(sum(a for c, a in charges
                    if c >= TODAY - dt.timedelta(days=14)), 2)
    conn.execute("UPDATE accounts SET balance_current=%s WHERE id='lag'",
                 (bal,))
    conn.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                 "('lag', %s)", (jsonb({
                     "last_statement_issue_date": "2026-07-13",
                     "next_payment_due_date": WEEKEND_DUE.isoformat(),
                     "last_statement_balance": round(bal * 0.5, 2)}),))
    return conn


class EveryViewMarksTheDueDates(unittest.TestCase):
    def test_the_same_marks_in_every_scenario(self):
        for seed in props.seeds(3):
            conn = _three_cards(seed)
            try:
                st = report.gather(conn, TODAY)
            finally:
                conn.close()
            fc = st["forecast"]
            ctx = todayview.build_context(st)
            modes = {t["card"]: t["mode"] for t in fc["card_timing"]}
            sc = {s["key"]: s for s in ctx["scenarios"]}
            marks = {k: [(m["date"], m["label"]) for m in s["due_marks"]]
                     for k, s in sc.items()}
            with self.subTest(seed=seed, modes=modes):
                self.assertEqual(modes["Harbor card"], "rounds")
                self.assertEqual(modes["Summit card"], "autopay")
                self.assertEqual(modes["Cove card"], "lag")
                self.assertEqual(marks["usual"], marks["now"])
                self.assertEqual(marks["usual"], marks["due"])
                labels = {lab for _d, lab in marks["usual"]}
                self.assertEqual(labels, {"Harbor card due", "Summit card due",
                                          "Cove card due"})
                cove = [d for d, lab in marks["usual"]
                        if lab == "Cove card due"]
                # the real date, a Saturday; the due walk pays it Monday
                self.assertEqual(cove[0], WEEKEND_DUE.isoformat())
                pays = [d for d, _a, c in fc["pace_due"]["outflows"]
                        if c == "lag" and d >= WEEKEND_DUE.isoformat()]
                self.assertEqual(pays[0], "2026-08-10")
                # the due view still owes the statement on its day; the
                # round card in the usual view has paid it by then
                due_words = {m["date"]: m["words"]
                             for m in sc["due"]["due_marks"]
                             if m["card"] == "Cove card"}
                self.assertRegex(due_words[WEEKEND_DUE.isoformat()],
                                 r"^Cove card due — \$[\d,]+ owed$")
                now_words = [m["words"] for m in sc["now"]["due_marks"]]
                self.assertTrue(all(w.endswith((" owed", " — paid",
                                                " — carried balance"))
                                    for w in now_words), now_words)

    def test_the_email_chart_names_due_dates(self):
        conn = _three_cards(1)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        self.assertNotIn("autopay", ctx["fc_chart"].lower().replace(
            "autopay statement", ""))
        self.assertIn(" due", ctx["fc_chart"])


if __name__ == "__main__":
    unittest.main()
