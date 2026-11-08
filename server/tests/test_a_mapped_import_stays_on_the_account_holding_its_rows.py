"""A CSV whose columns must be mapped by hand lands where its rows are,
and an abandoned mapping leaves no account behind.

A new-account name decides its account only once the file's own rows can
be read: an upgraded household's second card may already be held by an
earlier account (see the test of the same name for files the importer
recognizes), and only the parsed rows say so. A CSV the importer cannot
map on its own is parked until the person maps its columns, so the
account it fills is decided when the mapping is submitted, never at the
upload: an account made at the upload would be empty, nothing would show
the earlier account holds the card, and the mapped rows would land on it
a second time. A mapping never submitted makes no account at all.

Every name, number and amount here is invented."""
import unittest

from oikonome.db import tenancy
from oikonome.web import bulk_import, pages

from .bulk_job_util import SignedInClient, csv_for, submit_and_run
from .test_a_second_card_reimport_stays_on_the_account_holding_its_rows \
    import CARD_A, CARD_B, OLD, _ledger, _seed_old_account
from .util import make_db, write_config

NEW_CARD = "Visa 4000555566660123"         # never filed anywhere


def unmapped_for(tag: str) -> bytes:
    """The rows bulk_job_util.csv_for gives `tag`, under column names the
    importer does not recognize, so the person has to map them."""
    return (b"Posted,Payee Text,Value\n"
            + f"2021-04-01,{tag} ONE,-10.00\n".encode()
            + f"2021-04-02,{tag} TWO,-20.00\n".encode()
            + f"2021-04-03,{tag} THREE,-30.00\n".encode())


COLUMNS = {"date_col": "Posted", "name_col": "Payee Text",
           "amount_col": "Value"}


class TheSingleFileMappingFlow(unittest.TestCase):
    def setUp(self):
        self.h = SignedInClient(prefix="map")
        conn = tenancy.tenant_connect(self.h.tid)
        try:
            write_config(conn)
            _seed_old_account(conn)
        finally:
            conn.close()
        # the first card's re-import claims the earlier account, so the
        # second card's name no longer leads to it by name alone
        r = self.h.c.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": CARD_A},
            files={"file": (f"{CARD_A}.csv", csv_for("HARBOR"), "text/csv")})
        assert not (r.json()["result"] or {}).get("error"), r.text

    def _read(self, fn):
        conn = tenancy.tenant_connect(self.h.tid)
        try:
            return fn(conn)
        finally:
            conn.close()

    def _accounts(self) -> int:
        return self._read(lambda c: c.execute(
            "SELECT count(*) AS n FROM accounts WHERE item_id = 'manual'"
        ).fetchone()["n"])

    def _upload(self, card, tag) -> dict:
        r = self.h.c.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": card},
            files={"file": (f"{card}.csv", unmapped_for(tag), "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        need = r.json()["mapping_needed"]
        self.assertTrue(need and need.get("token"), r.text)
        return need

    def _map(self, token) -> dict:
        r = self.h.c.post("/api/import/mapped",
                          json=dict(COLUMNS, token=token))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"]

    def test_the_second_card_mapped_stays_where_its_rows_are(self):
        before = self._read(_ledger)
        need = self._upload(CARD_B, "NORTHWIND")
        result = self._map(need["token"])
        self.assertFalse(result.get("error"), result)
        self.assertEqual(self._read(_ledger), before)
        self.assertEqual(self._accounts(), 1)

    def test_a_new_card_mapped_gets_its_own_account(self):
        before = self._read(_ledger)
        need = self._upload(NEW_CARD, "CONTOSO")
        self.assertEqual(self._accounts(), 1)
        result = self._map(need["token"])
        self.assertEqual(result.get("imported"), 3, result)
        self.assertEqual(self._accounts(), 2)
        n, s = self._read(_ledger)
        self.assertEqual((n, round(s, 2)),
                         (before[0] + 3, round(before[1] + 60.0, 2)))
        # the account the mapping made is the one the name fills from now
        ids = self._read(lambda c: [r["account_id"] for r in c.execute(
            "SELECT DISTINCT account_id FROM transactions"
            " WHERE name LIKE 'CONTOSO%%'").fetchall()])
        self.assertEqual(len(ids), 1)
        self.assertNotEqual(ids[0], OLD)

    def test_an_abandoned_mapping_makes_no_account(self):
        self._upload(NEW_CARD, "CONTOSO")
        self._upload(CARD_B, "NORTHWIND")
        self.assertEqual(self._accounts(), 1)

    def test_a_register_to_place_makes_no_account_until_placed(self):
        from .test_ynab_register_rows_land_on_the_account_they_came_from \
            import REGISTER
        r = self.h.c.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": "Shared Budget", "ynab_choice": "1"},
            files={"file": ("register.csv", REGISTER.encode(), "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        need = r.json()["mapping_needed"]
        self.assertEqual(need["kind"], "ynab_accounts", r.text)
        self.assertEqual(self._accounts(), 1)
        r = self.h.c.post("/api/import/ynab-accounts", json={
            "token": need["token"],
            "accounts": {"Checking": {"new": "Budget Checking"},
                         "Visa": "skip"},
            "remove_earlier": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("error", r.json()["result"])
        self.assertEqual(self._accounts(), 2)

    def test_a_mapping_parked_with_an_account_still_lands_there(self):
        def park(conn):
            text = unmapped_for("FABRIKAM").decode()
            return pages._stash_csv(conn, OLD, "bank", "older.csv", text,
                                    ["Posted", "Payee Text", "Value"])
        token = self._read(park)
        before = self._read(_ledger)
        result = self._map(token)
        self.assertEqual(result.get("imported"), 3, result)
        self.assertEqual(self._read(_ledger)[0], before[0] + 3)
        self.assertEqual(self._accounts(), 1)


class TheFolderImportRefusingAMapping(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        _seed_old_account(self.conn)

    def test_a_file_sent_back_to_map_alone_makes_no_account(self):
        for card, tag in ((CARD_B, "NORTHWIND"), (NEW_CARD, "CONTOSO")):
            plan = bulk_import.analyze(
                self.conn, [(f"{card}.csv", unmapped_for(tag))])
            entry = dict(plan["files"][0], account_id=None,
                         new_account_name=card, amount_sign="bank")
            res = submit_and_run(self.conn, plan["token"], [entry])[0]
            self.assertFalse(res["ok"], res)
            n = self.conn.execute(
                "SELECT count(*) AS n FROM accounts"
                " WHERE item_id = 'manual'").fetchone()["n"]
            self.assertEqual(n, 1, (card, res))


if __name__ == "__main__":
    unittest.main()
