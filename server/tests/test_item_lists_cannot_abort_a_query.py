"""One order's line items cannot take out the household's ledger search.

`items_json` on an order or a receipt is a JSON array, and every reader
walks it with `jsonb_array_elements`. That function does not skip a row
holding an object or a number there — it raises, and the raise aborts the
whole statement. A ledger search reads every row in the household, so one
badly shaped item list blanks the search for all of them, identically on
every retry, with nothing the reader can do about it from the app.

The shape is defended at three depths, and each covers a hole the others
leave open:

  * the restore boundary coerces an item list to a list, so an archive
    written or hand-edited outside the app degrades to "no items" for one
    order instead of failing the restore or poisoning the ledger;
  * the schema pins the column to arrays, so a writer nobody has audited —
    a collector push, the next importer — cannot store the wrong shape at
    all, and every future reader of these tables inherits that;
  * the read path treats a non-array as empty, so a database that already
    contains a bad row still answers.

The last of those is what a household migrating from an older instance
depends on, which is why it is tested against a row the constraint would
now refuse.
"""

import csv
import io
import json
import re
import unittest
import zipfile
from pathlib import Path

import psycopg

from oikonome.db import tenancy
from oikonome.sync import restore
from oikonome.web import data

from . import util
from .util import add_txn, make_db

AMAZON_COLS = ["dedup_key", "account", "date", "amount", "payee", "seller",
               "memo", "category", "category_source", "order_number",
               "is_refund", "payment_method", "items_json", "inserted_at"]
COSTCO_COLS = ["dedup_key", "account", "date", "amount", "receipt_type",
               "warehouse", "category", "category_source", "summary",
               "is_refund", "payment_method", "items_json", "inserted_at"]
MATCH_COLS = ["transaction_id", "dedup_key", "matched_at"]

ITEM_TABLES = (("amazon_orders", "amazon_orders_items_json_is_array"),
               ("costco_receipts", "costco_receipts_items_json_is_array"))


# --- reading the server's SQL --------------------------------------------
#
# Handing a JSONB value to `jsonb_array_elements` or `jsonb_array_length` is
# only safe when something PROVES it is an array first, and there are
# exactly two proofs: a CHECK constraint pinning the column, or the query
# testing `jsonb_typeof` before it reads. The scanner below finds every
# array read in the server and names the column behind it, so a new one has
# to pick one of the two.
#
# It reads whatever expression the call was handed, however wrapped. The
# first version matched only a BARE `alias.col`, which meant that wrapping
# the argument — in `COALESCE(alias.col, '[]'::jsonb)`, the idiom that looks
# like a shape guard and is not one — took the call site OUT of the check
# entirely. Three reads in three modules were invisible to it that way, and
# the narrowness, not any of the three, was the real defect: a guard that a
# wrapper defeats reports a clean tree while the class it exists to prevent
# spreads.

_ARRAY_READ = re.compile(r"\bjsonb_array_(?:elements(?:_text)?|length)\s*\(")
# calls whose result is an array by construction, plus the shape guard
# itself — an argument built out of these has already proven its shape
_PROVEN_CALL = re.compile(
    r"\b(?:jsonb_build_array|jsonb_agg|json_agg|array_to_json|_as_array)\s*\(")
# a value pulled out of a document by path. No column constraint can reach
# inside a JSONB value, so a path read has only the in-query proof available
_JSONB_PATH = re.compile(r"\s*(?:->>?|#>>?)")
_SQL_NOISE = frozenset("""
    all and any array as asc between by case cast coalesce cross desc
    distinct else end exists false from full group having in inner interval
    is join lateral left like not null nullif numeric on or order outer
    right select text then true union when where with date double precision
    jsonb json jsonb_typeof
""".split())


