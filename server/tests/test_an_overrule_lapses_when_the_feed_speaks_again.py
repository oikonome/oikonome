"""Setting an aggregator's name aside is a judgement about the strings a
row then carried — and about the aggregator having nothing but a guess.

Both can change. The feed can resolve the charge to an entity of its own,
which outranks any reading of a bank line; or it can restate the line or
the name, at which point the judgement was about a row that no longer
exists. Either way the decision lapses and the row is resolved afresh. It
must, however, survive the one journey that is meant to change nothing: an
export and a restore.
"""

import datetime as dt
import io
import unittest
import zipfile
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import merchant_identity
from oikonome.engine.compat import as_date, jsonb
from oikonome.sync import base, restore

from .util import _ensure_db, add_txn, make_db, write_config

LINE = "TapPay JUNIPER'S MKTSPRINGFIELD OR"
USUAL = "Juniper's Mkt"
STRAY = "Harbor Lights"
# the line the real business of that name prints for itself
OWN_LINE = "HARBOR LIGHTS CAFE 88"
ENTITY = "ENT-HARBORLIGHTS"
# its own id, so the migration case reads back exactly the row it made
MIGRATED_ROW = "stray-migrated"

# tables enough to carry the ledger and its merchants across
ROUNDTRIP_TABLES = ("items", "accounts", "merchants", "merchant_canonical",
                    "transactions")


