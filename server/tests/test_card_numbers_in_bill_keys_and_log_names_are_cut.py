"""A detected bill's match key, its identity names and the log's names keep
no card number whole.

Migration 206 cut a detected bill's name and the activity log's label and
sentence. It left the key a bill is matched by (bills.merchant), the
merchant names its identity is stored under, the log's target where that
is a payee or a merchant's name, and the names a log row's detail quotes.
Migration 208 cuts them, in Python (db/migrate.py, PYTHON_STEPS), and the
log cuts them on the way in. What a person typed as a bill's key is theirs
and stays; an id is never cut. Every number here is a test-card shape,
invented.
"""
import datetime as dt
import unittest

from oikonome.db import migrate, tenancy
from oikonome.engine import activity, bills

from .util import TEST_DB, _admin_dsn, add_bill, add_txn, make_db

TODAY = dt.date(2026, 8, 20)
APP_PASSWORD = "apppass"
WHOLE_KEY = "ox mobile 4000 0000 0000 0002"
CUT_KEY = "ox mobile …0002"
WHOLE_NAME = "OX MOBILE 4000-0000-0000-0002"
CUT_NAME = "OX MOBILE …0002"
STEP = "208_card_numbers_in_bill_keys_and_log_names_are_cut.sql"


def _step(conn):
    """The migration's step, as migrate runs it, on this household."""
    migrate.PYTHON_STEPS[STEP](conn)


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _stored_bill(self, payee, key, names, source="detected"):
        """A bill as an older version stored it: key and identity whole."""
        add_bill(self.conn, payee, 40, merchant=key, merchants=[],
                 source=source)
        refs = [{"id": None, "name": n} for n in names]
        self.conn.execute(
            "UPDATE bills SET raw = (raw || jsonb_build_object("
            " 'merchant_refs', %s::jsonb, 'merchant_names', %s::jsonb))"
            " - 'merchant_pinned' WHERE payee = %s",
            (activity.jsonb(refs), activity.jsonb(names), payee))

    def _bill(self, payee):
        return self.conn.execute(
            "SELECT merchant, raw FROM bills WHERE payee = %s",
            (payee,)).fetchone()

    def _log(self, kind, action, target, detail):
        # written past record(), as a row stored before it cut was
        return self.conn.execute(
            "INSERT INTO activity_log (actor, kind, action, target, label,"
            " summary, detail) VALUES ('sam@example.test', %s, %s, %s,"
            " 'x', 'x', %s) RETURNING id",
            (kind, action, target,
             activity.jsonb(detail) if detail is not None else None)
        ).fetchone()["id"]

    def _row(self, rid):
        return self.conn.execute(
            "SELECT target, detail FROM activity_log WHERE id = %s",
            (rid,)).fetchone()

    def _snapshot(self):
        return (
            [tuple(r.values()) for r in self.conn.execute(
                "SELECT id, merchant, raw::text FROM bills ORDER BY id")],
            [tuple(r.values()) for r in self.conn.execute(
                "SELECT id, target, detail::text FROM activity_log"
                " ORDER BY id")])


class ADetectedBillsKeyIsCut(_Db):
    def test_its_key_and_identity_names_are_cut(self):
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        _step(self.conn)
        b = self._bill("Ox Mobile")
        self.assertEqual(b["merchant"], CUT_KEY)
        self.assertEqual([r["name"] for r in b["raw"]["merchant_refs"]],
                         [CUT_NAME])
        self.assertEqual(b["raw"]["merchant_names"], [CUT_NAME])

    def test_cut_it_still_finds_cut_and_whole_rows(self):
        add_txn(self.conn, "2026-08-02", 40.0, "OX MOBILE …0002 AUTOPAY",
                txn_id="cut")
        add_txn(self.conn, "2026-07-02", 40.0,
                "OX MOBILE 4000 0000 0000 0002 AUTOPAY", txn_id="whole")
        add_txn(self.conn, "2026-06-02", 40.0,
                "OX MOBILE 4000000000000002 AUTOPAY", txn_id="unbroken")
        self._stored_bill("Ox Mobile", WHOLE_KEY, [])
        self.conn.execute(
            "UPDATE bills SET raw = raw - 'merchant_refs' - 'merchant_names'")
        before = {t["txn_id"] for t in bills.merchant_history(
            self.conn, "Ox Mobile", today=TODAY)["txns"]}
        _step(self.conn)
        self.assertEqual(self._bill("Ox Mobile")["merchant"], CUT_KEY)
        after = {t["txn_id"] for t in bills.merchant_history(
            self.conn, "Ox Mobile", today=TODAY)["txns"]}
        self.assertEqual(before, {"cut", "whole", "unbroken"})
        self.assertEqual(after, before)

    def test_a_key_a_person_typed_stays(self):
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME],
                          source="manual")
        _step(self.conn)
        b = self._bill("Ox Mobile")
        self.assertEqual(b["merchant"], WHOLE_KEY)
        self.assertEqual(b["raw"]["merchant_names"], [WHOLE_NAME])

    def test_a_name_a_live_merchant_holds_whole_stays(self):
        # a person named the merchant so, and migration 204 kept it: the
        # whole name is what finds that merchant
        self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES (%s, 'manual')",
            (WHOLE_NAME,))
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        _step(self.conn)
        b = self._bill("Ox Mobile")
        self.assertEqual(b["merchant"], CUT_KEY)
        self.assertEqual(b["raw"]["merchant_names"], [WHOLE_NAME])


