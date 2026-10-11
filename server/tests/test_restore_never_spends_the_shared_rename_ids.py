"""A restore cannot move the merchant-rename id counter every household shares.

The rename journal's ids come from one sequence for the whole instance.
An archive is the household's own file, so any id in it can be anything;
keeping those ids and advancing the sequence past them let one archive
push the shared counter to the edge of its range, after which every
household's next rename failed. The journal's ids are its own business
order, not identities anything else points at, so a restore gives each
row a fresh id in the archive's order and leaves the counter where its
own inserts put it.
"""
import io
import unittest
import zipfile

from oikonome.sync import restore

from .test_restore import _fresh_tenant

HUGE_ID = 2 ** 63 - 1024


def _archive(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("merchant_renames.csv",
                   "id,raw_merchant,from_canonical,to_canonical,at\n"
                   + "".join(f"{i},{raw},,{to},{at}\n"
                             for i, raw, to, at in rows))
    return buf.getvalue()


class RenameIdsTests(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_tenant()
        self.addCleanup(self.conn.close)

    def _last(self):
        return self.conn.execute(
            "SELECT last_value FROM merchant_renames_id_seq"
        ).fetchone()["last_value"]

    def _journal(self):
        return [(r["raw_merchant"], r["to_canonical"]) for r in
                self.conn.execute("SELECT raw_merchant, to_canonical"
                                  " FROM merchant_renames ORDER BY id")]

    def test_an_archive_id_cannot_push_the_shared_counter(self):
        before = self._last()
        restore.restore_zip(self.conn, _archive([
            (HUGE_ID, "KIOSK 1", "Kiosk", "2026-01-02 10:00:00+00"),
            (7, "KIOSK 2", "Kiosk Two", "2026-01-01 10:00:00+00")]))
        self.assertLessEqual(self._last(), before + 2 + 1)
        # the archive's order survives the new ids
        self.assertEqual(self._journal(),
                         [("KIOSK 2", "Kiosk Two"), ("KIOSK 1", "Kiosk")])
        # and a rename after the restore still gets an id
        self.conn.execute("INSERT INTO merchant_renames (raw_merchant,"
                          " to_canonical) VALUES ('KIOSK 3', 'Kiosk Three')")

    def test_restoring_the_same_archive_twice_keeps_one_journal(self):
        data = _archive([(3, "KIOSK 1", "Kiosk", "2026-01-02 10:00:00+00")])
        restore.restore_zip(self.conn, data)
        restore.restore_zip(self.conn, data)
        self.assertEqual(self._journal(), [("KIOSK 1", "Kiosk")])


if __name__ == "__main__":
    unittest.main()
