"""Two cards whose numbers share the last four read the same in a picker
("Visa …1111" twice), and the account the person picks is remembered, so
a wrong pick sticks. Every door that asks the person to choose between
them must therefore describe each choice by what tells the cards apart:
how many rows it holds, the dates they span, and its latest balance.

The single-file import door and the YNAB card refuse a typed new-account
name that leads to both cards; their refusals carry the described choices.
The folder import's plan carries the same description for its picker.
"""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base
from oikonome.web import bulk_import, pages

from .bulk_job_util import submit_and_run
from .util import _ensure_db, make_db, write_config

NUMBER = "4111111111111111"            # invented card numbers that share
OTHER = "5500000000001111"             # their last four
OLD_A = (b"Date,Description,Amount\n"
         b"2021-05-01,HARBOR BAKERY,-8.00\n"
         b"2021-05-02,NORTHWIND FUEL,-31.00\n"
         b"2021-05-03,CONTOSO STREAM,-12.00\n")
OLD_B = (b"Date,Description,Amount\n"
         b"2022-02-10,FABRIKAM BOOKS,-4.00\n")
NEW = (b"Date,Description,Amount\n"
       b"2023-03-01,SPRINGFIELD KIOSK,-2.50\n")
YNAB = ('"Account","Flag","Date","Payee","Category Group/Category",'
        '"Category Group","Category","Memo","Outflow","Inflow","Cleared"\n'
        f'"Visa {NUMBER}","","2026-04-02","Harbor Bakery","","","","",'
        '"$8.00","$0.00","Cleared"\n'
        '"Everyday Checking","","2026-04-03","Northwind Fuel","","","","",'
        '"$31.00","$0.00","Cleared"\n')
# a YNAB account whose own name holds no number: the person types the
# numbered name for it, and that typed name is what the next file spells
YNAB_PLAIN = YNAB.replace(f'"Visa {NUMBER}"', '"Travel Card"')
UNREADABLE = b"Foo,Bar,Baz\nx,y,z\n"     # the detector cannot place a column
WANT = [  # (rows, first, last, balance in cents), by account
    (3, "2021-05-01", "2021-05-03", -5100),
    (1, "2022-02-10", "2022-02-10", 12345),
]


def _two_cards(conn) -> list[str]:
    """Two cards made before names were masked, each with its own
    history and balance, then moved onto ids keeping only the last four:
    one typed number now leads to both. Their ids, in WANT's order."""
    conn.execute("INSERT INTO items (id, aggregator, institution_name)"
                 " VALUES ('manual','manual','Manual')"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")
    for num, csv, (*_, cents) in ((NUMBER, OLD_A, WANT[0]),
                                  (OTHER, OLD_B, WANT[1])):
        aid = f"manual:visa-{num}"
        conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype,"
            " balance_current) VALUES (%s,'manual',%s,'credit',"
            "'credit card',%s)", (aid, f"Visa {num}", cents / 100))
        plan = bulk_import.analyze(conn, [("card.csv", csv)])
        res = submit_and_run(conn, plan["token"], [
            dict(plan["files"][0], account_id=aid, new_account_name="",
                 amount_sign="bank")])[0]
        assert res["ok"], res
    moved = base.rekey_numbered_accounts(conn)
    return [moved[f"manual:visa-{NUMBER}"], moved[f"manual:visa-{OTHER}"]]


def _facts(choices) -> dict:
    return {c["id"]: (c["rows"], c["first_date"], c["last_date"],
                      c["balance_cents"]) for c in choices}


