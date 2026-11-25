"""A backup taken on a managed Postgres service restores into a stock server.

Some managed services stamp a column option of their own on the tables they
host, and pg_dump writes it out as `ALTER TABLE … ALTER COLUMN … SET
(pg_attver=…);`. The stock server refuses the option, so a restore that
feeds the dump through unchanged stops on the first such line and the
recovery fails. The option carries no data: restore leaves those lines out,
and leaves every other statement — including ordinary column options —
exactly as written.
"""

import gzip
import pathlib
import subprocess
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]

DUMP = """\
CREATE TABLE public.notes (id integer, body text);
ALTER TABLE ONLY public.notes ALTER COLUMN body SET (pg_attver=100000000001);
ALTER TABLE public.notes ALTER COLUMN id SET (pg_attver=12);
ALTER TABLE ONLY public.notes ALTER COLUMN body SET (n_distinct=5);
COPY public.notes (id, body) FROM stdin;
1\tALTER TABLE ONLY public.notes ALTER COLUMN body SET (pg_attver=1);
2\tplain
\\.
"""


class RestoreFromManagedServerTests(unittest.TestCase):
    def setUp(self):
        src = (APP / "oikonome.sh").read_text()
        start = src.index("dump_statements() {")
        self.fn = src[start:src.index("\n}\n", start) + 3]
        self.src = src

    def run_filter(self, text):
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "d.sql.gz"
            f.write_bytes(gzip.compress(text.encode()))
            r = subprocess.run(
                ["bash", "-c", self.fn + '\nset -o pipefail\ndump_statements "$1"',
                 "x", str(f)], capture_output=True, text=True)
        return r

    def test_the_vendor_option_lines_are_left_out_and_nothing_else(self):
        r = self.run_filter(DUMP)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.splitlines()
        want = [ln for i, ln in enumerate(DUMP.splitlines()) if i not in (1, 2)]
        self.assertEqual(out, want)
        # a data row that merely contains the words is data, and stays
        self.assertIn("1\tALTER TABLE ONLY public.notes ALTER COLUMN body SET (pg_attver=1);", out)
        self.assertIn("ALTER TABLE ONLY public.notes ALTER COLUMN body SET (n_distinct=5);", out)

    def test_both_loads_of_a_restore_read_the_dump_through_it(self):
        self.assertEqual(self.src.count("gunzip -c \"$file\" | pg_app_sql"), 0)
        self.assertEqual(self.src.count("gunzip -c \"$1\" | pg_app_sql"), 0)
        self.assertIn('dump_statements "$file" | pg_app_sql -q -v ON_ERROR_STOP=1', self.src)
        self.assertIn('dump_statements "$1" | pg_app_sql -q -v ON_ERROR_STOP=1', self.src)


if __name__ == "__main__":
    unittest.main()
