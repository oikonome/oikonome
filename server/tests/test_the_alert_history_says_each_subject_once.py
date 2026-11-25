"""The alert history says each subject once.

A count that is re-worded as it moves ("19 proposed recurring changes",
"18 …", "17 …") was logged as one row per wording, so the history was
mostly a counter ticking. The history is one line per subject: the latest
wording, the first day the subject was ever seen and the last. Every line
says where it is acted on, so a cold load of the page (no Today cached)
still opens a charge's alert on that charge, not on the whole ledger.
"""

import datetime as dt
import unittest

from oikonome.engine import alerts, anomalies

from .util import TODAY, add_txn, make_db, write_config


def _day(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


class HistoryLinesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _say(self, day, *rows):
        alerts.log(self.conn, [{"kind": k, "severity": "info", "message": m}
                               for k, m in rows], day)

    def test_a_count_that_moves_is_one_line_with_its_latest_wording(self):
        for n, ago in ((19, 30), (18, 20), (17, 10)):
            self._say(_day(ago), ("proposals",
                                  f"{n} proposed recurring changes awaiting "
                                  f"review on the Bills page."))
        self._say(_day(5))                       # cleared
        lines = [r for r in alerts.history_lines(self.conn)
                 if r["kind"] == "proposals"]
        self.assertEqual(len(lines), 1, [r["message"] for r in lines])
        self.assertTrue(lines[0]["message"].startswith("17 proposed"))
        self.assertEqual(lines[0]["first_seen"], _day(30))
        self.assertEqual(lines[0]["last_seen"], _day(10))
        self.assertEqual(lines[0]["link"], "/bills")
        self.assertEqual(lines[0]["kind_label"], "bill proposals")
        self.assertEqual(lines[0]["group"], "bills")

    def test_a_line_in_an_older_wording_reads_in_todays(self):
        """The history prints today's words (`text`) while `message` stays
        the words as logged — the key dismiss and restore match on."""
        old = ("1 merchant pair look like one business (found 01/02/26) "
               "— review on the Merchants page.")
        self._say(_day(9), ("merges", old))
        self._say(_day(5))                       # cleared
        line = next(r for r in alerts.history_lines(self.conn)
                    if r["kind"] == "merges")
        self.assertEqual(line["text"],
                         "1 merchant pair looks like one merchant "
                         "(found 01/02/26) — review on the Merchants page.")
        self.assertEqual(line["message"], old)

    def test_restoring_a_reworded_line_through_the_door_restores_it(self):
        from unittest import mock

        from oikonome.web import api_today
        from oikonome.web.fields import Fields

        class _Kept:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, k):
                return getattr(self._c, k)

            def close(self):
                pass

        old = ("1 merchant pair look like one business (found 01/02/26) "
               "— review on the Merchants page.")
        self._say(_day(9), ("merges", old))
        alerts.dismiss(self.conn, "merges", old)
        self._say(_day(5))                       # cleared
        line = next(r for r in alerts.history_lines(self.conn)
                    if r["kind"] == "merges")
        self.assertEqual(line["dismissed"], 1)
        with mock.patch.object(api_today, "_conn",
                               return_value=_Kept(self.conn)):
            got = api_today.alerts_restore_api(
                {"tenant_id": "x"},
                Fields({"kind": line["kind"], "message": line["message"]}))
        self.assertEqual(got["restored"], 1)

    def test_a_live_subject_shows_its_live_wording_and_its_first_day(self):
        self._say(_day(9), ("merges", "3 new merchant pairs look like one "
                                      "merchant (found 01/01/26) — review."))
        self._say(_day(1), ("merges", "1 new merchant pair looks like one "
                                      "merchant (found 01/09/26) — review."))
        lines = [r for r in alerts.history_lines(self.conn)
                 if r["kind"] == "merges"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["active"], 1)
        self.assertTrue(lines[0]["message"].startswith("1 new"))
        self.assertEqual(lines[0]["first_seen"], _day(9))

    def test_one_bills_drift_is_one_subject_and_two_charges_are_two(self):
        self._say(_day(8), ("drift", "City Power plan updated $440.00 → "
                                     "$370.00 (tracks recent amounts)."),
                  ("anomaly", "First-ever charge from Acme Tools: $250 — "
                              "verify you recognize it"))
        self._say(_day(2), ("drift", "City Power plan updated $370.00 → "
                                     "$380.00 (tracks recent amounts)."),
                  ("anomaly", "First-ever charge from Bolt Hardware: $300 "
                              "— verify you recognize it"))
        lines = alerts.history_lines(self.conn)
        drift = [r for r in lines if r["kind"] == "drift"]
        self.assertEqual(len(drift), 1)
        self.assertIn("$380.00", drift[0]["message"])
        self.assertEqual(drift[0]["link"],
                         "/bills/history?payee=City%20Power")
        charges = [r for r in lines if r["kind"] == "anomaly"]
        self.assertEqual(len(charges), 2)

    def test_a_bonus_in_another_month_is_its_own_line(self):
        """A bonus, a large transfer and money pulled from investments are
        events of their month: March's and December's are two lines, each
        with its own amount; inside one month the growing total is one."""
        D = dt.date
        self._say(D(2026, 3, 15), ("bonus", "Bonus received: $8,000 — x."))
        self._say(D(2026, 3, 28), ("bonus", "Bonus received: $9,000 — x."))
        self._say(D(2026, 4, 2))                 # the month turned
        self._say(D(2026, 12, 20), ("bonus", "Bonus received: $6,500 — x."))
        self._say(D(2026, 12, 31))
        lines = [(r["message"][:22], r["first_seen"])
                 for r in alerts.history_lines(self.conn)
                 if r["kind"] == "bonus"]
        self.assertEqual(lines, [("Bonus received: $6,500", D(2026, 12, 20)),
                                 ("Bonus received: $9,000", D(2026, 3, 15))])

    def test_two_live_rows_of_one_subject_are_never_folded(self):
        """The bell counts live alerts and ✕ dismisses one: folding a live
        row into another would hide it from both."""
        for msg in ("2 charges awaiting reimbursement ($80, oldest 01/02/26)",
                    "3 charges awaiting reimbursement ($90, oldest 01/02/26)"):
            alerts.log(self.conn, [{"kind": "reimb", "severity": "info",
                                    "message": msg}], TODAY, partial=True)
        live = [r for r in alerts.history_lines(self.conn)
                if r["kind"] == "reimb" and r["active"]]
        self.assertEqual(len(live), 2)

    def test_a_charge_alert_opens_on_that_charge(self):
        cases = {
            "First-ever charge from Acme Tools: $250.40 — verify you "
            "recognize it": "/transactions?q=Acme%20Tools",
            "Possible double charge: Corner Cafe $12.50 ×2 (01/03 + 01/04)":
                "/transactions?q=Corner%20Cafe",
            "Fee charged: Foreign Transaction Fee $7.50 on 01/04 (Visa)":
                "/transactions?q=Foreign%20Transaction%20Fee",
            "Interest charged: Visa $31 on 01/04 — first ever on this "
            "account": "/transactions?q=interest",
        }
        for msg, want in cases.items():
            self.assertEqual(anomalies.link_for(msg), want, msg)
        built = alerts.build({"anomalies": list(cases)})
        self.assertEqual([a["link"] for a in built], list(cases.values()))
        self._say(TODAY, ("anomaly", next(iter(cases))))
        line = [r for r in alerts.history_lines(self.conn)
                if r["kind"] == "anomaly"][0]
        # a first-ever charge opens on the days it can have been charged
        # (it is written the day it posts or the next), not on the
        # merchant's whole history
        day = line["last_seen"]
        self.assertEqual(
            line["link"], "/transactions?q=Acme%20Tools"
            f"&date_from={day - dt.timedelta(days=1)}&date_to={day}")

    def test_two_double_charges_at_one_merchant_are_two_lines(self):
        """A double charge's subject keeps its amount: only its dates and
        its count move as it grows. Folding every double charge at one
        merchant into one line gave that line another charge's first day."""
        self._say(_day(40), ("anomaly", "Possible double charge: Corner "
                                        "Cafe $30.00 ×2 (01/02 + 01/03)"))
        self._say(_day(20), ("anomaly", "Possible double charge: Corner "
                                        "Cafe $45.00 ×2 (01/20 + 01/21)"))
        self._say(_day(19), ("anomaly", "Possible double charge: Corner "
                                        "Cafe $45.00 ×3 (01/20 + 01/21 + "
                                        "01/22)"))
        self._say(_day(1))                          # cleared
        lines = sorted((r for r in alerts.history_lines(self.conn)
                        if r["kind"] == "anomaly"),
                       key=lambda r: r["first_seen"])
        self.assertEqual(len(lines), 2, [r["message"] for r in lines])
        self.assertIn("$30.00", lines[0]["message"])
        self.assertEqual((lines[0]["first_seen"], lines[0]["last_seen"]),
                         (_day(40), _day(40)))
        self.assertIn("$45.00 ×3", lines[1]["message"])
        self.assertEqual((lines[1]["first_seen"], lines[1]["last_seen"]),
                         (_day(20), _day(19)))

    def test_a_number_in_a_name_keeps_two_accounts_apart(self):
        """Only amounts, dates and counts are taken out of a subject: an
        account's last four is part of its name."""
        self._say(_day(9), ("source-failover", "Card 1111: primary source "
                                               "is down — showing Card 2111 "
                                               "instead"))
        self._say(_day(3), ("source-failover", "Card 3333: primary source "
                                               "is down — showing Card 4333 "
                                               "instead"))
        lines = [r for r in alerts.history_lines(self.conn)
                 if r["kind"] == "source-failover"]
        self.assertEqual(len(lines), 2, [r["message"] for r in lines])

    def test_a_double_charge_opens_on_its_own_days(self):
        msg = "Possible double charge: Corner Cafe $12.50 ×2 (12/30 + 01/02)"
        self.assertEqual(
            anomalies.link_for(msg, on=dt.date(2027, 1, 3)),
            "/transactions?q=Corner%20Cafe&date_from=2026-12-30"
            "&date_to=2027-01-02")
        self._say(dt.date(2027, 1, 3), ("anomaly", msg))
        line = [r for r in alerts.history_lines(self.conn)
                if r["kind"] == "anomaly"][0]
        self.assertEqual(line["link"],
                         "/transactions?q=Corner%20Cafe&date_from=2026-12-30"
                         "&date_to=2027-01-02")
        built = alerts.build({"anomalies": [msg],
                              "today": dt.date(2027, 1, 3)})
        self.assertEqual(built[0]["link"], line["link"])

    def test_the_same_charge_alert_in_another_month_is_its_own_line(self):
        """A subscription double-billed in March and again in September,
        or the same fee charged in January and in March, are two
        occurrences: each is its own line with its own days, and each
        opens on its own charge."""
        D = dt.date
        self._say(D(2026, 3, 11), ("anomaly", "Possible double charge: "
                                              "Streamly $15.49 ×2 (03/10 + 03/11)"))
        self._say(D(2026, 3, 13))
        self._say(D(2026, 9, 5), ("anomaly", "Possible double charge: "
                                             "Streamly $15.49 ×2 (09/04 + 09/05)"))
        self._say(D(2026, 9, 7))
        self._say(D(2026, 1, 4), ("anomaly", "Fee charged: OVERDRAFT FEE "
                                             "$35.00 on 01/04 (Checking)"))
        self._say(D(2026, 1, 6))
        self._say(D(2026, 3, 9), ("anomaly", "Fee charged: OVERDRAFT FEE "
                                             "$35.00 on 03/09 (Checking)"))
        self._say(D(2026, 3, 10))
        got = sorted((r["first_seen"], r["last_seen"], r["link"])
                     for r in alerts.history_lines(self.conn)
                     if r["kind"] == "anomaly")
        self.assertEqual(got, [
            (D(2026, 1, 4), D(2026, 1, 4),
             "/transactions?q=OVERDRAFT%20FEE&date_from=2026-01-04"
             "&date_to=2026-01-04"),
            (D(2026, 3, 9), D(2026, 3, 9),
             "/transactions?q=OVERDRAFT%20FEE&date_from=2026-03-09"
             "&date_to=2026-03-09"),
            (D(2026, 3, 11), D(2026, 3, 11),
             "/transactions?q=Streamly&date_from=2026-03-10"
             "&date_to=2026-03-11"),
            (D(2026, 9, 5), D(2026, 9, 5),
             "/transactions?q=Streamly&date_from=2026-09-04"
             "&date_to=2026-09-05"),
        ])

    def test_a_fee_opens_on_its_own_day(self):
        msg = "Fee charged: Foreign Transaction Fee $7.50 on 12/30 (Visa)"
        self.assertEqual(
            anomalies.link_for(msg, on=dt.date(2027, 1, 2)),
            "/transactions?q=Foreign%20Transaction%20Fee"
            "&date_from=2026-12-30&date_to=2026-12-30")

    def test_merchant_pairs_have_their_own_chip(self):
        self._say(_day(2), ("merges", "1 new merchant pair looks like one "
                                      "merchant (found 01/09/26) — review."))
        line = [r for r in alerts.history_lines(self.conn)
                if r["kind"] == "merges"][0]
        self.assertEqual(line["group"], "merchants")

    def test_the_merge_alert_counts_new_pairs_and_says_merchant(self):
        one = alerts.build({"merge_offers": {"batch": 1,
                                             "found": "2026-01-09"}})[0]
        self.assertEqual(one["message"], "1 new merchant pair looks like one "
                         "merchant (found 01/09/26) — review on the "
                         "Merchants page.")
        two = alerts.build({"merge_offers": {"batch": 2,
                                             "found": "2026-01-09"}})[0]
        self.assertTrue(two["message"].startswith(
            "2 new merchant pairs look like one merchant"))


