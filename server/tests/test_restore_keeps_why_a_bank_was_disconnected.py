"""A restored disconnected bank still knows why, and since when.

`archived_reason` and `archived_at` are not decoration. The reason is what
refuses a reconnect that would bring a bank's accounts in twice (a newer
link adopted them) or bring back an account the owner deleted, what lets a
new bridge token revive only the banks that went down with the bridge, and
what the Accounts page and alerts read to explain a disconnect. The date is
where a returning bank's catch-up pull starts. An archive that dropped them
would restore every disconnected bank as an ordinary one: the reconnect
doors stop refusing, and the catch-up starts a week ago instead of at the
disconnect.

Only rows that restore as disconnected carry them; a live connection's
restored shell has no business holding a disconnect reason.
"""

import datetime as dt
import unittest

from oikonome.sync import export, restore

from .util import make_db

GONE = dt.datetime(2026, 3, 2, 9, 30, tzinfo=dt.timezone.utc)


class RestoreKeepsDisconnectFactsTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        self.dest = make_db()
        self.addCleanup(self.src.close)
        self.addCleanup(self.dest.close)
        for iid, status, reason, at in (
                ("sfin:adopted", "archived",
                 "reconnected — accounts adopted by a new link", GONE),
                ("sfin:purged", "archived", "account-purged", GONE),
                ("sfin:bridge", "archived", "bridge-disconnect", GONE),
                ("live-bank", "ok", "stale-reason", GONE)):
            self.src.execute(
                "INSERT INTO items (id, aggregator, institution_name, status, "
                "archived_reason, archived_at, linked_at) "
                "VALUES (%s, 'simplefin-org', 'Juniper Credit Union', %s, %s, "
                "%s, %s)", (iid, status, reason, at, GONE))
        restore.restore_zip(self.dest, export.build_zip(self.src))

    def _item(self, iid):
        return self.dest.execute(
            "SELECT status, archived_reason, archived_at, linked_at "
            "FROM items WHERE id=%s", (iid,)).fetchone()

    def test_a_disconnected_bank_keeps_its_reason_and_date(self):
        for iid, reason in (
                ("sfin:adopted",
                 "reconnected — accounts adopted by a new link"),
                ("sfin:purged", "account-purged"),
                ("sfin:bridge", "bridge-disconnect")):
            row = self._item(iid)
            self.assertEqual(row["status"], "archived")
            self.assertEqual(row["archived_reason"], reason, iid)
            self.assertEqual(row["archived_at"], GONE, iid)

    def test_a_live_connection_restores_without_a_disconnect_reason(self):
        row = self._item("live-bank")
        self.assertEqual(row["status"], "restored")
        self.assertIsNone(row["archived_reason"])
        self.assertIsNone(row["archived_at"])

    def test_when_the_bank_was_linked_survives(self):
        self.assertEqual(self._item("sfin:adopted")["linked_at"], GONE)
        self.assertEqual(self._item("live-bank")["linked_at"], GONE)

    def test_an_archive_without_the_columns_still_restores(self):
        """An older archive lacks them; the bank restores, merely unmarked."""
        import csv
        import io
        import zipfile
        data = export.build_zip(self.src)
        zin = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zout:
            for n in zin.namelist():
                body = zin.read(n)
                if n == "items.csv":
                    rows = list(csv.DictReader(io.StringIO(body.decode())))
                    keep = [c for c in rows[0] if c not in (
                        "archived_reason", "archived_at", "linked_at")]
                    s = io.StringIO()
                    w = csv.DictWriter(s, fieldnames=keep,
                                       extrasaction="ignore")
                    w.writeheader()
                    w.writerows(rows)
                    body = s.getvalue().encode()
                zout.writestr(n, body)
        fresh = make_db()
        try:
            restore.restore_zip(fresh, out.getvalue())
            row = fresh.execute(
                "SELECT status, archived_reason, archived_at, linked_at "
                "FROM items WHERE id='sfin:purged'").fetchone()
            self.assertEqual(row["status"], "archived")
            self.assertIsNone(row["archived_reason"])
            self.assertIsNotNone(row["linked_at"])
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main()
