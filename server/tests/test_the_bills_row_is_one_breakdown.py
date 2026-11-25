"""The Bills row is one breakdown: every bills figure a page prints for a
month comes from one set of parts that add up to the month's plan, in
the whole dollars printed, and every surface prints the same words for
the same figure (specs/today-figures.md, "The Bills row is one
breakdown").

    plan = posted + due + not_paid + under

A bill still unpaid from an earlier month (`late`) is still due and in
none of this month's plan; the Year row counts it once, in the month it
was due, or says it apart when no budgeted month of the year planned it.
An envelope is not a bill: its pools are the Envelopes row's, under
Bills (specs/today-figures.md, "Envelopes are their own row"), and no
pool reaches a Bills figure or word.
"""

import datetime as dt
import random
import re
import unittest
from unittest import mock

from oikonome.engine.reporting import whole_dollars
from oikonome.web import api_bills, lens_email, lenses, report, todayview

from . import props
from .util import add_bill, add_txn, freeze_month, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


def _bills_row(rows):
    """The map's Bills row; None when the period has no dated bill."""
    return next((r for r in rows or () if r["label"] == "Bills"), None)


def _env_row(rows):
    return next((r for r in rows or () if r["label"] == "Envelopes"), None)


def _still(text: str | None) -> int:
    """The one figure the Bills row's words print: "$1,610 still due" →
    1610; no words → 0."""
    if text is None:
        return 0
    m = re.fullmatch(r"\$([\d,]+) still due", text)
    assert m, text
    return int(m.group(1).replace(",", ""))


def _caption(text: str | None) -> list[tuple[int, str]]:
    """A Bills caption's figures with the words before each: "posted
    $1,200 · plan $1,350" → [(1200, "posted"), (1350, "plan")]; "posted
    this year $900" → [(900, "posted this year")]; "posted in 2026
    $1,600" → [(1600, "posted in 2026")]."""
    return [(int(v.replace(",", "")), w) for w, v in re.findall(
        r"(posted this year|posted in \d{4}|\w+) \$([\d,]+)", text or "")]


# The table's labels, each mapped to the breakdown part it prints.
_KINDS = (("Paid", "posted"), ("Refunds beyond charges", "refunds"),
          ("Still due", "due"), ("Due this month, not paid yet", "due"),
          ("Late from ", "late"), ("Not paid", "not_paid"),
          ("Planned in ", "moved"),
          ("Under plan", "under"), ("Over plan", "under"))


def _kind(label: str) -> str:
    for start, kind in _KINDS:
        if label.startswith(start):
            return kind
    raise AssertionError(f"a table label no part prints: {label}")


def _parts(table: dict) -> dict[str, int]:
    """The table's rows above its total, summed by the part each prints,
    with its sign: {"posted": 1200, "under": -42, ...}."""
    out: dict[str, int] = {}
    for r in table["rows"][:-1]:
        v = r["amount"] if r["sign"] == "+" else -r["amount"]
        out[_kind(r["label"])] = out.get(_kind(r["label"]), 0) + v
    return out


def _labels(table: dict) -> list[str]:
    return [r["label"] for r in table["rows"] + table["outside"]]


def _table(row_or_bd: dict) -> dict:
    bd = row_or_bd.get("breakdown", row_or_bd)
    if "table" in bd:
        return bd["table"]
    from oikonome.web import bills_breakdown as bb
    return bb.table(bd)


class Reads:
    """Every surface of one household on `today`: Today's reply (which
    the Budget page draws), the daily email's plain text, the Month lens
    of the running month, the Year lens, the Bills page's headline and
    the monthly email built from the Month lens."""

    def __init__(self, conn, today: dt.date):
        conn.commit()
        noon = dt.datetime.combine(today, dt.time(12))
        with mock.patch("oikonome.localtime.now_local", return_value=noon):
            self.st = report.gather(conn, today)
            rows, _t = todayview.today_map(self.st)
            self.today_rows = todayview.sent_plan_rows(rows)
            self.today = _bills_row(self.today_rows)
            self.today_env = _env_row(self.today_rows)
            _s, self.plain, _h = report.build(self.st)
            self.month = lenses.month_summary(conn, today.year, today.month,
                                              today=today)
            self.month_row = _bills_row(self.month["plan_rows"])
            self.month_env = _env_row(self.month["plan_rows"])
            self.year = lenses.year_summary(conn, today.year, today=today)
            self.year_row = (_bills_row(self.year["plan_rows"])
                             if self.year["plan_rows"] else None)
            self.year_env = _env_row(self.year["plan_rows"])
            hero = api_bills.bills_list(user=_user(conn),
                                        month=None)["totals"]["hero"]
            self.tile = next((t for t in hero if t["key"] == "due"), None)
            self.mail = lens_email.month_mail_context(
                self.month, self.year, today.year, today.month)
            self.mail_bills = next((b for b in self.mail["rows"]
                                    if b["label"] == "Bills"), None)


def _a_month(conn):
    """A household with a budget of its own."""
    write_config(conn, food_monthly=600, other_monthly=400,
                 budgeted_income_monthly=5200)


class TheBreakdownAddsUpTests(unittest.TestCase):
    """The row prints posted and still due; its table adds the parts up
    to the plan, as printed, on every surface that prints the running
    month."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _a_month(self.conn)
        # posted under its plan, due later, due already and not posted
        add_bill(self.conn, "Harbor Lofts", 1475.0,
                 next_due=dt.date(2026, 7, 3), last_seen=dt.date(2026, 6, 3))
        add_txn(self.conn, dt.date(2026, 7, 3), 1463.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_bill(self.conn, "Kestrel Power", 135.0,
                 next_due=dt.date(2026, 7, 24), last_seen=dt.date(2026, 6, 24))
        add_bill(self.conn, "Willow Daycare", 65.0,
                 next_due=dt.date(2026, 7, 12), last_seen=dt.date(2026, 6, 12))
        self.r = Reads(self.conn, dt.date(2026, 7, 15))

    def test_the_row_prints_two_figures_and_its_table_adds_up(self):
        row = self.r.today
        cap = _caption(row["caption"])
        self.assertEqual([w for _v, w in cap], ["posted", "plan"])
        posted, plan = (v for v, _w in cap)
        self.assertEqual(row["actual"], posted)
        self.assertEqual(row["status"]["text"], "$200 still due")
        self.assertEqual(posted + 200, row["budget"])
        # the awaiting part of still due is named in the caption, outside
        # the table, and is the status's `pending`
        self.assertEqual(row["caption"], "posted $1,463 · $65 awaiting · "
                         "plan $1,675 · not counted in the verdict")
        self.assertEqual(row["status"]["pending"], 65.0)
        t = _table(row)
        self.assertEqual([(r["label"], r["figure"]) for r in t["rows"]],
                         [("Paid so far", "+$1,463"), ("Still due", "+$200"),
                          ("Under plan", "+$12"),
                          ("This month's plan", "$1,675")])
        self.assertEqual(t["rows"][-1]["amount"], plan)
        self.assertEqual((t["outside_heading"], t["outside"]), (None, []))

    def test_every_surface_prints_the_same_figures(self):
        r = self.r
        text = r.today["status"]["text"]
        self.assertEqual(r.month_row["status"]["text"], text)
        self.assertEqual(r.month_row["caption"], r.today["caption"])
        self.assertEqual((r.month_row["actual"], r.month_row["budget"]),
                         (r.today["actual"], r.today["budget"]))
        self.assertEqual(_table(r.month_row), _table(r.today))
        # the paid-vs-planned card: the row's posted and its plan, and no
        # parts of its own
        bills = r.month["bills"]
        self.assertEqual(bills["bills_posted"], r.today["actual"])
        self.assertEqual(bills["notes"], [])
        self.assertIn(f"plan ${whole_dollars(bills['bills_planned']):,}",
                      r.today["caption"].split(" · "))
        self.assertEqual(bills["bills_awaiting"], 65.0)
        # the Bills page's tile is the still-due figure, under its label
        self.assertEqual((r.tile["label"], r.tile["amount"]),
                         ("Still due", _still(text)))
        # the Year row: the same figure in the same words
        self.assertEqual(r.year_row["status"]["text"], text)
        # the daily email's plain text and the monthly email print it,
        # and no table
        self.assertIn(text, r.plain)
        self.assertNotIn("Under plan", r.plain)
        self.assertIn(text, r.mail_bills["note"])
        self.assertNotIn("under plan", r.mail_bills["note"])
        # the mail's bar is posted against the month's plan
        self.assertEqual((r.mail_bills["actual"], r.mail_bills["plan"]),
                         (r.today["actual"], _table(r.today)["rows"][-1]
                          ["amount"]))


class TheMonthlyEmailNamesWhatWasNotPaidTests(unittest.TestCase):
    def test_a_closed_months_mail_names_each_bill_it_did_not_pay(self):
        """The mail's Bills line says what the Month card says: each bill
        a closed month did not pay, by name, beside the row's words."""
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Harbor Lofts", 1475.0, next_due=dt.date(2026, 7, 3),
                 last_seen=dt.date(2026, 6, 3))
        add_txn(conn, dt.date(2026, 6, 3), 1475.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 24),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 24))
        freeze_month(conn, 2026, 6)
        conn.commit()
        today = dt.date(2026, 7, 15)
        d = lenses.month_summary(conn, 2026, 6, today=today)
        y = lenses.year_summary(conn, 2026, today=today)
        self.assertEqual(d["bills"]["notes"], ["not paid: Kestrel Power $135"])
        note = lens_email.month_mail_context(d, y, 2026, 6)["rows"][0]["note"]
        # said once, by name
        self.assertIn("not paid: Kestrel Power $135", note)
        self.assertEqual(note.count("not paid"), 1)


