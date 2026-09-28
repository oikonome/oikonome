"""The merchant-wide category undo is client-held state posted back: the
apply response hands the SPA a snapshot, the SPA returns it verbatim, and
nothing ties that body to a prior apply. So it is validated like any other
body — a wrong shape is a 400 (never a 500), the lists are bounded, a rule
must belong to a touched merchant and carry provenance from the known set.
Prior-RESTORING values (a row's prev category, a snapshotted rule's
category) get shape checks only — bounded length, no control characters —
because restoring what was there before is always legitimate: membership
or flow-refusal tests would reject genuine snapshots (a custom label that
lived only on the moved rows, a flow-named rule) and make their undo 400
forever. The merchant detail read stays control-character-strict
but uncapped: an imported descriptor longer than the rename write's cap
must stay openable."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import llm_categorize
from oikonome.web import data

from .test_canonical_rules import canon, cat
from .test_llm_categorize import add_raw_txn
from .util import _ensure_db, add_txn, make_db, seed_accounts

PASSWORD = "correct-horse-battery"


def _owner_client(app):
    client = TestClient(app)
    email = f"undo-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email,
                                         "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client, client.get("/api/me").json()["tenant_id"]


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return appmod.app


class UndoSnapshotShapeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for raw in ("NORTHWIND COFFEE", "SQ *NRTHWND 4SPRINGFIELD IL"):
            canon(self.conn, raw, "Northwind Coffee")
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "NORTHWIND COFFEE",
                    primary="PERSONAL_CARE", raw={})
        add_raw_txn(self.conn, "flow", "2025-07-02", 500,
                    "SQ *NRTHWND 4SPRINGFIELD IL",
                    primary="TRANSFER_OUT", raw={})
        self.res = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                              "ENTERTAINMENT")

    def tearDown(self):
        self.conn.close()

    def _refused(self, undo, why: str):
        with self.assertRaises(ValueError, msg=why):
            data.undo_merchant_category(self.conn, undo)
        # and the refusal left the ledger exactly as the apply put it
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "flow"), "ENTERTAINMENT")

    def test_a_genuine_snapshot_still_restores_flow_rows(self):
        # TRANSFER_OUT as a PRIOR category is the point of undo: the write
        # moved that row into spend and undo must put the transfer back
        n = data.undo_merchant_category(self.conn, self.res["undo"])
        self.assertEqual(n, 2)
        self.assertEqual(cat(self.conn, "flow"), "TRANSFER_OUT")
        self.assertEqual(cat(self.conn, "a"), "PERSONAL_CARE")

    def test_lists_of_strings_are_refused_not_crashed(self):
        # a list of strings where objects belong is a 400, never a 500
        self._refused({"canons": ["x"], "affected": ["y"]}, "strings")
        self._refused({"canons": [{"canon": "Northwind Coffee"}],
                       "affected": [["id", "prev"]]}, "nested list")
        self._refused({"canons": "Northwind Coffee"}, "canons not a list")
        self._refused("not even an object", "scalar body")

    def test_oversized_lists_are_refused(self):
        big = [{"id": f"t{i}", "prev": "PERSONAL_CARE"}
               for i in range(data.UNDO_MAX_AFFECTED + 1)]
        self._refused({"canons": [{"canon": "Northwind Coffee"}],
                       "affected": big}, "affected over cap")
        rules = [{"merchant": "Northwind Coffee",
                  "prior": {"category_primary": "ENTERTAINMENT"}}
                 ] * (data.UNDO_MAX_RULES + 1)
        self._refused({"canons": [{"canon": "Northwind Coffee"}],
                       "rules": rules}, "rules over cap")

    def test_prev_is_shape_checked_not_membership_checked(self):
        # malformed prev values — over-long, control characters — are 400
        for prev in ("X" * (data._NAME_MAX + 1), "bad\x07category", ""):
            bad = {"canons": self.res["undo"]["canons"],
                   "rules": self.res["undo"]["rules"],
                   "affected": [{"id": "a", "prev": prev}]}
            self._refused(bad, f"malformed prev {prev!r}")
        # …but a custom label the ledger holds NOWHERE else is a restore,
        # not authored text: once the write moved every row, the displaced
        # label lives only in the snapshot, so a membership test against
        # "known" categories would 400 exactly those undos forever
        ok = {"canons": self.res["undo"]["canons"],
              "rules": self.res["undo"]["rules"],
              "affected": [{"id": "a", "prev": "Long-Gone Custom Label"}]}
        data.undo_merchant_category(self.conn, ok)
        self.assertEqual(cat(self.conn, "a"), "Long-Gone Custom Label")
        # None (the row had no category before) is a legitimate restore
        ok = {"canons": self.res["undo"]["canons"],
              "rules": self.res["undo"]["rules"],
              "affected": [{"id": "a", "prev": None}]}
        data.undo_merchant_category(self.conn, ok)
        self.assertIsNone(cat(self.conn, "a"))

    def test_a_custom_category_named_by_a_snapshotted_rule_restores(self):
        # a custom label the write displaced lives nowhere but the snapshot
        # once every row moved — it restores like any bounded name
        undo = {"canons": [{"canon": "Northwind Coffee",
                            "prior_rule": {"category_primary": "Kid Stuff",
                                           "source": "user",
                                           "disabled": False}}],
                "rules": [{"merchant": "Northwind Coffee",
                           "prior": {"category_primary": "Kid Stuff",
                                     "source": "user", "disabled": False}}],
                "affected": [{"id": "a", "prev": "Kid Stuff"}]}
        data.undo_merchant_category(self.conn, undo)
        self.assertEqual(cat(self.conn, "a"), "Kid Stuff")

    def test_forged_rule_provenance_is_refused(self):
        # a user rule dressed up as the aggregator's / the model's
        undo = {"canons": [{"canon": "Northwind Coffee"}],
                "rules": [{"merchant": "Northwind Coffee",
                           "prior": {"category_primary": "ENTERTAINMENT",
                                     "source": "plaid"}}],
                "affected": []}
        self._refused(undo, "unknown source")

    def test_a_rule_for_an_untouched_merchant_is_refused(self):
        canon(self.conn, "ACME GYM", "Acme Gym")
        undo = {"canons": [{"canon": "Northwind Coffee"}],
                "rules": [{"merchant": "Acme Gym",
                           "prior": {"category_primary": "PERSONAL_CARE",
                                     "source": "user"}}],
                "affected": []}
        self._refused(undo, "rule outside the snapshot's canons")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='Acme Gym'"
        ).fetchone())

    def test_a_flow_named_prior_rule_restores_and_stays_inert(self):
        # A snapshot can genuinely hold a flow-named rule (a rename, a
        # replayed undo, an older row) — refusing it would make that undo
        # 400 forever. Restoring it is safe because apply() refuses to act on
        # flow-named rules (llm_categorize's rules CTE), so the restored
        # rule categorizes nothing.
        undo = {"canons": [{"canon": "Northwind Coffee",
                            "prior_rule": {"category_primary": "TRANSFER_OUT",
                                           "source": "llm"}}],
                "affected": []}
        data.undo_merchant_category(self.conn, undo)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE category_primary='TRANSFER_OUT'").fetchone())
        llm_categorize.apply(self.conn)
        # the rows the apply in setUp moved stay put — the restored
        # flow-named rule moved nothing
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "flow"), "ENTERTAINMENT")

    def test_a_prior_rule_up_to_the_snapshot_name_cap_restores(self):
        # rule categories are bounded like every other snapshot name (200),
        # so a snapshot holding a custom category set_merchant_category
        # itself accepted always restores
        label = "Very Specific Custom Category " * 5   # 150 chars
        undo = {"canons": [{"canon": "Northwind Coffee",
                            "prior_rule": {"category_primary": label,
                                           "source": "user",
                                           "disabled": False}}],
                "affected": []}
        data.undo_merchant_category(self.conn, undo)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE category_primary=%s",
            (label,)).fetchone())


class UndoApiTests(unittest.TestCase):
    """The HTTP door: malformed is 400, never 500; the merchant-wide
    write is demo-denied like the other merchant-wide rewrites."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-08-01", 20, "Cedar Physio",
                    primary="MEDICAL")
        finally:
            conn.close()

    def test_malformed_undo_bodies_are_400(self):
        for body in ({}, {"undo": "x"}, {"undo": ["x"]},
                     {"undo": {"canons": ["x"], "affected": ["y"]}},
                     {"undo": {"canons": [{"canon": "Cedar Physio"}],
                               "affected": [{"id": 1, "prev": "MEDICAL"}]}},
                     {"undo": {"canons": [{"canon": "Cedar Physio"}],
                               "affected": [{"id": "t",
                                             "prev": "no\x07pe"}]}}):
            r = self.client.post("/api/bills/merchant-category/undo",
                                 json=body)
            self.assertEqual(r.status_code, 400, f"{body!r}: {r.text}")

    def test_round_trip_through_the_api(self):
        r = self.client.post("/api/bills/merchant-category",
                             json={"payee": "Cedar Physio",
                                   "category": "PERSONAL_CARE"})
        self.assertEqual(r.status_code, 200, r.text)
        u = self.client.post("/api/bills/merchant-category/undo",
                             json={"undo": r.json()["undo"]})
        self.assertEqual(u.status_code, 200, u.text)
        self.assertEqual(u.json()["restored"], 1)

    def test_merchant_wide_writes_are_demo_denied(self):
        from oikonome.engine import budget
        from oikonome.web import demoguard
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["demo_mode"] = True
            demoguard.invalidate(self.tid)
            for path, body in (
                    ("/api/bills/merchant-category/preview",
                     {"payee": "Cedar Physio", "category": "PERSONAL_CARE"}),
                    ("/api/bills/merchant-category",
                     {"payee": "Cedar Physio", "category": "PERSONAL_CARE"}),
                    ("/api/bills/merchant-category/undo",
                     {"undo": {"canons": [], "affected": []}}),
                    # the SAME rewrite under its other two spellings: the
                    # rules editor's set, and the ledger row's "apply to
                    # whole merchant" (denied before the row lookup, so a
                    # made-up id proves the guard runs first)
                    ("/api/rules/set",
                     {"merchant": "Cedar Physio",
                      "category": "PERSONAL_CARE"}),
                    ("/api/transactions/nope/category",
                     {"category": "PERSONAL_CARE", "scope": "all"})):
                r = self.client.post(path, json=body)
                self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            # the single-row correction stays open on demo — an existing
            # row is needed to see past the guard, and none is at hand
            # here, so assert the door is not the demo 403
            r = self.client.post("/api/transactions/nope/category",
                                 json={"category": "PERSONAL_CARE",
                                       "scope": "one"})
            self.assertEqual(r.status_code, 404, r.text)
        finally:
            with budget.config_txn(conn) as cfg:
                cfg.pop("demo_mode", None)
            demoguard.invalidate(self.tid)
            conn.close()


