"""Every importer caps its input and retires rows the way the push doors do.

Four invariants, one shape:

* An importer parses whatever file it is handed, so it needs the same row
  ceiling the push doors carry (MAX_ROWS) — checked before any DB write.

* Pruning a restated date-set SOFT-retires (removed=1) rather than
  DELETEing. Soft is reversible — a returning id flips back to removed=0
  through the upsert — and it preserves the overrides a person put on those
  rows. Holdings and reporting read removed=0 only, so the math is
  unchanged either way.

* A transaction that vanishes from a FULL successful pull is soft-retired
  too, mirroring the push door's full_replace. A failed pull raises before
  the write transaction, so a partial fetch can never retire anything.

* Restore must not read a numeric cell for its truthiness: a non-string 0
  would restore as NULL. The `_f` helper is used at every numeric site.
"""

import io
import unittest
import zipfile

import httpx

from oikonome.sync import coinbase, plan_csv, restore
from oikonome.sync.restore import _f

from .test_coinbase import _PEM, KEY_NAME
from .util import make_db, write_config

HEADER = ('"Trade Date","Investments","Ticker","Transaction",'
          '"Transaction Amount","Share Price","Total shares"\n')


def _plan_csv(rows):
    return HEADER + "".join(
        f'"{d}","{fund}","{fund.replace(" ", "").upper()}","{typ}",'
        f'"{amt}","10.00","{sh}"\n'
        for d, fund, typ, amt, sh in rows)


class PlanImportRowCapTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_over_20k_rows_rejected_before_any_write(self):
        big = HEADER + "".join(
            f'"01/15/2026","Fund X","FX","Contribution","{i}.00","10.00","1.000"\n'
            for i in range(plan_csv.MAX_ROWS + 1))
        with self.assertRaises(ValueError) as cm:
            plan_csv.import_csv(self.conn, "403B", big)
        self.assertIn("too many transactions", str(cm.exception))
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions WHERE id LIKE 'plan:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_cap_matches_the_coinbase_door(self):
        from oikonome.sync import coinbase_push
        self.assertEqual(plan_csv.MAX_ROWS, coinbase_push.MAX_ROWS)