class UnderAndOverTests(unittest.TestCase):
    """A row with nothing still due says how its month came out, in one
    short phrase beside its figures, on every surface — an older phone
    reads only those words."""

    def _closed(self, paid):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Harbor Lofts", 1475.0, next_due=dt.date(2026, 7, 3),
                 last_seen=dt.date(2026, 6, 3))
        add_txn(conn, dt.date(2026, 6, 3), paid, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        freeze_month(conn, 2026, 6)
        conn.commit()
        return lenses.month_summary(conn, 2026, 6, today=dt.date(2026, 7, 15))

    def test_a_bill_posted_under_its_plan_reads_under_plan(self):
        row = _bills_row(self._closed(1438.0)["plan_rows"])
        self.assertEqual((row["status"]["text"], row["status"]["kind"],
                          row["status"]["tone"]),
                         ("$37 under plan", "left", "mut"))
        self.assertEqual(_parts(_table(row))["under"], 37)
        self.assertEqual(row["actual"] + 37, row["budget"])

    def test_a_bill_posted_over_its_plan_says_over_with_its_minus(self):
        d = self._closed(1517.0)
        row = _bills_row(d["plan_rows"])
        self.assertEqual((row["status"]["text"], row["status"]["kind"],
                          row["status"]["amount"], row["status"]["tone"]),
                         ("over plan by $42", "over", 42.0, "neg"))
        over = [r for r in _table(row)["rows"] if r["label"] == "Over plan"]
        self.assertEqual([(r["sign"], r["figure"]) for r in over],
                         [("-", "−$42")])
        self.assertEqual(d["bills"]["notes"], [])
        self.assertEqual(row["actual"] - 42, row["budget"])

    def test_a_running_month_already_over_its_plan_says_so(self):
        from oikonome.web import bills_breakdown as bb
        bd = bb.from_parts("running", plan=500.0, posted=560.0)
        self.assertEqual(bd["text"], "over plan by $60")
        bd = bb.from_parts("running", plan=500.0, posted=520.0, due=30.0)
        self.assertEqual(bd["text"], "$30 still due · over plan by $50")
        self.assertEqual(bb.row_status(bd)["kind"], "due")
        # under plan so far, with nothing still due: the month is not over
        bd = bb.from_parts("running", plan=500.0, posted=480.0)
        self.assertIsNone(bd["text"])

    def test_a_past_year_says_how_it_came_out(self):
        from oikonome.web import bills_breakdown as bb
        jun = dict(bb.from_parts("closed", plan=900.0, posted=850.0,
                                 unpaid=[("Kestrel Power", 70.0,
                                          "2026-06-14")]), y=2026, m=6)
        jul = dict(bb.from_parts("closed", plan=900.0, posted=905.0),
                   y=2026, m=7)
        yr = bb.year([jun, jul])
        self.assertEqual(yr["text"], "$70 not paid · over plan by $25")
        # a year that is over has no earlier months to speak of
        self.assertIn(("Not paid", "+$70"),
                      [(r["label"], r["figure"]) for r in bb.table(yr)["rows"]])
        self.assertEqual(bb.row_status(yr)["kind"], "over")
        _invariants(self, yr)


def _pool(conn, *, spent=(), payee="Namehost", pool=660.0):
    """A yearly envelope of `pool`, spent on each (date, amount)."""
    add_bill(conn, payee, pool, bill_type="envelope", frequency=None,
             interval=12, category="ENTERTAINMENT", match_category=True)
    for n, (day, amt) in enumerate(spent):
        add_txn(conn, day, amt, payee.upper(), primary="ENTERTAINMENT",
                txn_id=f"pool-{payee}-{n}")


def _closed_months(conn, year, months):
    for m in months:
        freeze_month(conn, year, m)
    conn.commit()


def _pair(row):
    """A map row as printed: (left figure, right figure, words, tone)."""
    st = row["status"] or {}
    return row["actual"], row["budget"], st.get("text"), st.get("tone")


class AnEnvelopeIsNotOnTheBillsRowTests(unittest.TestCase):
    """A pool is paid when it is paid: it is the Envelopes row's, a pool
    gauge under Bills, never red and never "over" unless a pool's own
    period spent past it — and nothing of it reaches the Bills row."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _a_month(self.conn)

    def _no_pool_on_bills(self, *rows):
        for row in rows:
            if row is None:
                continue
            self.assertNotIn("pool", (row["status"] or {}).get("text") or "")
            self.assertFalse([r for r in _labels(_table(row))
                              if "envelope" in r.lower()])

    def test_a_pool_spent_in_an_earlier_month_has_nothing_left(self):
        _pool(self.conn, spent=[(dt.date(2026, 2, 10), 660.0)])
        r = Reads(self.conn, dt.date(2026, 7, 15))
        # where Bills would be: after the spending rows, before Excess
        labels = [x["label"] for x in r.today_rows]
        if r.today_env is not None and "Excess cash" in labels:
            self.assertLess(labels.index("Envelopes"),
                            labels.index("Excess cash"))
        # no dated bill: no Bills row; a pool with nothing drawn and
        # nothing left this month has nothing to print either
        self.assertIsNone(r.today)
        self.assertIsNone(r.today_env)
        self._no_pool_on_bills(r.today, r.month_row, r.year_row)

    def test_a_pool_nobody_touched_keeps_its_whole_pool_mid_year(self):
        _pool(self.conn)
        for today in (dt.date(2026, 7, 15), dt.date(2026, 10, 3)):
            with self.subTest(today=today):
                r = Reads(self.conn, today)
                left, right, words, tone = _pair(r.today_env)
                self.assertEqual((left, right), (0.0, 660.0))
                # where Bills would be: after the spending rows, before
                # Excess cash
                labels = [x["label"] for x in r.today_rows]
                self.assertEqual(labels.index("Envelopes"),
                                 labels.index("Everything else") + 1)
                # nothing past the pool, nothing settled: no words; the
                # caption's one figure is the envelopes half of the
                # month-end figure
                self.assertIsNone(words)
                self.assertEqual(_pair(r.month_env), _pair(r.today_env))
                self._no_pool_on_bills(r.today, r.month_row, r.year_row)
                self.assertEqual(
                    r.today_env["caption"],
                    f"${r.st['month_end_parts']['envelopes']:,} still "
                    "expected this month · not counted in the verdict")

    def test_a_pool_paid_in_december_is_never_over(self):
        """$660 paid on Dec 10: the running December, the closed one, the
        finished Year and both emails read the pool full, never over."""
        _pool(self.conn, spent=[(dt.date(2026, 12, 10), 660.0)])
        _closed_months(self.conn, 2026, range(1, 12))
        r = Reads(self.conn, dt.date(2026, 12, 15))
        for row in (r.today_env, r.month_env):
            self.assertEqual(_pair(row), (660.0, 660.0, None, None))
        self._no_pool_on_bills(r.today, r.month_row)
        self.assertIsNone(r.mail_bills)
        freeze_month(self.conn, 2026, 12)
        self.conn.commit()
        jan = dt.date(2027, 1, 15)
        dec = lenses.month_summary(self.conn, 2026, 12, today=jan)
        self.assertEqual(_pair(_env_row(dec["plan_rows"])),
                         (660.0, 660.0, None, None))
        y = lenses.year_summary(self.conn, 2026, today=jan)
        self.assertEqual(_pair(_env_row(y["plan_rows"])),
                         (660.0, 660.0, None, None))
        self.assertIsNone(_bills_row(y["plan_rows"]))

    def test_a_finished_year_with_an_untouched_pool_reads_it_unspent(self):
        _pool(self.conn)
        _closed_months(self.conn, 2026, range(1, 13))
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 2, 9))
        self.assertEqual(_pair(_env_row(y["plan_rows"])),
                         (0.0, 660.0, "$660 unspent", "mut"))
        self.assertIsNone(_bills_row(y["plan_rows"]))

    def test_a_pool_spent_in_march_reads_full_when_the_year_is_done(self):
        _pool(self.conn, spent=[(dt.date(2026, 3, 9), 660.0)])
        _closed_months(self.conn, 2026, range(1, 13))
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 2, 9))
        self.assertEqual(_pair(_env_row(y["plan_rows"])),
                         (660.0, 660.0, None, None))
        self.assertIsNone(_bills_row(y["plan_rows"]))

    def test_a_running_year_shows_what_is_left_in_the_pool(self):
        _pool(self.conn, spent=[(dt.date(2026, 3, 9), 250.0)])
        _closed_months(self.conn, 2026, range(1, 7))
        r = Reads(self.conn, dt.date(2026, 7, 15))
        left, right, words, _tone = _pair(r.year_env)
        self.assertEqual((left, right), (250.0, 660.0))
        self.assertNotIn("over", words or "")
        self._no_pool_on_bills(r.year_row)

    def test_a_pool_overspent_is_spending_past_the_pool(self):
        """$720 swiped on a $660 pool: the pool's $660 is the Envelopes
        row's; the $60 past it is spending, counted once, in Everything
        else — the row says so, and its bar is never past its figure."""
        _pool(self.conn, spent=[(dt.date(2026, 5, 4), 400.0),
                                (dt.date(2026, 5, 20), 320.0)])
        _closed_months(self.conn, 2026, range(1, 6))
        may = lenses.month_summary(self.conn, 2026, 5,
                                   today=dt.date(2026, 6, 10))
        env = _env_row(may["plan_rows"])
        self.assertEqual(_pair(env), (
            660.0, 660.0, "$60 past the pool in May, counted in spending",
            "neg"))
        self.assertEqual(env["status"]["kind"], "over")
        self.assertIsNone(_bills_row(may["plan_rows"]))

    def test_two_pools_one_spent_one_untouched(self):
        _pool(self.conn, spent=[(dt.date(2026, 4, 2), 660.0)])
        _pool(self.conn, payee="Hobby Pool", pool=240.0)
        _closed_months(self.conn, 2026, range(1, 13))
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 2, 9))
        env = _env_row(y["plan_rows"])
        # one pool's unspent on a row of two names it
        self.assertEqual(_pair(env), (660.0, 900.0, "Hobby Pool: $240 unspent",
                                      "mut"))
        self.assertEqual(len(env["breakdown"]["table"]["rows"]), 2)

    def test_the_emails_draw_the_row_and_a_pinned_pool_at_its_pool(self):
        """The monthly email's Envelopes bar is the row's drawn of cap; a
        yearly pool pinned to Today leaves it for a bar of its own whose
        cap is the pool — on the yearly email too, never its monthly
        shares summed over the budgeted months."""
        _pool(self.conn, spent=[(dt.date(2026, 5, 4), 380.0)])
        add_bill(self.conn, "Hobby Pool", 240.0, bill_type="envelope",
                 frequency=None, interval=12, category="TRAVEL",
                 match_category=True, show_today=True)
        add_txn(self.conn, dt.date(2026, 3, 2), 90.0, "HOBBY POOL",
                primary="TRAVEL", txn_id="hobby-1")
        _closed_months(self.conn, 2026, range(1, 6))
        may = lenses.month_summary(self.conn, 2026, 5,
                                   today=dt.date(2026, 6, 10))
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2026, 6, 10))
        bars = {b["label"]: b for b in
                lens_email.month_mail_context(may, y, 2026, 5)["rows"]}
        self.assertEqual((bars["Envelopes"]["actual"],
                          bars["Envelopes"]["plan"]), (380.0, 660.0))
        # pinned, the pool is named: its own pair, the year so far of the
        # pool, as Today's card and the Bills page print it
        self.assertEqual((bars["Hobby Pool"]["actual"],
                          bars["Hobby Pool"]["plan"]), (90.0, 240.0))
        self.assertNotIn("Bills", bars)
        self.assertIn("without Hobby Pool", bars["Envelopes"]["note"])
        past = lenses.year_summary(self.conn, 2026,
                                   today=dt.date(2027, 1, 20),
                                   with_detail=True)
        ybars = {b["label"]: b for b in
                 lens_email.year_mail_context(past, 2026)["rows"]}
        self.assertEqual((ybars["Hobby Pool"]["actual"],
                          ybars["Hobby Pool"]["plan"]), (90.0, 240.0))
        self.assertEqual(ybars["Envelopes"]["plan"], 660.0)
        self.assertNotIn("Envelopes", [b["label"] for b in ybars.values()
                                       if b["bar"] == "#e0695d"])

    def test_a_month_with_a_pool_and_a_bill_not_paid(self):
        _pool(self.conn, spent=[(dt.date(2026, 6, 8), 660.0)])
        add_bill(self.conn, "Kestrel Power", 135.0,
                 next_due=dt.date(2026, 6, 24), frequency="YEARLY",
                 last_seen=dt.date(2025, 6, 24))
        _closed_months(self.conn, 2026, range(1, 7))
        jun = lenses.month_summary(self.conn, 2026, 6,
                                   today=dt.date(2026, 7, 10))
        row = _bills_row(jun["plan_rows"])
        self.assertEqual(row["status"]["text"], "$135 not paid")
        self.assertEqual((row["actual"], row["budget"]), (0.0, 135.0))
        self.assertEqual(_pair(_env_row(jun["plan_rows"])),
                         (660.0, 660.0, None, None))
        # the card: the bills plan and the set-aside, and the plan both
        # make as printed
        self.assertEqual((jun["bills"]["bills_planned"],
                          jun["bills"]["envelopes_set_aside"],
                          jun["bills"]["set_aside"]), (135.0, 55.0, 190.0))
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2026, 7, 10))
        note = lens_email.month_mail_context(jun, y, 2026, 6)["rows"][0]["note"]
        self.assertIn("not paid: Kestrel Power $135", note)
        self.assertNotIn("over", note)
        _invariants(self, row["breakdown"], row=row)


class ALateBillTests(unittest.TestCase):
    """A bill of last month still unpaid is still due and in none of
    this month's plan."""

    def _carried(self, *, frozen=True, today=dt.date(2026, 7, 2),
                 due=dt.date(2026, 6, 29)):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        # a yearly bill, so this month has no date of its own
        add_bill(conn, "Kestrel Power", 135.0, next_due=due,
                 frequency="YEARLY", last_seen=due.replace(year=due.year - 1))
        # this month's own bill, still to come; last month's was paid
        nxt = today + dt.timedelta(days=5)
        add_bill(conn, "Harbor Lofts", 1475.0, next_due=nxt,
                 last_seen=due.replace(day=nxt.day))
        add_txn(conn, due.replace(day=nxt.day), 1475.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        if frozen:
            freeze_month(conn, due.year, due.month)
        return conn, Reads(conn, today)

    def test_last_months_bill_on_the_2nd_is_listed_below_the_plan(self):
        _conn, r = self._carried()
        # the row: one still-due figure, this month's and the late bill
        self.assertEqual(r.today["status"]["text"], "$1,610 still due")
        self.assertEqual(r.month_row["status"]["text"], "$1,610 still due")
        self.assertEqual(r.today["budget"], r.today["actual"] + 1610)
        # the table: this month's own part under another name, the late
        # bill below the plan's total with its month and year
        t = _table(r.today)
        self.assertEqual(_parts(t), {"posted": 0, "due": 1475})
        self.assertNotIn("Still due", _labels(t))
        self.assertEqual(t["outside_heading"],
                         "Also still due, from earlier months (not in this "
                         "plan)")
        self.assertEqual([(i["label"], i["figure"]) for i in t["outside"]],
                         [("Late from Jun 2026: Kestrel Power", "+$135")])
        # the Bills page's tile: the row's figure under the row's label,
        # its note the parts in the table's words
        self.assertEqual((r.tile["label"], r.tile["amount"], r.tile["note"]),
                         ("Still due", 1610,
                          "$1,475 of bills due this month + $135 late from "
                          "Jun 2026"))

    def test_the_year_counts_last_months_bill_once_inside_its_plan(self):
        _conn, r = self._carried()
        self.assertEqual(r.year_row["status"]["text"], "$1,610 still due")
        t = _table(r.year_row)
        parts = _parts(t)
        self.assertEqual(parts["late"], 135)
        self.assertNotIn("not_paid", parts)
        self.assertNotIn("under", parts)
        self.assertEqual(t["outside"], [])
        self.assertEqual(r.year_row["budget"], t["rows"][-1]["amount"])

    def test_a_december_bill_carried_into_january(self):
        _conn, r = self._carried(today=dt.date(2027, 1, 2),
                                 due=dt.date(2026, 12, 29))
        self.assertEqual(r.month_row["status"]["text"], "$1,610 still due")
        t = _table(r.year_row)
        self.assertEqual(t["outside_heading"],
                         "Also still due, from outside this year's plan")
        self.assertEqual([i["label"] for i in t["outside"]],
                         ["Late from Dec 2026: Kestrel Power"])
        self.assertNotIn("Over plan", _labels(t))
        self.assertEqual(r.year_row["budget"],
                         r.year_row["actual"] + 1475
                         + r.year_row["breakdown"]["under"])

    def test_a_bill_carried_from_a_month_with_no_budget_snapshot(self):
        _conn, r = self._carried(frozen=False)
        t = _table(r.year_row)
        self.assertEqual([i["label"] for i in t["outside"]],
                         ["Late from Jun 2026: Kestrel Power"])
        self.assertNotIn("Over plan", _labels(t))

    def test_a_payee_spelled_another_way_is_counted_once(self):
        conn, _r = self._carried()
        conn.execute("UPDATE bills SET payee='KESTREL' "
                     "WHERE payee='Kestrel Power'")
        r = Reads(conn, dt.date(2026, 7, 2))
        self.assertEqual(r.year_row["status"]["text"], "$1,610 still due")
        parts = _parts(_table(r.year_row))
        self.assertEqual(parts["late"], 135)
        self.assertNotIn("not_paid", parts)

    def test_only_the_late_part_left_prints_no_zero(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 29),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
        freeze_month(conn, 2026, 6)
        r = Reads(conn, dt.date(2026, 7, 2))
        self.assertEqual(r.today["status"]["text"], "$135 still due")
        t = _table(r.today)
        self.assertNotIn("+$0", [x["figure"] for x in t["rows"][1:-1]])
        self.assertNotIn("due", _parts(t))
        self.assertEqual([i["amount"] for i in t["outside"]], [135])

    def test_a_weekly_bills_missed_last_date_is_counted_once(self):
        """Paid on its other June dates, missed on its last, carried on
        July 1: the Year row counts it once, as still due."""
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Willow Daycare", 285.0, frequency="WEEKLY",
                 next_due=dt.date(2026, 6, 30), last_seen=dt.date(2026, 6, 23))
        for d in (2, 9, 16, 23):
            add_txn(conn, dt.date(2026, 6, d), 285.0, "WILLOW DAYCARE")
        freeze_month(conn, 2026, 6)
        r = Reads(conn, dt.date(2026, 7, 1))
        bd = r.year_row["breakdown"]
        self.assertEqual(sum(i["amount"] for i in bd["late"]), 285)
        parts = _parts(_table(r.year_row))
        self.assertEqual(parts["late"], 285)
        self.assertNotIn("not_paid", parts)
        _invariants(self, bd, row=r.year_row)


class TwoBillsOnOneDateTests(unittest.TestCase):
    """Two bills that each carry their own identity are two bills, even
    on the same due date: the Year never takes one's unpaid date for the
    other's late one."""

    def test_a_new_bill_on_an_archived_bills_date_is_late_from_outside_the_plan(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 28),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 28))
        freeze_month(conn, 2026, 6)
        # the June bill is archived, unpaid; a different bill with the same
        # due date is added after June closed
        conn.execute("UPDATE bills SET active=0 WHERE payee='Kestrel Power'")
        add_bill(conn, "Lantern Shop", 90.0, next_due=dt.date(2026, 6, 28),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 28))
        r = Reads(conn, dt.date(2026, 7, 2))
        june = lenses.month_summary(conn, 2026, 6, today=dt.date(2026, 7, 2))
        jb = _bills_row(june["plan_rows"])["breakdown"]
        self.assertEqual(jb["not_paid"], 135)
        self.assertEqual([(i["payee"], i["amount"]) for i in
                          r.month_row["breakdown"]["late"]],
                         [("Lantern Shop", 90)])
        yb = r.year_row["breakdown"]
        # the Year agrees with both months: June's bill not paid, the new
        # one late from outside the year's plan
        self.assertEqual(yb["not_paid"], 135)
        self.assertEqual([(i["payee"], i["amount"], i["inside_plan"])
                          for i in yb["late"]],
                         [("Lantern Shop", 90, False)])
        _invariants(self, yb, row=r.year_row)


