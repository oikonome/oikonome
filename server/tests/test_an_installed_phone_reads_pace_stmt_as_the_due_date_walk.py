"""An installed phone from before the three card scenarios labels
`forecast.pace_stmt` "autopay statement balance", draws `card_autopay` on
it as autopay lines and prints `forecast_rows` with `bal_stmt` under
"Autopay stmt". Those fields keep that meaning — every card paid at its
statements' due dates, the "Pay on the due date" walk — while every
server-worded headline (the cash notice, the runs-out mark, the low points
in words, "checking at month end") reads "Your usual" (specs/today-figures.md,
"Card spending leaves checking when the household pays the card").

The reading below is what such a phone takes from the reply, field by
field. Figures and names are invented."""

import datetime as dt
import os
import unittest
import uuid

from oikonome.engine.compat import jsonb

from .util import add_txn, write_config


class AnInstalledPhonesReading(unittest.TestCase):
    def test_the_legacy_fields_are_the_due_date_walk(self):
        os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        from oikonome.db import tenancy
        from oikonome.web.app import app

        from .util import _ensure_db, seed_accounts
        _ensure_db()
        appmod.DEV_MODE = True
        client = TestClient(app)
        r = client.post("/api/signup",
                        data={"email": f"legacy-{uuid.uuid4().hex[:8]}@x.dev",
                              "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        tid = client.get("/api/me").json()["tenant_id"]
        today = dt.date.today()          # the API reads the real day
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=900, other_monthly=900)
            conn.execute("UPDATE accounts SET balance_available=6000, "
                         "balance_current=6000 WHERE id='chk'")
            conn.execute("UPDATE accounts SET balance_current=600 "
                         "WHERE id='card'")
            conn.execute(
                "INSERT INTO liabilities (account_id, raw) VALUES "
                "('card', %s)", (jsonb({
                    "last_statement_balance": 450.0,
                    "last_statement_issue_date":
                        (today - dt.timedelta(days=12)).isoformat(),
                    "next_payment_due_date":
                        (today + dt.timedelta(days=13)).isoformat()}),))
            # a card paid by hand every two weeks: a short lag, so the
            # usual walk pays it sooner than the due dates do
            d = today - dt.timedelta(days=120)
            while d < today:
                add_txn(conn, d, 30.0, "CORNER MARKET",
                        primary="FOOD_AND_DRINK")
                if (today - d).days % 14 == 0:
                    add_txn(conn, d, -420.0, "PAYMENT THANK YOU",
                            primary="LOAN_PAYMENTS",
                            detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                d += dt.timedelta(days=1)
        finally:
            conn.close()
        rep = client.get("/api/today/full").json()
        fc = rep["forecast"]
        sc = {s["key"]: s for s in fc["scenarios"]}
        self.assertNotEqual(sc["usual"]["series"], sc["due"]["series"])
        # what the installed phone draws and labels "autopay statement"
        self.assertEqual(fc["pace_stmt"]["series"], sc["due"]["series"])
        self.assertEqual(fc["card_autopay"], sc["due"]["marks"])
        # pace_now keeps its meaning (cash after card balances); the
        # selector's "now" is pay all cards today
        self.assertEqual(fc["pace_now"]["series"][0][1],
                         fc["checking"] - fc["card_debt"])
        self.assertNotEqual(fc["pace_now"]["series"], sc["now"]["series"])
        due_bal = dict(sc["due"]["series"])
        future = [r for r in rep["forecast_rows"]
                  if r["bal_stmt"] is not None]
        self.assertTrue(future)
        for r in future:
            self.assertEqual(r["bal_stmt"], due_bal[r["date"]], r)
        ends = [r["label"] for r in future if r["kind"] == "month_end"]
        self.assertTrue(all(lbl == "checking at month end — pay on the due date"
                            for lbl in ends), ends)
        # the server-worded headlines read the usual walk
        usual = dict(sc["usual"]["series"])
        end = max(d for d in usual if d[:7] == today.isoformat()[:7])
        if rep["month_end_left"] is not None:
            self.assertEqual(rep["month_end_left"], usual[end])
        self.assertEqual(fc["scenarios"][0]["key"], "usual")


if __name__ == "__main__":
    unittest.main()