class ChoicesDescribeEachCard(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.ids = _two_cards(self.conn)

    def _same_label(self, choices):
        self.assertEqual(len(choices), 2)
        self.assertEqual(len({(c["name"], c["mask"]) for c in choices}), 1,
                         "the two cards are expected to read the same")

    def test_the_ynab_refusal_describes_each_card(self):
        result, need = pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "register.csv", YNAB.encode(),
            role="owner", ynab_choice=True)
        self.assertIsNotNone(need, result)
        names = [a["name"] for a in need["accounts"]]
        visa = next(n for n in names if n.lower().startswith("visa"))
        answers = {n: "chk" for n in names}
        answers[visa] = {"new": f"Visa {NUMBER}"}
        out = pages.run_ynab_import(self.conn, need["token"], answers, None)
        self.assertIn("choose one", out["error"])
        self.assertEqual(out["choice_for"], visa)
        self._same_label(out["choices"])
        self.assertEqual(_facts(out["choices"]), dict(zip(self.ids, WANT)))

    def test_a_ynab_pick_is_remembered_under_the_name_the_person_typed(self):
        result, need = pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "register.csv", YNAB_PLAIN.encode(),
            role="owner", ynab_choice=True)
        self.assertIsNotNone(need, result)
        typed = f"Visa {NUMBER}"
        answers = {n["name"]: "chk" for n in need["accounts"]}
        answers["Travel Card"] = {"new": typed}
        out = pages.run_ynab_import(self.conn, need["token"], answers, None)
        self.assertEqual(out.get("choice_for"), "Travel Card", out)
        answers["Travel Card"] = {"id": self.ids[1], "typed": typed}
        out = pages.run_ynab_import(self.conn, need["token"], answers, None)
        self.assertNotIn("error", out, out)
        self.assertEqual(bulk_import.account_choices(self.conn, typed), [],
                         "the pick should answer the typed name next time")
        plan = bulk_import.analyze(self.conn, [(f"{typed}.csv", NEW)])
        self.assertEqual(plan["files"][0]["account_id"], self.ids[1])

    def test_the_folder_plan_names_its_choices_as_before(self):
        plan = bulk_import.analyze(
            self.conn, [(f"Visa {NUMBER}.csv", NEW)])
        entry = plan["files"][0]
        self.assertIsNone(entry["account_id"])
        self.assertIn("choose one", entry["note"])
        self._same_label(entry["choices"])
        self.assertEqual(sorted(c["id"] for c in entry["choices"]),
                         sorted(self.ids))
        # the plan's picker tells the two apart by the same facts
        self.assertEqual(_facts(entry["choices"]), dict(zip(self.ids, WANT)))


