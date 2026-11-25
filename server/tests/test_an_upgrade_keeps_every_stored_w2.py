"""An upgrade never deletes a stored W-2 (specs/w2-wages.md).

A build older than the person-per-W-2 question stored every W-2 with no
owner. Two of them with the same employer and the same box 1 in one year
can be two people's forms (spouses at one employer on one pay grade), so
the upgrade keeps every row: the one the year's figure stands for stays
the primary earner's and the others wait, unassigned, for their person.
"""

import unittest

from oikonome.db import migrate, tenancy

from .util import TEST_DB, _admin_dsn, make_db

W2_MIGRATIONS = (
    "221_a_w2_says_whose_it_is.sql",
    "225_one_w2_row_is_not_enforced.sql",
    "226_a_w2_figure_remembers_what_it_wrote.sql",
    "227_w2s_an_older_build_filed_under_the_primary_earner.sql",
)


class _Undo(Exception):
    pass


class AnUpgradeKeepsEveryW2(unittest.TestCase):
    def test_lookalike_w2s_of_one_year_all_survive_the_upgrade(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        try:
            with admin.transaction():
                # the table as a build before 221 left it: no owner said
                admin.execute("DROP INDEX IF EXISTS uq_income_documents_w2")
                admin.execute("ALTER TABLE income_documents "
                              "ALTER COLUMN owner DROP NOT NULL")
                for doc_id, year, owner in (
                        # 2021: two spouses' forms, one employer, one box 1
                        (1, 2021, None), (2, 2021, "primary"),
                        # 2022: the same form three times over
                        (3, 2022, None), (4, 2022, None), (5, 2022, None)):
                    admin.execute(
                        """INSERT INTO income_documents (tenant_id, id, year,
                               form, owner, payer, ein_last4, primary_amount)
                           VALUES (%s, %s, %s, 'W-2', %s, 'ACME STAFFING',
                                   '4321', 40000.0)""",
                        (tid, doc_id, year, owner))
                for name in W2_MIGRATIONS:
                    admin.execute(
                        (migrate.HERE / "migrations" / name).read_text())
                owners = {r["id"]: r["owner"] for r in admin.execute(
                    "SELECT id, owner FROM income_documents "
                    "WHERE tenant_id = %s", (tid,)).fetchall()}
                self.assertEqual(owners, {
                    1: "primary", 2: "unassigned",
                    3: "primary", 4: "unassigned", 5: "unassigned"})
                raise _Undo
        except _Undo:
            pass


if __name__ == "__main__":
    unittest.main()
