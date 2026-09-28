"""Both clients type and total money the same way.

The web (webapp/src/moneymath.ts) and mobile (mobile/src/lib/pure.ts) each
carry the helpers that parse a typed amount, sign a balance for a headline
total, and merge the budget planner's rows into the live bucket list. One
case table (mobile/scripts/money-cases.mjs) runs against both here, so a fix
on one side that misses the other fails the suite. The shape checks pin the
screens to the helpers: a field that parses on every keystroke cannot take
"-" or a trailing ".", a headline that sums raw balances adds debt to cash,
and a planner that rebuilds every bucket from its mount-time seed reverts
rule edits saved since.
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"
_CASES = _APP / "mobile" / "scripts" / "money-cases.mjs"

_RUNNER = """
import assert from "node:assert/strict";
import { moneyCases } from %(cases)s;
const impl = await import(%(impl)s);
const failed = [];
for (const [name, fn] of Object.entries(moneyCases(impl, assert))) {
  try { fn(); } catch (e) { failed.push(name + ": " + e.message); }
}
if (failed.length) { console.error(failed.join("\\n")); process.exit(1); }
"""


class ClientMoneyTwins(unittest.TestCase):
    def _run_cases(self, impl: pathlib.Path):
        for p in (impl, _CASES):
            if not p.exists():
                self.skipTest(f"{p} not present")
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        src = _RUNNER % {"cases": json.dumps(_CASES.as_uri()),
                         "impl": json.dumps(impl.as_uri())}
        r = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module",
             "-e", src], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_web_money_helpers_pass_the_shared_cases(self):
        self._run_cases(_WEB / "moneymath.ts")

    def test_mobile_money_helpers_pass_the_shared_cases(self):
        self._run_cases(_MOB / "lib" / "pure.ts")

    def _src(self, path: pathlib.Path) -> str:
        if not path.exists():
            self.skipTest(f"{path} not present")
        return path.read_text()

    def test_budget_planners_merge_instead_of_rebuilding(self):
        for p in (_WEB / "components" / "BudgetPlanner.tsx",
                  _MOB / "app" / "budget.tsx"):
            src = self._src(p)
            self.assertIn("mergePlannerBuckets(", src, p)
            self.assertNotIn("[...foodBuckets, ...catRows]", src, p)
            self.assertIn("orig: b.name", src, p)

    def test_accounts_headline_signs_debt(self):
        for p in (_WEB / "pages" / "Accounts.tsx",
                  _MOB / "app" / "(tabs)" / "accounts.tsx"):
            src = self._src(p)
            head = src[src.index("const liveTotal"):]
            head = head[:head.index(";")]
            self.assertIn("signedBalance(a)", head, p)

    def test_money_fields_keep_their_text_until_save(self):
        # no per-keystroke Number() on the asset value or tax-year cells
        for p in (_WEB / "pages" / "NetWorth.tsx", _MOB / "app" / "networth.tsx",
                  _WEB / "pages" / "Import.tsx", _MOB / "app" / "imports.tsx"):
            src = self._src(p)
            self.assertIn("parseMoneyDraft(", src, p)
            self.assertNotIn('Number(v.replace(/[$,\\s]/g, "")', src, p)

    def test_signed_mobile_money_fields_offer_a_minus_key(self):
        acc = self._src(_MOB / "app" / "(tabs)" / "accounts.tsx")
        for placeholder in ('placeholder="balance ($)"',
                            'placeholder="starting balance ($)"'):
            tail = acc[acc.index(placeholder):]
            tail = tail[:tail.index("/>")]
            self.assertNotIn("decimal-pad", tail, placeholder)
        imp = self._src(_MOB / "app" / "imports.tsx")
        cell = imp[imp.index("function TaxDocsCard"):]
        cell = cell[:cell.index("export ") if "export " in cell else None]
        self.assertNotIn('keyboardType="decimal-pad"', cell)

    def test_reimbursement_matcher_block_is_the_same_text_on_both(self):
        """The matcher's "why" column and gap line decide what a person
        believes Pair will record; the case table checks behaviour, and the
        block itself is kept character-for-character equal so a fix made on
        one client cannot quietly skip the other."""
        head = "// ---- reimbursement matcher: twin block"
        tail = "// ---- end of the reimbursement matcher twin block ----"
        blocks = []
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = self._src(p)
            self.assertEqual(src.count(head), 1, p)
            blocks.append(src[src.index(head):src.index(tail)])
        self.assertEqual(blocks[0], blocks[1])

    def test_the_web_shell_adopts_a_zone_only_through_the_shared_rule(self):
        """Adopting the device zone silently on a self-hosted instance pins
        the household and makes the operator's configured zone stop
        applying; the rule (hosted owner, instance zone, not UTC) lives in
        adoptsDeviceZone, which the case table checks."""
        src = self._src(_WEB / "App.tsx")
        self.assertIn("adoptsDeviceZone(m, device)", src)
        self.assertNotIn('m.role !== "owner" || m.demo) return;', src)


_REIMB_RUNNER = """
import { REIMB_ROWS, REIMB_OWED } from %(cases)s;
const impl = await import(%(impl)s);
console.log(JSON.stringify({ rows: REIMB_ROWS, want: REIMB_OWED,
                             got: impl.reimbOwed(REIMB_ROWS) }));
"""


class ReimbOwedMatchesTheServer(unittest.TestCase):
    """The Reimburse page's "owed back" headline and the Today strip / daily
    email (alerts.reimb_pending) must state one figure for the same rows.
    The client helpers once took a flagged deposit's absolute value, so a
    deposit flagged to pair later added its whole face value to the page
    while the server counted it as nothing owed."""

    def _client(self, impl: pathlib.Path) -> dict:
        for p in (impl, _CASES):
            if not p.exists():
                self.skipTest(f"{p} not present")
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        src = _REIMB_RUNNER % {"cases": json.dumps(_CASES.as_uri()),
                               "impl": json.dumps(impl.as_uri())}
        r = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module",
             "-e", src], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        return json.loads(r.stdout)

    def _server_total(self, rows) -> float:
        import datetime as dt
        from oikonome.engine import alerts
        from .util import add_txn, make_db, seed_accounts
        conn = make_db()
        try:
            seed_accounts(conn)
            day = dt.date(2031, 3, 1)
            for i, row in enumerate(rows):
                tid = add_txn(conn, day, row["amount"], f"FLAGGED {i}",
                              account="chk")
                conn.execute("INSERT INTO reimburse_flags (txn_id, expected)"
                             " VALUES (%s,%s)", (tid, row.get("expected")))
                if row.get("received"):
                    back = add_txn(conn, day + dt.timedelta(days=3),
                                   -row["received"], f"PAYBACK {i}",
                                   account="chk")
                    conn.execute(
                        "INSERT INTO reimbursements (expense_id, reimburse_id,"
                        " partial, amount) VALUES (%s,%s,1,%s)",
                        (tid, back, row["received"]))
            rp = alerts.reimb_pending(conn)
        finally:
            conn.close()
        return rp["total"] if rp else 0.0

    def test_client_owed_figure_equals_the_servers_for_the_same_rows(self):
        from .util import _ensure_db
        _ensure_db()
        for impl in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            c = self._client(impl)
            server = self._server_total(c["rows"])
            self.assertAlmostEqual(c["got"], server, places=2, msg=str(impl))
            self.assertAlmostEqual(c["want"], server, places=2,
                                   msg="the case table's expected figure")

if __name__ == "__main__":
    unittest.main()
