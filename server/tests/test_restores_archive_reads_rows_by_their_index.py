"""Whether a plan restores an archive is read from the staged file each
decision names by its index, never by its position in what is left.

Read by position, a stage with a row missing shifts every later file down
one: the decision naming the archive points past the end, no step-up is
asked, and the archive is restored anyway when the plan runs."""

import unittest

from oikonome.db import staging
from oikonome.web import bulk_import

from .util import make_db, write_config


class RestoresArchiveByIndexTests(unittest.TestCase):
    def test_a_missing_row_does_not_hide_the_archive(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                ("a.csv", b"Date,Name,Amount\n"), ("b.csv", b"x"),
                ("c.csv", b"y"), ("export.zip", b"PK\x05\x06" + b"\0" * 18)])
            conn.execute("DELETE FROM import_staging WHERE token = %s"
                         " AND idx = 0", (token,))
            self.assertTrue(bulk_import.restores_archive(
                conn, token, [{"index": 3, "action": "import"}],
                role="owner"))
            self.assertFalse(bulk_import.restores_archive(
                conn, token, [{"index": 2, "action": "import"}],
                role="owner"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