class PlanPruneSoftRetiresTests(unittest.TestCase):
    """Stale rows on a restated date are soft-retired, never DELETEd."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # two rows on the same date, distinct funds → distinct ids
        self.csv_both = _plan_csv([
            ("03/15/2026", "Fund A", "Contribution", "100.00", "10.000"),
            ("03/15/2026", "Fund B", "Contribution", "200.00", "20.000")])
        self.csv_only_a = _plan_csv([
            ("03/15/2026", "Fund A", "Contribution", "100.00", "10.000")])
        plan_csv.import_csv(self.conn, "403B", self.csv_both)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return {r["raw"]["fund"]: r for r in self.conn.execute(
            "SELECT removed, raw FROM transactions "
            "WHERE account_id='plan-403b'").fetchall()}

    def test_stale_row_soft_removed_not_gone(self):
        plan_csv.import_csv(self.conn, "403B", self.csv_only_a)
        rows = self._rows()
        self.assertEqual(rows["Fund A"]["removed"], 0)
        self.assertIn("Fund B", rows, "stale row was hard-deleted")
        self.assertEqual(rows["Fund B"]["removed"], 1)

    def test_reappearing_id_comes_back_live(self):
        plan_csv.import_csv(self.conn, "403B", self.csv_only_a)
        plan_csv.import_csv(self.conn, "403B", self.csv_both)  # B returns
        rows = self._rows()
        self.assertEqual(rows["Fund B"]["removed"], 0)
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM transactions WHERE account_id='plan-403b'"
        ).fetchall()), 2)                       # upserted, not duplicated

    def test_holdings_exclude_soft_removed_rows(self):
        res = plan_csv.import_csv(self.conn, "403B", self.csv_only_a)
        # Fund B's 20 shares are retired → only Fund A's 10 × $10 remain
        self.assertEqual(res["holdings_value"], 100.0)
        syms = {r["symbol"] for r in self.conn.execute(
            "SELECT symbol FROM holdings WHERE account_id='plan-403b'")}
        self.assertEqual(syms, {"FUNDA"})


W_BTC = {"id": "w-btc", "balance": {"amount": "0.5", "currency": "BTC"},
         "currency": {"type": "crypto", "code": "BTC"}}


def _txn(tid):
    return {"id": tid, "type": "buy", "status": "completed",
            "created_at": "2024-01-05T12:00:00Z",
            "amount": {"amount": "0.1", "currency": "BTC"},
            "native_amount": {"amount": "100.00", "currency": "USD"}}


class CoinbaseFullPullRetiresVanishedTests(unittest.TestCase):
    """A txn absent from the NEXT full pull is soft-retired; a failed pull
    retires nothing."""

    def setUp(self):
        self.conn = make_db()
        self.iid = coinbase.link(self.conn, "retire", KEY_NAME, _PEM)
        self.txns = [_txn("tx-1"), _txn("tx-2")]
        self.fail_txns = False

        def handler(request):
            p = request.url.path
            if p == "/v2/accounts":
                return httpx.Response(200, json={"data": [W_BTC],
                                                 "pagination": {}})
            if p == "/v2/prices/BTC-USD/spot":
                return httpx.Response(200, json={"data": {"amount": "40000"}})
            if p == "/v2/accounts/w-btc/transactions":
                if self.fail_txns:
                    return httpx.Response(401, text="Unauthorized")
                return httpx.Response(200, json={"data": self.txns,
                                                 "pagination": {}})
            return httpx.Response(404, text=f"unmocked {p}")

        self.transport = httpx.MockTransport(handler)

    def tearDown(self):
        self.conn.close()

    def _sync(self):
        return coinbase.sync(self.conn, self.iid, transport=self.transport)

    def _removed(self, tid):
        return self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s",
            (f"coinbase:{tid}",)).fetchone()["removed"]

    def test_vanished_txn_soft_retired_on_full_pull(self):
        self._sync()
        self.txns = [_txn("tx-1")]                  # tx-2 restated away
        res = self._sync()
        self.assertEqual(res["stale_removed"], 1)
        self.assertEqual(self._removed("tx-1"), 0)
        self.assertEqual(self._removed("tx-2"), 1)  # retired, not deleted

    def test_returning_txn_flips_back_live(self):
        self._sync()
        self.txns = [_txn("tx-1")]
        self._sync()
        self.txns = [_txn("tx-1"), _txn("tx-2")]    # tx-2 reappears
        res = self._sync()
        self.assertEqual(res["stale_removed"], 0)
        self.assertEqual(self._removed("tx-2"), 0)

    def test_failed_pull_retires_nothing(self):
        self._sync()
        self.fail_txns = True                       # partial pull: pages fail
        with self.assertRaises(RuntimeError):
            self._sync()
        self.assertEqual(self._removed("tx-1"), 0)
        self.assertEqual(self._removed("tx-2"), 0)


class RestoreKeepsNumericZeroTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_f_helper_handles_non_string_zero(self):
        # THE CASE: truthiness turned a non-string 0 into NULL
        self.assertEqual(_f(0), 0.0)
        self.assertEqual(_f("0"), 0.0)
        self.assertIsNone(_f(""))
        self.assertIsNone(_f(None))

    def test_zero_cells_restore_as_zero_not_null(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("items.csv",
                       "id,aggregator,institution_name\nit1,manual,Box\n")
            z.writestr("accounts.csv",
                       "id,item_id,name,type,balance_current,"
                       "balance_available\n"
                       "acc1,it1,Empty,depository,0,\n")
            z.writestr("transactions.csv",
                       "id,account_id,date,amount,name\n"
                       "zt1,acc1,2026-07-01,0,Zero-dollar auth\n")
        restore.restore_zip(self.conn, buf.getvalue())
        acct = self.conn.execute(
            "SELECT balance_current, balance_available FROM accounts "
            "WHERE id='acc1'").fetchone()
        self.assertEqual(acct["balance_current"], 0.0)   # 0, not NULL
        self.assertIsNone(acct["balance_available"])     # '' still NULL
        txn = self.conn.execute(
            "SELECT amount FROM transactions WHERE id='zt1'").fetchone()
        self.assertEqual(txn["amount"], 0.0)


if __name__ == "__main__":
    unittest.main()