class WholeDollarsOnceTests(unittest.TestCase):
    """Posted and still due in cents that round apart print one set of
    integers on every surface."""

    def _cents(self, posted, due):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Harbor Lofts", posted, next_due=dt.date(2026, 7, 3),
                 last_seen=dt.date(2026, 6, 3))
        add_txn(conn, dt.date(2026, 7, 3), posted, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_bill(conn, "Kestrel Power", due, next_due=dt.date(2026, 7, 24),
                 last_seen=dt.date(2026, 6, 24))
        return Reads(conn, dt.date(2026, 7, 15))

    def _check(self, r):
        posted, plan = (v for v, _w in _caption(r.today["caption"]))
        still = _still(r.today["status"]["text"])
        self.assertEqual(r.today["actual"], posted)
        self.assertEqual(posted + still, r.today["budget"])
        self.assertEqual(posted + still, plan)
        self.assertEqual(r.tile["amount"], still)
        self.assertEqual(r.month["bills"]["bills_posted"], posted)
        # the card's bills plan is in cents (with the set-aside it adds
        # up to the plan to the cent); it prints as the row's plan
        self.assertEqual(whole_dollars(r.month["bills"]["bills_planned"]),
                         plan)
        self.assertEqual(r.month_row["budget"], r.today["budget"])
        self.assertEqual(_still(r.year_row["status"]["text"]), still)
        t = _table(r.today)
        self.assertEqual((t["rows"][0]["amount"], t["rows"][-1]["amount"]),
                         (posted, plan))

    def test_cents_under_a_half(self):
        self._check(self._cents(64.40, 46.40))

    def test_cents_over_a_half(self):
        self._check(self._cents(72.65, 52.65))


class OneReadTests(unittest.TestCase):
    def test_a_failed_breakdown_leaves_no_figures_from_another_reading(self):
        """With no breakdown the row prints its bar and nothing beside it:
        no words, no caption, no table. The month-end figure the cash
        tiles subtract is read apart from it (report.month_end_parts) and
        stands."""
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 7, 24),
                 last_seen=dt.date(2026, 6, 24))
        from oikonome.web import bills_breakdown
        with mock.patch.object(bills_breakdown, "running",
                               side_effect=RuntimeError("walk failed")):
            r = Reads(conn, dt.date(2026, 7, 15))
        self.assertEqual(r.st["bills_remaining_month"], 135.0)
        for row in (r.today, r.month_row):
            self.assertIsNone(row["status"])
            self.assertIsNone(row["caption"])
            self.assertIsNone(row["breakdown"])
        self.assertIsNone(r.year_row["status"])

    def test_a_past_day_prints_no_caption_from_another_reading(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 7, 4),
                 last_seen=dt.date(2026, 6, 4))
        add_txn(conn, dt.date(2026, 7, 4), 131.6, "KESTREL POWER",
                account="chk", primary="RENT_AND_UTILITIES")
        conn.commit()
        st = report.gather(conn, dt.date(2026, 7, 10), live=False)
        rows, _t = todayview.today_map(st)
        row = _bills_row(todayview.sent_plan_rows(rows))
        self.assertIsNone(row["breakdown"])
        self.assertIsNone(row["status"])
        self.assertIsNone(row["caption"])


