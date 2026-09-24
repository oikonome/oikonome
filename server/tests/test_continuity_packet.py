"""The continuity packet: the household's money on paper for a survivor.

It prints every account, bill, income and business the household has and
no credential — a packet found in a drawer is a map of the money, not a
key to it. It leaves through the same ticket door as the full export
(owner, fresh elevation, one shot) and, when mailed, the recipient is
held to the household mail rule.
"""

import io
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, continuity, entities
from oikonome.engine.compat import jsonb

from .util import TODAY, _ensure_db, add_bill, make_db, seed_accounts, write_config

PW = "correct-horse-battery"
MEMBER_PW = "another-fine-passphrase"


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in
                     PdfReader(io.BytesIO(data)).pages)


class PacketContentTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, continuity_note="Passwords: the manager.\n"
                                                "Will: top drawer.",
                     continuity_for="Sam")
        self.conn.execute(
            "UPDATE accounts SET mask='4321', owner='Alex' WHERE id='chk'")
        add_bill(self.conn, "Power Co", 120, next_due="2026-08-01",
                 category="utilities")
        add_bill(self.conn, "Payroll", 3000, income=True, interval=2,
                 frequency="WEEKLY", next_due="2026-07-24")
        self.conn.execute(
            "UPDATE bills SET account_id='chk' WHERE payee='Power Co'")
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="single_member_llc",
            state="DE", ein="12-3456789", registered_agent="R. Agent")
        entities.add_member(self.conn, self.ent["id"], member_name="Alex",
                            ownership_pct=100, is_manager=True)
        self.members = [{"email": "alex@example.dev", "role": "owner"},
                        {"email": "sam@example.dev", "role": "viewer"}]

    def tearDown(self):
        self.conn.close()

    def test_the_packet_carries_the_accounts_bills_income_and_businesses(self):
        p = continuity.gather(self.conn, members=self.members,
                              base_url="https://money.example/")
        self.assertEqual(p["prepared_for"], "Sam")
        self.assertIn("Will: top drawer.", p["note"])
        self.assertEqual(p["instance_url"], "https://money.example")
        self.assertEqual([m["email"] for m in p["people"]],
                         ["alex@example.dev", "sam@example.dev"])
        inst, = p["institutions"]
        self.assertEqual(inst["institution"], "Test Bank")
        chk = next(a for a in inst["accounts"] if a["name"] == "Test Checking")
        self.assertEqual((chk["kind"], chk["mask"], chk["owner"], chk["balance"]),
                         ("checking", "4321", "Alex", 5000))
        bill, = p["bills"]
        self.assertEqual((bill["payee"], bill["amount"], bill["cadence"],
                          bill["due_on"], bill["account"]),
                         ("Power Co", 120, "monthly", "08/01/26",
                          "Test Checking ····4321"))
        inc, = p["income"]
        self.assertEqual((inc["payee"], inc["amount"], inc["cadence"]),
                         ("Payroll", 3000, "every 2 weeks"))
        ent, = p["entities"]
        self.assertEqual((ent["name"], ent["structure"], ent["ein_last4"],
                          ent["registered_agent"]),
                         ("Acme LLC", "single-member LLC", "6789", "R. Agent"))
        self.assertEqual(ent["members"][0]["member_name"], "Alex")
        # the card is a debt; its balance is what the survivor pays first
        self.assertEqual([d["name"] for d in p["debts"]], ["Test Card"])
        self.assertEqual(p["networth"]["financial"], 4750.0)

    def test_the_pdf_prints_the_map_and_never_a_credential(self):
        p = continuity.gather(self.conn, members=self.members)
        pdf = continuity.render_pdf(p)
        self.assertTrue(pdf.startswith(b"%PDF"))
        text = _pdf_text(pdf)
        for needle in ("Continuity packet for Sam", "Test Bank",
                       "Test Checking", "4321", "Power Co", "Payroll",
                       "Acme LLC", "R. Agent", "Will: top drawer.",
                       "alex@example.dev", "sam@example.dev",
                       "If you are reading this because I am gone"):
            self.assertIn(needle, text, needle)
        # the bank token, the full EIN and any password hash stay home
        self.assertNotIn("tok-test", text)
        self.assertNotIn("123456789", text.replace("-", ""))
        self.assertNotIn("12-3456789", text)
        self.assertIn("6789", text)

    def test_an_empty_household_still_prints(self):
        conn = make_db()
        try:
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM items")
            pdf = continuity.render_pdf(
                continuity.gather(conn, members=[]))
        finally:
            conn.close()
        text = _pdf_text(pdf)
        self.assertIn("No accounts are connected", text)
        self.assertIn("No recurring bills", text)
        self.assertIn("has not written this section yet", text)

    def test_hidden_and_shadow_accounts_stay_off_the_page(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current,"
            "user_removed_at) VALUES ('old','it1','Closed Savings','depository',"
            "'savings',10,now())")
        p = continuity.gather(self.conn, members=[])
        names = [a["name"] for g in p["institutions"] for a in g["accounts"]]
        self.assertNotIn("Closed Savings", names)

    def test_a_mortgage_prints_the_payment_the_servicer_collects(self):
        """The planner walks a mortgage's principal and interest, but the
        servicer collects that plus escrow. The packet tells a survivor
        what must be paid, so it prints the servicer's whole payment; a
        packet that printed the P&I would have them short every month."""
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,mask,"
            "balance_current) VALUES "
            "('mort','it1','Home Loan','loan','mortgage','0002',300000)")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('mort', %s)",
            (jsonb({"interest_rate": {"percentage": 6.5},
                    "next_monthly_payment": 2496,
                    "maturity_date": TODAY.replace(
                        year=TODAY.year + 30).isoformat()}),))
        p = continuity.gather(self.conn, members=[], today=TODAY)
        mort = next(d for d in p["debts"] if d["name"] == "Home Loan")
        self.assertEqual(mort["payment_reported"], 2496.0)
        self.assertEqual(mort["min_payment"], 1896.21)
        text = " ".join(_pdf_text(continuity.render_pdf(p)).split())
        self.assertIn("$2,496.00 incl. escrow", text)
        self.assertIn("(P&I $1,896.21)", text)
        self.assertIn("escrow included", text)

    def test_the_note_is_bounded_and_the_name_is_one_line(self):
        self.assertEqual(continuity.clean_note("  a  \n\n b \t c "), "a\n\nb c")
        with self.assertRaises(ValueError):
            continuity.clean_note("x" * (continuity.NOTE_MAX + 1))
        self.assertEqual(continuity.clean_for(" Sam\nJones "), "Sam Jones")
        with self.assertRaises(ValueError):
            continuity.clean_for("x" * (continuity.FOR_MAX + 1))


class PacketDoorTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.owner = TestClient(appmod.app)
        cls.email = f"cont-{uuid.uuid4().hex[:8]}@example.dev"
        cls.owner.post("/api/signup", data={"email": cls.email,
                                            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        # a viewer in the household, for the doors that must refuse one
        r = cls.owner.post("/api/invites", json={"label": "partner",
                                                 "role": "viewer",
                                                 "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(appmod.app)
        cls.viewer_email = f"view-{uuid.uuid4().hex[:8]}@example.dev"
        got = cls.viewer.post("/api/invite/claim", json={
            "token": token, "email": cls.viewer_email, "password": MEMBER_PW})
        assert got.status_code == 200, got.text

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ticket(self, client=None, pw=PW):
        return (client or self.owner).post(
            "/api/export/token", json={"kind": "continuity", "password": pw})

    def test_a_bare_get_is_refused_and_a_ticket_opens_it_once(self):
        self.assertEqual(self.owner.get("/export/continuity").status_code, 403)
        r = self._ticket()
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["url"]
        self.assertTrue(url.startswith("/export/continuity?t="))
        got = self.owner.get(url)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.headers["content-type"], "application/pdf")
        self.assertIn("oikonome-continuity-", got.headers["content-disposition"])
        self.assertTrue(got.content.startswith(b"%PDF"))
        self.assertIn("Test Checking", _pdf_text(got.content))
        self.assertEqual(self.owner.get(url).status_code, 403)
        # the download is an act the household log remembers
        acts = self.owner.get("/api/activity").json()["rows"]
        self.assertTrue(any("downloaded the continuity packet" in a["summary"]
                            for a in acts), acts[:3])

    def test_a_zip_ticket_does_not_open_the_packet(self):
        r = self.owner.post("/api/export/token",
                            json={"kind": "zip", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.owner.get(f"/export/continuity?t={r.json()['token']}"
                           ).status_code, 403)

    def test_a_viewer_cannot_mint_or_download_or_write(self):
        self.assertEqual(self._ticket(self.viewer, MEMBER_PW).status_code, 403)
        self.assertEqual(self.viewer.get("/export/continuity?t=x").status_code,
                         403)
        self.assertEqual(self.viewer.put("/api/continuity",
                                         json={"note": "x"}).status_code, 403)
        self.assertEqual(self.viewer.post(
            "/api/continuity/email",
            json={"to": self.email, "password": MEMBER_PW}).status_code, 403)
        # reading the fields is household truth, like every other page
        self.assertEqual(self.viewer.get("/api/continuity").status_code, 200)

    def test_the_note_and_the_name_round_trip(self):
        r = self.owner.put("/api/continuity", json={
            "note": "Passwords live in the manager.\nWill: lawyer.",
            "prepared_for": "Sam"})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.owner.get("/api/continuity").json()
        self.assertEqual(got["prepared_for"], "Sam")
        self.assertEqual(got["note"], "Passwords live in the manager.\nWill: lawyer.")
        self.assertEqual(got["note_max"], continuity.NOTE_MAX)
        self.assertIn(self.email, got["members"])
        self.assertIn(self.viewer_email, got["members"])
        self.assertIs(got["members_only"], False)
        # a save of one field leaves the other alone; empty clears
        self.owner.put("/api/continuity", json={"prepared_for": ""})
        got = self.owner.get("/api/continuity").json()
        self.assertEqual(got["prepared_for"], "")
        self.assertIn("Will: lawyer.", got["note"])
        self.assertEqual(self.owner.put("/api/continuity", json={
            "note": "x" * (continuity.NOTE_MAX + 1)}).status_code, 400)
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        self.assertNotIn("continuity_for", cfg)

    def test_the_emailed_copy_carries_the_pdf_to_one_checked_address(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": "smtp.test"}), \
             mock.patch("oikonome.web.report.send") as send:
            r = self.owner.post("/api/continuity/email",
                                json={"to": "Sam@Example.dev", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["to"], "Sam@Example.dev")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[3], ["Sam@Example.dev"])
        self.assertIs(send.call_args.kwargs["bcc"], False)
        (name, data, mime), = send.call_args.kwargs["attachments"]
        self.assertTrue(name.startswith("oikonome-continuity-"))
        self.assertEqual(mime, "application/pdf")
        self.assertIn("Test Checking", _pdf_text(data))
        acts = self.owner.get("/api/activity").json()["rows"]
        self.assertTrue(any("emailed the continuity packet to sam@example.dev"
                            in a["summary"].lower() for a in acts), acts[:3])

    def test_the_email_door_wants_the_password_and_a_real_address(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": "smtp.test"}), \
             mock.patch("oikonome.web.report.send") as send:
            self.assertEqual(self.owner.post(
                "/api/continuity/email",
                json={"to": "sam@example.dev", "password": "wrong"}
            ).status_code, 401)
            self.assertEqual(self.owner.post(
                "/api/continuity/email",
                json={"to": "sam@example.dev"}).status_code, 403)
            self.assertEqual(self.owner.post(
                "/api/continuity/email",
                json={"to": "not an address", "password": PW}
            ).status_code, 400)
        send.assert_not_called()

    def test_without_a_relay_the_door_says_to_download_instead(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": ""}), \
             mock.patch("oikonome.web.report.send") as send:
            r = self.owner.post("/api/continuity/email",
                                json={"to": "sam@example.dev", "password": PW})
        self.assertEqual(r.status_code, 400)
        self.assertIn("download", r.json()["detail"])
        send.assert_not_called()

    def test_hosted_mails_only_a_household_member(self):
        # the rule itself, not the hosted flag: hosted also demands a
        # second factor on every write, which is another test's subject
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": "smtp.test"}), \
             mock.patch("oikonome.web.mailguard.members_only",
                        return_value=True), \
             mock.patch("oikonome.web.report.send") as send:
            self.assertIs(self.owner.get("/api/continuity").json()["members_only"],
                          True)
            r = self.owner.post("/api/continuity/email",
                                json={"to": "stranger@example.dev",
                                      "password": PW})
            self.assertEqual(r.status_code, 400, r.text)
            send.assert_not_called()
            r = self.owner.post("/api/continuity/email",
                                json={"to": self.viewer_email, "password": PW})
            self.assertEqual(r.status_code, 200, r.text)
            send.assert_called_once()

    def test_a_refused_relay_is_a_502_not_a_silent_ok(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": "smtp.test"}), \
             mock.patch("oikonome.web.report.send", side_effect=OSError("boom")):
            r = self.owner.post("/api/continuity/email",
                                json={"to": "sam@example.dev", "password": PW})
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
