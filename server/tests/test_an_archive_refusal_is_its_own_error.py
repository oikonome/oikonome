"""Every rule that refuses a whole restore archive raises the restore's own
archive-refusal error, never a bare ValueError.

A bare ValueError is also what Python's own parsers raise for one odd
cell, so a reader that wants to cost such a cell only its row has to be
able to tell the two apart: the refusal must keep refusing the archive
(nothing lands, the restore is one transaction), and the odd cell must
not. The refusal stays a ValueError, so every caller that reports a
refused archive to the person keeps doing so.
"""

import csv
import io
import unittest
import zipfile

from oikonome.sync import restore


def _zip(files: dict) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _read(z, name="bills.csv"):
    return list(restore._rows(z, name))


class EveryArchiveRefusalIsItsOwnError(unittest.TestCase):
    def refused(self, fn, *a):
        with self.assertRaises(restore._ArchiveRefused) as e:
            fn(*a)
        self.assertIsInstance(e.exception, ValueError)
        return str(e.exception)

    def test_a_non_finite_number(self):
        for v in ("nan", "inf", "-Infinity", "1e999"):
            self.refused(restore._f, v)
            self.refused(restore._numeric, v, "an amount", 14, 2)

    def test_text_where_a_number_belongs_is_the_rows_not_the_archives(self):
        with self.assertRaises(restore._NotANumber) as e:
            restore._f("twelve")
        self.assertNotIsInstance(e.exception, restore._ArchiveRefused)

    def test_a_record_longer_than_the_budget(self):
        reset = restore._MemberLines.reset
        old = reset.__defaults__
        reset.__defaults__ = (64,)
        self.addCleanup(setattr, reset, "__defaults__", old)
        self.refused(_read, _zip({"bills.csv": "id,name\n1," + "x" * 500
                                  + "\n"}))

    def test_a_record_with_too_many_separators(self):
        old = restore.MAX_RECORD_COMMAS
        restore.MAX_RECORD_COMMAS = 5
        self.addCleanup(setattr, restore, "MAX_RECORD_COMMAS", old)
        self.refused(_read, _zip({"bills.csv": "id,name\n" + "," * 20
                                  + "\n"}))

    def test_a_header_wider_than_any_table(self):
        self.refused(_read, _zip({"bills.csv": ",".join(
            f"c{i}" for i in range(restore.MAX_COLUMNS + 1)) + "\n1\n"}))

    def test_a_row_far_wider_than_its_header(self):
        self.refused(_read, _zip({"bills.csv": "id,name\n" + ",".join(
            "x" for _ in range(restore.MAX_ROW_OVERFLOW + 5)) + "\n"}))

    def test_too_many_rows_in_one_member(self):
        old = restore.MAX_ROWS_PER_MEMBER
        restore.MAX_ROWS_PER_MEMBER = 3
        self.addCleanup(setattr, restore, "MAX_ROWS_PER_MEMBER", old)
        self.refused(_read, _zip({"bills.csv": "id\n" + "1\n" * 10}))

    def test_a_torn_csv(self):
        # a cell past the reader's field limit is csv's own error
        old = csv.field_size_limit(16)
        self.addCleanup(csv.field_size_limit, old)
        self.refused(_read, _zip({"bills.csv": "id,name\n1," + "x" * 64
                                  + "\n"}))

    def test_a_member_that_is_not_utf8(self):
        self.refused(_read, _zip({"bills.csv": b"id,name\n1,\xff\xfe\n"}))

    def test_an_ai_routing_that_is_not_a_map(self):
        self.refused(restore._check_config, {"llm_roles": ["bundled"]})

    def test_a_zip_that_expands_past_the_limit(self):
        info = zipfile.ZipInfo("bills.csv")
        info.file_size = restore.MAX_MEMBER_BYTES + 1
        info.compress_size = 1

        class Z:
            def infolist(self):
                return [info]
        self.refused(restore._check_zip_sizes, Z())

    def test_a_data_portability_archive(self):
        self.refused(restore._refuse_portability_archive,
                     _zip({"data/accounts.json": "[]"}))
        self.refused(restore._refuse_portability_archive, _zip({
            restore._upgrades.MANIFEST:
                '{"export_format": "oikonome-tenant-export/1"}',
            "accounts.csv": "id\n"}))

    def test_a_zip_that_is_no_oikonome_export(self):
        z = _zip({"Register.csv": "Date,Payee,Amount\n"})
        self.assertIsNotNone(restore.foreign_archive_refusal(z))
        self.refused(restore._restore_zip, None, z.fp.getvalue(),
                     restore._Outcomes())

    def test_the_stops_before_any_row_are_refusals_too(self):
        # an erased household and a signed-out starter end the restore
        # before a row is written, whatever a row's reader catches
        self.assertTrue(issubclass(restore.HouseholdBeingDeleted,
                                   restore._ArchiveRefused))
        self.assertTrue(issubclass(restore.StarterSignedOut,
                                   restore._ArchiveRefused))


if __name__ == "__main__":
    unittest.main()
