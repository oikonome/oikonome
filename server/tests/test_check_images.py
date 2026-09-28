"""Check images: a receipt attachment marked kind='check' parses with the
check prompt (number/payee/amount/date/memo/bank, no line items), the payee
feeds the ordinary categorization flow (rule cache first, LLM once, cached;
overrides and flow rows untouchable), and a parsed amount that disagrees
with the transaction surfaces as a display-only mismatch flag."""

import json
import unittest

import httpx

from oikonome.engine import budget, llm_categorize, receipts

from .util import TODAY, add_txn, make_db, write_config

CHECK_JSON = {"check_number": "1234", "payee": "ACME PLUMBING",
              "amount": 245.00, "date": "2026-07-10",
              "memo": "water heater repair", "bank": "First Synthetic Bank"}


def _enable_llm(conn, *, consent=True):
    cfg = budget.load_config(conn)
    cfg["llm_url"] = "http://llm.test"
    cfg["llm_model"] = "vision-model"
    # llm.test reads as REMOTE to the sensitive-documents gate (it does
    # not resolve), and a check image needs that consent like a W-2 does —
    # these tests grant it so they exercise the parse, and the gate test
    # below withholds it
    if consent:
        cfg["taxdocs_allow_remote_llm"] = "1"
    budget.save_config(conn, cfg)


