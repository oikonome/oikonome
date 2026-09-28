"""The Retire page's what-if fields are text boxes, and an emptied box
means "use the default" — never an error.

Both adjust panels label the cash-yield box "blank = keeps pace with
inflation", and the setup walkthrough hands its own boxes straight to the
query string. So a cleared field arrives as `x=`, an empty string, which
must revert that one assumption rather than refuse the whole projection.
The same handler also has to survive a value that is not a number at all:
"nan" parses as a float and then slips through a min/max clamp — `max(lo,
min(nan, hi))` is `lo` — so a NaN would silently become the worst
assumption in the range and be echoed back as if it had been asked for.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class ClearedWhatIfFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"blank-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # no birthdate: the derived age is the handler's own 45, so an
            # emptied age field has an unambiguous answer to fall back to
        finally:
            conn.close()
        cls.client.post("/api/settings", json={"birthdate": ""})

    def test_a_cleared_cash_yield_means_the_inflation_default(self):
        r = self.client.get("/api/retirement?age=60&end=70&infl=3&cash=")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body["inputs"]["cash"])
        # the default IS "cash keeps pace with inflation": no real gain
        self.assertEqual(body["cash_real_return"], 0.0)

    def test_every_cleared_what_if_field_falls_back_to_its_default(self):
        """The walkthrough sends its boxes unfiltered, so any one of them
        can arrive empty; an emptied box must not 422 the projection."""
        blank_all = ("age=&spend=&ret=&infl=&end=&ssage=&employer_mo="
                     "&taxable_mo=&resume=&stockpct=&cash=")
        r = self.client.get(f"/api/retirement?{blank_all}")
        self.assertEqual(r.status_code, 200, r.text)
        inp = r.json()["inputs"]
        self.assertEqual(inp["age"], 45)            # no birthdate configured
        self.assertEqual(inp["ret"], 7.0)
        self.assertEqual(inp["infl"], 3.0)
        self.assertEqual(inp["end"], 95)
        self.assertEqual(inp["ssage"], 67)
        self.assertEqual(inp["stockpct"], 90)
        self.assertEqual(inp["employer_mo"], 0)
        self.assertEqual(inp["taxable_mo"], 0)
        self.assertIsNone(inp["cash"])
        # one emptied field beside real answers reverts only that one
        r = self.client.get("/api/retirement?age=60&end=70&ret=&stockpct=50")
        self.assertEqual(r.status_code, 200, r.text)
        inp = r.json()["inputs"]
        self.assertEqual((inp["age"], inp["end"], inp["ret"],
                          inp["stockpct"]), (60, 70, 7.0, 50))

    def test_a_rate_that_is_not_a_number_reads_as_its_default(self):
        """NaN is not a what-if. It must not land on the floor of the
        clamp — a page echoing "cash -20%" nobody typed is worse than a
        page that quietly models the default."""
        for field, default in (("cash", None), ("ret", 7.0), ("infl", 3.0)):
            for spelling in ("nan", "NaN", "-nan"):
                r = self.client.get(
                    f"/api/retirement?age=60&end=70&{field}={spelling}")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["inputs"][field], default,
                                 f"{field}={spelling}")

    def test_an_infinite_rate_clamps_like_any_other_huge_number(self):
        """±inf is ordinary out-of-range input — the top and bottom of the
        range the controls offer, not a fallback."""
        for qs, field, want in (("cash=inf", "cash", 30.0),
                                ("cash=-inf", "cash", -20.0),
                                ("cash=1e400", "cash", 30.0),
                                ("ret=inf", "ret", 30.0),
                                ("infl=-inf", "infl", -20.0)):
            r = self.client.get(f"/api/retirement?age=60&end=70&{qs}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["inputs"][field], want, qs)

    def test_a_field_that_is_not_a_number_is_still_refused(self):
        """Blank is an answer; junk is not. Coercing "abc" to a default
        would hide a broken client instead of reporting it."""
        for qs in ("cash=abc", "age=abc", "ret=lots", "stockpct=90%"):
            r = self.client.get(f"/api/retirement?{qs}")
            self.assertEqual(r.status_code, 422, qs)


if __name__ == "__main__":
    unittest.main()
