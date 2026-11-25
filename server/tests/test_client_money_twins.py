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
import re
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
  try { await fn(); } catch (e) { failed.push(name + ": " + e.message); }
}
if (failed.length) { console.error(failed.join("\\n")); process.exit(1); }
"""


def _blank_literals(src: str) -> str:
    """`src` with the inside of every string, template literal and comment
    blanked to spaces (same length, same offsets), so a bracket or comma
    in a label or a comment is never read as code."""
    out, i, n = list(src), 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif c in "\"'`":
            j = i + 1
            # a quoted string ends at its line (JSX text such as "you're"
            # is not a string); only a template literal spans lines
            while j < n and src[j] != c and (c == "`" or src[j] != "\n"):
                j += 2 if src[j] == "\\" else 1
            j += 1
            i += 1                          # keep the opening quote
            j -= 1                          # and the closing one
        else:
            i += 1
            continue
        for k in range(i, min(j, n)):
            if out[k] != "\n":
                out[k] = " "
        i = j + 1
    return "".join(out)


def _save_settings_calls(src: str):
    """Every `saveSettings(...)` call in a source file (its definition
    left out) as `(line, top-level arguments)`. Brackets and braces nest;
    strings and comments are skipped while counting them."""
    code = _blank_literals(src)
    i = 0
    while True:
        i = code.find("saveSettings(", i)
        if i < 0:
            return
        if code[max(0, i - 9):i].endswith("function "):
            i += 1
            continue
        j = a = i + len("saveSettings(")
        depth, args = 1, []
        while depth:
            c = code[j]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    args.append(src[a:j])
            elif c == "," and depth == 1:
                args.append(src[a:j])
                a = j + 1
            j += 1
        yield src.count("\n", 0, i) + 1, [x.strip() for x in args if x.strip()]
        i = j


_WHOLE_LIST_KEYS = ("savings_goals", "custom_buckets", "income_scenarios",
                    "email_schedule", "llm_backends", "llm_roles",
                    "manual_assets")


def _unbased_saves(src: str, body_at: int):
    """The saveSettings calls in `src` that send no basis, as `(line,
    verdict)`: `whole-list` when the body names a whole-list key, `opaque`
    when this reading cannot see every key the body carries."""
    for line, args in _save_settings_calls(src):
        if len(args) <= body_at:
            continue
        body = args[body_at]
        basis = args[body_at + 1] if len(args) > body_at + 1 else None
        # an explicit undefined/null basis is the cache's copy all the same
        if basis is not None and basis not in ("undefined", "null"):
            continue
        if [k for k in _WHOLE_LIST_KEYS if k in body]:
            yield line, "whole-list"
        elif not body.startswith("{") or _hides_top_level_keys(body):
            # a name, a member, a call, a spread or a computed key: the
            # keys it sends cannot be read from here
            yield line, "opaque"


def _hides_top_level_keys(body: str) -> bool:
    """Does this object literal spread something, or compute a key, at its
    own top level? Nested values may do either: they name no settings key."""
    depth, expect_key = 0, False
    body = _blank_literals(body)
    for i, c in enumerate(body):
        if c in "([{":
            if depth == 1 and expect_key and c == "[":
                return True
            depth += 1
            expect_key = depth == 1
            continue
        if c in ")]}":
            depth -= 1
            continue
        if depth != 1:
            continue
        if c == ",":
            expect_key = True
        elif not c.isspace():
            if expect_key and body.startswith("...", i):
                return True
            expect_key = False
    return False


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
            # rows remember the bucket they were seeded from
            self.assertIn("plannerBucketsSaved(", src, p)

    def test_budget_planners_merge_onto_the_last_saved_copy(self):
        """The planner's merge base follows each save, as the Bucket-rules
        card's does: a row added and saved here must not come back as
        "added elsewhere" once it is removed or renamed."""
        for p, card in ((_WEB / "components" / "BudgetPlanner.tsx",
                         "export default function BudgetPlanner"),
                        (_MOB / "app" / "budget.tsx",
                         "export function Planner(")):
            src = self._src(p)
            body = src[src.index(card):]
            body = body[:body.index("\n}\n")]
            self.assertRegex(body, r"mergePlannerBuckets\(sent\b", p)
            save = body[body.index("const save = useMutation("):]
            ok = save[save.index("onSuccess:"):save.index("\n  });")]
            self.assertIn("setBaseNames(next.base)", ok, p)
            self.assertIn("plannerBucketsSaved(", ok, p)

    def test_bucket_rules_cards_merge_onto_the_last_saved_copy(self):
        """Both Bucket-rules cards merge through the shared helper and
        take the saved view as their next merge base: a base frozen at
        mount reads a bucket added and saved here as "added elsewhere" and
        carries it back after it is removed or renamed."""
        for p, card in ((_WEB / "components" / "BudgetFineTune.tsx",
                         "export default function BudgetFineTune"),
                        (_MOB / "app" / "budget.tsx",
                         "function BucketRules(")):
            src = self._src(p)
            body = src[src.index(card):]
            body = body[:body.index("\n}\n")]
            self.assertIn("mergeBucketRules(sent,", body, p)
            save = body[body.index("const save = useMutation("):]
            ok = save[save.index("onSuccess:"):save.index("\n  });")]
            self.assertIn("baseline.current = ", ok, p)
            self.assertIn("bucketRulesSaved(", ok, p)

    def test_accounts_headline_is_the_servers_financial_figure(self):
        """The headline is Net Worth's Financial accounts figure, sent by
        the server with card debt already netted and loans listed apart
        (specs/counted-figures.md) — neither client adds balances up."""
        for p in (_WEB / "pages" / "Accounts.tsx",
                  _MOB / "app" / "(tabs)" / "accounts.tsx"):
            src = self._src(p)
            self.assertIn("totals.financial_total", src, p)
            self.assertIn("totals.loans_owed", src, p)
            self.assertNotIn("signedBalance(", src, p)

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

    def test_business_unflag_patch_is_one_block_both_screens_use(self):
        """Unflagging a worksheet row patches cached views before the
        refetch; a view of another year must keep its count and total, so
        both clients patch through the same block, and neither screen
        subtracts on its own."""
        head = "// ---- flagged-business unflag patch: twin block"
        tail = "// ---- end of the flagged-business unflag patch twin block ----"
        blocks = []
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = self._src(p)
            self.assertEqual(src.count(head), 1, p)
            blocks.append(src[src.index(head):src.index(tail)])
        self.assertEqual(blocks[0], blocks[1])
        for p in (_WEB / "pages" / "Business.tsx", _MOB / "app" / "business.tsx"):
            src = self._src(p)
            self.assertTrue("bizViewWithout(" in src, p)
            self.assertNotRegex(src, r"total:\s*\w+\.total\s*-", p)

    def test_whole_list_settings_save_through_the_one_compare_and_swap(self):
        """Four settings are stored as whole lists; a save built from a
        stale copy would erase what another device saved since. Each client
        sends every settings save through saveWholeLists inside its one
        saveSettings, the block is the same text on both, no second door
        posts the email schedule around it, and each list editor names the
        fresh read it merged onto as the basis its versions come from."""
        head = "// ---- whole-list settings: compare-and-swap twin block ----"
        tail = "// ---- end of the whole-list settings twin block ----"
        blocks = []
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = self._src(p)
            self.assertEqual(src.count(head), 1, p)
            blocks.append(src[src.index(head):src.index(tail)])
        self.assertEqual(blocks[0], blocks[1])
        for p in (_WEB / "api" / "cache.ts", _MOB / "lib" / "cache.ts"):
            src = self._src(p)
            fn = src[src.index("export function saveSettings("):]
            fn = fn[:fn.index("\n}\n")]
            self.assertIn("saveWholeLists(body, was,", fn, p)
            self.assertIn("SettingsMovedError", fn, p)
        self.assertNotIn("settingsSaveSchedule",
                         self._src(_MOB / "lib" / "api.ts"))
        # how many whole-list saves each editor makes, every one of them
        # handed the read it merged onto
        editors = {
            _WEB / "components" / "BudgetPlanner.tsx": 1,
            _WEB / "components" / "BudgetFineTune.tsx": 1,
            _WEB / "components" / "SavingsGoalsCard.tsx": 1,
            _WEB / "components" / "ManualBudgets.tsx": 1,
            _WEB / "components" / "IncomeScenarioCard.tsx": 1,
            _WEB / "pages" / "Welcome.tsx": 1,
            _WEB / "pages" / "Settings.tsx": 1,
            _MOB / "app" / "budget.tsx": 4,
            _MOB / "app" / "notifications.tsx": 1,
            _MOB / "components" / "wizard-email.tsx": 1,
            _MOB / "components" / "wizard-budgets.tsx": 1,
        }
        for p, want in editors.items():
            calls = [c[:c.index(";")] for c in
                     self._src(p).split("saveSettings(qc, ")[1:]]
            based = [c for c in calls if c.rstrip().endswith(", cur)")
                     or "{ ...cur, email_schedule:" in c]
            self.assertEqual(len(based), want, p)
        # Not only the editors listed above: EVERY saveSettings call in
        # either client whose body names a whole-list key must hand over
        # the read it merged onto. Without one the versions and the 409
        # re-merge base come from whatever the cache holds, which may be
        # older or newer than the copy the body was built from.
        # A body this reading cannot see into (a name, a member, a spread,
        # a computed key) without a basis is accounted for here: each posts
        # no whole-list key.
        passthrough = {
            # retirement wizard steps: answers and step marks; on the
            # phone also the saving step, which spreads in the Roth figure
            # only when the server knows it (a number, never a list)
            _WEB / "components" / "RetireWizard.tsx": 1,
            _MOB / "app" / "retire.tsx": 2,
            # the email form's non-schedule edits (a schedule edit carries
            # its base and takes the based call beside it), the investing
            # form, the mail relay save (it spreads in a typed password
            # only), and the switch and home-screen saves whose computed
            # keys are boolean flags or home_web/home_mobile
            _WEB / "pages" / "Settings.tsx": 5,
            # the welcome email step's recipient adds
            _WEB / "pages" / "Welcome.tsx": 1,
            # the investing form's sections; the mail relay save (it
            # spreads in a typed secret only); the toggle and home-screen
            # switches, whose computed keys are boolean flags or
            # home_mobile
            _MOB / "app" / "settings.tsx": 4,
        }
        seen_pass: dict = {}
        for tree, body_at in ((_WEB, 1), (_MOB, 2)):
            for p in sorted(list(tree.rglob("*.ts")) + list(tree.rglob("*.tsx"))):
                for line, verdict in _unbased_saves(p.read_text(), body_at):
                    where = f"{p.relative_to(_APP)}:{line}"
                    self.assertNotEqual(
                        verdict, "whole-list",
                        f"{where} saves a whole list without its basis")
                    seen_pass[p] = seen_pass.get(p, 0) + 1
        self.assertEqual(seen_pass, passthrough,
                         "a body passed by name without a basis must be "
                         "accounted for here")

    def test_the_basis_check_sees_every_body_it_cannot_read(self):
        """The scan above is only as good as its reading of a body: one
        that hides its keys behind a spread, a member, a computed key, or
        a basis written as `undefined`/`null` must not pass as safe."""
        unsafe = [
            "saveSettings(qc, { ...patch });",
            "saveSettings(qc, form.body);",
            "saveSettings(qc, { savings_goals: goals }, undefined);",
            "saveSettings(qc, { savings_goals: goals }, null);",
            "saveSettings(qc, { [listKey]: goals });",
            "saveSettings(qc, client!, { ...patch });",
            # a closing brace inside a string is not the object's end
            'saveSettings(qc, { label: "}", ...patch });',
            "saveSettings(qc, { label: `}`, ...patch });",
            "saveSettings(qc, { label: '}', [listKey]: goals });",
        ]
        for src in unsafe:
            body_at = 2 if "client!" in src else 1
            self.assertTrue(list(_unbased_saves(src, body_at)), src)
        safe = [
            "saveSettings(qc, { theme: next });",
            "saveSettings(qc, { savings_goals: goals }, cur);",
            "saveSettings(qc, client!, { custom_buckets: out }, cur);",
        ]
        for src in safe:
            body_at = 2 if "client!" in src else 1
            self.assertEqual(list(_unbased_saves(src, body_at)), [], src)

    def test_every_import_count_goes_through_the_one_sentence(self):
        """An import's count says what it ADDED apart from what was already
        stored; a screen that prints `imported` itself would call a second
        import of the same file new rows again. The single-upload result
        and the folder's per-file line, on each client."""
        for p in (_WEB / "pages" / "Import.tsx", _MOB / "app" / "imports.tsx"):
            src = self._src(p)
            self.assertEqual(len(re.findall(r"importCountText\(", src)), 2, p)
            self.assertNotIn("transactions`", src, p)
            self.assertNotIn("{res.imported", src, p)

    def test_a_dropped_credential_is_named_once_on_every_result(self):
        """A restore or a connections-bundle import never keeps a stored AI
        key or mail password whose address the file moved. The bundle
        import says so only in `credentials_dropped`, so its message passes
        that list to the one shared sentence. A restore already says it in
        its own warnings, which both restore views print, so they must not
        add the shared sentence beside it and tell the person twice."""
        sites = {
            _WEB / "pages" / "Settings.tsx": 1,     # bundle import
            _WEB / "pages" / "Import.tsx": 0,       # restore: the warnings
            _MOB / "app" / "settings.tsx": 1,
            _MOB / "app" / "imports.tsx": 0,
        }
        for p, want in sites.items():
            calls = re.findall(r"droppedCredentialsNote\(\s*[\w?.!]*"
                               r"credentials_dropped\s*\)", self._src(p))
            self.assertEqual(len(calls), want, p)
        for p in (_WEB / "pages" / "Import.tsx", _MOB / "app" / "imports.tsx"):
            self.assertGreaterEqual(self._src(p).count("warnings"), 2, p)

    def test_the_web_shell_adopts_a_zone_only_through_the_shared_rule(self):
        """Adopting the device zone silently on a self-hosted instance pins
        the household and makes the operator's configured zone stop
        applying; the rule (hosted owner, instance zone, not UTC) lives in
        adoptsDeviceZone, which the case table checks."""
        src = self._src(_WEB / "App.tsx")
        self.assertIn("adoptsDeviceZone(m, device)", src)
        self.assertNotIn('m.role !== "owner" || m.demo) return;', src)