class AnOlderPhoneTests(unittest.TestCase):
    def test_the_still_due_words_stand_alone(self):
        """An older phone prints `status.text` beside posted / total and
        drops the caption: the words' one figure is the bar's gap, and
        kind / amount keep their meaning (still due, awaiting)."""
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 29),
                 last_seen=dt.date(2026, 5, 29))
        add_txn(conn, dt.date(2026, 5, 29), 135.0, "KESTREL POWER",
                account="chk", primary="RENT_AND_UTILITIES")
        add_bill(conn, "Harbor Lofts", 1475.0, next_due=dt.date(2026, 7, 7),
                 last_seen=dt.date(2026, 6, 7))
        freeze_month(conn, 2026, 6)
        r = Reads(conn, dt.date(2026, 7, 2))
        st = r.today["status"]
        due = _still(st["text"])
        self.assertEqual(r.today["actual"] + due, r.today["budget"])
        self.assertEqual((st["kind"], st["amount"]), ("due", float(due)))


class ClientsPrintTheBillsWordsTests(unittest.TestCase):
    """The web and phone maps print the Bills row's caption and words
    and open the server's table; no client drops a figure or composes
    one."""

    def _src(self, rel):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2] / rel).read_text()

    def test_the_web_map_and_budget_bars_print_the_bills_words(self):
        mm = self._src("webapp/src/components/MoneyMap.tsx")
        self.assertNotIn('row.label === "Bills") return null', mm)
        self.assertIn("st.text", mm)
        bars = self._src("webapp/src/components/PlanBars.tsx")
        self.assertIn("row.status.text", bars)

    def test_the_web_opens_the_servers_table_from_the_row(self):
        """The web map (and the Budget page's bars) show "How this adds
        up" only when the reply carries the table, and print its rows'
        labels and figures as sent. The webapp has no component test
        runner, so this reads its source; the phone's twin is tested by
        behaviour (scripts/pure-logic-tests.mjs, billsTableLines)."""
        web = self._src("webapp/src/components/BillsTable.tsx")
        for needle in ("How this adds up", "table.outside_heading",
                       "r.figure", "r.label"):
            self.assertIn(needle, web)
        for rel in ("webapp/src/components/MoneyMap.tsx",
                    "webapp/src/components/PlanBars.tsx"):
            self.assertIn("row.breakdown?.table", self._src(rel), rel)

    def test_the_budget_bars_print_words_beside_the_bills_row_alone(self):
        """Budget's bars print the Bills and Envelopes rows' words; a
        Savings row over its plan prints none there, as before."""
        bars = self._src("webapp/src/components/PlanBars.tsx")
        self.assertIn('(row.label === "Bills" || row.label === "Envelopes")'
                      '\n            && row.status', bars)
        self.assertNotIn("!row.judged && row.status", bars)

    def test_the_phone_map_prints_the_bills_caption_beside_its_breakdown(self):
        pure = self._src("mobile/src/lib/pure.ts")
        self.assertIn("older-server path: stands in for `breakdown`", pure)
        self.assertIn('label === "Bills" && breakdown === undefined', pure)
        mm = self._src("mobile/src/components/money-map.tsx")
        self.assertIn("terseCaption(row.label, row.caption, row.breakdown)",
                      mm)

    def test_the_month_card_calls_the_awaiting_part_by_the_rows_word(self):
        self.assertIn('<div className="sub">awaiting</div>',
                      self._src("webapp/src/pages/MonthLens.tsx"))
        self.assertIn('k="Awaiting"', self._src("mobile/src/app/month.tsx"))


