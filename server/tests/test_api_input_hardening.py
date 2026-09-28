"""API input hardening: hostile or malformed JSON must come back as a 400,
never surface as a 500.

The API's body fields are whatever JSON a client chooses to send. A number
where a name belongs, an id past bigint, a page number that multiplies into
an OFFSET the driver cannot bind, a megabyte merchant name — unguarded,
each of these reaches Python string methods or the database driver and
blows up as an opaque server error. These tests pin the contract: bad input
is refused loudly at the door, and the merchant identity doors (rename /
undo) are owner-only and closed on demo instances.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PASSWORD = "correct-horse-battery"


def _owner_client(app):
    client = TestClient(app)
    email = f"harden-{uuid.uuid4().hex[:10]}@example.dev"
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


class NonStringBodyFieldsAreRefused(unittest.TestCase):
    """A JSON number/object/array where short human text belongs is a 400.

    Unguarded, `{"display": 123}` reaches `.strip()` on an int and the
    request dies as an AttributeError → 500."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())

    def test_numeric_display_on_merchant_rename_is_a_400(self):
        r = self.client.post("/api/merchants/rename",
                             json={"display": 123, "to": "Acme"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_composite_values_are_refused_not_stringified(self):
        # str()-coercing these would have written "{'a': 1}" into the ledger
        for bad in ({"a": 1}, ["x"], 1.5, True):
            r = self.client.post("/api/merchants/rename",
                                 json={"display": "Acme", "to": bad})
            self.assertEqual(r.status_code, 400, f"to={bad!r}: {r.text}")

    def test_numeric_category_on_a_transaction_is_a_400(self):
        r = self.client.post("/api/transactions/nope/category",
                             json={"category": 123})
        self.assertEqual(r.status_code, 400, r.text)

    def test_numeric_scope_on_a_transaction_is_a_400(self):
        r = self.client.post("/api/transactions/nope/category",
                             json={"category": "Food", "scope": 123})
        self.assertEqual(r.status_code, 400, r.text)

    def test_non_string_owner_is_a_400_on_both_owner_doors(self):
        for path in ("/api/accounts/chk/owner", "/api/transactions/x/owner"):
            r = self.client.post(path, json={"owner": {"name": "me"}})
            self.assertEqual(r.status_code, 400, f"{path}: {r.text}")

    def test_non_string_payee_is_a_400_across_the_bill_doors(self):
        for path in ("/api/bills/save", "/api/bills/delete",
                     "/api/bills/restore", "/api/bills/toggle",
                     "/api/bills/hint", "/api/bills/merchant-category",
                     "/api/bills/merchant-category/preview"):
            r = self.client.post(path, json={"payee": 5,
                                             "category": "Food",
                                             "amount": 10})
            self.assertEqual(r.status_code, 400, f"{path}: {r.text}")

    def test_absent_and_null_fields_still_read_as_missing(self):
        # the guard must not change the shape of ordinary refusals:
        # nothing sent (or null) is still the same "required" 400
        for body in ({}, {"payee": None}):
            r = self.client.post("/api/bills/delete", json=body)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("payee required", r.text)


class UndoIdIsBounded(unittest.TestCase):
    """The undo journal id is a bigserial; anything outside its range must
    be refused before the driver binds it (out-of-range bind → 500)."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())

    def test_an_id_past_bigint_is_a_400_not_a_driver_error(self):
        for bad in (1e30, 2 ** 63, -1, 0):
            r = self.client.post("/api/merchants/undo", json={"id": bad})
            self.assertEqual(r.status_code, 400, f"id={bad!r}: {r.text}")

    def test_a_non_numeric_id_is_a_400(self):
        for bad in ("abc", None, [1], {"id": 1}):
            r = self.client.post("/api/merchants/undo", json={"id": bad})
            self.assertEqual(r.status_code, 400, f"id={bad!r}: {r.text}")

    def test_a_well_formed_unknown_id_is_a_404(self):
        r = self.client.post("/api/merchants/undo", json={"id": 999_999_999})
        self.assertEqual(r.status_code, 404, r.text)


class PageNumbersAreClamped(unittest.TestCase):
    """An unbounded page multiplies into an OFFSET past bigint, which comes
    back as NumericValueOutOfRange → 500 on both paged lists."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())

    def test_a_giant_page_on_transaction_search_returns_empty_not_500(self):
        r = self.client.get("/api/transactions",
                            params={"q": "zzz", "page": 10 ** 19})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"], [])

    def test_a_giant_page_on_rules_returns_not_500(self):
        r = self.client.get("/api/rules", params={"page": 10 ** 19})
        self.assertEqual(r.status_code, 200, r.text)

    def test_page_zero_still_means_the_first_page(self):
        r = self.client.get("/api/transactions",
                            params={"q": "zzz", "page": 0})
        self.assertEqual(r.status_code, 200, r.text)


class BusinessWorksheetPagingIsClamped(unittest.TestCase):
    """`limit` on the business worksheet is bounded and `offset` must be
    clamped as well as floored: an offset past bigint reaches OFFSET and
    dies inside the driver as NumericValueOutOfRange -> 500. The same clamp
    the ledger's `page` carries."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())

    def test_a_giant_offset_returns_an_empty_page_not_500(self):
        r = self.client.get("/api/business", params={"offset": 10 ** 19})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"], [])

    def test_a_negative_offset_still_means_the_first_page(self):
        r = self.client.get("/api/business", params={"offset": -5})
        self.assertEqual(r.status_code, 200, r.text)


class EveryBusinessRouteRangeChecksItsYear(unittest.TestCase):
    """A `year` is never just a number here: it becomes dt.date(year, 1, 1),
    `year + 1` for the Q4 estimated-tax deadline, or a bigint bind. Every
    route that takes one range-checks it, or the same parameter answers 400
    on one door and something else on the next.
    """

    YEAR_ROUTES = ("transactions", "pnl", "package.zip", "pnl.csv",
                   "mileage", "vendors", "estimated-tax")

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())
        cls.eid = cls.client.post("/api/business/entities", json={
            "name": "Gadgets LLC", "structure": "sole_prop"}).json()["id"]

    def test_an_out_of_range_year_is_refused_on_every_entity_route(self):
        for path in self.YEAR_ROUTES:
            for bad in (0, 99999, 10 ** 19, -1):
                r = self.client.get(
                    f"/api/business/entities/{self.eid}/{path}",
                    params={"year": bad})
                self.assertEqual(r.status_code, 400,
                                 f"{path}?year={bad}: {r.text}")

    def test_every_route_refuses_the_same_year(self):
        """One shared bound, not a per-route copy: an inline bound of
        1900..9998 on one door beside the shared 1900..2100 makes year=5000
        answer 400 on six routes and 200 on the seventh — the drift a
        per-route copy of a range check always produces. 2100 is the safe
        ceiling here: the widest thing a year reaches is the Q4
        1040-ES deadline's `dt.date(year + 1, 1, 15)`, and 2101 is far
        inside dt.date's range; the mileage-rate and wage-base tables fall
        back to their latest published year for anything unlisted."""
        for path in self.YEAR_ROUTES:
            r = self.client.get(f"/api/business/entities/{self.eid}/{path}",
                                params={"year": 5000})
            self.assertEqual(r.status_code, 400, f"{path}: {r.text}")

    def test_the_worksheet_and_its_export_agree(self):
        for path in ("/api/business", "/api/business/export.csv"):
            r = self.client.get(path, params={"year": 99999})
            self.assertEqual(r.status_code, 400, f"{path}: {r.text}")

    def test_a_real_year_still_works(self):
        for path in self.YEAR_ROUTES:
            r = self.client.get(
                f"/api/business/entities/{self.eid}/{path}",
                params={"year": 2026})
            self.assertEqual(r.status_code, 200, f"{path}: {r.text}")


class MerchantNamesAreBounded(unittest.TestCase):
    """The rename target is written into merchant_canonical for every
    variant and rendered on every surface — so its size and content are
    capped at the door."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _owner_client(_app())
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-08-01", 20, "Cedar Lane Cleaners")
        finally:
            conn.close()

    def test_a_300_char_name_is_refused(self):
        r = self.client.post("/api/merchants/rename",
                             json={"display": "Cedar Lane Cleaners",
                                   "to": "A" * 300})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("too long", r.text)

    def test_control_characters_are_refused_c0_and_c1(self):
        for bad in ("Bad\x07Name",          # C0 bell
                    "Bad\x00Name",          # NUL
                    "Bad\x85Name",          # C1 next-line
                    "Tab\tName"):
            r = self.client.post("/api/merchants/rename",
                                 json={"display": "Cedar Lane Cleaners",
                                       "to": bad})
            self.assertEqual(r.status_code, 400, f"to={bad!r}: {r.text}")
        # the display side is checked the same way
        r = self.client.post("/api/merchants/rename",
                             json={"display": "Bad\x07", "to": "Fine"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_name_at_the_cap_is_accepted(self):
        r = self.client.post("/api/merchants/rename",
                             json={"display": "Cedar Lane Cleaners",
                                   "to": "B" * 200})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"], 1)


class MerchantDoorsAreOwnerOnlyAndDemoClosed(unittest.TestCase):
    """Rename/undo change merchant identity for the whole household —
    a family viewer may not use them, and a demo instance (shared
    credentials, synthetic data) refuses them outright."""

    @classmethod
    def setUpClass(cls):
        cls.appobj = _app()
        cls.owner, cls.tid = _owner_client(cls.appobj)

    def _viewer(self):
        inv = self.owner.post("/api/invites",
                              json={"label": "fam", "password": PASSWORD})
        assert inv.status_code == 200, inv.text
        token = inv.json()["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.appobj)
        r = viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"fam-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text
        assert viewer.get("/api/me").json()["role"] == "viewer"
        return viewer

    def test_a_viewer_may_not_rename_or_undo(self):
        viewer = self._viewer()
        r = viewer.post("/api/merchants/rename",
                        json={"display": "A", "to": "B"})
        self.assertEqual(r.status_code, 403, r.text)
        r = viewer.post("/api/merchants/undo", json={"id": 1})
        self.assertEqual(r.status_code, 403, r.text)

    def test_a_demo_instance_refuses_rename_and_undo(self):
        from oikonome.web import demoguard
        demo, demo_tid = _owner_client(self.appobj)
        conn = tenancy.tenant_connect(demo_tid)
        try:
            write_config(conn, demo_mode=True)
        finally:
            conn.close()
        demoguard.invalidate(demo_tid)
        for path, body in (("/api/merchants/rename",
                            {"display": "A", "to": "B"}),
                           ("/api/merchants/undo", {"id": 1})):
            r = demo.post(path, json=body)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("demo", r.text)


class ExportOrderNamesTheRenameJournal(unittest.TestCase):
    """The export's presentation order must place the rename journal next
    to the canonical-name map it undoes — discovery would still export it,
    but dangling at the alphabetical tail, away from its context."""

    def test_merchant_renames_follows_merchant_canonical(self):
        from oikonome.web import pages
        order = pages._EXPORT_ORDER
        self.assertIn("merchant_renames", order)
        self.assertEqual(order.index("merchant_renames"),
                         order.index("merchant_canonical") + 1)


if __name__ == "__main__":
    unittest.main()
