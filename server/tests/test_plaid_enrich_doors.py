"""The HTTP doors around Plaid Enrich, and the two neighbouring import /
export doors on the same module.

Enrich is the one door in the app that spends real per-row money on the
operator's Plaid account, so the monthly ceiling has to be something a
person can actually set, not a value reachable only by editing the settings
document by hand.

The other two are ordinary hardening of doors that live beside it: a CSV
cell whose dangerous first character hides behind an invisible one is still
a formula when a spreadsheet opens it, and a body field of the wrong type
is a client mistake (400), never a server error (500).
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import plaid
from oikonome.web.api import _csv_safe

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return appmod.app


def _owner_client():
    client = TestClient(_app())
    email = f"enrichdoor-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


class TheMonthlyEnrichCapIsSettable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _owner_client()

    def test_the_cap_saves_and_reads_back(self):
        r = self.client.post("/api/settings", json={"plaid_enrich_cap": 500})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get("/api/settings").json()["plaid_enrich_cap"], 500)

    def test_the_cap_is_clamped_to_a_sane_range(self):
        self.client.post("/api/settings", json={"plaid_enrich_cap": -40})
        self.assertEqual(
            self.client.get("/api/settings").json()["plaid_enrich_cap"], 0)
        self.client.post("/api/settings", json={"plaid_enrich_cap": 10**9})
        self.assertEqual(
            self.client.get("/api/settings").json()["plaid_enrich_cap"], 100000)

    def test_a_non_numeric_cap_is_refused(self):
        r = self.client.post("/api/settings", json={"plaid_enrich_cap": "lots"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_non_finite_number_is_refused_not_a_500(self):
        # json.loads accepts the Infinity token; int(inf) raises
        # OverflowError, which is not a ValueError — a 400, not a crash
        r = self.client.post("/api/settings",
                             content='{"plaid_enrich_cap": Infinity}',
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.post("/api/import/enrich",
                             content='{"limit": Infinity}',
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_the_status_door_reports_the_cap_it_would_enforce(self):
        self.client.post("/api/settings", json={"plaid_enrich_cap": 250})
        body = self.client.get("/api/import/enrich").json()
        self.assertEqual(body["cap"], 250)
        self.assertIn("candidates", body)


class TheEnrichCapIsAnOperatorCeilingOnHosted(unittest.TestCase):
    """A tenant may lower the monthly Enrich cap but never raise it past
    what the instance allows.

    Enrich is billed per row to whoever owns the Plaid credentials. On
    hosted that is the OPERATOR — a hosted tenant cannot bring its own keys
    — so this number is not a preference there, it is the operator's spend
    ceiling, and a ceiling the spender can raise is not a ceiling at all.
    Self-host bills its own Plaid account, so the full range stays theirs."""

    @classmethod
    def setUpClass(cls):
        cls.client = _owner_client()

    @staticmethod
    def _as_hosted():
        """Make the settings door see a hosted instance without setting the
        env: OIKONOME_HOSTED also arms the forced-2FA write gate, which
        would 403 the very POST under test on an account that has no
        second factor — a different rule, not this one."""
        import oikonome.web.api as apimod
        real = apimod.env_flag
        return mock.patch.object(
            apimod, "env_flag",
            lambda name: True if name == "OIKONOME_HOSTED" else real(name))

    def _cap(self):
        return self.client.get("/api/settings").json()["plaid_enrich_cap"]

    def _save(self, body):
        with self._as_hosted():
            r = self.client.post("/api/settings", json=body)
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_hosted_tenant_can_still_lower_it_or_turn_it_off(self):
        self._save({"plaid_enrich_cap": 250})
        self.assertEqual(self._cap(), 250)
        self._save({"plaid_enrich_cap": 0})
        self.assertEqual(self._cap(), 0)

    def test_self_host_keeps_the_full_range(self):
        r = self.client.post("/api/settings", json={"plaid_enrich_cap": 100000})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._cap(), 100000)


class TheEnrichCeilingHoldsAtSpendTimeNotOnlyAtWriteTime(unittest.TestCase):
    """The ceiling has to be applied where the money is spent.

    The settings door clamps what a tenant WRITES, which does nothing about a
    number already sitting in the document — one restored from a backup,
    say. Unless that value is read through the ceiling it keeps authorising
    the operator's Plaid spend for as long as nobody saves settings again,
    which may be never.
    So these go at `_cap_from`/`enrich_usage` directly and put the oversized
    value in the document WITHOUT passing the settings door."""

    @classmethod
    def setUpClass(cls):
        cls.client = _owner_client()
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _store(self, value):
        """Write plaid_enrich_cap straight into the settings document — the
        value the write-time clamp never saw."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                if value is None:
                    cfg.pop("plaid_enrich_cap", None)
                else:
                    cfg["plaid_enrich_cap"] = value
        finally:
            conn.close()

    def _cap_seen(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return plaid.enrich_usage(conn)["cap"]
        finally:
            conn.close()

    @staticmethod
    def _as_hosted():
        import oikonome.web.api as apimod
        real = apimod.env_flag
        return mock.patch.object(
            apimod, "env_flag",
            lambda name: True if name == "OIKONOME_HOSTED" else real(name))

    def test_self_host_still_reads_the_number_it_was_given(self):
        # self-host bills its own Plaid account, so the range stays theirs
        self._store(100000)
        self.assertEqual(self._cap_seen(), 100000)

    def test_zero_still_means_off_and_never_falls_back_to_the_default(self):
        self._store(0)
        with self._as_hosted():
            self.assertEqual(self._cap_seen(), 0)
        self.assertEqual(self._cap_seen(), 0)

    def test_an_absent_key_is_the_only_thing_that_takes_the_default(self):
        self._store(None)
        with self._as_hosted():
            self.assertEqual(self._cap_seen(), plaid.ENRICH_CAP_DEFAULT)

    def test_a_value_under_the_ceiling_is_read_back_untouched(self):
        self._store(250)
        with self._as_hosted():
            self.assertEqual(self._cap_seen(), 250)


class CsvCellsCannotHideAFormulaBehindAnInvisibleCharacter(unittest.TestCase):
    """An invisible first character does not stop a spreadsheet reaching the
    `=` behind it, so a leading tab, CR, line feed or byte-order mark is
    neutralized the same way as the formula character itself."""

    def test_a_plain_formula_is_still_quoted(self):
        for cell in ("=1+1", "+1", "-1", "@SUM(A1)", "\t=1+1", "\r=1+1"):
            self.assertTrue(_csv_safe(cell).startswith("'"), cell)

    def test_an_invisible_leading_character_does_not_smuggle_one_through(self):
        for cell in ("\n=1+1", "﻿=1+1", " =1+1", "﻿\t@SUM(A1)"):
            self.assertTrue(_csv_safe(cell).startswith("'"), repr(cell))

    def test_ordinary_text_and_non_strings_are_untouched(self):
        self.assertEqual(_csv_safe("Acme Wholesale"), "Acme Wholesale")
        self.assertEqual(_csv_safe("2026-08-17"), "2026-08-17")
        self.assertEqual(_csv_safe(12.5), 12.5)
        self.assertEqual(_csv_safe(None), None)


class ThePlanImportDoorRefusesBadBodiesInsteadOf500ing(unittest.TestCase):
    """Every field of this body is whatever JSON the caller chose to send.
    A number where the CSV text belongs, or an unparseable CSV, must be a
    400; an opaque server error tells a scripted client nothing about what
    it got wrong."""

    @classmethod
    def setUpClass(cls):
        cls.client = _owner_client()

    def test_a_non_string_csv_is_a_400(self):
        for bad in (123, {"a": 1}, ["row"], True):
            r = self.client.post("/api/import/plan-activity",
                                 json={"plan": "401K", "csv": bad})
            self.assertEqual(r.status_code, 400, f"csv={bad!r}: {r.text}")

    def test_a_non_numeric_balance_is_a_400(self):
        r = self.client.post("/api/import/plan-activity",
                             json={"plan": "401K", "csv": "Date,Amount\n",
                                   "balance": "abc"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_csv_nothing_parses_out_of_is_a_400(self):
        r = self.client.post("/api/import/plan-activity",
                             json={"plan": "401K", "csv": "not a plan csv"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
