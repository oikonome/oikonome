"""The data-portability archive is not a backup, and the restore door says
so before it writes anything.

The archive an operator builds for a data-portability request
(tenant_export.build_archive) is JSON under data/, read by people; a
restore reads the household export's CSVs. Uploaded to restore, it used
to land nothing, replay the data migrations over the household, and
report success — the person believes their household came back."""

import io
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.sync import archive_upgrades, restore

from .test_account_erasure_export_and_consent import _seed_tenant
from .util import _ensure_db


class APortabilityArchiveIsRefused(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _counts(self, conn):
        return conn.execute(
            "SELECT (SELECT count(*) FROM accounts) AS a,"
            " (SELECT count(*) FROM items) AS i,"
            " (SELECT count(*) FROM transactions) AS t").fetchone()

    def test_refused_with_a_sentence_and_nothing_written(self):
        tid, _ = _seed_tenant()
        data = tenant_export.build_archive(tid, operator="op",
                                           consented=True)
        # into a household that does not hold the archive's rows already
        other, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(other)
        try:
            before = self._counts(conn)
            with self.assertRaises(ValueError) as e:
                restore.restore_zip(conn, data)
            self.assertIn("data-portability archive", str(e.exception))
            self.assertIn("Settings", str(e.exception))
            self.assertEqual(self._counts(conn), before)
        finally:
            conn.close()

    def test_one_with_its_manifest_dropped_is_refused_by_its_shape(self):
        tid, _ = _seed_tenant()
        src = zipfile.ZipFile(io.BytesIO(tenant_export.build_archive(
            tid, operator="op", consented=True)))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in src.namelist():
                if name != archive_upgrades.MANIFEST:
                    z.writestr(name, src.read(name))
        conn = tenancy.tenant_connect(tid)
        try:
            with self.assertRaises(ValueError) as e:
                restore.restore_zip(conn, buf.getvalue())
            self.assertIn("data-portability archive", str(e.exception))
        finally:
            conn.close()


class AForeignZipIsRefused(unittest.TestCase):
    """Any other ZIP — a budgeting app's own export — holds none of the
    export's members, so it would land nothing and report 'Restored 0
    transactions'. The door says what it is instead, before anything is
    written."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_damaged_archive_is_left_to_the_restore_to_name(self):
        """A lone transactions.csv whose deflate stream is damaged: the
        sniff must not raise (a 500 at the door) but say nothing, so the
        restore's own checks report what broke."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("transactions.csv", "date,amount\n" + "x" * 4000)
        raw = bytearray(buf.getvalue())
        start = raw.find(b"transactions.csv") + len("transactions.csv")
        for i in range(start, start + 3):
            raw[i] ^= 0xFF
        self.assertIsNone(restore.foreign_archive_refusal(bytes(raw)))

    @staticmethod
    def _zip(members: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, body in members.items():
                z.writestr(name, body)
        return buf.getvalue()

    def test_a_budget_app_export_is_refused_with_what_to_do(self):
        data = self._zip({
            "Household Plan as of 2026-03-01 - Register.csv":
                "Account,Flag,Date,Payee,Outflow,Inflow\n"
                "Checking,,03/01/2026,Corner Bakery,$12.00,$0.00\n",
            "Household Plan as of 2026-03-01 - Plan.csv": "Month,Category\n"})
        tid, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(tid)
        try:
            with self.assertRaises(ValueError) as e:
                restore.restore_zip(conn, data)
            self.assertIn("isn't an Oikonome export", str(e.exception))
            self.assertIn("Register.csv", str(e.exception))
        finally:
            conn.close()

    def test_a_zipped_spending_csv_named_like_a_table_is_refused(self):
        data = self._zip({"transactions.csv":
                          "Date,Description,Amount\n03/01/2026,Cafe,4.00\n"})
        self.assertIsNotNone(restore.foreign_archive_refusal(data))

    def test_the_household_export_is_not_refused(self):
        tid, _ = _seed_tenant()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.sync import export
            data = export.build_zip(conn)
        finally:
            conn.close()
        self.assertIsNone(restore.foreign_archive_refusal(data))
        # an archive from before the manifest is known by its tables
        src = zipfile.ZipFile(io.BytesIO(data))
        old = self._zip({n: src.read(n) for n in src.namelist()
                         if n != archive_upgrades.MANIFEST})
        self.assertIsNone(restore.foreign_archive_refusal(old))

    def test_an_export_zipped_inside_its_folder_says_to_zip_the_files(self):
        """Safari unzips a download and Finder's Compress on that folder
        makes a ZIP whose members sit under one folder. That is the
        household export, not another app's: it must not be sent to the
        transactions-CSV import, which would pour every account's history
        into the one account picked."""
        tid, _ = _seed_tenant()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.sync import export
            data = export.build_zip(conn)
        finally:
            conn.close()
        src = zipfile.ZipFile(io.BytesIO(data))
        members = {f"oikonome-export-20260301/{n}": src.read(n)
                   for n in src.namelist()}
        members["__MACOSX/oikonome-export-20260301/._accounts.csv"] = b"x"
        members["oikonome-export-20260301/.DS_Store"] = b"x"
        nested = self._zip(members)
        refusal = restore.foreign_archive_refusal(nested)
        self.assertIsNotNone(refusal)
        self.assertIn("zip the files themselves", refusal)
        self.assertNotIn("transactions CSV", refusal)
        tid2, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(tid2)
        try:
            with self.assertRaises(ValueError) as e:
                restore.restore_zip(conn, nested)
            self.assertIn("zip the files themselves", str(e.exception))
        finally:
            conn.close()
        # a budget app's folder of CSVs is still another app's export
        other = self._zip({"plan/Register.csv": "Account,Date,Payee\n"})
        self.assertIn("isn't an Oikonome export",
                      restore.foreign_archive_refusal(other))

    def test_every_member_a_restore_reads_marks_the_export(self):
        import re
        src = open(restore.__file__, encoding="utf-8").read()
        read = set(re.findall(r'"([a-z_0-9]+\.csv)"', src))
        self.assertEqual(read - {"transactions.csv"},
                         set(restore._EXPORT_MEMBERS))

    def test_the_folder_plan_says_so_before_any_step_up(self):
        from oikonome.web import bulk_import
        data = self._zip({"Plan - Register.csv": "Account,Date,Payee\n"})
        tid, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(tid)
        try:
            plan = bulk_import.analyze(conn, [("budget.zip", data)])
        finally:
            conn.close()
        entry = plan["files"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("isn't an Oikonome export", entry["note"])

if __name__ == "__main__":
    unittest.main()