def _balanced(text: str, open_at: int) -> str:
    """What sits between `text[open_at]`, which is '(', and its match."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    return text[open_at + 1:]


def _drop_proven_calls(expr: str) -> str:
    """Remove every already-an-array call, arguments and all, so what is
    left is the part of the expression still owing a proof."""
    while True:
        m = _PROVEN_CALL.search(expr)
        if not m:
            return expr
        opened = m.end() - 1
        inner = _balanced(expr, opened)
        expr = expr[:m.start()] + " " + expr[opened + len(inner) + 2:]


def _strip_comments(text: str) -> str:
    """Prose must not be mistaken for column names: drop SQL `--` comments
    and any line that is entirely a Python comment. A `#` mid-line is left
    alone — that is where the JSONB path operators live."""
    out = []
    for line in text.splitlines():
        out.append("" if line.lstrip().startswith("#")
                   else re.sub(r"--.*$", "", line))
    return "\n".join(out)


def _unproven_reads(arg: str) -> set[tuple[str, bool]]:
    """(name, is_path) for every value this argument reads as an array with
    nothing establishing the shape. `is_path` marks a read INTO a document,
    which a column constraint can never satisfy."""
    rest = _drop_proven_calls(arg)
    if "jsonb_typeof" in rest:            # a hand-written CASE guard
        return set()
    rest = rest.replace('"""', " ")
    rest = re.sub(r"'[^']*'|\"[^\"]*\"", " ", rest)   # SQL + Python literals
    found = set()
    for ref in re.finditer(r"(?<![\w.%])([A-Za-z_]\w*)(?:\s*\.\s*(\w+))?",
                           rest):
        head, tail = ref.group(1), ref.group(2)
        after = rest[ref.end():]
        if after.lstrip().startswith("("):            # a function, not a value
            continue
        if tail is None and head.lower() in _SQL_NOISE:
            continue
        name = f"{head}.{tail}" if tail else head
        found.add((name, bool(_JSONB_PATH.match(after))))
    return found


def _array_read_arguments(text: str):
    """Every argument handed to an array read in this source text."""
    text = _strip_comments(text)
    for m in _ARRAY_READ.finditer(text):
        yield _balanced(text, m.end() - 1)


def _csv(cols: list[str], rows: list[dict]) -> str:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({**{c: "" for c in cols}, **r})
    return s.getvalue()


