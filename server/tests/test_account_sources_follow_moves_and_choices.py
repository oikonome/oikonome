"""Which account a source fills (account_sources) follows the account when
its id moves, and is written whenever a person picks the account.

A re-key that moves a hand-made account off a full number, or a merge of
one account into another, re-points every column naming the account; a
source row left on the old id is found only through the move's record,
and stops leading anywhere once that record cannot say which account is
meant. And a door that takes an account the person picked for a name two
cards share must record the pick, or the same question comes back on the
next file of that card.

Every name and number here is invented."""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import account_sources as srcs
from oikonome.sync import adopt, base
from oikonome.web import bulk_import, pages

from .util import _ensure_db, make_db, write_config

NUMBER = "4111111111111111"            # a test card number
OTHER = "5500000000001111"             # another, with the same last four
CSV = b"Date,Description,Amount\n2026-07-01,HARBOR BAKERY,-4.50\n"
YNAB = ('"Account","Flag","Date","Payee","Category Group/Category",'
        '"Category Group","Category","Memo","Outflow","Inflow","Cleared"\n'
        f'"Visa {NUMBER}","","2026-04-02","Harbor Bakery","","","","",'
        '"$8.00","$0.00","Cleared"\n'
        '"Everyday Checking","","2026-04-03","Northwind Fuel","","","","",'
        '"$31.00","$0.00","Cleared"\n')


def _manual_item(conn):
    conn.execute("INSERT INTO items (id, aggregator, institution_name)"
                 " VALUES ('manual','manual','Manual')"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")


def _rows(conn, aid):
    return sorted((r["door"], r["source_hmac"][:2]) for r in conn.execute(
        "SELECT door, source_hmac FROM account_sources"
        " WHERE account_id = %s", (aid,)).fetchall())


class SourceRowsFollowAMove(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        _manual_item(self.conn)

    def _account(self, aid, name):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s,'manual',%s,'credit','credit card')", (aid, name))
        srcs.remember(self.conn, srcs.DOOR_MANUAL, name, aid)
        srcs.remember_name(self.conn, aid, name)
        return aid

    def test_a_rekey_moves_every_row_and_the_lookup_reads_the_new_id(self):
        old = self._account(f"manual:visa-{NUMBER}", f"Visa {NUMBER}")
        held = _rows(self.conn, old)
        self.assertEqual([k for _, k in held], ["c:", "d:", "s:"])
        new = base.rekey_numbered_accounts(self.conn)[old]
        self.assertEqual(_rows(self.conn, old), [])
        self.assertEqual(_rows(self.conn, new), held)
        # read directly, not through the move's record
        row = self.conn.execute(
            "SELECT account_id FROM account_sources WHERE door = %s"
            "   AND source_hmac = %s",
            (srcs.DOOR_MANUAL, srcs.keyed(base.txn_id_secret(self.conn),
                                          srcs.DOOR_MANUAL,
                                          f"Visa {NUMBER}"))).fetchone()
        self.assertEqual(row["account_id"], new)
        self.assertEqual(srcs.lookup(self.conn, srcs.DOOR_MANUAL,
                                     f"Visa {NUMBER}"), new)

    def test_a_merge_onto_an_account_holding_the_same_rows_keeps_one(self):
        """Two accounts answering to the same name and card merge into
        one: the rows they share are not a duplicate key, the rest move."""
        a = self._account("manual:aaaaaaaaaaaaaaaa", f"Visa {NUMBER}")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('manual:bbbbbbbbbbbbbbbb','manual',%s,'credit',"
            "'credit card')", (f"Visa {NUMBER}",))
        b = "manual:bbbbbbbbbbbbbbbb"
        srcs.remember_name(self.conn, b, f"Visa {NUMBER}")
        # the card's d-row on both, as two doors can leave it
        d = srcs.card_key(base.txn_id_secret(self.conn), srcs.DOOR_MANUAL,
                          f"Visa {NUMBER}")
        self.conn.execute("INSERT INTO account_sources (door, source_hmac,"
                          " account_id) VALUES (%s, %s, %s)",
                          (srcs.DOOR_MANUAL, d, b))
        with self.conn.transaction():
            adopt._move_account_children(self.conn, a, b)
        self.assertEqual(_rows(self.conn, a), [])
        self.assertEqual([k for _, k in _rows(self.conn, b)],
                         ["c:", "d:", "s:"])
        self.assertEqual(srcs.lookup(self.conn, srcs.DOOR_MANUAL,
                                     f"Visa {NUMBER}"), b)


class _TwoMovedCards:
    """Two cards an earlier release named by their full numbers, moved
    onto ids keeping only the last four they share."""

    def _two_cards(self, conn, stem):
        _manual_item(conn)
        for n in (NUMBER, OTHER):
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype)"
                " VALUES (%s,'manual',%s,'credit','credit card')",
                (f"manual:{stem}-{n}", f"{stem.title()} {n}"))
        return sorted(base.rekey_numbered_accounts(conn).values())


class TheSingleFileDoorRemembersAPick(unittest.TestCase, _TwoMovedCards):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"pick-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()

    def _post(self, filename, csv=CSV, **data):
        r = self.client.post(
            "/api/import", data={"account_id": "", "amount_sign": "bank",
                                 **data},
            files={"file": (filename, csv, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"]

    def test_a_card_picked_once_is_not_asked_again(self):
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        moved = self._two_cards(conn, "picked")
        typed = f"Picked {NUMBER}"
        self.assertIn("choose one", self._post("a.csv",
                                               new_account_name=typed)
                      ["error"])
        out = self._post(f"{typed}.csv", account_id=moved[1],
                         new_account_name=typed)
        self.assertNotIn("error", out, out)
        # asked once: the typed name and the file's own name both know it
        self.assertEqual(bulk_import.account_choices(conn, typed), [])
        out = self._post("b.csv", new_account_name=typed, csv=CSV.replace(
            b"HARBOR BAKERY", b"NORTHWIND FUEL"))
        self.assertNotIn("error", out, out)
        self.assertEqual(conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = %s",
            (moved[1],)).fetchone()["n"], 2)


class TheYnabDoorRemembersAPick(unittest.TestCase, _TwoMovedCards):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _park(self):
        result, need = pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "register.csv", YNAB.encode(),
            role="owner", ynab_choice=True)
        self.assertIsNotNone(need, result)
        names = sorted(a["name"] for a in need["accounts"])
        visa = next(n for n in names if n.lower().startswith("visa"))
        return need["token"], {n: "skip" for n in names}, visa

    def test_a_card_picked_once_is_not_asked_again(self):
        moved = self._two_cards(self.conn, "visa")
        token, answers, visa = self._park()
        answers[visa] = {"new": visa}
        self.assertIn("choose one", pages.run_ynab_import(
            self.conn, token, answers, None)["error"])
        answers[visa] = moved[0]
        out = pages.run_ynab_import(self.conn, token, answers, None)
        self.assertNotIn("error", out, out)
        # the register's next upload: its card's default new name finds
        # the picked card without asking
        count = "SELECT count(*) AS n FROM accounts"
        before = self.conn.execute(count).fetchone()["n"]
        token, answers, visa = self._park()
        answers[visa] = {"new": visa}
        out = pages.run_ynab_import(self.conn, token, answers, None)
        self.assertNotIn("error", out, out)
        self.assertEqual(self.conn.execute(count).fetchone()["n"], before)


if __name__ == "__main__":
    unittest.main()
