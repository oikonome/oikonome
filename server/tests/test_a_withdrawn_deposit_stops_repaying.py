"""A deposit the bank feed withdraws stops repaying its charge, and a
deposit the feed reports again never doubles its links
(specs/counted-figures.md, "A withdrawn deposit repays nothing").

* A full pair on a deposit the feed withdraws: the charge is spend again
  at once, not only after the reconcile window. The pair becomes the
  partial link recording what it repaid and waits for a re-numbered twin
  to carry it; a twin that arrives, or the same id reported again, repays
  the charge again under its own category.
* A partial link on a withdrawn deposit claims nothing, so a second
  deposit can repay the charge. When the feed reports the first deposit
  again under its id, the returning deposit's links take only the room
  the charge has left: the charge is never repaid past its cost and the
  money that came in is counted once.
* The repair (`data.repair_pairs`) puts a ledger written before this rule
  in the same state, closed months included, and running it again
  changes nothing.
* Its dry run (`data.repair_pairs_preview`, `oikonome repair-pairs
  --dry-run`) says what the repair would change in counts and per-month
  In/Out sums, names no row, and leaves every table exactly as it was.
"""
from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import budget, reporting
from oikonome.sync import base
from oikonome.web import data as d

from .util import TEST_DB, TODAY, _admin_dsn, add_txn, make_db, write_config


def database_digest(tid: str) -> dict:
    """Every table's content, as text: the household's rows of a table
    that carries a household, and the whole of one that does not."""
    from oikonome.db import tenancy
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        tables = [(r["t"], r["scoped"]) for r in admin.execute(
            """SELECT c.relname AS t,
                      EXISTS (SELECT 1 FROM pg_attribute a
                               WHERE a.attrelid = c.oid
                                 AND a.attname = 'tenant_id'
                                 AND NOT a.attisdropped) AS scoped
                 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                ORDER BY 1""").fetchall()]
        out = {}
        for t, scoped in tables:
            where = " WHERE x.tenant_id = %s" if scoped else ""
            out[t] = admin.execute(
                f"SELECT md5(COALESCE(string_agg(x::text, E'\\n' "
                f"ORDER BY x::text), '')) AS h FROM {t} x{where}",
                (tid,) if scoped else ()).fetchone()["h"]
        return out
    finally:
        admin.close()


def _cents(x) -> int:
    return int(round(float(x) * 100))


def in_and_out(conn) -> tuple[int, int]:
    """Cash Flow's In and Out over every year, in cents."""
    cf = reporting.compute_cashflow(conn, TODAY)
    # income month by month: a year's In is printed only while all of its
    # income is known, and withdrawing a month's only bank row unmakes that
    inc = sum(_cents(v) for _m, v in cf["income_by_month"])
    out = sum(_cents(y[2]) for y in cf["savings_by_year"])
    return inc, out


def report_again(conn, txn_id, day, amount, name):
    """What a sync page does with a row the feed reports again."""
    base.upsert_transactions(conn, [base.Transaction(
        id=txn_id, account_id="chk", date=day, amount=amount, name=name,
        category_primary="INCOME")])