def _archive(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


class RestoredItemListShapes(unittest.TestCase):
    """An archive whose item lists are not lists still restores, and the
    search still answers."""

    def setUp(self):
        self.conn = make_db()
        self.t_obj = add_txn(self.conn, "2026-08-01", 40.00, "Amazon Object")
        self.t_num = add_txn(self.conn, "2026-08-02", 25.00, "Amazon Number")
        self.t_ok = add_txn(self.conn, "2026-08-03", 60.00, "Amazon Good")
        self.c_obj = add_txn(self.conn, "2026-08-04", 90.00, "Costco Object")
        self.c_ok = add_txn(self.conn, "2026-08-05", 30.00, "Costco Good")

    def tearDown(self):
        self.conn.close()

    def _restore_mixed_shapes(self):
        orders = [
            {"dedup_key": "az-obj", "date": "2026-08-01", "amount": "40",
             "payee": "Amazon", "is_refund": "0",
             "items_json": json.dumps({"title": "widget sprocket"})},
            {"dedup_key": "az-num", "date": "2026-08-02", "amount": "25",
             "payee": "Amazon", "is_refund": "0", "items_json": "5"},
            {"dedup_key": "az-ok", "date": "2026-08-03", "amount": "60",
             "payee": "Amazon", "is_refund": "0",
             "items_json": json.dumps([{"title": "widget sprocket",
                                        "price": "60.00"}])},
        ]
        receipts = [
            {"dedup_key": "co-obj", "date": "2026-08-04", "amount": "90",
             "receipt_type": "warehouse", "is_refund": "0",
             "items_json": json.dumps({"title": "widget crate"})},
            {"dedup_key": "co-ok", "date": "2026-08-05", "amount": "30",
             "receipt_type": "warehouse", "is_refund": "0",
             "items_json": json.dumps([{"title": "widget crate",
                                        "description": "bulk widget"}])},
        ]
        restore.restore_zip(self.conn, _archive({
            "amazon_orders.csv": _csv(AMAZON_COLS, orders),
            "amazon_matches.csv": _csv(MATCH_COLS, [
                {"transaction_id": self.t_obj, "dedup_key": "az-obj"},
                {"transaction_id": self.t_num, "dedup_key": "az-num"},
                {"transaction_id": self.t_ok, "dedup_key": "az-ok"}]),
            "costco_receipts.csv": _csv(COSTCO_COLS, receipts),
            "costco_matches.csv": _csv(MATCH_COLS, [
                {"transaction_id": self.c_obj, "dedup_key": "co-obj"},
                {"transaction_id": self.c_ok, "dedup_key": "co-ok"}]),
        }))

    def test_every_restored_item_list_is_an_array(self):
        self._restore_mixed_shapes()
        for table, _ in ITEM_TABLES:
            for row in self.conn.execute(
                    f"SELECT dedup_key, items_json FROM {table}").fetchall():
                self.assertIsInstance(row["items_json"], list,
                                      f"{table}/{row['dedup_key']}")

    def test_a_malformed_item_list_does_not_blank_the_ledger_search(self):
        self._restore_mixed_shapes()
        rows, total, _amount, az_items, _spend, _hits = \
            data.search_transactions(self.conn, "widget")
        # the two orders whose items really are items are found by them
        found = {r["id"] for r in rows}
        self.assertIn(self.t_ok, found)
        self.assertIn(self.c_ok, found)
        self.assertEqual(total, len(rows))
        # and the per-item aggregate still answers rather than raising
        self.assertEqual(az_items["count"], 1)

    def test_the_orders_themselves_survive_with_no_items(self):
        self._restore_mixed_shapes()
        # the row keeps everything else it says about itself; only the
        # unreadable item list is gone
        row = self.conn.execute(
            "SELECT amount, payee, items_json FROM amazon_orders "
            "WHERE dedup_key = 'az-obj'").fetchone()
        self.assertEqual(row["payee"], "Amazon")
        self.assertEqual(row["amount"], 40.0)
        self.assertEqual(row["items_json"], [])

    def test_the_shape_check_is_not_the_falsy_check(self):
        # `x or []` looks like this check and is not: it substitutes on a
        # falsy value and waves an object or a number straight through
        self.assertEqual(restore._json_list({"title": "x"}), [])
        self.assertEqual(restore._json_list(5), [])
        self.assertEqual(restore._json_list("[]"), [])
        self.assertEqual(restore._json_list(None), [])
        self.assertEqual(restore._json_list([{"title": "x"}]),
                         [{"title": "x"}])


class ADatabaseThatAlreadyHoldsABadRow(unittest.TestCase):
    """A household whose ledger was poisoned before the column was pinned
    still gets an answer — the read path never assumes the constraint."""

    CHECK = "amazon_orders_items_json_is_array"

    def setUp(self):
        self.conn = make_db()
        self.tenant_id = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.txn = add_txn(self.conn, "2026-08-01", 40.00, "Legacy Order")
        self.admin = tenancy.admin_connect(util._admin_dsn(util.TEST_DB))
        # put back exactly what was there, so this test cannot hand the
        # rest of the module a schema they did not ask for
        self.pinned = bool(self.admin.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s",
            (self.CHECK,)).fetchone())

    def tearDown(self):
        try:
            self.admin.execute(
                "DELETE FROM amazon_orders WHERE dedup_key = 'legacy-bad'")
            if self.pinned:
                self.admin.execute(
                    f"ALTER TABLE amazon_orders ADD CONSTRAINT {self.CHECK} "
                    "CHECK (jsonb_typeof(items_json) = 'array')")
        finally:
            self.admin.close()
            self.conn.close()

    def test_search_answers_over_a_non_array_items_json(self):
        # reach past the constraint the way an older instance's data does:
        # the row predates it
        if self.pinned:
            self.admin.execute("ALTER TABLE amazon_orders DROP CONSTRAINT "
                               f"{self.CHECK}")
        self.admin.execute(
            """INSERT INTO amazon_orders (tenant_id, dedup_key, account, date,
                   amount, is_refund, items_json)
               VALUES (%s, 'legacy-bad', 'main', DATE '2026-08-01', 40, 0,
                       %s::jsonb)""",
            (self.tenant_id, json.dumps({"title": "widget"})))
        self.admin.execute(
            """INSERT INTO amazon_matches (tenant_id, transaction_id,
                   dedup_key)
               VALUES (%s, %s, 'legacy-bad')""", (self.tenant_id, self.txn))
        rows, total, _amount, az_items, _spend, _hits = \
            data.search_transactions(self.conn, "widget")
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertIsNone(az_items)


