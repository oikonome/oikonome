"""A NUL character in imported text is dropped before it reaches Postgres.

Postgres text and jsonb cannot hold U+0000: a bind carrying one raises, and
the whole import fails with it. It turns up in real input, not only hostile
input — a spreadsheet saved as UTF-16 decodes to text with a NUL after every
ASCII letter, and a collector can relay whatever a portal's JSON held. The
character means nothing in a payee, a memo or a file name, so every import
door drops it and keeps the row.
"""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile

from oikonome.engine.compat import jsonb, strip_nul
from oikonome.sync import amazon_orders, base, batches, coinbase_push, \
    csvimport, plan_csv, restore

from .util import TODAY, make_db

NUL = "\x00"


def _no_nul(conn, sql, params=()):
    rows = conn.execute(sql, params).fetchall()
    return rows and all(NUL not in str(v) and "\\u0000" not in str(v)
                        for r in rows for v in r.values())


class NulBytesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_strip_nul_reaches_values_and_nesting(self):
        self.assertEqual(strip_nul({"a": ["b\x00c", {"d": "e\x00"}], "n": 1}),
                         {"a": ["bc", {"d": "e"}], "n": 1})
        self.assertEqual(strip_nul("plain"), "plain")

    def test_a_nul_in_a_key_is_refused_never_folded_onto_the_real_key(self):
        # dropping the NUL from "used\x00" would make it "used", replacing
        # the real key's value after every check that compares keys exactly
        # had already let the stranger through
        doc = {"used": {"2026-10": 5000}, "used\x00": {}}
        with self.assertRaises(ValueError):
            strip_nul(doc)
        with self.assertRaises(ValueError):
            strip_nul([{"ok": 1}, {"nested": {"k\x00": 1}}])
        with self.assertRaises(ValueError):
            self.conn.execute("SELECT %s::jsonb AS v", (jsonb(doc),))

    def test_a_jsonb_write_drops_the_nul_and_keeps_an_escaped_backslash(self):
        # a literal backslash-u-zero sequence is text, not a NUL
        row = self.conn.execute(
            "SELECT %s::jsonb AS v",
            (jsonb({"memo": "a\x00b", "path": "C:\\u0000x"}),)).fetchone()
        self.assertEqual(row["v"], {"memo": "ab", "path": "C:\\u0000x"})

    def test_a_csv_import_with_nul_in_its_text_lands_its_rows(self):
        text = ("Date,Description,Amount\n"
                f"01/05/2026,COF{NUL}FEE SHOP,-4.25\n")
        out = csvimport.import_csv(
            self.conn, "chk", text,
            {"date": "Date", "name": "Description", "amount": "Amount"})
        self.assertEqual(out["rows"], 1)
        self.assertTrue(_no_nul(self.conn,
                                "SELECT name, raw FROM transactions"))
        self.assertEqual(self.conn.execute(
            "SELECT name FROM transactions").fetchone()["name"],
            "COFFEE SHOP")

    def test_an_import_batch_file_name_with_nul_is_recorded(self):
        bid = batches.create(self.conn, "csv", "chk", f"export{NUL}.csv")
        self.assertEqual(self.conn.execute(
            "SELECT filename FROM import_batches WHERE id=%s",
            (bid,)).fetchone()["filename"], "export.csv")

    def test_an_aggregator_row_and_account_with_nul_land(self):
        base.upsert_accounts(self.conn, "it1", [base.Account(
            id="n1", name=f"Every{NUL}day", type="depository",
            raw={"official_name": f"x{NUL}"})])
        base.upsert_transactions(self.conn, [base.Transaction(
            id="nul-1", account_id="n1", date=TODAY, amount=3.0,
            name=f"KIOSK{NUL}", merchant_name=f"Kiosk{NUL}",
            raw={"location": {"city": f"River{NUL}ton"}})])
        self.assertTrue(_no_nul(self.conn,
                                "SELECT name, raw FROM accounts WHERE id='n1'"))
        self.assertTrue(_no_nul(
            self.conn, "SELECT name, merchant_name, location_city, raw"
                       " FROM transactions WHERE id='nul-1'"))

    def test_a_collector_push_with_nul_lands(self):
        coinbase_push.import_payload(self.conn, {
            "items": [{"id": "coinbase-test", "institution_name": f"Co{NUL}in"}],
            "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                          "name": f"Wallet{NUL}", "type": "investment",
                          "balance_current": 1.0}],
            "transactions": [{"id": "coinbase:n1", "account_id": "coinbase-test",
                              "date": TODAY.isoformat(), "amount": 5.0,
                              "name": f"Buy{NUL}", "raw": {"k": f"v{NUL}"}}]})
        self.assertTrue(_no_nul(
            self.conn, "SELECT name, raw FROM transactions"
                       " WHERE id='coinbase:n1'"))
        amazon_orders.import_orders(self.conn, [{
            "dedup_key": f"k{NUL}1", "date": TODAY.isoformat(),
            "amount": 12.5, "order_number": "111-1", "items": [f"Cable{NUL}"]}])
        self.assertTrue(_no_nul(self.conn, "SELECT * FROM amazon_orders"))

    def test_a_push_with_a_nul_in_a_key_is_a_bad_payload_that_writes_nothing(self):
        # ValueError is what the push doors answer as a 400
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, {
                "items": [{"id": "coinbase-test"}],
                "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                              "name": "Wallet", "type": "investment",
                              "balance_current": 1.0}],
                "transactions": [{"id": "coinbase:k1",
                                  "account_id": "coinbase-test",
                                  "date": TODAY.isoformat(), "amount": 5.0,
                                  "name": "Buy", "raw": {"k" + NUL: "v"}}]})
        with self.assertRaises(ValueError):
            amazon_orders.import_orders(self.conn, [{
                "dedup_key": "k1", "date" + NUL: TODAY.isoformat(),
                "amount": 1.0}])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM transactions WHERE id='coinbase:k1'"
        ).fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM amazon_orders").fetchone()["n"], 0)

    def test_a_plan_file_with_nul_lands(self):
        day = TODAY - dt.timedelta(days=3)
        text = ('"Trade Date","Investments","Ticker","Transaction",'
                '"Transaction Amount","Share Price","Total Shares"\n'
                f'"{day:%m/%d/%Y}","Fund{NUL} A","FUNDA","Contribution",'
                '"50.00","10.00","5.000"\n')
        plan_csv.import_csv(self.conn, "401K", text)
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        self.assertTrue(_no_nul(
            self.conn, "SELECT name, raw FROM transactions WHERE account_id=%s",
            (aid,)))

    def test_a_restored_archive_with_nul_lands(self):
        cols = ["id", "account_id", "date", "amount", "name", "raw"]
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=cols)
        w.writeheader()
        w.writerow({"id": "r-nul", "account_id": "chk",
                    "date": TODAY.isoformat(), "amount": "7.00",
                    "name": f"BAKERY{NUL}", "raw": '{"memo": "a\\u0000b"}'})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("transactions.csv", s.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())
        self.assertTrue(_no_nul(
            self.conn, "SELECT name, raw FROM transactions WHERE id='r-nul'"))