class WithdrawnDeposits(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.day = TODAY.replace(day=1)
        add_txn(self.conn, self.day, -3000.0, "PAYROLL", primary="INCOME",
                account="chk")

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def _links(self, charge):
        return sorted((r["reimburse_id"], r["partial"],
                       None if r["amount"] is None else float(r["amount"]))
                      for r in self.conn.execute(
                          "SELECT reimburse_id, partial, amount FROM "
                          "reimbursements WHERE expense_id = %s", (charge,)))

    def _charge(self, amount, name="GARDEN CENTER"):
        return add_txn(self.conn, self.day, amount, name)

    def _deposit(self, amount, txn_id, name="CLAIM PAYMENT"):
        return add_txn(self.conn, self.day, amount, name, primary="INCOME",
                       account="chk", txn_id=txn_id)

    # (2) a full pair on a deposit the feed withdraws
    def test_a_full_pair_on_a_withdrawn_deposit_is_spend_again(self):
        charge = self._charge(35.0)
        dep = self._deposit(-35.0, "depfull")
        d.link_reimbursement(self.conn, charge, dep)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self.assertEqual(in_and_out(self.conn), (300000, 0))
        base.mark_removed(self.conn, [dep])
        self.assertEqual(in_and_out(self.conn), (300000, 3500))
        self.assertIsNone(self._cat(charge))
        # the pair waits for a twin, as the link recording what it repaid
        self.assertEqual(self._links(charge), [("depfull", 1, 35.0)])
        # the feed reports the deposit again: repaid again
        report_again(self.conn, dep, self.day, -35.0, "CLAIM PAYMENT")
        self.assertEqual(self._links(charge), [("depfull", 1, 35.0)])
        self.assertEqual(self._cat(dep), "TRANSFER_IN")
        self.assertEqual(in_and_out(self.conn), (300000, 0))

    def test_a_person_pin_comes_back_with_the_spend(self):
        charge = self._charge(35.0)
        d.set_category(self.conn, charge, "SHOPPING")
        dep = self._deposit(-35.0, "deppin")
        d.link_reimbursement(self.conn, charge, dep)
        base.mark_removed(self.conn, [dep])
        self.assertEqual(self._cat(charge), "SHOPPING")
        report_again(self.conn, dep, self.day, -35.0, "CLAIM PAYMENT")
        self.assertEqual(self._cat(charge), "SHOPPING")
        self.assertEqual(in_and_out(self.conn), (300000, 0))
        d.unlink_reimbursement(self.conn, charge, dep)
        self.assertEqual(self._cat(charge), "SHOPPING")
        self.assertEqual(in_and_out(self.conn), (303500, 3500))

    def test_a_twin_carries_the_pair_back(self):
        from oikonome.sync import reanchor
        charge = self._charge(35.0)
        dep = self._deposit(-35.0, "deptw")
        d.link_reimbursement(self.conn, charge, dep)
        base.mark_removed(self.conn, [dep])
        self.assertIsNone(self._cat(charge))
        self._deposit(-35.0, "deptw2")
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 1)
        self.assertEqual(self._links(charge), [("deptw2", 1, 35.0)])
        self.assertIsNone(self._cat(charge))
        self.assertEqual(in_and_out(self.conn), (300000, 0))

    def test_a_twin_yields_to_a_later_repayment(self):
        from oikonome.sync import reanchor
        charge = self._charge(35.0)
        dep = self._deposit(-35.0, "depty")
        d.link_reimbursement(self.conn, charge, dep)
        base.mark_removed(self.conn, [dep])
        other = self._deposit(-35.0, "depother", name="REFUND")
        d.link_reimbursement(self.conn, charge, other, partial=True)
        self._deposit(-35.0, "depty2")
        self.assertEqual(reanchor.reanchor_stranded(self.conn)["moved"], 1)
        self.assertEqual(self._links(charge), [("depother", 1, 35.0)])
        self.assertEqual(in_and_out(self.conn), (303500, 0))

    # (1) a partial link whose deposit the feed reports again
    def test_a_returning_deposit_takes_only_the_room_left(self):
        charge = self._charge(60.0)
        a = self._deposit(-50.0, "depA")
        d.link_reimbursement(self.conn, charge, a, partial=True)
        base.mark_removed(self.conn, [a])
        b = self._deposit(-60.0, "depB")
        d.link_reimbursement(self.conn, charge, b, partial=True)
        self.assertEqual(in_and_out(self.conn), (300000, 0))
        report_again(self.conn, a, self.day, -50.0, "CLAIM PAYMENT")
        # B repaid the charge while A was gone; A's $50 is income again
        self.assertEqual(self._links(charge), [("depB", 1, 60.0)])
        self.assertEqual(in_and_out(self.conn), (305000, 0))

    def test_a_returning_deposit_whose_trim_fails_does_not_land(self):
        # the nightly repair cannot tell which link came back, so a failed
        # trim fails the page (fetched again) instead of landing the row
        from unittest import mock
        charge = self._charge(60.0)
        a = self._deposit(-50.0, "depAf")
        d.link_reimbursement(self.conn, charge, a, partial=True)
        base.mark_removed(self.conn, [a])
        b = self._deposit(-60.0, "depBf")
        d.link_reimbursement(self.conn, charge, b, partial=True)
        with mock.patch.object(d, "_settle_pairs_locked",
                               side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            report_again(self.conn, a, self.day, -50.0, "CLAIM PAYMENT")
        self.assertNotEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id = %s",
            (a,)).fetchone()["removed"], 0)
        self.assertEqual(in_and_out(self.conn), (300000, 0))
        report_again(self.conn, a, self.day, -50.0, "CLAIM PAYMENT")
        self.assertEqual(self._links(charge), [("depBf", 1, 60.0)])
        self.assertEqual(in_and_out(self.conn), (305000, 0))

    def test_a_returning_deposit_yields_to_a_full_pair(self):
        charge = self._charge(60.0)
        a = self._deposit(-50.0, "depA2")
        d.link_reimbursement(self.conn, charge, a, partial=True)
        base.mark_removed(self.conn, [a])
        b = self._deposit(-60.0, "depB2")
        d.link_reimbursement(self.conn, charge, b)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        report_again(self.conn, a, self.day, -50.0, "CLAIM PAYMENT")
        self.assertEqual(self._links(charge), [("depB2", 0, None)])
        self.assertEqual(in_and_out(self.conn), (305000, 0))

    def test_a_returning_full_pair_yields_to_a_later_repayment(self):
        charge = self._charge(60.0)
        a = self._deposit(-60.0, "depA3")
        d.link_reimbursement(self.conn, charge, a)
        base.mark_removed(self.conn, [a])
        self.assertIsNone(self._cat(charge))
        b = self._deposit(-60.0, "depB3")
        d.link_reimbursement(self.conn, charge, b, partial=True)
        report_again(self.conn, a, self.day, -60.0, "CLAIM PAYMENT")
        self.assertEqual(self._links(charge), [("depB3", 1, 60.0)])
        self.assertIsNone(self._cat(charge))
        self.assertIsNone(self._cat(a))
        self.assertEqual(in_and_out(self.conn), (306000, 0))

    # the repair, for a ledger written before the rule
    def _as_before_the_rule(self):
        """Both scenarios as the old code left them: a doubled charge and a
        stamp on a charge whose deposit is gone, in a closed month."""
        old = (self.day - dt.timedelta(days=40)).replace(day=3)
        c1 = add_txn(self.conn, old, 60.0, "HARDWARE")
        a = add_txn(self.conn, old, -50.0, "CLAIM PAYMENT", primary="INCOME",
                    account="chk", txn_id="oldA")
        b = add_txn(self.conn, old, -60.0, "CLAIM PAYMENT", primary="INCOME",
                    account="chk", txn_id="oldB")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, "
            "amount, created_at) VALUES (%s,%s,1,50,now() - interval '2 day'),"
            " (%s,%s,1,60,now() - interval '1 day')", (c1, a, c1, b))
        c2 = add_txn(self.conn, old, 35.0, "PHARMACY")
        dep = add_txn(self.conn, old, -35.0, "CLAIM PAYMENT",
                      primary="INCOME", account="chk", txn_id="oldF")
        d.link_reimbursement(self.conn, c2, dep)
        self.conn.execute(
            "UPDATE transactions SET removed = 1, raw = jsonb_build_object("
            "%s::text, now()) WHERE id = %s", (budget.FEED_REMOVED_KEY, dep))
        return c1, c2

    def test_the_repair_mends_closed_months_once(self):
        c1, c2 = self._as_before_the_rule()
        before = in_and_out(self.conn)
        self.assertEqual(self._cat(c2), "TRANSFER_OUT")
        out = d.repair_pairs(self.conn)
        self.assertEqual(out, {"stamps_released": 1, "rows_rechecked": 1})
        # the newest link gives way, as everywhere links overrun a side
        self.assertEqual(self._links(c1), [("oldA", 1, 50.0), ("oldB", 1, 10.0)])
        self.assertIsNone(self._cat(c2))
        after = in_and_out(self.conn)
        self.assertEqual(after[0] - before[0], 5000)
        self.assertEqual(after[1] - before[1], 3500)
        self.assertEqual(d.repair_pairs(self.conn),
                         {"stamps_released": 0, "rows_rechecked": 0})
        self.assertEqual(in_and_out(self.conn), after)

    def _tid(self):
        return self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def test_the_dry_run_says_what_would_change_and_changes_nothing(self):
        c1, c2 = self._as_before_the_rule()
        tid = self._tid()
        before = database_digest(tid)
        got = d.repair_pairs_preview(self.conn)
        self.assertEqual(database_digest(tid), before)
        # c1's newer link cut and c2's full pair turned partial; the pin
        # the pair wrote on c2 released
        self.assertEqual(got["links_settled"], 2)
        self.assertEqual(got["pins_restored"], 1)
        self.assertEqual(round(sum(m["in"] for m in got["months"]), 2), 50.0)
        self.assertEqual(round(sum(m["out"] for m in got["months"]), 2),
                         35.0)
        # and the repair then does what the dry run said
        flows = in_and_out(self.conn)
        d.repair_pairs(self.conn)
        after = in_and_out(self.conn)
        self.assertEqual((after[0] - flows[0], after[1] - flows[1]),
                         (5000, 3500))
        # the digest sees a real write
        self.assertNotEqual(database_digest(tid), before)

    def test_the_dry_run_refuses_to_run_inside_a_transaction(self):
        self._as_before_the_rule()
        with self.assertRaises(RuntimeError):
            with self.conn.transaction():
                d.repair_pairs_preview(self.conn)

    def test_the_dry_run_command_prints_counts_only(self):
        import contextlib
        import io
        import json
        import os
        from unittest import mock

        from oikonome import cli
        from oikonome.db import tenancy
        c1, c2 = self._as_before_the_rule()
        tid = self._tid()
        before = database_digest(tid)
        app_dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        real_admin, real_tenant = tenancy.admin_connect, tenancy.tenant_connect
        buf = io.StringIO()
        with mock.patch.object(tenancy, "admin_connect",
                               lambda *a, **k: real_admin(_admin_dsn(TEST_DB))), \
             mock.patch.object(tenancy, "tenant_connect",
                               lambda t, *a, **k: real_tenant(t, app_dsn)), \
             contextlib.redirect_stdout(buf):
            self.assertEqual(cli.repair_pairs_cmd(tid, dry_run=True), 0)
        self.assertEqual(database_digest(tid), before)
        text = buf.getvalue()
        lines = [json.loads(x) for x in text.splitlines()]
        self.assertEqual(lines[0]["household"], 1)
        self.assertEqual((lines[0]["links_settled"],
                          lines[0]["pins_restored"]), (2, 1))
        self.assertEqual(lines[-1]["total"]["affected"], 1)
        self.assertEqual((lines[-1]["total"]["in"], lines[-1]["total"]["out"]),
                         (50.0, 35.0))
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions").fetchall()]
        for secret in [tid, *ids, "HARDWARE", "PHARMACY", "CLAIM", "PAYROLL"]:
            self.assertNotIn(secret, text)

    def test_the_repair_leaves_a_counting_pair_alone(self):
        charge = self._charge(35.0)
        dep = self._deposit(-35.0, "depok")
        d.link_reimbursement(self.conn, charge, dep)
        self.assertEqual(d.repair_pairs(self.conn),
                         {"stamps_released": 0, "rows_rechecked": 0})
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")


if __name__ == "__main__":
    unittest.main()