def _signed_in_with_two_cards():
    """A signed-in client whose own household holds the two cards."""
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    client.post("/api/signup", data={
        "email": f"cc-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        write_config(conn)
        ids = _two_cards(conn)
    finally:
        conn.close()
    return client, ids


class TheSingleFileDoorDescribesEachCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.ids = _signed_in_with_two_cards()

    def test_the_refusal_carries_each_card_described(self):
        r = self.client.post(
            "/api/import", files={"file": ("new.csv", NEW, "text/csv")},
            data={"new_account_name": f"Visa {NUMBER}",
                  "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        self.assertIn("choose one", res["error"])
        choices = res["choices"]
        self.assertEqual(len(choices), 2)
        self.assertEqual(len({(c["name"], c["mask"]) for c in choices}), 1)
        self.assertEqual(_facts(choices), dict(zip(self.ids, WANT)))

    def test_a_push_token_is_refused_without_the_cards_described(self):
        """A push-scoped token may import but never read: the refusal
        names the count, never the cards' balances, rows or dates."""
        minted = self.client.post("/api/tokens", json={
            "name": "collector", "password": "correct-horse-battery"}).json()
        r = self.client.post(
            "/api/import", files={"file": ("new.csv", NEW, "text/csv")},
            data={"new_account_name": f"Visa {NUMBER}",
                  "amount_sign": "bank"},
            headers={"Authorization": f"Bearer {minted['token']}"})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        self.assertIn("choose one", res["error"])
        self.assertNotIn("choices", res)


    def test_the_ynab_door_takes_a_pick_with_the_name_typed(self):
        # runs last in this class (it settles the household's choice)
        r = self.client.post(
            "/api/import",
            files={"file": ("register.csv", YNAB_PLAIN.encode(),
                            "text/csv")},
            data={"amount_sign": "bank", "ynab_choice": "1",
                  "account_id": self.ids[0]})
        need = r.json()["mapping_needed"]
        self.assertEqual((need or {}).get("kind"), "ynab_accounts", r.text)
        answers = {a["name"]: "skip" for a in need["accounts"]}
        answers["Travel Card"] = {"id": self.ids[1],
                                  "typed": f"Visa {NUMBER}"}
        r = self.client.post("/api/import/ynab-accounts", json={
            "token": need["token"], "accounts": answers})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["result"].get("error"), r.text)


class TheSingleFileDoorRemembersOnlyWhatLanded(unittest.TestCase):
    """Its own household: a pick it remembers settles the choice there."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.ids = _signed_in_with_two_cards()

    def _tid_conn(self):
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        self.addCleanup(conn.close)
        return conn

    def test_a_pick_is_remembered_only_once_its_file_lands(self):
        typed = f"Visa {NUMBER}"
        r = self.client.post(
            "/api/import",
            files={"file": ("odd.csv", UNREADABLE, "text/csv")},
            data={"new_account_name": typed, "amount_sign": "bank",
                  "account_id": self.ids[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["mapping_needed"], r.text)
        conn = self._tid_conn()
        self.assertEqual(len(bulk_import.account_choices(conn, typed)), 2,
                         "a file that has not landed must not settle "
                         "the choice")
        r = self.client.post(
            "/api/import", files={"file": ("new.csv", NEW, "text/csv")},
            data={"new_account_name": typed, "amount_sign": "bank",
                  "account_id": self.ids[0]})
        self.assertFalse(r.json()["result"].get("error"), r.text)
        self.assertEqual(
            bulk_import.account_choices(self._tid_conn(), typed), [])


ODD = (b"When,What,HowMuch\n"              # columns no detector places
       b"2023-03-01,SPRINGFIELD KIOSK,-2.50\n")


class _OwnHousehold(unittest.TestCase):
    """Its own household per class: a remembered pick settles it."""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.ids = _signed_in_with_two_cards()

    def _choices(self, typed):
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        self.addCleanup(conn.close)
        return bulk_import.account_choices(conn, typed)


class APickWhoseColumnsWereMappedIsRemembered(_OwnHousehold):
    def test_the_pick_is_remembered_once_the_mapped_file_lands(self):
        typed = f"Visa {NUMBER}"
        r = self.client.post(
            "/api/import", files={"file": ("odd.csv", ODD, "text/csv")},
            data={"new_account_name": typed, "amount_sign": "bank",
                  "account_id": self.ids[0]})
        need = r.json()["mapping_needed"]
        self.assertIsNotNone(need, r.text)
        self.assertEqual(len(self._choices(typed)), 2,
                         "a file still to be mapped settles nothing")
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When",
            "name_col": "What", "amount_col": "HowMuch"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["result"].get("error"), r.text)
        self.assertEqual(self._choices(typed), [],
                         "the mapped file landed: the pick answers the "
                         "typed name next time")


class APickThatBecameAYnabRegisterIsRemembered(_OwnHousehold):
    def test_the_pick_is_remembered_once_the_register_lands(self):
        typed = f"Visa {NUMBER}"
        r = self.client.post(
            "/api/import",
            files={"file": ("register.csv", YNAB_PLAIN.encode(),
                            "text/csv")},
            data={"new_account_name": typed, "amount_sign": "bank",
                  "ynab_choice": "1", "account_id": self.ids[0]})
        need = r.json()["mapping_needed"]
        self.assertEqual((need or {}).get("kind"), "ynab_accounts", r.text)
        self.assertEqual(len(self._choices(typed)), 2)
        answers = {a["name"]: "skip" for a in need["accounts"]}
        answers["Travel Card"] = self.ids[0]
        r = self.client.post("/api/import/ynab-accounts", json={
            "token": need["token"], "accounts": answers})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["result"].get("error"), r.text)
        self.assertEqual(self._choices(typed), [])

    def test_a_register_that_left_the_pick_empty_settles_nothing(self):
        typed = f"Visa {OTHER}"
        r = self.client.post(
            "/api/import",
            files={"file": ("register.csv", YNAB_PLAIN.encode(),
                            "text/csv")},
            data={"new_account_name": typed, "amount_sign": "bank",
                  "ynab_choice": "1", "account_id": self.ids[1]})
        need = r.json()["mapping_needed"]
        self.assertEqual((need or {}).get("kind"), "ynab_accounts", r.text)
        answers = {a["name"]: "skip" for a in need["accounts"]}
        answers["Travel Card"] = self.ids[0]
        r = self.client.post("/api/import/ynab-accounts", json={
            "token": need["token"], "accounts": answers})
        self.assertFalse(r.json()["result"].get("error"), r.text)
        self.assertEqual(len(self._choices(typed)), 2,
                         "nothing landed on the pick, so it settles "
                         "nothing")


class TheFolderImportRemembersOnlyWhatLanded(unittest.TestCase):
    """A folder job remembers the pick for a file named for both cards
    only once that file has imported: one that needs its columns mapped
    settles nothing, and later files of that number are asked again."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.ids = _two_cards(self.conn)
        self.name = f"Visa {NUMBER}"

    def _run(self, data):
        plan = bulk_import.analyze(self.conn, [(f"{self.name}.csv", data)])
        return submit_and_run(self.conn, plan["token"], [
            dict(plan["files"][0], account_id=self.ids[0],
                 new_account_name="", amount_sign="bank")])[0]

    def test_a_file_that_did_not_land_settles_nothing(self):
        res = self._run(UNREADABLE)
        self.assertFalse(res["ok"], res)
        self.assertEqual(
            len(bulk_import.account_choices(self.conn, self.name)), 2,
            "a file that needs mapping must not settle the choice")
        plan = bulk_import.analyze(self.conn, [(f"{self.name}.csv", NEW)])
        self.assertIsNone(plan["files"][0]["account_id"])

    def test_a_file_that_landed_remembers_the_pick(self):
        res = self._run(NEW)
        self.assertTrue(res["ok"], res)
        self.assertEqual(
            bulk_import.account_choices(self.conn, self.name), [])
        plan = bulk_import.analyze(self.conn, [(f"{self.name}.csv", NEW)])
        self.assertEqual(plan["files"][0]["account_id"], self.ids[0])


if __name__ == "__main__":
    unittest.main()