def _check_transport(check=CHECK_JSON, category="GENERAL_SERVICES",
                     calls=None):
    """One mock backend answering BOTH shapes: the vision call (image in the
    content) gets the check JSON; the follow-up text classification gets a
    numbered-category reply — exactly the two requests a check parse makes."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        is_vision = any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for m in body["messages"]
            for p in (m["content"] if isinstance(m["content"], list) else []))
        if calls is not None:
            calls.append("vision" if is_vision else "classify")
        content = (json.dumps(check) if is_vision
                   else json.dumps({"1": category}))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": content}}]})
    return httpx.MockTransport(handler)


class CheckUploadTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 245.00, "CHECK #1234",
                           primary="OTHER")

    def tearDown(self):
        self.conn.close()

    def test_upload_with_kind(self):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png",
                           kind="check")
        lst = receipts.for_txn(self.conn, self.txn)
        self.assertEqual([(str(r["id"]), r["kind"]) for r in lst],
                         [(rid, "check")])
        # default stays 'receipt'; junk kinds are refused
        receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        kinds = sorted(r["kind"] for r in receipts.for_txn(self.conn, self.txn))
        self.assertEqual(kinds, ["check", "receipt"])
        with self.assertRaises(ValueError):
            receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png",
                         kind="selfie")


class CheckParseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 245.00, "CHECK #1234",
                           primary="OTHER")
        self.rid = receipts.add(self.conn, self.txn, b"\x89PNG...",
                                "image/png", kind="check")

    def tearDown(self):
        self.conn.close()

    def _row(self):
        return receipts.for_txn(self.conn, self.txn)[0]

    def test_remote_llm_without_consent_refuses_before_claiming(self):
        """A check's face carries the full routing and account number —
        sending it to a REMOTE model takes the same sensitive-documents
        consent as a W-2, and the refusal names what would be sent. It
        fires before the claim, so nothing is left stamped 'parsing'."""
        from oikonome.engine import budget as _b
        cfg = _b.load_config(self.conn)
        cfg.pop("taxdocs_allow_remote_llm", None)
        _b.save_config(self.conn, cfg)
        with self.assertRaisesRegex(ValueError, "routing and account"):
            receipts.parse_one(self.conn, self.rid,
                               transport=_check_transport())
        # recorded as a FAILED parse with its reason on this path too: the
        # clients show the reason (and the retry) for 'failed' only, so a
        # refusal left at 'uploaded' was invisible and re-queued nightly
        self.assertEqual(self._row()["status"], "failed")
        self.assertIn("routing and account", self._row()["error"] or "")
        # the API routes claim BEFORE calling — a refusal there must land
        # as a FAILED parse with the reason, never a row stuck at
        # 'parsing' with the retry button hidden and no error shown
        self.assertTrue(receipts.claim(self.conn, self.rid))
        with self.assertRaisesRegex(ValueError, "routing and account"):
            receipts.parse_one(self.conn, self.rid,
                               transport=_check_transport(), claimed=True)
        row = self._row()
        self.assertEqual(row["status"], "failed")
        self.assertIn("routing and account", row["error"])

    def test_check_parse_stores_fields_no_items(self):
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_check_transport())
        self.assertEqual((out["status"], out["items"]), ("parsed", 0))
        row = self._row()
        self.assertEqual(row["status"], "parsed")
        self.assertEqual(row["parsed"]["check_number"], "1234")
        self.assertEqual(row["parsed"]["payee"], "ACME PLUMBING")
        self.assertEqual(row["parsed"]["amount"], 245.00)
        self.assertEqual(row["parsed"]["memo"], "water heater repair")
        self.assertEqual(row["parsed"]["bank"], "First Synthetic Bank")
        self.assertEqual(row["items"], [])
        # amounts agree → no mismatch flag
        self.assertFalse(row.get("amount_mismatch"))

    def test_parse_categorizes_via_llm_and_caches_rule(self):
        calls = []
        receipts.parse_one(self.conn, self.rid,
                           transport=_check_transport(calls=calls))
        self.assertEqual(calls, ["vision", "classify"])
        cat = self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()["category_primary"]
        self.assertEqual(cat, "GENERAL_SERVICES")
        # the classification is cached under the payee like any merchant
        rule = self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant=%s", ("ACME PLUMBING",)).fetchone()
        self.assertEqual((rule["category_primary"], rule["source"]),
                         ("GENERAL_SERVICES", "llm"))

    def test_cached_user_rule_wins_without_classify_call(self):
        llm_categorize.upsert_user_rule(self.conn, "ACME PLUMBING",
                                        "HOME_IMPROVEMENT")
        calls = []
        receipts.parse_one(self.conn, self.rid,
                           transport=_check_transport(calls=calls))
        self.assertEqual(calls, ["vision"])       # no second LLM round-trip
        cat = self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()["category_primary"]
        self.assertEqual(cat, "HOME_IMPROVEMENT")

    def test_mismatch_flag_when_amounts_disagree(self):
        bad = dict(CHECK_JSON, amount=199.99)
        receipts.parse_one(self.conn, self.rid,
                           transport=_check_transport(check=bad))
        row = self._row()
        self.assertTrue(row["amount_mismatch"])
        # display only — the transaction's amount is untouched
        amt = self.conn.execute(
            "SELECT amount FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()["amount"]
        self.assertEqual(amt, 245.00)


class CheckCategorizeGuardTests(unittest.TestCase):
    """User overrides are sacred and flow rows stay flow rows — the check
    hookup honors every guard apply() honors."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_override_never_clobbered(self):
        txn = add_txn(self.conn, TODAY, 245.00, "CHECK #1234",
                      primary="OTHER", override="ENTERTAINMENT")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png",
                           kind="check")
        receipts.parse_one(self.conn, rid, transport=_check_transport())
        row = self.conn.execute(
            "SELECT category_primary, category_override FROM transactions "
            "WHERE id=%s", (txn,)).fetchone()
        self.assertEqual(row["category_override"], "ENTERTAINMENT")
        self.assertEqual(row["category_primary"], "OTHER")

    def test_flow_guard_row_untouched(self):
        # importer-assigned flow category (a transfer) must never become
        # counted spend, even with a cached rule for the payee
        txn = add_txn(self.conn, TODAY, 245.00, "CHECK #1234",
                      primary="TRANSFER_OUT")
        llm_categorize.upsert_user_rule(self.conn, "ACME PLUMBING",
                                        "GENERAL_SERVICES")
        out = llm_categorize.categorize_check_payee(
            self.conn, txn, "ACME PLUMBING")
        self.assertIsNone(out)
        cat = self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (txn,)).fetchone()["category_primary"]
        self.assertEqual(cat, "TRANSFER_OUT")

    def test_flow_payee_string_is_skipped(self):
        # a check written to pay a credit card is bank mechanics, not a
        # merchant — the flow-guard refuses the string outright
        txn = add_txn(self.conn, TODAY, 500.00, "CHECK #1235",
                      primary="OTHER")
        out = llm_categorize.categorize_check_payee(
            self.conn, txn, "CHASE CREDIT CARD PAYMENT")
        self.assertIsNone(out)

    def test_classify_failure_never_fails_the_parse(self):
        txn = add_txn(self.conn, TODAY, 245.00, "CHECK #1236",
                      primary="OTHER")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png",
                           kind="check")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            is_vision = any(
                isinstance(p, dict) and p.get("type") == "image_url"
                for m in body["messages"]
                for p in (m["content"]
                          if isinstance(m["content"], list) else []))
            if is_vision:
                return httpx.Response(200, json={"choices": [{"message": {
                    "content": json.dumps(CHECK_JSON)}}]})
            return httpx.Response(500, json={"error": "model died"})

        out = receipts.parse_one(self.conn, rid,
                                 transport=httpx.MockTransport(handler))
        self.assertEqual(out["status"], "parsed")   # the check still parsed
        row = receipts.for_txn(self.conn, txn)[0]
        self.assertEqual(row["parsed"]["payee"], "ACME PLUMBING")


class CheckUploadEndpointTests(unittest.TestCase):
    """The upload route carries kind through multipart to the stored row."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid as _uuid

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy as _tenancy

        from .util import seed_accounts, write_config as _wc
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"chk-{_uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = _tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            _wc(conn)
            from .util import add_txn as _add
            cls.txn = _add(conn, TODAY, 245.00, "CHECK #1234", account="chk")
        finally:
            conn.close()

    def test_upload_kind_check(self):
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            data={"kind": "check"},
            files={"file": ("check.png", b"\x89PNG...", "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["receipts"][-1]["kind"], "check")

    def test_upload_default_kind_receipt(self):
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": ("r.png", b"\x89PNG...", "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["id"]
        row = [x for x in r.json()["receipts"] if x["id"] == rid][0]
        self.assertEqual(row["kind"], "receipt")

    def test_upload_bad_kind_rejected(self):
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            data={"kind": "selfie"},
            files={"file": ("r.png", b"\x89PNG...", "image/png")})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
