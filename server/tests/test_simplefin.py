"""SimpleFIN connector: sign conventions at the boundary, idempotent
upserts, engine integration. Mocked HTTP — no live bridge."""

import base64
import copy
import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import budget
from oikonome.sync import base as sync_base
from oikonome.sync import csvimport, simplefin

from .util import make_db, write_config

PAYLOAD = {
    "errors": [],
    "accounts": [
        {"id": "acc-chk", "name": "Everyday Checking", "currency": "USD",
         "balance": "1500.00", "available-balance": "1450.00",
         "org": {"name": "Demo Bank"},
         "transactions": [
             # SimpleFIN bank sign: negative = money out
             {"id": "t1", "posted": 1752537600, "amount": "-42.50",
              "description": "SAFEWAY STORE 123", "payee": "Safeway"},
             {"id": "t2", "posted": 1752451200, "amount": "2000.00",
              "description": "EMPLOYER PAYROLL", "payee": "Employer"},
         ]},
        {"id": "acc-card", "name": "Rewards Credit Card", "currency": "USD",
         "balance": "-250.00",     # SimpleFIN: card debt is negative
         "transactions": [
             {"id": "t3", "posted": 1752537600, "amount": "-19.99",
              "description": "TARGET 00123", "payee": "Target",
              "pending": True},
         ]},
    ],
}


def _transport(payload=PAYLOAD):
    def handler(request):
        if request.method == "POST":            # claim
            return httpx.Response(200, text="https://u:p@bridge.test/simplefin")
        return httpx.Response(200, text=json.dumps(payload))
    return httpx.MockTransport(handler)


class SimpleFinTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _sync(self):
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_transport())

    def test_claim_setup_token(self):
        token = base64.b64encode(b"https://bridge.test/claim/abc").decode()
        url = simplefin.claim_setup_token(token, transport=_transport())
        self.assertEqual(url, "https://u:p@bridge.test/simplefin")

    def test_sign_conventions_flipped_at_boundary(self):
        self._sync()
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, amount, pending FROM transactions "
            "WHERE id LIKE 'sfin:%'").fetchall()}
        # bank -42.50 (out) → engine +42.50 (positive = money out)
        self.assertEqual(rows["sfin:acc-chk:t1"]["amount"], 42.50)
        # bank +2000 (payroll in) → engine -2000
        self.assertEqual(rows["sfin:acc-chk:t2"]["amount"], -2000.0)
        self.assertEqual(rows["sfin:acc-card:t3"]["pending"], 1)
        # card debt flipped positive; checking balance kept
        bal = {r["id"]: r for r in self.conn.execute(
            "SELECT id, balance_current, type FROM accounts "
            "WHERE id LIKE 'acc-%'").fetchall()}
        self.assertEqual(bal["acc-card"]["balance_current"], 250.0)
        self.assertEqual(bal["acc-card"]["type"], "credit")
        self.assertEqual(bal["acc-chk"]["balance_current"], 1500.0)

    def test_idempotent_resync(self):
        self._sync()
        self._sync()
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'sfin:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 3)
        ok = self.conn.execute(
            "SELECT status, institution_name FROM items WHERE id='sfin-demo'"
        ).fetchone()
        self.assertEqual(ok["status"], "ok")
        # the bridge item is plumbing; the institution lives on
        # per-org child items now
        self.assertEqual(ok["institution_name"], "SimpleFIN bridge")
        org = self.conn.execute(
            "SELECT institution_name FROM items "
            "WHERE aggregator='simplefin-org'").fetchone()
        self.assertEqual(org["institution_name"], "Demo Bank")

    def test_engine_counts_synced_spend(self):
        """End-to-end: a SimpleFIN charge lands in the month's verdict."""
        self._sync()
        # payload dates: 2025-07-15/14 (unix) — pin today to that month
        st = budget.month_status(self.conn, dt.date(2025, 7, 15))
        self.assertEqual(round(st["total_actual"], 2), 62.49)  # 42.50 + 19.99
        # payroll deposit is an inflow — excluded from spend by construction

    def test_error_marks_item_and_logs(self):
        def boom(request):
            return httpx.Response(500, text="bridge down")
        with self.assertRaises(Exception):
            simplefin.sync(self.conn, "sfin-demo",
                           "https://u:p@bridge.test/simplefin",
                           transport=httpx.MockTransport(boom))
        row = self.conn.execute(
            "SELECT status FROM items WHERE id='sfin-demo'").fetchone()
        self.assertIsNone(row)   # item never created on first-sync failure
        err = self.conn.execute(
            "SELECT error FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("HTTPStatusError", err["error"])
        # the access token is URL userinfo — it must NEVER reach a sink.
        # _url_auth pulls it into an Authorization header at the source, and
        # the sink redacts as well; the credential is absent here either way.
        self.assertNotIn("u:p@", err["error"])
        self.assertNotIn("//u:p", err["error"])

    def test_url_auth_splits_credential_out_of_url(self):
        clean, auth = simplefin._url_auth(
            "https://theuser:s3cret@bridge.test/simplefin/accounts")
        self.assertEqual(clean, "https://bridge.test/simplefin/accounts")
        self.assertEqual(auth, ("theuser", "s3cret"))
        # no-userinfo URL passes through unchanged, no auth
        self.assertEqual(
            simplefin._url_auth("https://bridge.test/x"),
            ("https://bridge.test/x", None))


CSV_MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}

