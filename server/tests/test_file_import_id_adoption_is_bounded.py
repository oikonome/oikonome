"""Looking for rows an earlier import left under a former account id costs
a bounded amount of work, whatever the account id or the file size.

A file row's id hashes the account id, so after a rename the importer
re-hashes each new line under the ids the account stood under before. If
every dash of the account id counted as a possible renamed comma, an id
with six dashes would multiply each of a 50,000-row file's lines by 63
spellings (and again by two when names carry long digit runs): millions of
hashes held in memory and sent as one query, enough to kill the web
process. The search must not run at all on a first import, must not treat
an app-minted id's dashes as commas, and must stay under a fixed ceiling
when it does run.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.sync import base, csvimport
from oikonome.sync.base import FileImportIds, Transaction

from .util import make_db, write_config

MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _csv(n, start=0):
    lines = ["Date,Description,Amount"]
    for i in range(start, start + n):
        d = dt.date(2025, 1, 1) + dt.timedelta(days=i % 300)
        lines.append(f"{d.isoformat()},SHOP {1234567890 + i},-{1 + i % 97}.00")
    return "\n".join(lines) + "\n"


class AdoptionWorkIsBounded(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.hashed = 0
        real = FileImportIds._hash

        def counting(ids, b, n):
            self.hashed += 1
            return real(ids, b, n)
        p = mock.patch.object(FileImportIds, "_hash", counting)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def _account(self, aid):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual','manual','Manual')"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type)"
            " VALUES (%s, 'manual', 'x', 'depository')", (aid,))

    def test_a_first_import_hashes_each_line_only_for_its_own_id(self):
        aid = "manual:a-b-c-d-e-f-g"
        self._account(aid)
        csvimport.import_csv(self.conn, aid, _csv(500), MAPPING)
        # one id per line, plus the pre-masking id each long-digit name
        # carries — nothing per former spelling
        self.assertLessEqual(self.hashed, 2 * 500)

    def test_an_app_minted_id_is_never_read_as_a_renamed_comma_id(self):
        ids = FileImportIds("csv", "manual:a-b-c-d-e-f-g")
        self.assertEqual(ids._former_account_ids(self.conn), [])

    def test_a_dashed_external_id_stays_under_the_ceiling(self):
        aid = "bridge-a-b-c-d-e-f"          # six dashes, 63 comma spellings
        self._account(aid)
        csvimport.import_csv(self.conn, aid, _csv(50), MAPPING)
        self.hashed = 0
        with mock.patch.object(base, "ADOPT_MAX_GUESSES", 3_000), \
                mock.patch.object(base, "ADOPT_SLICE", 100):
            csvimport.import_csv(self.conn, aid, _csv(400, start=50),
                                 MAPPING)
        # the file's own ids (two per line) plus at most the ceiling
        self.assertLessEqual(self.hashed, 2 * 400 + 3_000)

    def test_recorded_adoptions_are_searched_before_comma_spellings(self):
        aid = "bridge-a-b-c"
        self._account(aid)
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id) VALUES (%s, 'old-acct', 'manual')", (aid,))
        former = FileImportIds("csv", aid)._former_account_ids(self.conn)
        self.assertEqual(former[0], "old-acct")
        self.assertIn("bridge,a-b-c", former)

    def test_the_lookup_never_takes_the_whole_candidate_list_at_once(self):
        aid = "bridge-a-b"
        self._account(aid)
        ids = FileImportIds("csv", aid)
        txns = [Transaction(id=ids.id_for(dt.date(2025, 1, 1), -1.0 - i,
                                          f"SHOP {i}"),
                            account_id=aid, date=dt.date(2025, 1, 1),
                            amount=-1.0 - i, name=f"SHOP {i}")
                for i in range(250)]
        # an earlier row of this importer, so the search runs
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " VALUES ('csv:earlier', %s, '2024-01-01', -1, 'x')", (aid,))
        sizes = []
        real = self.conn.execute

        def spy(sql, args=None, *a, **k):
            if "account_id = %s" in sql and "id = ANY" in sql:
                sizes.append(len(args[0]))
            return real(sql, args, *a, **k)
        with mock.patch.object(base, "ADOPT_SLICE", 100), \
                mock.patch.object(self.conn, "execute", spy):
            ids.resolve(self.conn, txns)
        self.assertTrue(sizes)
        # three comma spellings of a two-dash id, one tail per line
        self.assertLessEqual(max(sizes), 100 * 3)


if __name__ == "__main__":
    unittest.main()