def _settings_zip(cfg_json: str) -> bytes:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=["config"])
    w.writeheader()
    w.writerow({"config": cfg_json})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class NulKeysInARestoredConfigTests(unittest.TestCase):
    """A settings key spelled with a trailing NUL must not slip past the
    restore's exact-key policy checks and then land as the real key."""

    REAL = {"plaid_enrich_used": {"2026-10": 5000},
            "notify_phone": {"number": "+15550100", "verified": False},
            "llm_url": "http://localhost:11434"}

    def setUp(self):
        from .util import write_config
        self.conn = make_db()
        write_config(self.conn, **self.REAL)

    def tearDown(self):
        self.conn.close()

    def _cfg(self):
        return dict(self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"])

    def test_a_nul_spelled_policy_key_is_refused_and_changes_nothing(self):
        before = self._cfg()
        for key, val in (("plaid_enrich_used", {}),
                         ("notify_phone", {"number": "+15550199",
                                           "verified": True}),
                         ("llm_url", "http://169.254.169.254/"),
                         ("demo_mode", True)):
            # the JSON escape survives the CSV reader's raw-byte strip and
            # becomes a real NUL only when the cell is parsed
            cell = json.dumps({key + NUL: val, "food_monthly": 321})
            self.assertIn("\\u0000", cell)
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    restore.restore_zip(self.conn, _settings_zip(cell))
                cfg = self._cfg()
                self.assertEqual(cfg, before)
                self.assertFalse(any(NUL in k for k in cfg))

    def test_a_nul_in_a_config_value_is_still_dropped(self):
        restore.restore_zip(self.conn, _settings_zip(
            json.dumps({"food_monthly": 321, "household_name": "Ho" + NUL + "me"})))
        cfg = self._cfg()
        self.assertEqual(cfg["food_monthly"], 321)
        self.assertEqual(cfg["household_name"], "Home")
        self.assertEqual(cfg["plaid_enrich_used"], {"2026-10": 5000})


if __name__ == "__main__":
    unittest.main()
