"""Restore-ZIP guards.

* Without an uncompressed-size cap a zip bomb (tiny file, huge declared
expansion) runs unbounded; the caps are checked against the ZIP directory's
declared sizes BEFORE anything is read, and a crafted directory claiming
huge sizes is refused with a friendly error and zero rows landed.
* A config merge that takes the ZIP's config wholesale (minus secrets)
wipes destination-only keys added since the export; keys present only in
the destination survive unless the ZIP explicitly carries a replacement.
"""

import io
import struct
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore

from .util import make_db, write_config


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _claim_huge(data: bytes, huge: int) -> bytes:
    """Rewrite every central-directory entry's uncompressed-size field so
    the ZIP *claims* to expand to `huge` bytes per member while staying a
    few hundred bytes on the wire — the guarded zip-bomb shape."""
    out = bytearray(data)
    pos = 0
    while True:
        pos = out.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        # central file header: uncompressed size is 4 bytes at offset 24
        out[pos + 24:pos + 28] = struct.pack("<I", huge)
        pos += 4
    return bytes(out)


class ZipBombTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_zip_claiming_huge_total_is_refused_before_extraction(self):
        # 5 members x 110MB claimed — each under the per-file cap, the
        # sum well over the 512MB total cap
        data = _claim_huge(_zip({
            f"part{i}.csv": "id,date,amount,name\n" for i in range(5)}),
            110 * 1024 * 1024)
        with self.assertRaises(ValueError) as e:
            restore.restore_zip(self.conn, data)
        self.assertIn("512", str(e.exception))

    def test_zip_claiming_huge_member_is_refused(self):
        data = _claim_huge(_zip({"transactions.csv": "id\n"}),
                           200 * 1024 * 1024)   # one member over the 128MB cap
        with self.assertRaises(ValueError) as e:
            restore.restore_zip(self.conn, data)
        self.assertIn("large", str(e.exception).lower())

    def test_refusal_lands_no_rows(self):
        data = _claim_huge(_zip({
            "transactions.csv": ("id,account_id,date,amount,name\n"
                                 "bomb-1,chk,2026-07-01,12.5,X\n"),
            "pad.csv": "x\n"}), 300 * 1024 * 1024)
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, data)
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM transactions WHERE id='bomb-1'").fetchone())

    def test_ordinary_export_is_untouched_by_the_caps(self):
        counts = restore.restore_zip(self.conn, _zip({
            "transactions.csv": ("id,account_id,date,amount,name\n"
                                 "ok-1,chk,2026-07-01,12.5,OK\n")}))
        self.assertEqual(counts.get("transactions"), 1)


class ConfigMergeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _restore_cfg(self, cfg_json: str):
        restore.restore_zip(self.conn, _zip({
            "tenant_settings.csv": f'config\n"{cfg_json}"\n'}))
        return budget.load_config(self.conn)

    def test_destination_only_keys_survive_a_restore(self):
        # keys added since the export was taken must not be wiped
        budget.save_config(self.conn, {
            "food_monthly": 1000, "other_monthly": 900,
            "envelope_months": 12, "theme": "dark"})
        got = self._restore_cfg('{""food_monthly"": 750}')
        self.assertEqual(got["food_monthly"], 750)       # ZIP replacement wins
        self.assertEqual(got["other_monthly"], 900)      # destination-only kept
        self.assertEqual(got["envelope_months"], 12)
        self.assertEqual(got["theme"], "dark")

    def test_zip_values_still_replace_shared_keys(self):
        budget.save_config(self.conn, {"food_monthly": 1000,
                                       "other_monthly": 900})
        got = self._restore_cfg(
            '{""food_monthly"": 1, ""other_monthly"": 2}')
        self.assertEqual((got["food_monthly"], got["other_monthly"]), (1, 2))

    def test_secrets_handling_unchanged(self):
        # destination secrets kept; crafted ZIP secrets never land
        budget.save_config(self.conn, {"llm_api_key": "enc:keep",
                                       "food_monthly": 1000})
        got = self._restore_cfg(
            '{""food_monthly"": 5, ""llm_api_key"": ""enc:attacker"", '
            '""simplefin_access_url"": ""http://evil""}')
        self.assertEqual(got["llm_api_key"], "enc:keep")
        self.assertNotIn("simplefin_access_url", got)
        self.assertEqual(got["food_monthly"], 5)


class LinkDiesWithItsAccount(unittest.TestCase):
    """The restore refuses to plant an account_link whose account is not
    there; deleting an account must honour the same rule from the other
    direction.

    A stale link keeps shadow-excluding its account from every money
    aggregate, and collector account ids are derived from the plan/label —
    so an unlink followed by a re-import reproduces the id and the orphan
    re-attaches to a brand-new account nobody linked. The money simply
    stops being counted, with nothing to say why. Only purge_account_data
    cleaned links up by hand; the collector rollbacks issue a bare DELETE
    FROM accounts, so the constraint has to carry it."""

    def test_deleting_an_account_takes_its_link_with_it(self):
        conn = make_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('sav','it1','Test Savings','depository','savings',900)")
        from oikonome.engine import links
        links.create(conn, ["chk", "sav"])

        # a collector rollback: the account goes, nothing tidies up after it
        conn.execute("DELETE FROM transactions WHERE account_id='sav'")
        conn.execute("DELETE FROM accounts WHERE id='sav'")

        left = [r["account_id"] for r in conn.execute(
            "SELECT account_id FROM account_links").fetchall()]
        self.assertEqual(left, ["chk"])


if __name__ == "__main__":
    unittest.main()