# same charges as PAYLOAD's acc-chk, as a bank CSV (bank sign: negative=out)
CSV_OVERLAP = """Date,Amount,Description
2025-07-15,-42.50,SAFEWAY STORE 123
2025-07-14,2000.00,EMPLOYER PAYROLL
"""


class ImportOverlapTests(unittest.TestCase):
    """A file import and an aggregator can cover the same window: the user
    imports bank files first (manual account), THEN connects SimpleFIN over
    the same dates — the aggregator must not re-insert charges the file
    import already covers."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _sync(self, payload=PAYLOAD):
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_transport(payload))

    def _sfin_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'sfin:%'"
        ).fetchone()["n"]

    def test_import_then_sync_no_double_count(self):
        # rows land on the seeded 'chk' account — a DIFFERENT account id
        # than SimpleFIN's acc-chk, exactly the un-matchable defect case
        r = csvimport.import_csv(self.conn, "chk", CSV_OVERLAP, CSV_MAPPING)
        self.assertEqual(r["imported"], 2)
        res = self._sync()
        # SAFEWAY + PAYROLL suppressed; only the card's TARGET row inserts
        self.assertEqual(res["skipped_import_duplicates"], 2)
        self.assertEqual(self._sfin_count(), 1)
        row = self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'sfin:%'").fetchone()
        self.assertEqual(row["id"], "sfin:acc-card:t3")
        # spend for the month is UNCHANGED vs a single-source setup:
        # 42.50 (csv) + 19.99 (sfin target) — not 42.50 twice
        st = budget.month_status(self.conn, dt.date(2025, 7, 15))
        self.assertEqual(round(st["total_actual"], 2), 62.49)

    def test_hourly_resync_stays_stable(self):
        csvimport.import_csv(self.conn, "chk", CSV_OVERLAP, CSV_MAPPING)
        self._sync()
        res = self._sync()                       # the hourly worker replay
        self.assertEqual(res["skipped_import_duplicates"], 2)
        self.assertEqual(self._sfin_count(), 1)  # no creep

    def test_new_transactions_after_import_coverage_insert(self):
        csvimport.import_csv(self.conn, "chk", CSV_OVERLAP, CSV_MAPPING)
        payload = copy.deepcopy(PAYLOAD)
        # identical charge 10 days AFTER the import coverage window — a
        # genuinely new transaction, must always insert
        payload["accounts"][0]["transactions"].append(
            {"id": "t9", "posted": 1753401600, "amount": "-42.50",   # 2025-07-25
             "description": "SAFEWAY STORE 123", "payee": "Safeway"})
        res = self._sync(payload)
        self.assertEqual(res["skipped_import_duplicates"], 2)
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'sfin:%'").fetchall()}
        self.assertIn("sfin:acc-chk:t9", ids)
        self.assertEqual(self._sfin_count(), 2)  # t9 + card t3

    def test_one_import_row_suppresses_at_most_one(self):
        # ONE imported coffee, TWO real same-day identical charges from the
        # aggregator → exactly one suppressed, the second survives
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2025-07-15,-13.13,COFFEE SHOP\n",
            CSV_MAPPING)
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"][0]["transactions"] = [
            {"id": "c1", "posted": 1752537600, "amount": "-13.13",
             "description": "COFFEE SHOP"},
            {"id": "c2", "posted": 1752537600, "amount": "-13.13",
             "description": "COFFEE SHOP"}]
        payload["accounts"][1]["transactions"] = []
        res = self._sync(payload)
        self.assertEqual(res["skipped_import_duplicates"], 1)
        self.assertEqual(self._sfin_count(), 1)

    def test_same_day_exact_amount_matches_without_name_overlap(self):
        # bank exports often carry opaque names ("POS DEBIT ...") — the
        # import-side matcher never compared names, so same-day exact
        # amount suppresses even with zero token overlap
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2025-07-15,-55.00,POS DEBIT 4417\n",
            CSV_MAPPING)
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"][0]["transactions"] = [
            {"id": "f1", "posted": 1752537600, "amount": "-55.00",
             "description": "SAFEWAY FUEL"}]
        payload["accounts"][1]["transactions"] = []
        res = self._sync(payload)
        self.assertEqual(res["skipped_import_duplicates"], 1)
        self.assertEqual(self._sfin_count(), 0)

    def test_nearby_amount_without_name_overlap_does_not_match(self):
        # ±3-day amount coincidence with unrelated names must NOT suppress
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2025-07-13,-77.00,POS DEBIT 4417\n",
            CSV_MAPPING)
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"][0]["transactions"] = [
            {"id": "g1", "posted": 1752537600, "amount": "-77.00",  # 07-15
             "description": "FLOWER SHOP"}]
        payload["accounts"][1]["transactions"] = []
        res = self._sync(payload)
        self.assertEqual(res["skipped_import_duplicates"], 0)
        self.assertEqual(self._sfin_count(), 1)


class ClassificationTests(unittest.TestCase):
    """The hourly sync must never clobber a user's account classification,
    and the credit balance sign flip must follow the persisted user type —
    not the name heuristic."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _sync(self, payload=PAYLOAD):
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_transport(payload))

    def test_org_name_classifies_card_and_labels_connection(self):
        # a card named plainly ("Alex Rivera (1234)") but whose ORG says it's a
        # credit card → classify credit (was landing as depository); and the
        # per-org item must show up in the Accounts-page connection list so
        # it's labeled SimpleFIN, not "Import".
        from oikonome.web.pages import _connections
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-disc", "name": "Alex Rivera (1234)",     # no credit hint
             "currency": "USD", "balance": "-43.00",
             "org": {"name": "Northwind Credit Card"}, "transactions": []},
            {"id": "acc-amex", "name": "Blue (6004)",
             "currency": "USD", "balance": "-12.00",
             "org": {"name": "American Express"}, "transactions": []}]
        self._sync(payload)
        types = {r["id"]: r["type"] for r in self.conn.execute(
            "SELECT id, type FROM accounts")}
        self.assertEqual(types["acc-disc"], "credit")   # org → credit
        self.assertEqual(types["acc-amex"], "credit")   # "american express"
        # the per-org child items are surfaced as connections (aggregator
        # 'simplefin-org') so the SPA can label them SimpleFIN
        aggs = {r["aggregator"] for r in _connections(self.conn)}
        self.assertIn("simplefin-org", aggs)

    def test_user_classification_survives_resync(self):
        # an account whose NAME reads depository but is actually a card
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-hsbc", "name": "Everyday Spending",  # no credit hint
             "currency": "USD", "balance": "-250.00",
             "org": {"name": "Demo Bank"}, "transactions": []}]
        self._sync(payload)
        row = self.conn.execute(
            "SELECT type, balance_current FROM accounts WHERE id='acc-hsbc'"
        ).fetchone()
        self.assertEqual(row["type"], "depository")      # heuristic misfires
        self.assertEqual(row["balance_current"], -250.0)
        # user fixes it via /accounts/classify → mark_type_user_set
        sync_base.mark_type_user_set(self.conn, "acc-hsbc", "credit",
                                     "credit card")
        self._sync(payload)                              # the hourly worker
        row = self.conn.execute(
            "SELECT type, subtype, type_user_set, balance_current "
            "FROM accounts WHERE id='acc-hsbc'").fetchone()
        self.assertEqual(row["type"], "credit")          # NOT clobbered
        self.assertEqual(row["subtype"], "credit card")
        self.assertTrue(row["type_user_set"])
        # flip follows the persisted type: card debt lands positive
        self.assertEqual(row["balance_current"], 250.0)

    def test_user_depository_pin_disables_flip(self):
        # inverse: name reads credit, user says depository → no sign flip.
        # (A genuine card name, so the heuristic really does say credit —
        # "Cardinal Savings" would not, being a word-boundary miss.)
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-x", "name": "Rewards Card",   # real credit hint
             "currency": "USD", "balance": "900.00",
             "org": {"name": "Demo Bank"}, "transactions": []}]
        self._sync(payload)
        self.assertEqual(self.conn.execute(
            "SELECT type FROM accounts WHERE id='acc-x'"
        ).fetchone()["type"], "credit")                  # heuristic → credit
        sync_base.mark_type_user_set(self.conn, "acc-x", "depository",
                                     "savings")
        self._sync(payload)
        row = self.conn.execute(
            "SELECT type, balance_current FROM accounts WHERE id='acc-x'"
        ).fetchone()
        self.assertEqual(row["type"], "depository")
        self.assertEqual(row["balance_current"], 900.0)  # kept, not flipped

    def test_classifying_a_card_flips_the_stored_balance_at_once(self):
        """Between the user's correction and the next hourly pull, the
        stored SimpleFIN balance must already carry the engine's sign.
        Left as pulled, a card the heuristic missed reads as a $250 asset
        in net worth (the debt sign-flipped) and as $0 debt in the runway."""
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-hsbc", "name": "Everyday Spending",  # no credit hint
             "currency": "USD", "balance": "-250.00",
             "available-balance": "-100.00",
             "org": {"name": "Demo Bank"}, "transactions": []}]
        self._sync(payload)
        sync_base.mark_type_user_set(self.conn, "acc-hsbc", "credit",
                                     "credit card")
        row = self.conn.execute(
            "SELECT balance_current, balance_available FROM accounts "
            "WHERE id='acc-hsbc'").fetchone()
        self.assertEqual(row["balance_current"], 250.0)
        self.assertEqual(row["balance_available"], 100.0)
        from oikonome.engine import reporting
        nw = reporting.compute_networth(self.conn)
        by_class = dict(map(tuple, nw["by_asset_class"]))
        # fixture card 250 + the corrected one 250, both owed
        self.assertEqual(by_class["Card debt"], -500.0)
        self.assertEqual(nw["current_total"], 4500.0)
        # the next sync keeps the flipped sign (no double flip)
        self._sync(payload)
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='acc-hsbc'"
        ).fetchone()["balance_current"], 250.0)

    def test_reclassifying_a_card_as_deposit_flips_back_at_once(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-x", "name": "Rewards Card",   # heuristic → credit
             "currency": "USD", "balance": "900.00",
             "org": {"name": "Demo Bank"}, "transactions": []}]
        self._sync(payload)
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='acc-x'"
        ).fetchone()["balance_current"], -900.0)        # flipped as a card
        sync_base.mark_type_user_set(self.conn, "acc-x", "depository",
                                     "savings")
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='acc-x'"
        ).fetchone()["balance_current"], 900.0)         # back, before any sync
        # a same-type change (credit → credit) never flips
        sync_base.mark_type_user_set(self.conn, "acc-x", "depository",
                                     "checking")
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='acc-x'"
        ).fetchone()["balance_current"], 900.0)

    def test_hourly_pull_keeps_a_loan_classification_signed(self):
        """A loan is debt like a card, so the pull owes it the same flip as
        the classification did. Flipping only at classify time would let
        the next hourly sync write the bank's sign back, and the mortgage
        would reappear as a positive asset within the hour."""
        payload = copy.deepcopy(PAYLOAD)
        payload["accounts"] = [
            {"id": "acc-mort", "name": "Home Mortgage",   # no type hint
             "currency": "USD", "balance": "-200000.00",
             "org": {"name": "Demo Bank"}, "transactions": []}]
        self._sync(payload)
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='acc-mort'"
        ).fetchone()["balance_current"], -200000.0)     # bank's own sign
        sync_base.mark_type_user_set(self.conn, "acc-mort", "loan",
                                     "mortgage")
        self._sync(payload)                              # the hourly worker
        row = self.conn.execute(
            "SELECT type, balance_current FROM accounts WHERE id='acc-mort'"
        ).fetchone()
        self.assertEqual(row["type"], "loan")
        self.assertEqual(row["balance_current"], 200000.0)

    def test_other_aggregators_store_card_debt_positive_already(self):
        """Plaid and MX report card debt as a positive owed amount, so a
        reclassification must not touch their balances."""
        sync_base.mark_type_user_set(self.conn, "card", "depository",
                                     "checking")
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='card'"
        ).fetchone()["balance_current"], 250.0)

    def test_heuristic_still_classifies_fresh_accounts(self):
        self._sync()
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, type, type_user_set, balance_current FROM accounts "
            "WHERE id LIKE 'acc-%'").fetchall()}
        self.assertEqual(rows["acc-card"]["type"], "credit")
        self.assertEqual(rows["acc-card"]["balance_current"], 250.0)  # flipped
        self.assertEqual(rows["acc-chk"]["type"], "depository")
        self.assertFalse(rows["acc-card"]["type_user_set"])
        self.assertFalse(rows["acc-chk"]["type_user_set"])