class AMovedDueDateTests(unittest.TestCase):
    """A bill whose due date was changed after its month closed is the
    same bill: the Year counts it once."""

    def _june(self, conn):
        _a_month(conn)
        add_bill(conn, "Harbor Lofts", 1475.0, next_due=dt.date(2026, 7, 7),
                 last_seen=dt.date(2026, 6, 7))
        add_txn(conn, dt.date(2026, 6, 7), 1475.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 29),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
        freeze_month(conn, 2026, 6)

    def _year(self, conn):
        r = Reads(conn, dt.date(2026, 7, 2))
        _invariants(self, r.year_row["breakdown"], row=r.year_row)
        return r

    def test_a_late_bill_re_dated_inside_its_month_is_matched_by_bill(self):
        conn = make_db()
        self.addCleanup(conn.close)
        self._june(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 25),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
        r = self._year(conn)
        self.assertEqual(r.year_row["status"]["text"], "$1,610 still due")
        t = _table(r.year_row)
        self.assertNotIn("not_paid", _parts(t))
        self.assertEqual(t["outside"], [])

    def test_a_bill_re_dated_into_this_month_is_counted_once(self):
        conn = make_db()
        self.addCleanup(conn.close)
        self._june(conn)
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 7, 1),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
        t = _table(self._year(conn).year_row)
        self.assertIn(("Planned in Jun 2026, now due this month", "+$135"),
                      [(x["label"], x["figure"]) for x in t["rows"]])
        self.assertNotIn("not_paid", _parts(t))

    def test_a_weekly_schedule_re_anchored_by_a_day_is_counted_once(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Willow Daycare", 285.0, frequency="WEEKLY",
                 next_due=dt.date(2026, 6, 30), last_seen=dt.date(2026, 6, 23))
        for d in (2, 9, 16, 23):
            add_txn(conn, dt.date(2026, 6, d), 285.0, "WILLOW DAYCARE")
        freeze_month(conn, 2026, 6)
        add_bill(conn, "Willow Daycare", 285.0, frequency="WEEKLY",
                 next_due=dt.date(2026, 7, 1), last_seen=dt.date(2026, 6, 23))
        parts = _parts(_table(self._year(conn).year_row))
        self.assertEqual(parts["moved"], 285)
        self.assertNotIn("under", parts)


class AMissedDateBesideTheNextTests(unittest.TestCase):
    """A twice-monthly bill missed on May 27 and due again on Jun 2: the
    Jun 2 date is a real next occurrence, not the May date moved, so the
    miss stays the miss — "moved" is said only when the bill's current
    schedule no longer has the closed month's date."""

    def _year(self, *, paid_may_2):
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Willow Daycare", 285.0, by_month_day=[2, 27],
                 next_due=dt.date(2026, 5, 2), last_seen=dt.date(2026, 4, 27))
        if paid_may_2:
            add_txn(conn, dt.date(2026, 5, 2), 285.0, "WILLOW DAYCARE")
        freeze_month(conn, 2026, 5)
        # mid-June: the walk no longer carries the May date
        r = Reads(conn, dt.date(2026, 6, 15))
        _invariants(self, r.year_row["breakdown"], row=r.year_row)
        return r.year_row

    def test_the_miss_reads_not_paid(self):
        row = self._year(paid_may_2=False)
        parts = _parts(_table(row))
        self.assertNotIn("moved", parts)
        self.assertEqual(parts["not_paid"], 570)
        self.assertEqual(row["breakdown"]["moved"], 0)

    def test_a_bill_paid_on_its_other_date_is_not_moved_either(self):
        row = self._year(paid_may_2=True)
        self.assertNotIn("moved", _parts(_table(row)))
        self.assertEqual(row["breakdown"]["moved"], 0)


class APaymentExactInCentsTests(unittest.TestCase):
    def test_a_whole_dollar_payment_prints_exactly_beside_bills_not_paid(self):
        """$600.00 paid and four bills of $100.40 not paid: posted reads
        $600, the paid list's sum; the bills not paid carry the cents."""
        conn = make_db()
        self.addCleanup(conn.close)
        _a_month(conn)
        add_bill(conn, "Harbor Lofts", 600.0, next_due=dt.date(2026, 6, 3),
                 last_seen=dt.date(2026, 5, 3))
        add_txn(conn, dt.date(2026, 6, 3), 600.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        for i, p in enumerate(["Kestrel Power", "Willow Daycare",
                               "Lantern Shop", "Bean Haus"]):
            add_bill(conn, p, 100.40, next_due=dt.date(2026, 6, 10 + i),
                     frequency="YEARLY", last_seen=dt.date(2025, 6, 10 + i))
        freeze_month(conn, 2026, 6)
        conn.commit()
        d = lenses.month_summary(conn, 2026, 6, today=dt.date(2026, 8, 20))
        row = _bills_row(d["plan_rows"])
        self.assertEqual(row["actual"], 600.0)
        self.assertEqual(d["bills"]["bills_posted"], 600.0)
        items = sum(int(n.rsplit("$", 1)[1]) for n in d["bills"]["notes"]
                    if n.startswith("not paid:"))
        self.assertEqual(600 + items, row["budget"])
        self.assertEqual(row["status"]["text"], f"${items} not paid")
        t = _table(row)
        self.assertEqual(t["rows"][0]["figure"], "+$600")
        self.assertEqual(sum(x["amount"] for x in t["rows"]
                             if x["label"].startswith("Not paid: ")), items)


class AnUnreadableStoredMonthTests(unittest.TestCase):
    def test_a_malformed_due_date_leaves_the_row_without_words(self):
        st = {"buckets": {"fixed": {"month_budget": 200.0, "actual": 100.0,
                                    "envelopes": []}},
              "overdue_unpaid": [("Kestrel Power", 100.0, "not a date", None)],
              "paid_bills": []}
        w = lenses.closed_bill_words(st)
        self.assertEqual((w["breakdown"], w["notes"], w["status"]),
                         (None, [], None))


# ---- the parts, read by themselves ----------------------------------------

class PartsTests(unittest.TestCase):
    def test_late_months_carry_their_years_and_are_never_collapsed(self):
        from oikonome.web import bills_breakdown as bb
        bd = bb.from_parts("running", plan=1000.0, posted=600.0, due=300.0,
                           late=[("Kestrel Power", 135.0, dt.date(2025, 7, 29)),
                                 ("Harbor Lofts", 85.0, dt.date(2026, 6, 28))])
        self.assertEqual([i["label"] for i in bb.table(bd)["outside"]],
                         ["Late from Jul 2025: Kestrel Power",
                          "Late from Jun 2026: Harbor Lofts"])
        self.assertEqual(bb.months_words(bd["late"]),
                         "Jul 2025 and Jun 2026")
        bd = bb.from_parts("running", plan=1000.0, posted=600.0, due=300.0,
                           late=[("Kestrel Power", 135.0, dt.date(2026, 5, 29)),
                                 ("Harbor Lofts", 85.0, dt.date(2026, 6, 28))])
        self.assertEqual(bb.months_words(bd["late"]), "May and Jun 2026")

    def test_refunds_past_the_charges_are_their_own_row(self):
        """A month whose bill refunds came to more than its charges never
        prints a posted figure below nothing: posted is nothing, the
        refunds past the charges are their own row with its minus, what
        the plan had left is under plan (never "over plan"), and the rows
        still add up to the plan as printed."""
        from oikonome.web import bills_breakdown as bb
        bd = bb.from_parts("closed", plan=0.40, posted=-63.25,
                           unpaid=[("Kestrel", 17.30, "2026-06-12")])
        self.assertEqual((bd["posted"], bd["refunds"], bd["not_paid"],
                          bd["under"]), (0, 63, 17, 46))
        self.assertEqual([(r["label"], r["figure"])
                          for r in bb.table(bd)["rows"]],
                         [("Paid", "+$0"), ("Refunds beyond charges", "−$63"),
                          ("Not paid: Kestrel", "+$17"), ("Under plan", "+$46"),
                          ("The month's plan", "$0")])
        # the refund first, as the plan's remainder is mostly refunds
        self.assertEqual(bd["text"], "$63 refunded · $17 not paid")
        _invariants(self, bd)
        row = {"label": "Bills", "judged": False, "actual": 0.0,
               "expected": 0.0, "budget": 0.0, "caption": None}
        bb.apply_to_row(row, bd)
        self.assertEqual((row["actual"], row["caption"].split(" · ")[0]),
                         (0.0, "posted $0"))

    def test_a_bill_missed_on_several_dates_is_one_row(self):
        """A weekly bill missed on three dates is one "Not paid" row, its
        dates' dollars added, so no label names two figures."""
        from oikonome.web import bills_breakdown as bb
        bd = bb.from_parts("closed", plan=400.0, posted=100.0,
                           unpaid=[("Willow Daycare", 100.0, f"2026-06-{d:02d}")
                                   for d in (9, 16, 23)])
        self.assertEqual([(r["label"], r["figure"])
                          for r in bb.table(bd)["rows"]],
                         [("Paid", "+$100"), ("Not paid: Willow Daycare", "+$300"),
                          ("The month's plan", "$400")])
        _invariants(self, bd)

    def test_the_year_nets_a_refund_month_against_the_others(self):
        """A refund month beside months that posted: the Year's posted is
        the months' net, and the refunds are a row only when the year as
        a whole refunded more than it was charged."""
        from oikonome.web import bills_breakdown as bb
        jan = dict(bb.from_parts("closed", plan=40.0, posted=-63.0), y=2026,
                   m=1)
        feb = dict(bb.from_parts("closed", plan=500.0, posted=500.0), y=2026,
                   m=2)
        yr = bb.year([jan, feb])
        self.assertEqual((yr["posted"], yr["refunds"]), (437, 0))
        self.assertNotIn("refunds", _parts(bb.table(yr)))
        _invariants(self, yr)
        yr = bb.year([jan])
        self.assertEqual((yr["posted"], yr["refunds"]), (0, 63))
        _invariants(self, yr)

    def test_a_carried_bill_matches_its_month_by_date_not_spelling(self):
        from oikonome.web import bills_breakdown as bb
        june = dict(bb.from_parts(
            "closed", plan=1610.0, posted=1475.0,
            unpaid=[("Kestrel Power", 135.0, "2026-06-29")]), y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=1610.0, posted=0.0, due=1610.0,
            late=[("KESTREL", 135.0, dt.date(2026, 6, 29))]),
            y=2026, m=7)
        yr = bb.year([june, july])
        self.assertEqual(yr["not_paid"], 0)
        self.assertEqual(yr["late"][0]["inside_plan"], True)
        self.assertEqual(yr["posted"] + yr["due"] + 135 + yr["under"],
                         yr["plan"])

    def test_a_late_bill_is_matched_only_in_its_own_month(self):
        """A bill late from a month the year holds no plan for is outside
        the plan: it never takes the same bill's unpaid date of another
        month."""
        from oikonome.web import bills_breakdown as bb
        u = "00000000-0000-4000-8000-0000000000e1"
        june = dict(bb.from_parts(
            "closed", plan=500.0, posted=365.0,
            unpaid=[("Kestrel Power", 135.0, "2026-06-29", "rec:kp", u)]),
            y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=500.0, posted=0.0, due=500.0,
            late=[("Kestrel Power", 135.0, dt.date(2026, 5, 29), "rec:kp",
                   u)]), y=2026, m=7)
        yr = bb.year([june, july])
        self.assertEqual(yr["not_paid"], 135)
        self.assertEqual([i["inside_plan"] for i in yr["late"]], [False])
        _invariants(self, yr)

    def test_a_label_is_never_both_inside_the_total_and_below_it(self):
        """Two late dates of one bill in one month, one the year planned
        and one it did not: each row names its date."""
        from oikonome.web import bills_breakdown as bb
        june = dict(bb.from_parts(
            "closed", plan=600.0, posted=455.0,
            unpaid=[("Willow Daycare", 145.0, "2026-06-23")]), y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=600.0, posted=0.0, due=600.0,
            late=[("Willow Daycare", 145.0, dt.date(2026, 6, 23)),
                  ("Willow Daycare", 145.0, dt.date(2026, 6, 30))]),
            y=2026, m=7)
        yr = bb.year([june, july])
        t = bb.table(yr)
        inside = [r["label"] for r in t["rows"] if r["label"].startswith("Late")]
        below = [r["label"] for r in t["outside"]]
        self.assertEqual((inside, below),
                         (["Late from Jun 23, 2026: Willow Daycare"],
                          ["Late from Jun 30, 2026: Willow Daycare"]))
        _invariants(self, yr)

    def test_two_bills_of_one_payee_late_on_one_date_read_apart(self):
        """Two different bills named alike, late on one date, one the year
        planned and one it did not: each row carries its amount."""
        from oikonome.web import bills_breakdown as bb
        u1 = "00000000-0000-4000-8000-0000000000f1"
        u2 = "00000000-0000-4000-8000-0000000000f2"
        jan = dict(bb.from_parts(
            "closed", plan=500.0, posted=355.0,
            unpaid=[("Willow", 145.0, "2026-01-21", "rec:willow", u1)]),
            y=2026, m=1)
        feb = dict(bb.from_parts(
            "running", plan=500.0, posted=0.0, due=500.0,
            late=[("Willow", 145.0, dt.date(2026, 1, 21), "rec:willow", u1),
                  ("Willow", 90.0, dt.date(2026, 1, 21), "rec:willow-2",
                   u2)]), y=2026, m=2)
        t = bb.table(bb.year([jan, feb]))
        self.assertEqual(
            ([r["label"] for r in t["rows"] if r["label"].startswith("Late")],
             [r["label"] for r in t["outside"]]),
            (["Late from Jan 21, 2026: Willow ($145)"],
             ["Late from Jan 21, 2026: Willow ($90)"]))
        _invariants(self, bb.year([jan, feb]))

    def test_a_past_year_is_named_by_its_number(self):
        from oikonome.web import bills_breakdown as bb
        jun = dict(bb.from_parts("closed", plan=900.0, posted=880.0),
                   y=2025, m=6)
        yr = bb.year([jun])
        self.assertEqual(bb.table(yr)["rows"][0]["label"], "Paid in 2025")
        self.assertEqual(bb.caption(yr),
                         "posted in 2025 $880 · not counted in the verdict")
        self.assertNotIn("this year", " ".join(_labels(bb.table(yr))))

    def test_two_entries_with_their_own_uids_never_match_by_date(self):
        from oikonome.web import bills_breakdown as bb
        june = dict(bb.from_parts(
            "closed", plan=1610.0, posted=1475.0,
            unpaid=[("Kestrel Power", 135.0, "2026-06-29", "rec:kestrel-power",
                     "00000000-0000-4000-8000-00000000000a")]), y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=1610.0, posted=0.0, due=1610.0,
            late=[("Lantern Shop", 90.0, dt.date(2026, 6, 29),
                   "rec:lantern-shop",
                   "00000000-0000-4000-8000-00000000000b")]), y=2026, m=7)
        yr = bb.year([june, july])
        self.assertEqual(yr["not_paid"], 135)
        self.assertEqual([i["inside_plan"] for i in yr["late"]], [False])
        _invariants(self, yr)


