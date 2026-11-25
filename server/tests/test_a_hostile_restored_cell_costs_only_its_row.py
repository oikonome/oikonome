"""One hostile cell in a real export costs that row or that value, never
the archive, and never lands something no page can read
(specs/invariants-and-fuzzing.md, R3).

* Text where a number belongs (an amount, a 0/1 flag, a split's amount,
  a tax document's figure, a bill's amount or flag) refuses only its row
  as malformed, never the whole archive, and never with Python's own
  words ("could not convert string to float") for the message. A
  non-finite number still refuses the archive, as `restore._f` decides.
* A connection's `raw` nested past what the jsonb write can walk lands
  as an empty object, never as JSON null, which the column refuses.
* A JSON cell spelling half a surrogate pair ("\\ud800"), which
  PostgreSQL refuses in jsonb, reads as the replacement character.
* A member that is not UTF-8, and a settings document nested past the
  walk, are refused as a bad file, named, never with the codec's account
  of the byte or words that name no file.
* A pending bill offer with no kind or no amount is refused as
  malformed, so the Bills page never answers outside its documented
  shape. An answered one still lands, for its answer.
* A receipt with a status the app never writes lands as waiting to be
  read, inside the Receipts page's documented set.
* An account on a connection with no institution name is listed as
  "Other" in Net Worth's by-institution list, never as a nameless row.
"""

from __future__ import annotations

import unittest

from oikonome.sync import export, restore

from . import roundtrip_builders as rb
from .restore_invariants import (archived_rows, fresh_household,
                                 read_member, rewrite_member)


def _set_cell(column: str, value, row: int = 1):
    def edit(recs):
        recs = [list(r) for r in recs]
        recs[row][recs[0].index(column)] = value
        return recs
    return edit


class _Archive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = fresh_household()
        try:
            rb.build(src, 2)
            cls.data = export.build_zip(src)
        finally:
            src.close()

    def restore(self, data):
        conn = fresh_household()
        self.addCleanup(conn.close)
        _result, out = restore.restore_outcomes(conn, data)
        return conn, out

    def lands_but_one(self, member, column, value, *, group=False):
        """The row is refused as malformed (with the rest of its group,
        for a split: a split lands whole or not at all) and every other
        member lands as from the clean archive."""
        table = member[:-4]
        data = rewrite_member(self.data, member, _set_cell(column, value))
        conn, out = self.restore(data)
        more = (out.refused.get((table, "malformed"), 0)
                - self._clean_malformed(table))
        if group:
            self.assertGreaterEqual(more, 1, (member, column, out.refused))
        else:
            self.assertEqual(more, 1, (member, column, out.refused))
        # the restore finished, and every archived row has one outcome;
        # rows that hang on the refused one are orphaned, the rest land
        for t, n in archived_rows(data).items():
            if t in rb.CONTROL:
                continue
            refused = sum(v for (tt, _), v in out.refused.items() if tt == t)
            self.assertEqual(out.written.get(t, 0) + refused, n, t)
        self.assertEqual(out.written.get(table, 0),
                         self.clean_out().written.get(table, 0) - more)
        return conn, out

    def clean_out(self):
        if not hasattr(type(self), "_clean"):
            _conn, type(self)._clean = self.restore(self.data)
        return type(self)._clean

    def _clean_malformed(self, table):
        return self.clean_out().refused.get((table, "malformed"), 0)


class TextWhereANumberBelongsCostsItsRow(_Archive):

    def test_a_charge_amount(self):
        self.lands_but_one("transactions.csv", "amount", "maybe")

    def test_a_charge_flag(self):
        self.lands_but_one("transactions.csv", "removed", "+1-1")

    def test_a_bill_amount_and_flag(self):
        self.lands_but_one("bills.csv", "amount", "@SUM(A1)")
        self.lands_but_one("bills.csv", "active", "yes")

    def test_a_split_amount(self):
        self.lands_but_one("transaction_splits.csv", "amount", "twelve",
                           group=True)

    def test_a_tax_document_figure(self):
        self.lands_but_one("income_documents.csv", "primary_amount", "n/a")

    def test_a_balance_row(self):
        self.lands_but_one("networth_snapshot.csv", "total", "2026-02-30")

    def test_a_non_finite_number_still_refuses_the_archive(self):
        data = rewrite_member(self.data, "transactions.csv",
                              _set_cell("amount", "NaN"))
        conn = fresh_household()
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ValueError, "not finite"):
            restore.restore_outcomes(conn, data)