class TheLogsNamesAreCut(_Db):
    def test_name_targets_and_quoted_names_are_cut_and_ids_stay(self):
        bill = self._log("bill", "edited", WHOLE_NAME, {
            "before": {"payee": WHOLE_NAME, "amount": 40},
            "after": {"payee": "Ox Mobile", "amount": 40},
            "ids": ["csv:4000000000000002"]})
        rule = self._log("rule", "set", WHOLE_NAME, {"after": "PHONE"})
        renamed = self._log("merchant", "renamed", WHOLE_NAME,
                            {"before": WHOLE_NAME, "after": WHOLE_NAME})
        kept = self._log("merchant", "kept_apart", "pid-40000000000000020",
                         None)
        cat = self._log("category", "set", "csv:4000000000000002",
                        {"after": "PHONE", "merchant": WHOLE_NAME})
        acct = self._log("account", "renamed", "manual:visa-40000000",
                         {"before": "Visa 4000 0000 0000 0002",
                          "after": "Visa …0002"})
        note = self._log("note", "added", "csv:4000000000000002",
                         {"after": "paid 4000 0000 0000 0002"})
        _step(self.conn)
        r = self._row(bill)
        self.assertEqual(r["target"], CUT_NAME)
        self.assertEqual(r["detail"]["before"]["payee"], CUT_NAME)
        self.assertEqual(r["detail"]["ids"], ["csv:4000000000000002"])
        self.assertEqual(self._row(rule)["target"], CUT_NAME)
        r = self._row(renamed)
        self.assertEqual((r["target"], r["detail"]["before"],
                          r["detail"]["after"]),
                         (CUT_NAME, CUT_NAME, CUT_NAME))
        self.assertEqual(self._row(kept)["target"], "pid-40000000000000020")
        r = self._row(cat)
        self.assertEqual((r["target"], r["detail"]["merchant"]),
                         ("csv:4000000000000002", CUT_NAME))
        r = self._row(acct)
        self.assertEqual((r["target"], r["detail"]["before"]),
                         ("manual:visa-40000000", "Visa …0002"))
        # a note's own text is the note, quoted as the household wrote it
        self.assertEqual(self._row(note)["detail"]["after"],
                         "paid 4000 0000 0000 0002")

    def test_record_cuts_the_same_on_the_way_in(self):
        rid = activity.record(
            self.conn, "sam@example.test", "merchant", "renamed",
            target=WHOLE_NAME, label=WHOLE_NAME,
            detail={"before": WHOLE_NAME, "after": "Ox Mobile"})
        r = self._row(rid)
        self.assertEqual((r["target"], r["detail"]["before"]),
                         (CUT_NAME, CUT_NAME))


