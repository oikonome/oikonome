"""Unified alerts: build ordering + persistent log lifecycle. The
log/dismiss/restore semantics are frozen — every alert source feeds the
same generalized set."""

import datetime as dt
import unittest

from oikonome.engine import alerts

from .util import TODAY, add_txn, make_db, write_config


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_build_orders_worst_first(self):
        st = {"anomalies": ["Possible double charge: X $20 ×2"],
              "recurring_drifts": [{"payee": "City Power", "old": 440, "new": 370}],
              "stale_pulls": [{"kind": "stale-pull", "severity": "bad",
                               "message": "b"}]}
        a = alerts.build(st)
        sevs = [x["severity"] for x in a]
        self.assertEqual(sevs, sorted(sevs, key=["bad", "warn", "good", "info"].index))
        self.assertEqual(a[0]["severity"], "bad")

    def test_log_lifecycle(self):
        """New alert → active row; still-present → last_seen advances, no
        dup; gone → active=0 but the history row survives."""
        one = [{"kind": "anomaly", "severity": "warn", "message": "dup charge"}]
        alerts.log(self.conn, one, TODAY)
        alerts.log(self.conn, one, TODAY + dt.timedelta(days=1))
        rows = alerts.history(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_seen"], TODAY)
        self.assertEqual(rows[0]["last_seen"], TODAY + dt.timedelta(days=1))
        self.assertEqual(rows[0]["active"], 1)
        alerts.log(self.conn, [], TODAY + dt.timedelta(days=2))
        rows = alerts.history(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active"], 0)

    def test_partial_log_leaves_the_rest_of_the_set_alone(self):
        """alerts.log is a full-snapshot write, so a caller that passes
        ONLY its own rows — the hourly sync's failover branch, say — would
        deactivate and UN-DISMISS every other active alert, and the next
        Today load would re-activate them with dismissals reset, forever.
        partial=True contributes rows without asserting the whole set."""
        full = [{"kind": "anomaly", "severity": "warn",
                 "message": "dup charge"},
                {"kind": "drift", "severity": "info",
                 "message": "rent drifted"}]
        alerts.log(self.conn, full, TODAY)
        alerts.dismiss(self.conn, "anomaly", "dup charge")
        # the worker contributes a failover alert mid-day, partially
        alerts.log(self.conn, [{"kind": "source-failover", "severity":
                                "warn", "message": "chk: primary down"}],
                   TODAY, partial=True)
        rows = {(r["kind"], r["message"]): r
                for r in alerts.history(self.conn)}
        self.assertEqual(rows[("drift", "rent drifted")]["active"], 1,
                         "a partial log deactivated an unrelated alert")
        self.assertEqual(rows[("anomaly", "dup charge")]["dismissed"], 1,
                         "a partial log reset a user's dismissal")
        self.assertEqual(rows[("source-failover", "chk: primary down")]
                         ["active"], 1)

    def test_dismiss_hides_until_condition_clears(self):
        """Dismiss hides a persisting alert everywhere; when the condition
        clears and later RECURS, the alert shows again (recurrence is news)."""
        one = [{"kind": "anomaly", "severity": "warn", "message": "dup charge"}]
        alerts.log(self.conn, one, TODAY)
        alerts.dismiss(self.conn, "anomaly", "dup charge")
        self.assertEqual(alerts.visible(self.conn, one), [])
        # still dismissed while the condition persists
        alerts.log(self.conn, one, TODAY + dt.timedelta(days=1))
        self.assertEqual(alerts.visible(self.conn, one), [])
        # condition clears → dismissal resets; recurrence shows again
        alerts.log(self.conn, [], TODAY + dt.timedelta(days=2))
        alerts.log(self.conn, one, TODAY + dt.timedelta(days=9))
        self.assertEqual(alerts.visible(self.conn, one), one)

    def test_a_spurious_clear_keeps_the_dismissal_bit(self):
        """Two full-snapshot writers can disagree for a moment (Today load
        vs the hourly sweep). Clearing must not zero the dismissal — the
        'recurrence is news' reset belongs to REACTIVATION, where the
        upsert makes it atomic; otherwise the racing writer's re-log
        resurrected a dismissed alert."""
        one = [{"kind": "anomaly", "severity": "warn", "message": "dup charge"}]
        alerts.log(self.conn, one, TODAY)
        alerts.dismiss(self.conn, "anomaly", "dup charge")
        # a writer with a transiently different view asserts an empty set
        alerts.log(self.conn, [], TODAY)
        row = alerts.history(self.conn)[0]
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["dismissed"], 1,
                         "clearing reset the dismissal bit")

    def test_restore(self):
        one = [{"kind": "drift", "severity": "info", "message": "x plan updated"}]
        alerts.log(self.conn, one, TODAY)
        alerts.dismiss(self.conn, "drift", "x plan updated")
        alerts.restore(self.conn, "drift", "x plan updated")
        self.assertEqual(alerts.visible(self.conn, one), one)

    def test_build_sorts_stale_pulls_by_severity(self):
        st = {"stale_pulls": [
            {"kind": "stale-pull", "severity": "warn", "message": "w"},
            {"kind": "stale-pull", "severity": "bad", "message": "b"}],
            "recurring_drifts": [{"payee": "X", "old": 1, "new": 2}]}
        out = alerts.build(st)
        self.assertEqual([a["severity"] for a in out], ["bad", "warn", "info"])

    def test_freshness_sync_and_heartbeats(self):
        """Items with no clean sync → flagged; a fresh heartbeat clears its
        job; a stale one refires."""
        now = dt.datetime.now(dt.timezone.utc)
        jobs = [("nightly-report", "Nightly report", 50)]
        # the staleness judgment only applies to LIVE aggregators — a
        # file-only instance must never see "bank sync never completed" —
        # so make the fixture item a live one
        self.conn.execute(
            "UPDATE items SET aggregator='simplefin' WHERE id='it1'")
        out = alerts.freshness(self.conn, jobs=jobs, now_utc=now)
        msgs = " | ".join(a["message"] for a in out)
        self.assertIn("Bank sync has never completed", msgs)
        self.assertIn("Nightly report", msgs)
        # clean sync row + heartbeat clear both
        self.conn.execute(
            "INSERT INTO sync_log (item_id, added, modified, removed) "
            "VALUES ('it1', 1, 0, 0)")
        alerts.heartbeat(self.conn, "nightly-report")
        out = alerts.freshness(self.conn, jobs=jobs, now_utc=now)
        self.assertEqual(out, [])
        # stale heartbeat refires
        later = now + dt.timedelta(hours=60)
        out = alerts.freshness(self.conn, jobs=jobs, now_utc=later)
        self.assertIn("Nightly report",
                      " | ".join(a["message"] for a in out))


class AnomalyTests(unittest.TestCase):
    """Anomaly detection: what counts as news, and what does not."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def detect(self):
        from oikonome.engine import anomalies
        return anomalies.detect(self.conn, TODAY)

    def test_double_charge_flagged(self):
        add_txn(self.conn, TODAY, 45.00, "Uber Eats")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 45.00, "Uber Eats")
        msgs = self.detect()
        self.assertTrue(any("double charge" in m and "Uber Eats" in m
                            and "×2" in m for m in msgs), msgs)

    def test_separate_orders_same_price_not_flagged(self):
        """Separate orders at one price point carry distinct descriptors
        (the merchant's order reference) and must not read as a double
        charge; a pending row with the generic descriptor must not either."""
        for ref in ("1A2B3C", "2H4K9"):
            add_txn(self.conn, TODAY - dt.timedelta(days=1), 20.00,
                    f"AMAZON MKTPL*{ref}", merchant="Amazon")
        add_txn(self.conn, TODAY, 20.00, "AMAZON MKTPLACE PMTS",
                merchant="Amazon", pending=1)
        self.assertEqual(self.detect(), [])

    def test_pending_duplicate_waits_for_posting(self):
        add_txn(self.conn, TODAY, 45.00, "UBER EATS", pending=1)
        add_txn(self.conn, TODAY, 45.00, "UBER EATS", pending=1)
        self.assertEqual(self.detect(), [])

    def test_same_amount_far_apart_not_flagged(self):
        add_txn(self.conn, TODAY, 25.0, "Riverbend Counseling")
        add_txn(self.conn, TODAY - dt.timedelta(days=7), 25.0,
                "Riverbend Counseling")
        self.assertEqual(self.detect(), [])

    def test_small_duplicates_ignored(self):
        add_txn(self.conn, TODAY, 5.50, "Corner Creamery")
        add_txn(self.conn, TODAY, 5.50, "Corner Creamery")
        self.assertEqual(self.detect(), [])

    def test_older_twin_does_not_hide_fresh_pair(self):
        """A same-descriptor charge from three days ago is not a duplicate
        of today's, but it must not mask the real pair either: yesterday's
        and today's charges are a double charge on their own."""
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 45.00, "Uber Eats")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 45.00, "Uber Eats")
        add_txn(self.conn, TODAY, 45.00, "Uber Eats")
        msgs = self.detect()
        self.assertTrue(any("double charge" in m and "×2" in m for m in msgs),
                        msgs)

    def test_stale_pair_not_renagged(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 45.00, "Uber Eats")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 45.00, "Uber Eats")
        self.assertEqual(self.detect(), [])

    def test_new_large_merchant_flagged(self):
        add_txn(self.conn, TODAY, 450.0, "SHADY HOLDINGS LLC")
        msgs = self.detect()
        self.assertTrue(any("First-ever" in m and "SHADY HOLDINGS" in m
                            for m in msgs), msgs)

    def test_known_merchant_large_charge_ok(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=200), 12.0, "Costco")
        add_txn(self.conn, TODAY, 450.0, "Costco")
        self.assertEqual(self.detect(), [])

    def test_new_small_merchant_ok(self):
        add_txn(self.conn, TODAY, 45.0, "Some New Cafe")
        self.assertEqual(self.detect(), [])

    def test_first_ever_does_not_refire_within_window(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 250.0, "NEW VENDOR")
        add_txn(self.conn, TODAY, 300.0, "NEW VENDOR")
        msgs = self.detect()
        self.assertFalse(any("First-ever" in m for m in msgs), msgs)

    def test_money_keeps_cents(self):
        from oikonome.engine import anomalies
        self.assertEqual(anomalies._money(19.90), "$19.90")
        self.assertEqual(anomalies._money(210.10), "$210.10")
        self.assertEqual(anomalies._money(20.00), "$20")

    def test_transfers_never_flagged(self):
        for _ in range(2):
            add_txn(self.conn, TODAY, 5000.0, "NORTHWIND BROKERAGE TRANSFER",
                    override="TRANSFER_OUT")
        self.assertEqual(self.detect(), [])


if __name__ == "__main__":
    unittest.main()