def _zip_of(conn, drop: str | None = None) -> bytes:
    """The tables above as an export ZIP. `drop` omits one column, which is
    what an archive written before that column existed looks like."""
    import csv as _csv
    import json as _json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl in ROUNDTRIP_TABLES:
            rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
            s = io.StringIO()
            if rows:
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")
                        and c != drop]
                w = _csv.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (_json.dumps(r[k])
                                    if isinstance(r[k], (dict, list))
                                    else r[k]) for k in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class OverruleLapsesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        for d in range(merchant_identity.ESTABLISHED_ROWS):
            add_txn(self.conn, f"2026-08-{d + 1:02d}", 20.0 + d, LINE,
                    merchant=USUAL, txn_id=f"h{d}")
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY,
                txn_id="stray")
        merchant_identity.resolve(self.conn)
        self.home = self._merchant_of("h0")["id"]
        self.assertEqual(self._merchant_of("stray")["id"], self.home)
        self.assertEqual(self._cells("stray"), {"name": None, "aside": STRAY})

    def tearDown(self):
        self.conn.close()

    def _merchant_of(self, txn_id, conn=None):
        return (conn or self.conn).execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def _cells(self, txn_id, conn=None):
        """The two name columns, as the ingest rule leaves them."""
        return dict((conn or self.conn).execute(
            "SELECT merchant_name AS name, merchant_name_set_aside AS aside "
            "  FROM transactions WHERE id=%s", (txn_id,)).fetchone())

    def _set_aside(self, txn_id, conn=None):
        return self._cells(txn_id, conn)["aside"]

    def test_an_entity_arriving_for_that_row_takes_it_back(self):
        """The line outvoted a GUESS. Once the aggregator resolves the
        charge to an entity, there is no guess left to outvote."""
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = 'stray'",
            (jsonb({"merchant_entity_id": ENTITY, "merchant_name": STRAY,
                    "counterparties": [{"name": STRAY, "type": "merchant",
                                        "entity_id": ENTITY,
                                        "confidence_level": "VERY_HIGH"}]}),))
        merchant_identity.resolve(self.conn, txn_ids=["stray"],
                                  only_unresolved=False)
        self.assertEqual(self._cells("stray"), {"name": STRAY, "aside": None})
        self.assertEqual(self._merchant_of("stray")["name"], STRAY)
        self.assertNotEqual(self._merchant_of("stray")["id"], self.home)

    def test_the_line_s_own_rows_never_inherit_that_entity(self):
        """A row leaving the line's group must not lend it an identity on
        the way out. Keyed on the line while it carried an entity, the
        charge would hand that entity — and its logo, and its name — to
        every unnamed row the line has."""
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = 'stray'",
            (jsonb({"merchant_entity_id": ENTITY, "merchant_name": STRAY,
                    "counterparties": [{"name": STRAY, "type": "merchant",
                                        "entity_id": ENTITY,
                                        "confidence_level": "VERY_HIGH"}]}),))
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES ('bare','card',%s,6.50,%s,NULL,'GENERAL_MERCHANDISE',
                       0,0,%s)""",
            (as_date("2026-09-19"), LINE, jsonb({})))
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertNotEqual(self._merchant_of("bare")["id"],
                            self._merchant_of("stray")["id"])
        self.assertIsNone(self.conn.execute(
            "SELECT plaid_entity_id FROM merchants WHERE id=%s",
            (self._merchant_of("bare")["id"],)).fetchone()["plaid_entity_id"])

    def _resync(self, *, name, merchant_name):
        base.upsert_transactions(self.conn, [base.Transaction(
            id="stray", account_id="card", date=dt.date(2026, 4, 14),
            amount=7.40, name=name, merchant_name=merchant_name)])

    def test_a_resync_that_renames_the_charge_resolves_it_afresh(self):
        """The aggregator restating the name is a new claim about the
        payee, and the old judgement was about the old claim. Here it
        corrects itself to the name the line has always carried, and the
        charge settles on that — nothing is set aside any more."""
        self._resync(name=LINE, merchant_name=USUAL)
        self.assertEqual(self._cells("stray"), {"name": USUAL, "aside": None})
        self.assertEqual(self._merchant_of("stray")["id"], self.home)

    def test_a_resync_that_restates_the_line_resolves_it_afresh(self):
        """The judgement was about THIS line. A charge the feed re-reports
        under another one has to be read again from scratch."""
        self._resync(name=OWN_LINE, merchant_name=STRAY)
        self.assertEqual(self._cells("stray"), {"name": STRAY, "aside": None})
        self.assertEqual(self._merchant_of("stray")["name"], STRAY)
        self.assertNotEqual(self._merchant_of("stray")["id"], self.home)

    def test_a_resync_with_another_stray_name_is_judged_again(self):
        """Clearing the judgement is not immunity: the new strings get the
        same reading the old ones got."""
        self._resync(name=LINE, merchant_name="Beacon Point")
        self.assertEqual(self._cells("stray"),
                         {"name": None, "aside": "Beacon Point"})
        self.assertEqual(self._merchant_of("stray")["id"], self.home)

    def test_a_resync_that_changes_nothing_leaves_the_judgement_standing(self):
        """The aggregator re-sends every row it has, every hour, and the
        strings it re-sends are the ones the judgement was made about —
        the set-aside name among them. Comparing what it sends against the
        NULL now standing in merchant_name would read every such row as a
        new claim and hand the name straight back."""
        self._resync(name=LINE, merchant_name=STRAY)
        self.assertEqual(self._cells("stray"), {"name": None, "aside": STRAY})
        self.assertEqual(self._merchant_of("stray")["id"], self.home)

    def test_the_lapse_drops_the_merchant_before_anything_re_resolves(self):
        """The ingest half of it, on its own. The row's key changes with
        the name, so the merchant it was filed under is no longer the one
        its key points at — the upsert has to clear it, and not lean on
        the resolve it happens to call afterwards (a restore, a bridge
        push or a failing resolver all reach this same statement)."""
        with mock.patch.object(merchant_identity, "resolve",
                               lambda *a, **k: {}):
            self._resync(name=LINE, merchant_name="Beacon Point")
        row = dict(self.conn.execute(
            "SELECT merchant_name AS name, merchant_name_set_aside AS aside, "
            "       merchant_id FROM transactions WHERE id='stray'").fetchone())
        self.assertEqual(row, {"name": "Beacon Point", "aside": None,
                               "merchant_id": None})

    def test_a_row_that_was_never_set_aside_is_ingested_as_it_always_was(self):
        """The ordinary row, and the overwhelming majority: the feed's name
        lands in merchant_name, nothing is set aside, and the merchant is
        left for the resolve that follows to keep or revise — the lapse
        rule must not reach a row that was never judged."""
        before = self._merchant_of("h0")["id"]
        with mock.patch.object(merchant_identity, "resolve",
                               lambda *a, **k: {}):
            base.upsert_transactions(self.conn, [base.Transaction(
                id="h0", account_id="card", date=dt.date(2026, 8, 1),
                amount=20.0, name=LINE, merchant_name="Juniper's Mkt Cafe")])
        row = dict(self.conn.execute(
            "SELECT merchant_name AS name, merchant_name_set_aside AS aside, "
            "       merchant_id FROM transactions WHERE id='h0'").fetchone())
        self.assertEqual(row, {"name": "Juniper's Mkt Cafe", "aside": None,
                               "merchant_id": before})

    def test_a_restored_archive_keeps_the_judgement(self):
        """A restore is meant to reproduce the household, not re-open its
        decisions — and the re-resolve it ends with would file the row
        under the stray name all over again."""
        data = _zip_of(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            self.assertEqual(self._cells("stray", dest),
                             {"name": None, "aside": STRAY})
            self.assertEqual(self._merchant_of("stray", dest)["id"],
                             self._merchant_of("h0", dest)["id"])
        finally:
            dest.close()

    def test_an_archive_older_than_the_column_still_restores(self):
        """It says nothing about any of this: the charge arrives as a row
        the aggregator never named, which is the row the judgement made of
        it anyway, and it lands under the merchant its line means."""
        data = _zip_of(self.conn, drop="merchant_name_set_aside")
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            self.assertEqual(self._cells("stray", dest),
                             {"name": None, "aside": None})
            self.assertEqual(self._merchant_of("stray", dest)["id"],
                             self._merchant_of("h0", dest)["id"])
        finally:
            dest.close()


class MigrationTests(unittest.TestCase):
    """A ledger that upgrades across the change must MEAN the same
    afterwards. The build before this one left the stray name in
    merchant_name and marked the row with a boolean; the migration moves
    the name and drops the boolean, so a household that was carrying such
    a row keeps its judgement instead of answering to the name again.
    """

    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY,
                txn_id=MIGRATED_ROW)

    def tearDown(self):
        self.conn.close()

    def test_the_migration_moves_a_flagged_name_off_the_row(self):
        import pathlib

        import psycopg

        from oikonome.db import migrate
        sql = (pathlib.Path(migrate.__file__).parent / "migrations"
               / "137_merchant_name_set_aside.sql").read_text()
        _ensure_db()
        conn = tenancy.admin_connect()
        try:
            self.assertIsNotNone(self._column(conn, "merchant_name_set_aside"),
                                 "schema.sql did not create the column")
            # the old shape is rebuilt by hand inside a transaction that is
            # rolled back, so the suite's own database is left as it was
            with conn.transaction():
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN "
                    "merchant_name_overruled BOOLEAN NOT NULL DEFAULT false")
                conn.execute(
                    "UPDATE transactions SET merchant_name = %s, "
                    "       merchant_name_set_aside = NULL, "
                    "       merchant_name_overruled = true WHERE id = %s",
                    (STRAY, MIGRATED_ROW))
                conn.execute(sql)
                row = dict(conn.execute(
                    "SELECT merchant_name AS name, "
                    "       merchant_name_set_aside AS aside "
                    "  FROM transactions WHERE id = %s",
                    (MIGRATED_ROW,)).fetchone())
                self.assertEqual(row, {"name": None, "aside": STRAY})
                # and the flag is gone, so nothing can read it again
                self.assertIsNone(
                    self._column(conn, "merchant_name_overruled"))
                raise psycopg.Rollback
            self.assertIsNone(self._column(conn, "merchant_name_overruled"))
            self.assertIsNotNone(self._column(conn, "merchant_name_set_aside"))
        finally:
            conn.close()

    def test_the_migration_is_safe_on_a_ledger_that_never_had_the_flag(self):
        """A fresh install loads schema.sql — which creates the new column
        and not the old one — and then runs every numbered migration over
        it. Re-running is the same path, and both must leave a row that
        carries a set-aside name exactly as it stands."""
        import pathlib

        from oikonome.db import migrate
        sql = (pathlib.Path(migrate.__file__).parent / "migrations"
               / "137_merchant_name_set_aside.sql").read_text()
        _ensure_db()
        self.conn.execute(
            "UPDATE transactions SET merchant_name = NULL, "
            "       merchant_name_set_aside = %s WHERE id = %s",
            (STRAY, MIGRATED_ROW))
        conn = tenancy.admin_connect()
        try:
            conn.execute(sql)
        finally:
            conn.close()
        self.assertEqual(
            dict(self.conn.execute(
                "SELECT merchant_name AS name, "
                "       merchant_name_set_aside AS aside "
                "  FROM transactions WHERE id = %s",
                (MIGRATED_ROW,)).fetchone()),
            {"name": None, "aside": STRAY})

    @staticmethod
    def _column(conn, name):
        row = conn.execute(
            "SELECT data_type, is_nullable "
            "  FROM information_schema.columns "
            " WHERE table_name = 'transactions' "
            "   AND column_name = %s", (name,)).fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    unittest.main()