# The awaiting-reimbursement rows: `amount` is the ledger sign (a charge
# positive, a deposit negative).
_REIMB_ROWS = [
    # a partial expectation: only the insurer's share comes back
    {"amount": 100, "expected": 60, "received": 0},
    # no expectation: the whole charge, less what already came back
    {"amount": 40, "expected": None, "received": 15},
    # a flagged DEPOSIT with no expectation: nothing is owed back on money
    # that came in, so it adds 0 rather than its face value
    {"amount": -40, "expected": None, "received": 15},
    {"amount": -500, "expected": None, "received": 0},
    # more came back than expected: nothing owed, never negative
    {"amount": 80, "expected": 30, "received": 50},
    {"amount": 12},
    # an expectation above the charge (saved before the server refused
    # one) counts only the charge: no link can bring back more
    {"amount": 100, "expected": 150, "received": 0},
    # a flagged deposit carrying an expectation still owes nothing back
    {"amount": -40, "expected": 40, "received": 0}]
_REIMB_OWED = 60 + 25 + 0 + 0 + 0 + 12 + 100 + 0


class ReimbOwedIsTheServers(unittest.TestCase):
    """The Reimburse page's "owed back" headline and the Today strip / daily
    email (alerts.reimb_pending) state one figure: the page prints the
    reply's `owed` (alerts.reimb_owed) and adds nothing up. The client
    helpers once took a flagged deposit's absolute value, so a deposit
    flagged to pair later added its whole face value to the page while the
    server counted it as nothing owed."""

    def _seed(self, conn, rows):
        import datetime as dt
        from .util import add_txn, seed_accounts
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

    def test_the_owed_figure_counts_each_charge_against_its_expectation(self):
        from oikonome.engine import alerts
        from .util import make_db
        conn = make_db()
        try:
            self.assertEqual(alerts.reimb_owed(conn), 0.0)
            self._seed(conn, _REIMB_ROWS)
            self.assertAlmostEqual(alerts.reimb_owed(conn), _REIMB_OWED,
                                   places=2)
            self.assertEqual(alerts.reimb_owed(conn),
                             alerts.reimb_pending(conn)["total"])
        finally:
            conn.close()

    def test_a_rows_percent_received_is_of_what_is_expected_back(self):
        from oikonome.engine.alerts import reimb_received_pct
        self.assertEqual(reimb_received_pct(100, 60, 30), 50)
        self.assertEqual(reimb_received_pct(40, None, 15), 38)  # 37.5 → 38
        self.assertEqual(reimb_received_pct(80, 30, 50), 100)
        self.assertEqual(reimb_received_pct(-40, None, 10), 25)
        self.assertEqual(reimb_received_pct(12, None, None), 0)
        self.assertEqual(reimb_received_pct(50, 0, 0), 0)

    def test_neither_client_adds_the_owed_figure_up(self):
        for p in (_WEB / "pages" / "Reimburse.tsx",
                  _MOB / "app" / "reimburse.tsx"):
            if not p.exists():
                self.skipTest(f"{p} not present")
            src = p.read_text(encoding="utf-8")
            self.assertNotIn("reimbOwed", src, p)
            self.assertIn(".owed", src, p)


if __name__ == "__main__":
    unittest.main()