class TheRowSaysTwoFiguresTests(unittest.TestCase):
    """Whatever the parts, the row says posted (its caption) and still
    due (its words); every other part is a signed row of its table."""

    def test_a_month_of_every_kind_of_part_still_says_two_figures(self):
        from oikonome.web import bills_breakdown as bb
        bd = bb.from_parts("running", plan=1000.0, posted=650.0, due=300.0,
                           late=[("Kestrel", 85.0, dt.date(2026, 6, 28))])
        self.assertEqual(bd["text"], "$385 still due")
        self.assertEqual(bb.caption(bd), "posted $650 · plan $1,000"
                         " · not counted in the verdict")
        t = bb.table(bd)
        self.assertEqual(
            [(r["label"], r["figure"]) for r in t["rows"]],
            [("Paid so far", "+$650"),
             ("Due this month, not paid yet", "+$300"),
             ("Under plan", "+$50"), ("This month's plan", "$1,000")])
        _invariants(self, bd)

    def test_the_year_never_calls_a_year_figure_this_months(self):
        from oikonome.web import bills_breakdown as bb
        june = dict(bb.from_parts(
            "closed", plan=1610.0, posted=1475.0,
            unpaid=[("Kestrel Power", 135.0, "2026-06-29")]), y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=1610.0, posted=0.0, due=1500.0,
            late=[("Kestrel Power", 135.0, dt.date(2026, 6, 29))]),
            y=2026, m=7)
        yr = bb.year([june, july])
        self.assertTrue(yr["text"].startswith("$1,635 still due"), yr["text"])
        self.assertEqual(bb.caption(yr), "posted this year $1,475"
                         " · not counted in the verdict")
        self.assertEqual(bb.table(yr)["rows"][0]["label"], "Paid this year")
        _invariants(self, yr)

    def test_today_and_the_year_name_one_figure_alike_and_two_apart(self):
        """The still-due figure is the same on Today and the Year and
        reads the same; posted is a different figure on each and reads
        differently ("posted" / "posted this year")."""
        from oikonome.web import bills_breakdown as bb
        june = dict(bb.from_parts("closed", plan=900.0, posted=880.0),
                    y=2026, m=6)
        july = dict(bb.from_parts(
            "running", plan=950.0, posted=400.0, due=520.0,
            late=[("Kestrel Power", 70.0, dt.date(2026, 5, 20))]),
            y=2026, m=7)
        yr = bb.year([june, july])
        self.assertEqual(july["text"], yr["text"])
        self.assertEqual(_caption(bb.caption(july))[0], (400, "posted"))
        self.assertEqual(_caption(bb.caption(yr))[0],
                         (1280, "posted this year"))


