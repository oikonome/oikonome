"""The export ZIP's README says which files a restore loads back.

The export writes every RLS-scoped table; the restore reads a fixed
subset and silently ignores the rest (alerts, sync logs, staged uploads —
this instance's operating state, not the household's data). Unless the
README says so, a file the person can see in the ZIP simply "does not
restore".
"""

import io
import pathlib
import re
import unittest
import zipfile

from oikonome.sync import export, restore

from .util import make_db, write_config

APP = pathlib.Path(__file__).resolve().parents[2]


class ReadmeTests(unittest.TestCase):
    def test_restored_members_is_exactly_what_the_restore_reads(self):
        src = (APP / "server/oikonome/sync/restore.py").read_text()
        read = set(re.findall(r'_rows\(z, "([a-z_0-9]+)\.csv"\)', src))
        # members read under a VARIABLE (bills.csv / recurring.csv by
        # archive age): every `.csv` literal on a line that assigns a
        # `*_member` name counts too — a literal-only scan misses bills,
        # and the README then calls the household's bills records-only
        for line in src.splitlines():
            if re.search(r"\b[a-z_]+_member\s*=|^\s+else \"[a-z_]+\.csv\"", line):
                read |= set(re.findall(r'"([a-z_0-9]+)\.csv"', line))
        self.assertEqual(read, set(restore.RESTORED_MEMBERS))
        self.assertIn("bills", restore.RESTORED_MEMBERS)

    def test_readme_marks_the_records_only_files(self):
        conn = make_db()
        try:
            write_config(conn)
            data = export.build_zip(conn)
        finally:
            conn.close()
        z = zipfile.ZipFile(io.BytesIO(data))
        readme = z.read("README.txt").decode()
        members = {n[:-4] for n in z.namelist() if n.endswith(".csv")}
        only = members - set(restore.RESTORED_MEMBERS)
        self.assertTrue(only, "export writes nothing the restore skips?")
        for t in only:
            line = next(l for l in readme.splitlines()
                        if l.strip().startswith(f"{t}.csv"))
            self.assertIn("(records only)", line, t)
        for t in members & set(restore.RESTORED_MEMBERS):
            line = next(l for l in readme.splitlines()
                        if l.strip().startswith(f"{t}.csv"))
            self.assertNotIn("(records only)", line, t)