class NameClassificationTests(unittest.TestCase):
    """Type inference from account NAMES is word-bounded — 'card' the
    word means credit; 'card' the substring (Cardinal) must not."""

    def test_card_matches_the_word_not_the_substring(self):
        self.assertEqual(simplefin._classify({"name": "Cardinal Savings"}, None),
                         ("depository", "checking"))
        # a real card still classifies credit
        self.assertEqual(simplefin._classify({"name": "Rewards Card"}, None),
                         ("credit", "credit card"))
        self.assertEqual(simplefin._classify({"name": "Chase Visa"}, None),
                         ("credit", "credit card"))
        # investment substring hints still work (401A etc.)
        self.assertEqual(simplefin._classify({"name": "Employer 403B Plan"}, None),
                         ("investment", "403b"))

    def test_plan_names_carry_their_tax_wrapper(self):
        """The subtype must say how the money is taxed, not just that it is
        invested. A flat "retirement" stamp sends a pre-tax 401(k) to the
        taxable bucket of the retirement sim and the net-worth donut, where
        withdrawals take a capital-gains haircut on ordinary-income money."""
        cases = {
            "Fidelity 401(k)": "401k",
            "Employer 403B Plan": "403b",
            "Deferred Comp 457(b)": "457b",
            "Roth IRA": "roth",
            "Roth 401k": "roth",
            "Traditional IRA": "ira",
            "Health Savings HSA": "hsa",
            "College 529": "529",
            "Vanguard Brokerage": "brokerage",
            "Employee Pension Plan": "retirement",
            "Ret. Plan": "retirement",
            # every shape a bank writes the same wrapper in
            "Fidelity 401K": "401k",
            "Retirement 401A": "401k",
            "SEP-IRA": "ira",
            "Simple IRA": "ira",
            "Rollover IRAs": "ira",
            "Retirement Savings": "retirement",
        }
        for name, sub in cases.items():
            self.assertEqual(simplefin._classify({"name": name}, None),
                             ("investment", sub), name)

    def test_wrapper_glued_to_the_plan_number_still_counts(self):
        """Administrators run the wrapper into the plan number with no
        separator ("Roth401k"); a boundary that treats a digit like a
        letter sees no edge there and books a Roth 401(k) as checking."""
        for name, want in (("Roth401k", "roth"), ("Retirement401k", "401k"),
                           ("My Roth401(k)", "roth"), ("401K Plan", "401k")):
            typ, sub = simplefin._classify({"name": name}, None)
            self.assertEqual((typ, sub), ("investment", want), name)
        # and the coincidences stay excluded
        self.assertIsNone(simplefin._investment_subtype("vanguard admiral shares"))
        self.assertIsNone(simplefin._investment_subtype("everyday checking (4013)"))

    def test_tax_wrapper_needs_a_whole_token_not_a_substring(self):
        """The subtype decides how the retirement sim taxes every
        withdrawal from the account, so a wrapper inferred from a
        coincidence is a wrong tax bill, not a wrong label. The two
        coincidences banks actually produce: "ira" inside an ordinary word
        (Vanguard's Admiral share class, a holder's first name) and a plan
        number inside a masked last-4 ("Brokerage ...4013")."""
        # a fund share class is not an IRA, and a person is not one either
        _, sub = simplefin._classify({"name": "Vanguard Admiral Shares"}, None)
        self.assertNotIn(sub, ("ira", "roth", "401k", "403b", "457b"))
        self.assertEqual(
            simplefin._classify({"name": "Amira Patel (1234)"}, None),
            ("depository", "checking"))
        # a masked account number is not a plan number — and the TYPE
        # decision must agree with the subtype one, or an account with no
        # wrapper signal lands in the investment donut stamped "retirement"
        self.assertEqual(simplefin._classify({"name": "Brokerage 4013"}, None),
                         ("investment", "brokerage"))
        for masked in ("Everyday Checking (4013)", "Savings (4035)",
                       "Money Market (4570)", "Vacation Fund (5290)",
                       "Joint Checking 1401"):
            self.assertEqual(simplefin._classify({"name": masked}, None),
                             ("depository", "checking"), masked)


if __name__ == "__main__":
    unittest.main()
