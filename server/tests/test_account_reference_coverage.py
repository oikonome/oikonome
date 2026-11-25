"""Drift guard: every column that holds an account id is a CONSCIOUS
decision for the account move (a reconnect onto a fresh link, a
restore-merge into the live twin, a manual account renamed) — either carried
by sync.adopt.ACCOUNT_REFS, or listed in sync.adopt.NOT_ACCOUNT_REFS with the
reason it is not one. A column left out keeps naming an id the move deleted,
and every reader of it fails open and silently: the forecast's kept nights
did, so the grading around a reconnect was skipped as "your checking account
changed" or read against an empty ledger. Found from the catalog, by foreign
key to accounts.id and by a name that says account, checking or card."""
import unittest

from oikonome.db import tenancy
from oikonome.sync import adopt

from .util import TEST_DB, _admin_dsn, _ensure_db

NAMED = r"acc(oun)?t|acct|checking|card"


class EveryAccountReferenceIsClassified(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        conn = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            cls.named = {(r["t"], r["c"]) for r in conn.execute(
                """SELECT table_name AS t, column_name AS c
                     FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name <> 'accounts'
                      AND column_name ~* %s""", (NAMED,)).fetchall()}
            cls.keyed = {(r["t"], r["c"]) for r in conn.execute(
                """SELECT cl.relname AS t, a.attname AS c
                     FROM pg_constraint k
                     JOIN pg_class cl ON cl.oid = k.conrelid
                     CROSS JOIN LATERAL unnest(k.conkey, k.confkey)
                          AS x(s, f)
                     JOIN pg_attribute a ON a.attrelid = k.conrelid
                                        AND a.attnum = x.s
                     JOIN pg_attribute fa ON fa.attrelid = k.confrelid
                                         AND fa.attnum = x.f
                    WHERE k.contype = 'f'
                      AND k.confrelid = 'accounts'::regclass
                      AND fa.attname = 'id'""").fetchall()}
            cls.columns = {(r["t"], r["c"]) for r in conn.execute(
                """SELECT table_name AS t, column_name AS c
                     FROM information_schema.columns
                    WHERE table_schema = current_schema()""").fetchall()}
        finally:
            conn.close()

    def test_every_account_column_is_moved_or_explained(self):
        classified = set(adopt.ACCOUNT_REFS) | set(adopt.NOT_ACCOUNT_REFS)
        missing = sorted((self.named | self.keyed) - classified)
        self.assertEqual(
            missing, [],
            "these columns look like they hold an account id: add each to "
            "sync.adopt.ACCOUNT_REFS (a move carries it) or to "
            "NOT_ACCOUNT_REFS with the reason it is not one")

    def test_a_foreign_key_to_accounts_is_always_moved(self):
        self.assertLessEqual(self.keyed, set(adopt.ACCOUNT_REFS))

    def test_no_entry_names_a_column_that_does_not_exist(self):
        stale = sorted((set(adopt.ACCOUNT_REFS)
                        | set(adopt.NOT_ACCOUNT_REFS)) - self.columns)
        self.assertEqual(stale, [])

    def test_the_two_lists_do_not_overlap(self):
        self.assertEqual(
            set(adopt.ACCOUNT_REFS) & set(adopt.NOT_ACCOUNT_REFS), set())


if __name__ == "__main__":
    unittest.main()
