"""A NUL in a key of an archived JSON cell costs only that value.

A key holding U+0000 is never rewritten (compat.strip_nul: a check that
compares keys exactly must never see one spelling while the store keeps
another), and no export writes one. In an archive cell the document is
therefore malformed: the value lands as an empty cell would (a
transaction's `raw` as an empty document, the settings document as no
settings at all), every other row lands, and the summary names the file
and the column. A file that is no export at all is still refused.
"""

import csv
import io
import json
import unittest
import zipfile

from oikonome.sync import restore

from .util import TODAY, make_db, write_config

NUL_KEY = '{"a\\u0000": 1, "memo": "kept?"}'


def _csv(cols, rows) -> str:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return s.getvalue()


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


def _ledger(raw_bad: str) -> str:
    cols = ["id", "account_id", "date", "amount", "name", "raw"]
    return _csv(cols, [
        {"id": "nk-1", "account_id": "chk", "date": TODAY.isoformat(),
         "amount": "12.34", "name": "CORNER BAKERY", "raw": raw_bad},
        {"id": "nk-2", "account_id": "chk", "date": TODAY.isoformat(),
         "amount": "56.78", "name": "HARDWARE PLACE",
         "raw": '{"memo": "fine"}'},
    ])


class ANulInAJsonKeyCostsOnlyThatValue(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=432)

    def tearDown(self):
        self.conn.close()

    def _raws(self):
        return {r["id"]: r["raw"] for r in self.conn.execute(
            "SELECT id, raw FROM transactions WHERE id LIKE 'nk-%'")}

    def test_a_ledger_cell_with_a_nul_key_lands_its_row_without_it(self):
        got = restore.restore_zip(self.conn, _zip(
            {"transactions.csv": _ledger(NUL_KEY)}))
        raws = self._raws()
        self.assertEqual(set(raws), {"nk-1", "nk-2"})
        self.assertEqual(raws["nk-2"], {"memo": "fine"})
        self.assertNotIn("a", raws["nk-1"])
        self.assertNotIn("memo", raws["nk-1"])
        notes = got.get("_notes") or []
        self.assertTrue(any("transactions.csv" in n and "column raw" in n
                            and "NUL" in n for n in notes), notes)

    def test_a_settings_document_with_a_nul_key_is_left_out_and_the_rest_lands(self):
        before = dict(self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"])
        cfg = json.dumps({"food_monthly" + "\x00": 999, "food_monthly": 999})
        got = restore.restore_zip(self.conn, _zip({
            "tenant_settings.csv": _csv(["config"], [{"config": cfg}]),
            "transactions.csv": _ledger('{"memo": "also fine"}')}))
        self.assertEqual(set(self._raws()), {"nk-1", "nk-2"})
        after = dict(self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"])
        self.assertEqual(after, before)
        notes = got.get("_notes") or []
        self.assertTrue(any("tenant_settings.csv" in n and "column config" in n
                            for n in notes), notes)

    def test_a_second_restore_of_it_changes_nothing(self):
        data = _zip({"transactions.csv": _ledger(NUL_KEY)})
        restore.restore_zip(self.conn, data)
        first = self._raws()
        again = restore.restore_zip(self.conn, data)
        self.assertEqual(self._raws(), first)
        self.assertEqual(again.get("transactions", 0), 0)

    def test_a_file_that_is_no_export_is_still_refused(self):
        deep = '{"a":' * 5000 + "1" + "}" * 5000
        with self.assertRaises(ValueError) as e:
            restore.restore_zip(self.conn, _zip({
                "tenant_settings.csv": _csv(["config"], [{"config": deep}]),
                "transactions.csv": _ledger("{}")}))
        self.assertIn("genuine Oikonome export", str(e.exception))
        self.assertEqual(self._raws(), {})

    def test_the_bookkeeping_never_grows_with_the_number_of_cells(self):
        # every cell spells the escape, so a map keyed by cell text grows
        # with the archive; the drop it serves names a (file, column) and
        # needs nothing past the row being landed
        cols = ["id", "account_id", "date", "amount", "name", "raw"]
        rows = [{"id": f"nk-{i}", "account_id": "chk",
                 "date": TODAY.isoformat(), "amount": "1.00",
                 "name": f"PLACE \\u0000 {i}",
                 "raw": json.dumps({"memo": f"\\u0000 {i}"})}
                for i in range(600)]
        rows[300]["raw"] = NUL_KEY
        seen = []
        notes_of = restore._nul_key_notes

        def spy():
            cells = restore._NUL_KEY_CELLS.get() or {}
            sample = getattr(restore, "_NUL_KEY_SAMPLE", None)
            seen.append((sum(len(v) if isinstance(v, dict) else 1
                             for v in cells.values()),
                         len((sample.get() if sample else None) or {})))
            return notes_of()
        restore._nul_key_notes = spy
        try:
            got = restore.restore_zip(self.conn, _zip(
                {"transactions.csv": _csv(cols, rows)}))
        finally:
            restore._nul_key_notes = notes_of
        self.assertEqual(len(self._raws()), 600)
        self.assertTrue(seen)
        per_row, sampled = max(seen)
        self.assertLessEqual(per_row, len(cols), seen)
        self.assertLessEqual(sampled, 64, seen)
        notes = got.get("_notes") or []
        self.assertTrue(any("transactions.csv" in n and "column raw" in n
                            and "NUL" in n for n in notes), notes)

    def test_a_member_read_whole_before_it_lands_still_names_the_column(self):
        # the rename journal is sorted before a row lands, so the cell _j
        # is handed is no longer the row last read
        cols = ["id", "raw_merchant", "from_canonical", "to_canonical",
                "at", "rules"]
        got = restore.restore_zip(self.conn, _zip({
            "merchant_renames.csv": _csv(cols, [
                {"id": "2", "raw_merchant": "CORNER BAKERY",
                 "to_canonical": "Corner Bakery",
                 "at": "2026-01-02T00:00:00", "rules": NUL_KEY},
                {"id": "1", "raw_merchant": "HARDWARE PLACE",
                 "to_canonical": "Hardware Place",
                 "at": "2026-01-01T00:00:00", "rules": "{}"}]),
            "transactions.csv": _ledger("{}")}))
        notes = got.get("_notes") or []
        self.assertTrue(any("merchant_renames.csv" in n
                            and "column rules" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