def _invariants(case, bd, *, row=None):
    """The breakdown, its table and the row printed from them, for one
    month or the Year."""
    yr = bd["kind"] == "year"
    late = sum(i["amount"] for i in bd["late"])
    inside = sum(i["amount"] for i in bd["late"]
                 if yr and i["inside_plan"])
    # the parts add up to the plan, to the dollar
    case.assertEqual(bd["posted"] - bd["refunds"] + bd["due"]
                     + bd["not_paid"] + bd["under"]
                     + bd["moved"] + inside, bd["plan"])
    # no envelope part is left on the Bills row
    case.assertFalse([k for k in bd if k.startswith("envelope")
                      or k in ("pool_draw", "env_items")], sorted(bd))
    case.assertEqual(bd["still_due"], bd["due"] + late)
    case.assertGreaterEqual(min(bd["posted"], bd["due"], bd["awaiting"]), 0)
    case.assertLessEqual(bd["awaiting"], bd["due"])
    case.assertFalse(bd["posted"] and bd["refunds"])
    # the table: signed rows that add up to its last, the plan as printed
    t = _table(row) if row is not None and row.get("breakdown") else \
        _table(bd)
    rows, total = t["rows"][:-1], t["rows"][-1]
    case.assertEqual((total["sign"], total["amount"]), ("=", bd["plan"]))
    case.assertEqual(sum(r["amount"] if r["sign"] == "+" else -r["amount"]
                         for r in rows), bd["plan"])
    for r in rows + t["outside"]:
        case.assertIn(r["sign"], ("+", "-"))
        case.assertGreaterEqual(r["amount"], 0)
        # every row says its sign: a subtracted one its minus
        case.assertEqual(r["figure"], ("−" if r["sign"] == "-" else "+")
                         + f"${r['amount']:,}")
    # a row that prints $0 is left out (paid, the first, always shows)
    case.assertNotIn(0, [r["amount"] for r in rows[1:] + t["outside"]])
    case.assertTrue(rows[0]["label"].startswith("Paid"))
    labels = _labels(t)
    case.assertEqual(len(labels), len(set(labels)), labels)
    parts = _parts(t)
    case.assertEqual(parts.get("posted", 0), bd["posted"])
    case.assertEqual(parts.get("refunds", 0), -bd["refunds"])
    case.assertEqual(parts.get("due", 0), bd["due"])
    case.assertEqual(parts.get("late", 0), inside)
    case.assertEqual(parts.get("not_paid", 0), bd["not_paid"])
    case.assertEqual(parts.get("moved", 0), bd["moved"])
    case.assertEqual(parts.get("under", 0), bd["under"])
    # a late bill outside the plan is listed below it, in no sum
    case.assertEqual(sum(r["amount"] for r in t["outside"]), late - inside)
    case.assertEqual(t["outside_heading"] is None, not t["outside"])
    # one label per figure: "Still due" names the row's figure, so it is
    # this month's own part only while no late bill is part of it
    if bd["late"]:
        case.assertNotIn("Still due", labels)
    # the Year never calls a year figure this month's
    if yr:
        case.assertFalse([w for w in labels if "this month's share" in w])
    # the row's words: its still-due figure; with nothing still due, how a
    # settled month or year came out; over plan whenever it is
    said = []
    if bd["still_due"] > 0:
        said.append(f"${bd['still_due']:,} still due")
    if bd["settled"] and bd["refunds"] > 0:
        said.append(f"${bd['refunds']:,} refunded")
    if bd["settled"] and bd["not_paid"] > 0:
        said.append(f"${bd['not_paid']:,} not paid")
    if bd["under"] < 0:
        said.append(f"over plan by ${-bd['under']:,}")
    elif bd["settled"] and bd["under"] > 0:
        said.append(f"${bd['under']:,} under plan")
    case.assertEqual(bd["text"], " · ".join(said[:2]) or None)
    # the bar's right figure: posted + still due running, else the plan
    case.assertEqual(bd["total"], bd["posted"] + bd["still_due"]
                     if bd["kind"] == "running" else bd["plan"])
    if row is not None:
        # every figure the row prints is the breakdown's
        case.assertEqual(row["actual"], bd["posted"])
        case.assertEqual(row["budget"], bd["total"])
        case.assertEqual(row["status"]["text"] if row["status"] else None,
                         bd["text"])
        if row["status"] and bd["still_due"] > 0:
            case.assertEqual((row["status"]["kind"], row["status"]["amount"]),
                             ("due", bd["still_due"]))
        elif row["status"] and bd["under"] < 0:
            case.assertEqual((row["status"]["kind"], row["status"]["amount"],
                              row["status"]["tone"]),
                             ("over", -bd["under"], "neg"))
        if row["status"]:
            case.assertEqual(row["status"]["tone"],
                             "neg" if bd["under"] < 0 else "mut")
            case.assertEqual(row["status"]["kind"] == "over",
                             bd["still_due"] == 0 and bd["under"] < 0)
        cap = _caption(row["caption"])
        head = (f"posted in {bd['y']}" if yr and bd.get("past")
                else "posted this year" if yr else "posted")
        case.assertEqual(cap[0], (bd["posted"], head))
        if bd["kind"] == "running":
            case.assertEqual(cap[-1], (bd["plan"], "plan"))
            if bd["awaiting"] > 0:
                case.assertIn(f"${bd['awaiting']:,} awaiting", row["caption"])
        else:
            case.assertEqual(len(cap), 1)


class GeneratedPartsProperty(unittest.TestCase):
    """Many seeds of parts — bills paid under and over plan, refunds past
    the charges, late bills from last
    month, from another year and from two months, a payee spelled two
    ways, bills with and without a stable id (two bills on one date), a
    due date moved into the running month or a miss beside the next
    date, months over and the month running — through the breakdown, its
    table, the row printed from it and the Year fold. Long mode runs
    20,000 seeds."""

    UIDS = {"Kestrel": "00000000-0000-4000-8000-0000000000c1",
            "Willow": "00000000-0000-4000-8000-0000000000c2",
            "Daycare": "00000000-0000-4000-8000-0000000000c3"}

    def test_the_parts_hold_for_generated_months(self):
        from oikonome.web import bills_breakdown as bb
        n = props.examples(300) * (10 if props.LONG else 1)
        for seed in range(1, n + 1):
            rng = random.Random(seed)

            def cents(lo, hi):
                return round(rng.uniform(lo, hi), 2)

            def uid(name, frozen_before_ids=False):
                return None if frozen_before_ids else self.UIDS.get(name)
            months = []
            run_m = rng.randint(1, 12)

            for m in range(1, run_m + 1):
                plan = cents(50, 4000)
                if m < run_m:
                    old = rng.random() < 0.2      # frozen before bills had ids
                    unpaid = []
                    for k in range(rng.randint(0, 2)):
                        name = ("Kestrel", "Willow")[k]
                        unpaid.append((name, cents(5, 300),
                                       f"2026-{m:02d}-{rng.randint(1, 28):02d}",
                                       f"rec:{name.lower()}", uid(name, old)))
                    posted = max(0.0, plan - sum(x[1] for x in unpaid)
                                 + cents(-200, 200))
                    if rng.random() < 0.1:
                        # refunds past the month's charges
                        posted = -cents(1, 120)
                    covered = ([("Daycare", cents(5, 300), f"2026-{m:02d}-28",
                                 "rec:daycare", uid("Daycare", old))]
                               if rng.random() < 0.2 else [])
                    bd = bb.from_parts("closed", plan=plan, posted=posted,
                                       unpaid=unpaid, covered=covered)
                else:
                    late, own = [], []
                    prev = months[-1] if months else None
                    first = dt.date(2026, m, 1)
                    if prev and rng.random() < 0.6:
                        for i in (prev["unpaid"] + prev["covered"])[:2]:
                            name = (i["payee"].upper()
                                    if rng.random() < 0.3 else i["payee"])
                            u = self.UIDS.get(i["payee"])
                            if rng.random() < 0.15:
                                # a different bill on the same date
                                u = "00000000-0000-4000-8000-0000000000d9"
                            late.append((name, float(i["amount"]), i["due"],
                                         i["bill"], u))
                    if prev and rng.random() < 0.3:
                        # a closed month's bill now due a few days into
                        # this one: moved, or a miss beside its next date
                        for i in prev["unpaid"][:1]:
                            due = first + dt.timedelta(days=rng.randint(0, 4))
                            own.append((i["bill"], float(i["amount"]), due, 10,
                                        self.UIDS.get(i["payee"]),
                                        [i["due"]] if rng.random() < 0.5
                                        else []))
                    if rng.random() < 0.3:     # from another year
                        late.append(("Lantern Shop", cents(5, 300),
                                     dt.date(2025, 12, rng.randint(1, 28))))
                    posted = cents(0, plan * 1.2)
                    if rng.random() < 0.05:
                        posted = -cents(1, 120)
                    due = max(0.0, plan - posted + cents(-100, 100))
                    bd = bb.from_parts("running", plan=plan, posted=posted,
                                       due=due, awaiting=cents(0, due),
                                       late=late, own=own)
                months.append(dict(bd, y=2026, m=m))
            for mo in months:
                with self.subTest(seed=seed, m=mo["m"]):
                    row = {"label": "Bills", "judged": False, "actual": 0.0,
                           "expected": 0.0, "budget": 0.0,
                           "caption": "posted $0 · not counted in the verdict"}
                    bb.apply_to_row(row, mo)
                    _invariants(self, mo, row=row)
            if len(months) > 1:
                # the same months, finished: a past year
                done = bb.year(months[:-1], past=True)
                with self.subTest(seed=seed, past_year=True):
                    row = {"label": "Bills", "judged": False, "actual": 0.0,
                           "expected": 0.0, "budget": 0.0, "caption": None}
                    bb.apply_to_row(row, done)
                    _invariants(self, done, row=row)
            yr = bb.year(months)
            with self.subTest(seed=seed, year=True):
                row = {"label": "Bills", "judged": False, "actual": 0.0,
                       "expected": 0.0, "budget": 0.0, "caption": None}
                bb.apply_to_row(row, yr)
                _invariants(self, yr, row=row)
                run = months[-1]
                # the Year names the running month's own figures, and its
                # still-due figure is the running month's
                self.assertEqual((yr["due"], yr["awaiting"], yr["still_due"]),
                                 (run["due"], run["awaiting"],
                                  run["still_due"]))
                # ...in the same words; the Year's outcome is its own
                if run["still_due"] > 0:
                    self.assertTrue(yr["text"].startswith(
                        f"${run['still_due']:,} still due"), yr["text"])
                self.assertEqual(sum(i["amount"] for i in yr["late"]),
                                 sum(i["amount"] for i in run["late"]))
                # a late bill the year planned is in no not paid; one
                # with a stable id only ever took its own bill's entry
                unpaid = {(i["due"], i["uid"]) for i in yr["unpaid"]}
                for i in yr["late"]:
                    if i["inside_plan"] and i.get("uid"):
                        self.assertNotIn((i["due"], i["uid"]), unpaid)
                # the table sums its months: the year's plan is theirs
                self.assertEqual(bb.table(yr)["rows"][-1]["amount"],
                                 sum(mo["plan"] for mo in months))


