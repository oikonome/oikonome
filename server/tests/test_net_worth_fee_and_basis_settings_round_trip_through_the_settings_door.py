"""The Net Worth page's advisory-fee estimate and money-in anchors are
read from three settings keys, and the Settings page is the only door a
hosted household has to them — so they must come back from GET
/api/settings and be accepted, normalized and validated by the POST.

A key the read leaves out is a form that always opens empty; a key the
save drops is a form that silently does nothing. And the shapes matter to
the engine: a blank institution fragment matches EVERY institution (the
fee would be charged on the whole portfolio), a percentage typed as 25
instead of 0.0025 estimates a fee larger than the balance, and a mask that
is not an account's last letters or digits can never match an account.
Each of those is a 400, never a stored value.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


KEYS = ("advisory_fees", "advisory_fee_exempt_masks",
        "investment_basis_override")


class FeeAndBasisSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"fees-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def setUp(self):
        r = self.client.post("/api/settings", json={k: None for k in KEYS})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_read_publishes_all_three_keys(self):
        view = self.client.get("/api/settings").json()
        for k in KEYS:
            self.assertIn(k, view)
            self.assertIsNone(view[k])

    def test_a_save_round_trips_and_normalizes(self):
        r = self.client.post("/api/settings", json={
            "advisory_fees": {"  Robo Advisor ": "0.0025", "other": 0.004},
            "advisory_fee_exempt_masks": "1111, 2222 1111",
            "investment_basis_override": {"3333": "1,250.50", "4444": 0},
        })
        self.assertEqual(r.status_code, 200, r.text)
        want = {
            "advisory_fees": {"robo advisor": 0.0025, "other": 0.004},
            "advisory_fee_exempt_masks": ["1111", "2222"],
            "investment_basis_override": {"3333": 1250.5, "4444": 0.0},
        }
        # the save reply, the next read and the stored document agree
        for view in (r.json(), self.client.get("/api/settings").json(),
                     self._stored()):
            for k, v in want.items():
                self.assertEqual(view[k], v, k)

    def test_an_unrelated_save_leaves_them_alone(self):
        self.client.post("/api/settings", json={
            "advisory_fees": {"robo": 0.0025},
            "advisory_fee_exempt_masks": ["1111"],
            "investment_basis_override": {"3333": 400}})
        r = self.client.post("/api/settings", json={"food_monthly": 900})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored()
        self.assertEqual(cfg["advisory_fees"], {"robo": 0.0025})
        self.assertEqual(cfg["advisory_fee_exempt_masks"], ["1111"])
        self.assertEqual(cfg["investment_basis_override"], {"3333": 400.0})

    def test_empty_values_clear_the_keys(self):
        self.client.post("/api/settings", json={
            "advisory_fees": {"robo": 0.0025},
            "advisory_fee_exempt_masks": ["1111"],
            "investment_basis_override": {"3333": 400}})
        r = self.client.post("/api/settings", json={
            "advisory_fees": {}, "advisory_fee_exempt_masks": [],
            "investment_basis_override": {}})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored()
        for k in KEYS:
            self.assertNotIn(k, cfg)

    def test_bad_shapes_are_refused_and_store_nothing(self):
        bad = [
            {"advisory_fees": ["robo", 0.0025]},
            {"advisory_fees": {"": 0.0025}},
            {"advisory_fees": {"   ": 0.0025}},
            {"advisory_fees": {"robo": 25}},          # a percentage
            {"advisory_fees": {"robo": -0.01}},
            {"advisory_fees": {"robo": "lots"}},
            {"advisory_fees": {"robo": ""}},
            {"advisory_fees": {"robo": "nan"}},
            {"advisory_fee_exempt_masks": {"1111": True}},
            {"advisory_fee_exempt_masks": ["12-4"]},
            {"advisory_fee_exempt_masks": ["1"]},          # too short
            {"advisory_fee_exempt_masks": ["123456789"]},  # too long
            {"advisory_fee_exempt_masks": ["12\u00e94"]},  # not ASCII
            {"advisory_fee_exempt_masks": [True]},
            {"advisory_fee_exempt_masks": [["1111"]]},
            {"investment_basis_override": [["3333", 400]]},
            {"investment_basis_override": {"33-33": 400}},
            {"investment_basis_override": {"": 400}},
            {"investment_basis_override": {"3333": -1}},
            {"investment_basis_override": {"3333": "abc"}},
            {"investment_basis_override": {"3333": None}},
            {"investment_basis_override": {"3333": "inf"}},
        ]
        for body in bad:
            # a valid field beside the bad one must not persist either:
            # one save is one transaction
            r = self.client.post("/api/settings",
                                 json={**body, "food_monthly": 777})
            self.assertEqual(r.status_code, 400, (body, r.text))
            self.assertIsInstance(r.json().get("detail"), str, body)
        cfg = self._stored()
        for k in KEYS:
            self.assertNotIn(k, cfg)
        self.assertNotEqual(cfg.get("food_monthly"), 777)


    def test_settings_refuse_oversized_mask_lists_before_scanning_them(self):
        # a list near the body limit must be turned away at once: the
        # check runs with the household's config row locked, so a scan
        # that grows with the square of the list holds every other
        # settings write (and a worker) for tens of seconds
        import time
        # both forms the door takes, each just under the body limit
        for masks in (",".join(str(i) for i in range(150_000)),
                      list(range(120_000))):
            t0 = time.monotonic()
            r = self.client.post("/api/settings",
                                 json={"advisory_fee_exempt_masks": masks})
            took = time.monotonic() - t0
            self.assertEqual(r.status_code, 400, r.text[:200])
            self.assertIn("too many", r.json()["detail"])
            self.assertLess(took, 1.0)
        self.assertNotIn("advisory_fee_exempt_masks", self._stored())

    def test_mask_list_keeps_typed_order_and_tolerates_stray_separators(self):
        # the cap counts masks, not the empty pieces a trailing comma
        # leaves, so a full list typed with a trailing comma still saves
        full = ", ".join(str(1000 + i) for i in range(50)) + ", "
        r = self.client.post("/api/settings",
                             json={"advisory_fee_exempt_masks": full})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self._stored()["advisory_fee_exempt_masks"]), 50)
        r = self.client.post("/api/settings", json={
            "advisory_fee_exempt_masks": ["2222", "1111", "2222", ""]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored()["advisory_fee_exempt_masks"],
                         ["2222", "1111"])

    def test_a_save_naming_one_key_leaves_the_other_two_alone(self):
        # the Settings cards post only the sections someone edited, so a
        # save from a tab that never loaded another device's fee does not
        # replace it; the key it does name is validated as before
        self.client.post("/api/settings", json={
            "advisory_fees": {"robo": 0.0025},
            "advisory_fee_exempt_masks": ["1111"],
            "investment_basis_override": {"3333": 400}})
        r = self.client.post("/api/settings", json={
            "investment_basis_override": {"4444": 10}})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored()
        self.assertEqual(cfg["advisory_fees"], {"robo": 0.0025})
        self.assertEqual(cfg["advisory_fee_exempt_masks"], ["1111"])
        self.assertEqual(cfg["investment_basis_override"], {"4444": 10.0})
        r = self.client.post("/api/settings", json={
            "advisory_fees": {"robo": 25}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._stored()["advisory_fees"], {"robo": 0.0025})

    def test_masks_accept_letters_the_bank_uses(self):
        # some banks put letters in the mask Plaid reports; stored in the
        # feeds' upper case, so the engine's exact comparison matches
        r = self.client.post("/api/settings", json={
            "advisory_fee_exempt_masks": ["ab12", "AB12", "x9"],
            "investment_basis_override": {"z1y2": 400}})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored()
        self.assertEqual(cfg["advisory_fee_exempt_masks"], ["AB12", "X9"])
        self.assertEqual(cfg["investment_basis_override"], {"Z1Y2": 400.0})

    def test_a_section_save_with_a_stale_base_is_refused(self):
        # tab A loads the overrides, another device adds a row, then tab A
        # edits a different row of the same section: posting A's whole
        # copy would drop the other device's row, so it is refused
        self.client.post("/api/settings", json={
            "investment_basis_override": {"3333": 400},
            "advisory_fees": {"robo": 0.0025}})
        seen = self.client.get("/api/settings").json()
        r = self.client.post("/api/settings", json={
            "investment_basis_override": {"3333": 400, "5555": 7},
            "investment_basis_override_base":
                seen["investment_basis_override"]})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/settings", json={
            "investment_basis_override": {"3333": 450},
            "investment_basis_override_base":
                seen["investment_basis_override"],
            "food_monthly": 778})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("another device", r.json()["detail"])
        cfg = self._stored()
        self.assertEqual(cfg["investment_basis_override"],
                         {"3333": 400.0, "5555": 7.0})
        self.assertNotEqual(cfg.get("food_monthly"), 778)
        # a base that still matches saves, an empty base matches an empty
        # section, and a save with no base replaces as it always did
        r = self.client.post("/api/settings", json={
            "advisory_fees": {"robo": 0.003},
            "advisory_fees_base": seen["advisory_fees"],
            "advisory_fee_exempt_masks": ["1111"],
            "advisory_fee_exempt_masks_base": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored()["advisory_fees"], {"robo": 0.003})
        r = self.client.post("/api/settings", json={
            "investment_basis_override": {"3333": 450}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored()["investment_basis_override"],
                         {"3333": 450.0})
        # a base that is not a section is a malformed request, not a race
        r = self.client.post("/api/settings", json={
            "advisory_fees": {}, "advisory_fees_base": "robo"})
        self.assertEqual(r.status_code, 400, r.text)


class FeeAndBasisSettingsAreTheOwners(unittest.TestCase):
    """Every other knob in the Connections section is the owner's, and both
    clients show this card to the owner only, so the server agrees: a
    member's direct save of these keys is refused, not silently kept."""

    def test_a_member_cannot_save_the_investment_settings(self):
        owner = _make_client()
        pw = "correct-horse-battery"
        owner.post("/api/signup", data={
            "email": f"feesown-{uuid.uuid4().hex[:8]}@example.dev",
            "password": pw})
        tid = owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            write_config(conn)
        finally:
            conn.close()
        owner.post("/api/settings", json={"advisory_fees": {"robo": 0.0025}})
        inv = owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": pw})
        self.assertEqual(inv.status_code, 200, inv.text)
        from oikonome.web.app import app
        member = TestClient(app)
        got = member.post("/api/invite/claim", json={
            "token": inv.json()["url"].rsplit("token=", 1)[1],
            "email": f"feesmem-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(got.status_code, 200, got.text)
        for body in ({"advisory_fees": {"robo": 0.5}},
                     {"advisory_fee_exempt_masks": ["1111"]},
                     {"investment_basis_override": {"3333": 1}},
                     {"advisory_fees_base": {}}):
            r = member.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 403, (body, r.text))
            self.assertEqual(r.json()["detail"], "owner only")
        # the household money stays the member's to edit
        r = member.post("/api/settings", json={"food_monthly": 812})
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        self.assertEqual(cfg["advisory_fees"], {"robo": 0.0025})
        self.assertNotIn("advisory_fee_exempt_masks", cfg)


if __name__ == "__main__":
    unittest.main()
