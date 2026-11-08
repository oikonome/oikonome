"""A household can enter its Social Security estimates on either client.

The monthly benefit by claim age is the only source of the projection's
Social Security income. Without a screen that writes it, every household without the demo's seed
is projected on $0 for life and the claim-age what-if changes nothing, while
the demo, which seeds the key, looks right. So this protects three things:
the amounts the editor sends reach the projection, what a person types becomes
the body the settings door accepts (the same cases against both clients'
helper), and each Retirement screen actually carries the editor.
"""

import json
import os
import pathlib
import shutil
import subprocess
import unittest
import uuid

import oikonome
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB_HELPER = _APP / "webapp" / "src" / "ssEstimates.ts"
_WEB_MONEY = _APP / "webapp" / "src" / "moneymath.ts"
_MOB_HELPER = _APP / "mobile" / "src" / "lib" / "ss-estimates.ts"
_MOB_MONEY = _APP / "mobile" / "src" / "lib" / "pure.ts"
_WEB_DRAFT = _APP / "webapp" / "src" / "ssDraft.ts"
_MOB_DRAFT = _APP / "mobile" / "src" / "lib" / "ss-draft.ts"
_WEB_PAGE = _APP / "webapp" / "src" / "pages" / "Retirement.tsx"
_MOB_PAGE = _APP / "mobile" / "src" / "app" / "retire.tsx"
_GUIDE = _APP / "docs" / "guides" / "retirement.md"

_RUNNER = """
import assert from "node:assert/strict";
const { ssEstimatesBody, SS_CLAIM_AGES } = await import(%(helper)s);
const { parseMoneyDraft } = await import(%(money)s);
const body = (d) => ssEstimatesBody(d, parseMoneyDraft);
assert.deepEqual(SS_CLAIM_AGES, [62, 63, 64, 65, 66, 67, 68, 69, 70]);
// blank boxes are ages with no estimate; every box blank clears the schedule
assert.deepEqual(body({ 62: "", 67: " " }), { ok: true, body: {} });
assert.deepEqual(body({}), { ok: true, body: {} });
// a typed amount, the way a person types one
assert.deepEqual(body({ 62: "1,800", 67: "$2,600.50", 70: "0" }),
                 { ok: true, body: { 62: 1800, 67: 2600.5, 70: 0 } });
// never a silent drop: anything that is not an amount stops the save and
// names the box, or the rest of the schedule would be replaced without it
for (const bad of ["lots", "-100"]) {
  const r = body({ 62: "1800", 67: bad });
  assert.equal(r.ok, false, bad);
  assert.match(r.error, /67/);
}
"""

_DRAFT_RUNNER = """
import assert from "node:assert/strict";
const { rebaseSsDraft, ssDraftOf } = await import(%(helper)s);
assert.deepEqual(ssDraftOf(null), {});
assert.deepEqual(ssDraftOf({ 62: 1800, 67: 2600.5 }),
                 { 62: "1800", 67: "2600.5" });
// a box nobody touched follows what was saved elsewhere: the read that landed
// after the editor opened fills it, and the save cannot delete those ages
assert.deepEqual(
  rebaseSsDraft({}, {}, { 62: "1800", 67: "2600" }),
  { 62: "1800", 67: "2600" });
// a box the person typed in stays theirs; the rest still follow
assert.deepEqual(
  rebaseSsDraft({ 67: "3000" }, {}, { 62: "1800", 67: "2600" }),
  { 62: "1800", 67: "3000" });
// an age removed elsewhere leaves its untouched box empty
assert.deepEqual(rebaseSsDraft({ 62: "1800" }, { 62: "1800" }, {}), {});
// a box the person emptied on purpose stays empty
assert.deepEqual(
  rebaseSsDraft({ 62: "" }, { 62: "1800" }, { 62: "1900" }), { 62: "" });
// a box typed in and emptied again counts as untouched
assert.deepEqual(rebaseSsDraft({ 62: "" }, {}, { 62: "1900" }), { 62: "1900" });
"""


class EstimateDraftTests(unittest.TestCase):
    """Typed text to the settings body, the same on both clients."""

    def _run(self, helper: pathlib.Path, money: pathlib.Path):
        self._node(helper, _RUNNER % {"helper": json.dumps(helper.as_uri()),
                                      "money": json.dumps(money.as_uri())})

    def _node(self, helper: pathlib.Path, src: str):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        self.assertTrue(helper.exists(), f"{helper} is missing")
        r = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module",
             "-e", src], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_web_draft_becomes_the_body_the_door_takes(self):
        self._run(_WEB_HELPER, _WEB_MONEY)

    def test_mobile_draft_becomes_the_body_the_door_takes(self):
        self._run(_MOB_HELPER, _MOB_MONEY)

    def test_web_boxes_follow_a_newer_schedule_without_losing_typing(self):
        self._node(_WEB_DRAFT, _DRAFT_RUNNER % {
            "helper": json.dumps(_WEB_DRAFT.as_uri())})

    def test_mobile_boxes_follow_a_newer_schedule_without_losing_typing(self):
        self._node(_MOB_DRAFT, _DRAFT_RUNNER % {
            "helper": json.dumps(_MOB_DRAFT.as_uri())})