# ---- generated households, read through every surface -------------------

def _household(seed: int):
    """A household with a plan and a ledger whose bills are paid under
    and over plan, on time or not at all; weekly and twice-monthly bills
    with a missed date; envelopes monthly, quarterly and yearly at any
    point of their period; sometimes no income; last month frozen or not;
    a due date moved or a payee renamed since it closed; read on the 1st, the 2nd,
    mid-month, month end or January 2."""
    rng = random.Random(seed)
    today = rng.choice([dt.date(2026, 7, 1), dt.date(2026, 7, 2),
                        dt.date(2026, 7, 15), dt.date(2026, 7, 31),
                        dt.date(2027, 1, 2), dt.date(2026, 11, 20)])
    conn = make_db()
    over = {"food_monthly": 500, "other_monthly": 400}
    if rng.random() < 0.7:
        over["budgeted_income_monthly"] = 4800
    write_config(conn, **over)
    first = today.replace(day=1)
    prev = (first - dt.timedelta(days=1)).replace(day=1)
    payees = ["Harbor Lofts", "Kestrel Power", "Willow Daycare",
              "Lantern Shop"]
    made = []
    for i, payee in enumerate(payees[:rng.randint(1, 4)]):
        amt = round(rng.uniform(20, 1600), 2)
        kind = rng.choice(["monthly", "monthly", "weekly", "twice"])
        day = rng.randint(1, 28)
        kw = ({"frequency": "WEEKLY"} if kind == "weekly"
              else {"by_month_day": [5, 20]} if kind == "twice" else {})
        nd = prev.replace(day=5 if kind == "twice" else day)
        last = nd - dt.timedelta(days=7 if kind == "weekly" else 30)
        add_bill(conn, payee, amt, next_due=nd, last_seen=last, **kw)
        made.append((payee, amt, nd, last, kw))
        # last month's and this month's dates: paid at, under or over
        # plan, or not at all
        d, n = nd, 0
        while d <= today:
            n += 1
            if rng.random() >= 0.2:
                paid = round(amt + rng.choice([0, 0, -1, 1])
                             * rng.uniform(0, 30), 2)
                add_txn(conn, d, max(paid, 1.0), payee.upper(), account="chk",
                        primary="RENT_AND_UTILITIES",
                        txn_id=f"b{seed}-{i}-{n}")
            if kind == "weekly":
                d += dt.timedelta(days=7)
            elif kind == "twice" and d.day == 5:
                d = d.replace(day=20)
            else:
                d = (d.replace(day=1) + dt.timedelta(days=32)).replace(
                    day=5 if kind == "twice" else day)
    for months, payee in ((1, "Bean Haus"), (3, "Hobby Pool"),
                          (12, "Namehost")):
        if rng.random() < 0.5:
            add_bill(conn, payee, round(rng.uniform(40, 900), 2),
                     bill_type="envelope", frequency=None, interval=months,
                     category="ENTERTAINMENT", match_category=True)
            for n in range(rng.randint(0, 3)):
                add_txn(conn, today - dt.timedelta(days=rng.randint(0, 200)),
                        round(rng.uniform(5, 300), 2), payee.upper(),
                        primary="ENTERTAINMENT",
                        txn_id=f"e{seed}-{months}-{n}")
    if rng.random() < 0.75:
        freeze_month(conn, prev.year, prev.month)
        if rng.random() < 0.3:
            # a due date moved by a day or two since the month closed
            payee, amt, nd, last, kw = made[-1]
            add_bill(conn, payee, amt, last_seen=last,
                     next_due=nd + dt.timedelta(days=rng.choice([-2, 1, 2])),
                     **kw)
        if rng.random() < 0.3:
            conn.execute("UPDATE bills SET payee=upper(payee) "
                         "WHERE payee=%s", (payees[0],))
    return conn, today


class GeneratedHouseholdsProperty(unittest.TestCase):
    """For every seed and every surface: (1) the parts add up to the
    plan, (2) every printed figure is a part or a stated sum, (3) a
    figure carries one label everywhere, (4) Today, Budget, Bills,
    Month and Year agree for the month, (5) no late bill is counted
    twice or inside "under plan", (6) the sentence adds up as printed."""

    def test_every_surface_prints_one_breakdown(self):
        for seed in props.seeds(24):
            conn, today = _household(seed)
            try:
                r = Reads(conn, today)
                prev = today.replace(day=1) - dt.timedelta(days=1)
                closed = lenses.month_summary(conn, prev.year, prev.month,
                                              today=today)
                nxt = today.replace(day=28) + dt.timedelta(days=5)
                future = lenses.month_summary(conn, nxt.year, nxt.month,
                                              today=today)
            finally:
                conn.close()
            with self.subTest(seed=seed, today=today.isoformat()):
                self._check(r, closed, future)

    def _check(self, r, closed, future):
        bd = r.today["breakdown"]
        # the breakdown, its table and Today's row, which Budget draws
        _invariants(self, bd, row=r.today)
        # the Month lens's running month is the same breakdown and table
        self.assertEqual(r.month_row["breakdown"], bd)
        _invariants(self, bd, row=r.month_row)
        self.assertEqual(r.month["bills"]["breakdown"], bd)
        self.assertEqual((r.month["bills"]["bills_posted"],
                          whole_dollars(r.month["bills"]["bills_planned"]),
                          r.month["bills"]["bills_awaiting"]),
                         (bd["posted"], bd["plan"], bd["awaiting"]))
        self.assertEqual(r.month["bills"]["notes"], [])
        # the Bills page's tile is the still-due figure, under its label
        self.assertEqual((r.tile["label"], r.tile["amount"]),
                         ("Still due", bd["still_due"]))
        # the still-due figure is the bills half of the month-end figure
        # the cash tiles subtract, as printed beneath them
        self.assertEqual(r.st["month_end_parts"]["bills"], bd["still_due"])
        env = r.today_env
        if env is not None:
            eb = env["breakdown"]
            self.assertEqual(eb["reserve"],
                             r.st["month_end_parts"]["envelopes"])
            self.assertLessEqual(env["actual"], env["budget"] + 0.5)
            self.assertEqual((env["status"] or {}).get("tone") == "neg",
                             eb["overflow"] > 0)
        else:
            self.assertEqual(r.st["month_end_parts"]["envelopes"], 0)
        # nothing in two rows: in cents, the Bills row's posted and the
        # pools' draws are the month's fixed spend, and the map's rows
        # add up to the month's out — as printed, Bills (apportioned) is
        # under a dollar off its cents and each pool's draw half a dollar
        fx = r.st["buckets"]["fixed"]
        self.assertAlmostEqual(fx["occ_actual"] + sum(
            e["used"] for e in fx["envelopes"]), fx["actual"], places=6)
        out = sum(x["actual"] for x in r.today_rows
                  if x["judged"] and x.get("actual_label") != "—")
        out += bd["posted"] - bd["refunds"]
        out += env["actual"] if env is not None else 0.0
        pools = len(env["breakdown"]["items"]) if env is not None else 0
        self.assertLess(abs(out - r.st["total_actual"]),
                        1 + 0.5 * pools + 1e-6)
        # the emails print the row's two figures, and no table
        if bd["text"]:
            self.assertIn(bd["text"], r.plain)
            self.assertIn(bd["text"], r.mail_bills["note"])
        for label in _labels(bd["table"])[1:-1]:
            self.assertNotIn(label, r.mail_bills["note"])
        self.assertEqual((r.mail_bills["actual"], r.mail_bills["plan"]),
                         (bd["posted"], bd["plan"]))
        # the Year: the sum of its months, its still due the running
        # month's in the same words
        if r.year_row is not None and r.year_row["breakdown"] is not None:
            yb = r.year_row["breakdown"]
            _invariants(self, yb, row=r.year_row)
            if r.month.get("budget_set"):
                self.assertEqual((yb["due"], yb["awaiting"], yb["still_due"]),
                                 (bd["due"], bd["awaiting"], bd["still_due"]))
                if bd["still_due"] > 0:
                    self.assertTrue(yb["text"].startswith(
                        f"${bd['still_due']:,} still due"), yb["text"])
                self.assertEqual(sum(i["amount"] for i in yb["late"]),
                                 sum(i["amount"] for i in bd["late"]))
        # a closed month: nothing still due; a future one: its plan due
        for d in (closed, future):
            if not d["plan_rows"]:
                continue
            row = _bills_row(d["plan_rows"])
            _invariants(self, row["breakdown"], row=row)
            self.assertEqual(whole_dollars(d["bills"]["bills_planned"]),
                             row["breakdown"]["plan"])
        if closed["plan_rows"]:
            cb = _bills_row(closed["plan_rows"])["breakdown"]
            self.assertEqual((cb["due"], cb["late"]), (0, []))
            self.assertNotIn("still due", cb["text"] or "")


if __name__ == "__main__":
    unittest.main()