class AYearlyEventSaysItsYearTests(unittest.TestCase):
    """A card's annual fee, or a yearly bonus, recurs on the same day for
    the same amount. Its line has to name the year: the log keys a row on
    its words, so the same words a year apart would be one row and one
    history line, and nobody could tell which year it was about."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _raise(self, day, rows):
        alerts.log(self.conn, rows, day)
        alerts.log(self.conn, [], day + dt.timedelta(days=2))   # cleared

    def test_the_same_fee_a_year_apart_is_two_lines_with_their_years(self):
        for year in (2025, 2026):
            day = dt.date(year, 3, 14)
            add_txn(self.conn, day, 95, "ANNUAL MEMBERSHIP FEE",
                    account="card", primary="BANK_FEES",
                    detailed="BANK_FEES_OTHER_BANK_FEES")
            msgs = [m for m in anomalies.detect(self.conn, day)
                    if m.startswith("Fee charged")]
            self.assertEqual(len(msgs), 1, msgs)
            self.assertIn(f"on 03/14/{year % 100:02d} ", msgs[0])
            self._raise(day, [{"kind": "anomaly", "severity": "warn",
                               "message": msgs[0]}])
        lines = [r for r in alerts.history_lines(self.conn)
                 if r["kind"] == "anomaly"]
        self.assertEqual(sorted(r["first_seen"] for r in lines),
                         [dt.date(2025, 3, 14), dt.date(2026, 3, 14)])
        # each opens on its own year's charge
        self.assertEqual(sorted(r["link"].split("&", 1)[1] for r in lines),
                         ["date_from=2025-03-14&date_to=2025-03-14",
                          "date_from=2026-03-14&date_to=2026-03-14"])

    def test_the_same_bonus_a_year_apart_names_its_day(self):
        said = []
        for year in (2025, 2026):
            day = dt.date(year, 12, 19)
            built = alerts.build({"events": {
                "bonus": [(day.isoformat(), 4000.0)]}})
            msg = next(a["message"] for a in built if a["kind"] == "bonus")
            self.assertIn(f"12/19/{year % 100:02d}", msg)
            said.append(msg)
            self._raise(day, [{"kind": "bonus", "severity": "info",
                               "message": msg}])
        self.assertNotEqual(said[0], said[1])
        n = self.conn.execute("SELECT count(*) n FROM alerts_log "
                              "WHERE kind='bonus'").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_a_line_logged_without_a_year_still_opens_on_its_charge(self):
        """Lines logged before the days carried a year keep their words
        (the log's key); their days take the year the line was written."""
        msg = "Fee charged: Foreign Transaction Fee $7.50 on 12/30 (Visa)"
        self.assertEqual(anomalies.occurrence(msg, dt.date(2027, 1, 2)),
                         "2026-12-30")
        new = "Fee charged: Foreign Transaction Fee $7.50 on 12/30/26 (Visa)"
        self.assertEqual(anomalies.occurrence(new, dt.date(2027, 1, 2)),
                         "2026-12-30")
        dbl = "Possible double charge: Corner Cafe $12.50 ×2 (12/30/26 + 01/02/27)"
        self.assertEqual(
            anomalies.link_for(dbl, on=dt.date(2027, 1, 3)),
            "/transactions?q=Corner%20Cafe&date_from=2026-12-30"
            "&date_to=2027-01-02")



class ALineLoggedBeforeTheYearIsTheSameAlertTests(unittest.TestCase):
    """A line logged in the words an older build wrote ("on 03/14", no
    year) is the same alert as today's "on 03/14/26" for the same event:
    it keeps its dismissal and its mute, is reworded in place, and is not
    raised again. Another year's event is news."""

    OLD = "Fee charged: ANNUAL MEMBERSHIP FEE $95 on 03/14 (Test Card)"
    NEW = "Fee charged: ANNUAL MEMBERSHIP FEE $95 on 03/14/26 (Test Card)"
    DAY = dt.date(2026, 3, 14)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _log(self, day, message, kind="anomaly"):
        from unittest import mock
        with mock.patch("oikonome.notify.webhooks.emit") as emit:
            alerts.log(self.conn, [{"kind": kind, "severity": "warn",
                                    "message": message}], day)
        return [c.args[2]["message"] for c in emit.call_args_list]

    def _rows(self, kind="anomaly"):
        return self.conn.execute(
            "SELECT message, dismissed, active, mute_changed_at "
            "FROM alerts_log WHERE kind=%s ORDER BY id", (kind,)).fetchall()

    def test_a_dismissed_old_line_stays_dismissed_and_raises_nothing(self):
        self._log(self.DAY, self.OLD)
        alerts.dismiss(self.conn, "anomaly", self.OLD)
        self.assertEqual(self._log(self.DAY, self.NEW), [])
        rows = self._rows()
        self.assertEqual([(r["message"], r["dismissed"], r["active"])
                          for r in rows], [(self.NEW, 1, 1)])
        self.assertEqual(alerts.visible(self.conn, [
            {"kind": "anomaly", "message": self.NEW}]), [])

    def test_a_muted_old_line_stays_muted_and_its_mail_link_still_works(self):
        from oikonome.notify import mailact
        self._log(self.DAY, self.OLD)
        alerts.dismiss(self.conn, "anomaly", self.OLD)
        stamped = self._rows()[0]["mute_changed_at"]
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') t").fetchone()["t"]
        ref = mailact.alert_ref(tid, "anomaly", self.OLD)
        self._log(self.DAY, self.NEW)
        row = self._rows()[0]
        self.assertEqual((row["dismissed"], row["mute_changed_at"]),
                         (1, stamped))
        for value in (ref, self.OLD):      # digest and older raw tokens
            found = mailact._find_alert(self.conn, {
                "target": "anomaly", "value": value, "tenant_id": tid})
            self.assertIsNotNone(found, value)
            self.assertEqual(found["message"], self.NEW)

    def test_a_live_old_line_is_reworded_in_place_without_a_second_raise(self):
        self.assertEqual(self._log(self.DAY, self.OLD), [self.OLD])
        self.assertEqual(self._log(self.DAY, self.NEW), [])
        self.assertEqual([(r["message"], r["active"]) for r in self._rows()],
                         [(self.NEW, 1)])

    def test_the_same_bonus_in_its_old_words_is_the_same_alert(self):
        old = "Bonus received: $4,000 — excluded from all budget math."
        new = "Bonus received: $4,000 on 12/19/26 — excluded from all budget math."
        self._log(dt.date(2026, 12, 19), old, kind="bonus")
        alerts.dismiss(self.conn, "bonus", old)
        self.assertEqual(self._log(dt.date(2026, 12, 20), new, kind="bonus"),
                         [])
        self.assertEqual([(r["message"], r["dismissed"])
                          for r in self._rows("bonus")], [(new, 1)])

    def test_another_years_event_is_still_raised(self):
        self._log(dt.date(2025, 3, 14), self.OLD)
        alerts.log(self.conn, [], dt.date(2025, 3, 16))          # cleared
        alerts.dismiss(self.conn, "anomaly", self.OLD)
        self.assertEqual(self._log(self.DAY, self.NEW), [self.NEW])
        self.assertEqual(sorted(r["message"] for r in self._rows()),
                         sorted([self.OLD, self.NEW]))
        bonus_old = "Bonus received: $4,000 — excluded."
        self._log(dt.date(2025, 12, 19), bonus_old, kind="bonus")
        self.assertEqual(
            self._log(dt.date(2026, 12, 19),
                      "Bonus received: $4,000 on 12/19/26 — excluded.",
                      kind="bonus"),
            ["Bonus received: $4,000 on 12/19/26 — excluded."])

if __name__ == "__main__":
    unittest.main()