class TheStepIsAMigration(_Db):
    def test_applied_twice_it_changes_nothing(self):
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        self._log("bill", "edited", WHOLE_NAME,
                  {"before": {"payee": WHOLE_NAME}, "after": None})
        _step(self.conn)
        once = self._snapshot()
        _step(self.conn)
        self.assertEqual(self._snapshot(), once)
        self.assertEqual(bills.cut_detected_keys(self.conn), 0)
        self.assertEqual(activity.cut_stored_names(self.conn), 0)

    def test_migrate_runs_the_step_with_the_file_and_records_it(self):
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        done = {r["name"] for r in admin.execute(
            "SELECT name FROM schema_migrations")} - {STEP}
        admin.execute("DELETE FROM schema_migrations WHERE name = %s",
                      (STEP,))
        applied = []
        migrate._apply_dir(admin, migrate.HERE / "migrations", done, applied)
        self.assertEqual(applied, [STEP])
        self.assertEqual(self._bill("Ox Mobile")["merchant"], CUT_KEY)
        self.assertTrue(admin.execute(
            "SELECT 1 FROM schema_migrations WHERE name = %s",
            (STEP,)).fetchone())

    def _tid(self):
        return self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def test_a_row_a_save_beat_to_the_write_is_cut_by_the_next_migrate(
            self):
        """migrate runs while the app serves: a bill saved between the
        step's read and its write keeps its number past the step, and the
        file is recorded; the pass every migrate runs cuts it."""
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        self._log("bill", "edited", WHOLE_NAME, None)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        saver = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(saver.close)

        class _SaveLandsFirst:
            """The migrate's connection, with a save landing just before
            its first bill write."""
            def __init__(self, conn):
                self.conn, self.saved = conn, False

            def execute(self, sql, *a, **k):
                if sql.lstrip().startswith("UPDATE bills") and not self.saved:
                    self.saved = True
                    saver.execute(
                        "UPDATE bills SET raw = raw || '{\"due\": 1}'"
                        " WHERE payee = 'Ox Mobile'")
                return self.conn.execute(sql, *a, **k)

            def __getattr__(self, name):        # transaction(), info, …
                return getattr(self.conn, name)

        _step(_SaveLandsFirst(admin))
        self.assertEqual(self._bill("Ox Mobile")["merchant"], WHOLE_KEY)
        migrate.cut_bill_keys_and_log_names(admin, APP_PASSWORD)
        b = self._bill("Ox Mobile")
        self.assertEqual(b["merchant"], CUT_KEY)
        self.assertEqual(b["raw"]["merchant_names"], [CUT_NAME])
        self.assertEqual(b["raw"]["due"], 1)
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM activity_log WHERE target = %s",
            (WHOLE_NAME,)).fetchone())

    def test_a_busy_household_is_left_for_the_next_migrate(self):
        self._stored_bill("Ox Mobile", WHOLE_KEY, [WHOLE_NAME])
        tid = self._tid()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        holder = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                       (f"oikonome:sync:{tid}",))
        migrate.cut_bill_keys_and_log_names(admin, APP_PASSWORD)
        self.assertEqual(self._bill("Ox Mobile")["merchant"], WHOLE_KEY)
        holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                       (f"oikonome:sync:{tid}",))
        self.assertGreaterEqual(
            migrate.cut_bill_keys_and_log_names(admin, APP_PASSWORD), 1)
        self.assertEqual(self._bill("Ox Mobile")["merchant"], CUT_KEY)

    def test_the_pass_visits_no_household_whose_names_are_already_cut(self):
        """It runs on every migrate, so what it probes must be what a cut
        changes: a cut name keeps four digits, and is not visited again."""
        self._stored_bill("Ox Mobile", CUT_KEY, [CUT_NAME])
        self._log("bill", "edited", CUT_NAME,
                  {"before": {"payee": CUT_NAME}, "after": None})
        self._log("category", "set", "csv:4000000000000002",
                  {"after": "PHONE"})
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        self.assertNotIn(self._tid(), migrate._households_to_cut(admin))
        self._stored_bill("Ox Two", WHOLE_KEY, [])
        self.assertIn(self._tid(), migrate._households_to_cut(admin))

    def test_the_file_changes_no_row_itself(self):
        sql = (migrate.HERE / "migrations" / STEP).read_text()
        code = "\n".join(line.split("--")[0] for line in sql.splitlines())
        self.assertEqual(code.split(), ["SELECT", "1;"])


if __name__ == "__main__":
    unittest.main()


class ARestoreCutsWhatMigration208Cut(_Db):
    """An archive written before migration 208 brings a detected bill's
    match key and the log's name targets back whole; the restore cuts them
    as the migration cut the stored ones, so a restored household never
    holds the number 208 took out."""

    def test_an_older_archive_lands_with_its_keys_and_log_names_cut(self):
        import tempfile
        from oikonome.sync import export, restore
        self._stored_bill("Fabrikam Card 4000 1234 5678 9010",
                          "fabrikam card 4000 1234 5678 9010", [])
        self._log("bill", "updated", "Fabrikam Card 4000 1234 5678 9010",
                  {"before": {"payee": "Fabrikam Card 4000 1234 5678 9010"}})
        with tempfile.TemporaryFile() as fh:
            export.build_zip(self.conn, out=fh)
            fh.seek(0)
            data = fh.read()
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, data)
        keys = [r["merchant"] for r in dest.execute(
            "SELECT merchant FROM bills")]
        self.assertTrue(keys, "the bill came back")
        self.assertFalse(any("4000 1234" in (k or "") for k in keys), keys)
        targets = [r["target"] for r in dest.execute(
            "SELECT target FROM activity_log WHERE kind = 'bill'")]
        self.assertFalse(any("4000 1234" in (t or "") for t in targets),
                         targets)