class MerchantDetailInputTests(unittest.TestCase):
    """The detail read fires once per expanded row; its name must be
    control-character-free like the rename write's, but UNCAPPED — an
    imported descriptor longer than the write's 200-char cap is a real
    display name the catalog lists, and a capped read would make exactly
    those rows unopenable."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-08-01", 20, "Cedar Physio")
        finally:
            conn.close()

    def test_control_character_names_are_400(self):
        for bad in ("Bad\x07Name", "Bad\x85Name", "Tab\tName"):
            r = self.client.get("/api/merchants/detail",
                                params={"display": bad})
            self.assertEqual(r.status_code, 400, f"{bad!r}: {r.text}")

    def test_an_over_long_imported_name_stays_openable(self):
        long_name = "IMPORTED DESCRIPTOR " * 13    # 260 chars
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_txn(conn, "2026-08-02", 15, long_name)
        finally:
            conn.close()
        r = self.client.get("/api/merchants/detail",
                            params={"display": long_name})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"], 1)
        # an unknown long name is not-found, never refused for its length
        r = self.client.get("/api/merchants/detail",
                            params={"display": "A" * 300})
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_real_name_still_reads(self):
        r = self.client.get("/api/merchants/detail",
                            params={"display": "Cedar Physio"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"], 1)


if __name__ == "__main__":
    unittest.main()