class ItemListColumnsAreConstrained(unittest.TestCase):
    """The rule the next table with a list column inherits.

    Guarding each reader as it is written has been tried; a new table
    copies the write idiom and the readers it does not know about are the
    ones that break. Declaring '[]' as a column's default is the author
    saying the column is a list — so say it to the database too, and this
    test is what asks for it.
    """

    @classmethod
    def setUpClass(cls):
        util._ensure_db()
        cls.admin = tenancy.admin_connect(util._admin_dsn(util.TEST_DB))

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def _array_checked_columns(self) -> set[tuple[str, str]]:
        return {(r["table_name"], r["column_name"]) for r in self.admin.execute(
            """SELECT c.relname AS table_name, a.attname AS column_name
                 FROM pg_constraint k
                 JOIN pg_class c ON c.oid = k.conrelid
                 JOIN pg_attribute a ON a.attrelid = c.oid
                      AND a.attnum = ANY (k.conkey)
                WHERE k.contype = 'c'
                  AND pg_get_constraintdef(k.oid) LIKE %s""",
            ("%jsonb_typeof%array%",)).fetchall()}

    def test_a_jsonb_column_defaulting_to_an_empty_array_is_pinned_to_one(self):
        listy = {(r["table_name"], r["column_name"]) for r in self.admin.execute(
            """SELECT table_name, column_name
                 FROM information_schema.columns
                WHERE table_schema = 'public' AND data_type = 'jsonb'
                  AND column_default LIKE %s""",
            ("'[]'%",)).fetchall()}
        self.assertTrue(listy, "the rule has nothing to guard — has the "
                               "default moved?")
        self.assertEqual(listy - self._array_checked_columns(), set())

    def test_every_array_read_in_sql_proves_its_shape(self):
        """Two proofs are accepted and no third: the column carries an
        array CHECK, or the query tests jsonb_typeof before it reads.
        `COALESCE(x, '[]'::jsonb)` is neither — it substitutes for a
        missing value and waves the wrong shape straight through."""
        pinned = {col for _table, col in self._array_checked_columns()}
        seen = 0
        unproven = set()
        for path, arg in self._array_reads():
            seen += 1
            for name, is_path in _unproven_reads(arg):
                if is_path or name not in pinned:
                    unproven.add(f"{path.name}:{name}")
        self.assertTrue(seen, "no array reads found at all — has the "
                              "function been renamed?")
        self.assertEqual(unproven, set())

    def test_a_wrapped_argument_is_still_seen(self):
        """The hole this scanner replaced. Its predecessor matched a bare
        `alias.col` and nothing else, so every one of these read as clean
        while the value behind them was unproven."""
        for expr in ("COALESCE(t.raw->'companions','[]'::jsonb)",
                     "raw->'_batches'",
                     "COALESCE(t.raw->'_batches', "
                     "jsonb_build_array(t.raw->>'_batch'))"):
            with self.subTest(expr=expr):
                self.assertTrue(any(is_path for _n, is_path
                                    in _unproven_reads(expr)), expr)

    def test_a_proven_argument_is_accepted(self):
        # the guard helper, a hand-written CASE, and a constructed array
        for expr in ("CASE WHEN jsonb_typeof(b.raw->'companions') = 'array' "
                     "THEN b.raw->'companions' ELSE '[]'::jsonb END",
                     "_as_array(\"t.raw->'counterparties'\")",
                     "jsonb_build_array(t.raw->>'_batch')"):
            with self.subTest(expr=expr):
                self.assertEqual(set(), _unproven_reads(expr))

    def test_a_pinned_column_read_bare_is_accepted(self):
        # the other proof: the schema says the column can only be an array
        self.assertEqual({("ao.items_json", False)},
                         _unproven_reads("ao.items_json"))
        self.assertIn("items_json",
                      {c for _t, c in self._array_checked_columns()})

    def _array_reads(self):
        src = Path(__file__).resolve().parents[1] / "oikonome"
        for path in sorted(src.rglob("*.py")):
            for arg in _array_read_arguments(path.read_text()):
                yield path, arg


class TheConstraintRefusesTheWrongShape(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_non_array_item_list_cannot_be_stored(self):
        for table, cols in (
                ("amazon_orders",
                 "(dedup_key, account, date, amount, is_refund, items_json)"),
                ("costco_receipts",
                 "(dedup_key, account, date, amount, is_refund, items_json)")):
            with self.subTest(table=table):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.conn.transaction():
                        self.conn.execute(
                            f"INSERT INTO {table} {cols} VALUES "
                            "('bad','main',DATE '2026-08-01',1,0,'{}'::jsonb)")


if __name__ == "__main__":
    unittest.main()