class EstimateScreenTests(unittest.TestCase):
    """Source-shaped: the protected thing is that a screen writes the key."""

    def test_web_page_saves_estimates_for_non_viewers(self):
        src = _WEB_PAGE.read_text()
        self.assertIn("ss_estimates: ", src)
        self.assertRegex(src, r"!viewer && settings\.data[^\n]*\n?\s*<SsEstimates")
        # after a save the projection has to re-run with the new benefit
        self.assertIn('invalidateQueries({ queryKey: ["retirement"] })',
                      src[src.index("function SsEstimates"):])

    def test_mobile_screen_saves_estimates_for_non_viewers(self):
        src = _MOB_PAGE.read_text()
        self.assertIn("ss_estimates: ", src)
        self.assertRegex(src, r"!viewer && settings\.data[^\n]*\n?\s*<SsEstimates")
        self.assertIn('invalidateQueries({ queryKey: ["retirement"] })',
                      src[src.index("function SsEstimates"):])

    def test_the_editor_waits_for_a_read_made_since_the_screen_opened(self):
        """A saved or cached copy may predate another device's save, and a
        save replaces the whole schedule. A cache younger than the client's
        stale time is never refetched on mount by default, so the screen has
        to ask for the read it then waits on."""
        for path in (_WEB_PAGE, _MOB_PAGE):
            src = path.read_text()
            self.assertIn('refetchOnMount: "always"', src, path.name)
            self.assertRegex(
                src, r"settings\.dataUpdatedAt >= \w+", path.name)
            self.assertRegex(
                src, r"!viewer && settings\.data && \w+ && \(\s*<SsEstimates",
                path.name)

    def test_the_open_editor_rebases_on_a_newer_schedule(self):
        for path in (_WEB_PAGE, _MOB_PAGE):
            body = path.read_text()
            body = body[body.index("function SsEstimates"):]
            self.assertIn("rebaseSsDraft(", body, path.name)

    def test_no_statement_text_leads_to_the_editor_on_both_clients(self):
        for path in (_WEB_PAGE, _MOB_PAGE):
            src = path.read_text()
            at = src.index("no SSA statement")
            self.assertIn("setAdjust(true)", src[max(0, at - 300):at + 40],
                          path.name)

    def test_guide_describes_the_screen_not_the_raw_api(self):
        guide = " ".join(_GUIDE.read_text().split())
        self.assertNotIn("There is no screen", guide)
        self.assertNotIn("ss_estimates", guide)
        self.assertIn("Social Security $/mo by claim age", guide)


class EstimateReachesProjectionTests(unittest.TestCase):
    """What the editor sends, through the door, into the projection."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)

    def setUp(self):
        self.client.cookies.clear()
        self.client.post("/api/signup", data={
            "email": f"ss-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _inputs(self, **params):
        r = self.client.get("/api/retirement", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["inputs"]

    def test_saved_estimates_drive_the_claim_age_what_if(self):
        self.assertFalse(self._inputs()["has_sched"])
        saved = self.client.post("/api/settings", json={
            "ss_estimates": {"62": 1800, "67": 2600.5, "70": 0}})
        self.assertEqual(saved.status_code, 200, saved.text)
        # the reply is what seeds the editor's cache
        self.assertEqual(saved.json()["ss_estimates"],
                         {"62": 1800.0, "67": 2600.5, "70": 0.0})
        self.assertEqual(self._inputs(ssage=62)["your_ss"], 1800)
        self.assertEqual(self._inputs(ssage=67)["your_ss"], 2600.5)
        self.assertTrue(self._inputs(ssage=67)["has_sched"])

    def test_clearing_every_box_clears_the_schedule(self):
        self.client.post("/api/settings", json={"ss_estimates": {"67": 2600}})
        r = self.client.post("/api/settings", json={"ss_estimates": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["ss_estimates"])
        self.assertFalse(self._inputs()["has_sched"])

    def test_a_save_replaces_ages_the_person_blanked(self):
        self.client.post("/api/settings",
                         json={"ss_estimates": {"62": 1800, "67": 2600}})
        r = self.client.post("/api/settings", json={"ss_estimates": {"67": 2700}})
        self.assertEqual(r.json()["ss_estimates"], {"67": 2700.0})


if __name__ == "__main__":
    unittest.main()