class JsonCellsTheWriteCannotTake(_Archive):

    def test_a_connection_raw_nested_too_deep_lands_empty(self):
        recs = read_member(self.data, "items.csv")
        item = recs[1][recs[0].index("id")]
        deep = '{"a":' * 5000 + "1" + "}" * 5000
        conn, out = self.restore(rewrite_member(
            self.data, "items.csv", _set_cell("raw", deep)))
        row = conn.execute("SELECT raw FROM items WHERE id = %s",
                           (item,)).fetchone()
        self.assertEqual(row["raw"], {})

    def test_half_a_surrogate_pair_reads_as_the_replacement_character(self):
        conn, out = self.restore(rewrite_member(
            self.data, "networth_snapshot.csv",
            _set_cell("by_class", '{"cash": 1.5, "note": "a\\ud800b"}')))
        notes = [r["by_class"].get("note") for r in conn.execute(
            "SELECT by_class FROM networth_snapshot").fetchall()
            if isinstance(r["by_class"], dict)]
        self.assertIn("a�b", notes)


class AMemberThatIsNotUtf8(_Archive):

    def test_is_refused_in_words(self):
        data = rewrite_member(self.data, "bills.csv",
                              lambda recs: b"id,payee\r\nx,\xed\xa0\x80\r\n")
        conn = fresh_household()
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError) as e:
            restore.restore_outcomes(conn, data)
        self.assertNotIsInstance(e.exception, UnicodeError)
        self.assertIn("bills.csv: it is not UTF-8 text", str(e.exception))

    def test_a_settings_document_nested_too_deep_names_the_file(self):
        deep = '{"a":' * 5000 + "1" + "}" * 5000
        data = rewrite_member(self.data, "tenant_settings.csv",
                              _set_cell("config", deep))
        conn = fresh_household()
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(
                ValueError, "tenant_settings.csv: the document is nested "
                            "too deeply .* genuine Oikonome export"):
            restore.restore_outcomes(conn, data)


class RowsNoPageCanRead(_Archive):

    def test_a_pending_bill_offer_without_a_kind_or_an_amount_is_refused(self):
        for column in ("kind", "amount"):
            with self.subTest(column=column):
                data = rewrite_member(self.data, "bill_proposals.csv",
                                      _set_cell("status", "pending"))
                cut = rewrite_member(data, "bill_proposals.csv",
                                     _set_cell(column, ""))
                _conn, whole = self.restore(data)
                _conn, out = self.restore(cut)
                self.assertEqual(
                    out.refused.get(("bill_proposals", "malformed"), 0)
                    - whole.refused.get(("bill_proposals", "malformed"), 0),
                    1, out.refused)

    def test_an_answered_offer_without_a_kind_still_lands(self):
        data = rewrite_member(self.data, "bill_proposals.csv",
                              _set_cell("status", "rejected"))
        _conn, whole = self.restore(data)
        _conn, out = self.restore(rewrite_member(
            data, "bill_proposals.csv", _set_cell("kind", "")))
        self.assertEqual(out.written.get("bill_proposals"),
                         whole.written.get("bill_proposals"))

    def test_a_receipt_status_the_app_never_writes_lands_as_waiting(self):
        recs = read_member(self.data, "receipts.csv")
        rid = recs[1][recs[0].index("id")]
        conn, _out = self.restore(rewrite_member(
            self.data, "receipts.csv", _set_cell("status", "2099-01-01")))
        self.assertEqual(conn.execute(
            "SELECT status FROM receipts WHERE id = %s", (rid,)
        ).fetchone()["status"], "uploaded")
        self.assertEqual(archived_rows(self.data)["receipts"],
                         self.clean_out().written["receipts"])


class AConnectionWithNoInstitutionName(unittest.TestCase):

    def test_is_listed_as_other_in_net_worth(self):
        from oikonome.engine import reporting

        from .util import make_db
        conn = make_db()
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO items (id, aggregator, institution_name)"
                     " VALUES ('it-anon', 'manual', NULL)")
        conn.execute("INSERT INTO accounts (id, item_id, name, type, subtype,"
                     " balance_current) VALUES ('anon', 'it-anon',"
                     " 'Test Cash', 'depository', 'checking', 125)")
        nw = reporting.compute_networth(conn, trend=False)
        labels = [k for k, _v in nw["by_institution"]]
        self.assertNotIn(None, labels)
        self.assertIn("Other", labels)


if __name__ == "__main__":
    unittest.main()
