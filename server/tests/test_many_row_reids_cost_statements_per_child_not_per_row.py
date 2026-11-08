"""Moving many transaction rows onto new ids is one pass per child table,
not one per row.

Every carry of a row's id touches columns with no index on the id (the
pending pointer between rows, the refused-receipt arrays), so moving rows
one at a time costs a full-ledger scan per row. A re-id of every file row
of a long statement history then runs for minutes while the deploy's
migrate holds app start. The batched form must issue a number of
statements bounded by the child tables, whatever the number of rows —
and must leave every child exactly where the one-at-a-time form puts it.
"""
import time
import unittest

from oikonome.sync import adopt

from .util import add_txn, make_db


def _move_all(conn, pairs):
    """The batched form when there is one; otherwise the only way to move
    many rows is the per-row loop."""
    many = getattr(adopt, "rewrite_txn_ids", None)
    if many is None:
        return [adopt._rewrite_txn_id(conn, o, n) for o, n in pairs]
    return many(conn, pairs)


class _Counting:
    def __init__(self, conn):
        self._conn, self.statements = conn, 0

    def execute(self, sql, params=None):
        self.statements += 1
        return (self._conn.execute(sql) if params is None
                else self._conn.execute(sql, params))

    def transaction(self):
        return self._conn.transaction()


LEDGER, MOVES = 20_000, 2_000


class ManyRowReIdCostTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_moving_many_rows_issues_statements_per_child_not_per_row(self):
        c = self.conn
        c.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   pending, removed, raw)
               SELECT 'row-' || g, 'card', DATE '2024-01-01' + (g %% 700),
                      (g %% 97) + 0.5, 'SHOP ' || (g %% 50), 0, 0, '{}'
                 FROM generate_series(1, %s) g""", (LEDGER,))
        c.execute("ANALYZE transactions")
        pairs = [(f"row-{i}", f"keyed-{i}") for i in range(1, MOVES + 1)]
        counting = _Counting(c)
        t0 = time.monotonic()
        done = _move_all(counting, pairs)
        took = time.monotonic() - t0
        print(f"\n{MOVES} moves on a {LEDGER}-row ledger: "
              f"{counting.statements} statements, {took:.2f}s")
        self.assertEqual(done, [True] * MOVES)
        _, refs = adopt._txn_shape(c)
        slices = -(-MOVES // 1000)
        bound = slices * (len(refs) + 10)
        self.assertLessEqual(
            counting.statements, bound,
            f"{counting.statements} statements to move {MOVES} rows: the "
            "carry is per row, so its cost grows with rows x ledger")
        self.assertEqual(c.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'keyed-%%'"
        ).fetchone()["n"], MOVES)
        self.assertEqual(c.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"], LEDGER)


def _fixture(c):
    for i in range(1, 9):
        add_txn(c, "2024-03-0%d" % i, 10 + i, f"SHOP {i}", txn_id=f"o{i}")
    add_txn(c, "2024-03-20", 99, "ALREADY HERE", txn_id="n3")
    add_txn(c, "2024-03-21", 5, "STAYS", txn_id="stay")
    # pending pointers: moved -> moved, unmoved -> moved, moved -> unmoved
    c.execute("UPDATE transactions SET pending_transaction_id='o2' "
              "WHERE id='o1'")
    c.execute("UPDATE transactions SET pending_transaction_id='o1' "
              "WHERE id='stay'")
    c.execute("UPDATE transactions SET pending_transaction_id='stay' "
              "WHERE id='o4'")
    c.execute("INSERT INTO transaction_notes (txn_id, note) VALUES "
              "('o1','note one'), ('o5','note five'), ('stay','kept')")
    c.execute("INSERT INTO manual_categories (transaction_id, category) "
              "VALUES ('o1','TRAVEL'), ('o6','FOOD_AND_DRINK')")
    c.execute("INSERT INTO business_flags (txn_id) VALUES ('o2'), ('o7')")
    c.execute("INSERT INTO reimburse_flags (txn_id) VALUES ('o5')")
    c.execute("INSERT INTO reimbursements (expense_id, reimburse_id) "
              "VALUES ('o1','o2'), ('stay','o6')")
    c.execute("INSERT INTO transaction_splits (txn_id, line, category, "
              "amount) VALUES ('o4', 1, 'TRAVEL', 7), ('o4', 2, 'FOOD', 7)")
    c.execute("INSERT INTO business_txn_class (txn_id, bucket) "
              "VALUES ('o8','business')")
    c.execute("INSERT INTO receipts (txn_id, image, mime) "
              "VALUES ('o1', '\\x00', 'image/png')")
    c.execute("INSERT INTO receipts (image, mime, unmatched_txn_ids) "
              "VALUES ('\\x00', 'image/png', ARRAY['o2','stay','o5'])")
    c.execute("INSERT INTO receipts (image, mime, unmatched_txn_ids) "
              "VALUES ('\\x00', 'image/png', ARRAY['o7','o8'])")


# o3 onto a held id (left); o9 is no row (left); o5 -> o4 waits on o4
# moving first, o7 -> x7 then o8 -> x7 lands only the first; a chain
# o6 -> m6 -> z6 moves twice
_PAIRS = [("o1", "k1"), ("o2", "k2"), ("o3", "n3"), ("o9", "k9"),
          ("o4", "k4"), ("o5", "o4"), ("o7", "x7"), ("o8", "x7"),
          ("o6", "m6"), ("m6", "z6")]


def _state(c):
    out = {"transactions": [
        tuple(r.values()) for r in c.execute(
            "SELECT id, name, amount, pending_transaction_id "
            "FROM transactions ORDER BY id")]}
    for table, col in adopt._txn_children(adopt._txn_shape(c)[1]):
        if table == "transactions":
            continue
        out[f"{table}.{col}"] = sorted(
            r["v"] or "" for r in c.execute(
                f"SELECT {col} AS v FROM {table}"))
    out["receipts.unmatched"] = sorted(
        r["u"] for r in c.execute(
            "SELECT array_to_string(unmatched_txn_ids, ',') AS u "
            "FROM receipts"))
    out["notes"] = sorted(
        (r["txn_id"], r["note"]) for r in c.execute(
            "SELECT txn_id, note FROM transaction_notes"))
    out["pins"] = sorted(
        (r["transaction_id"], r["category"]) for r in c.execute(
            "SELECT transaction_id, category FROM manual_categories"))
    return out


class BatchedReIdMatchesOneAtATimeTests(unittest.TestCase):
    def setUp(self):
        self.one, self.many = make_db(), make_db()
        _fixture(self.one)
        _fixture(self.many)

    def tearDown(self):
        self.one.close()
        self.many.close()

    def test_batched_rewrite_leaves_every_child_where_the_per_row_one_does(
            self):
        per_row = [adopt._rewrite_txn_id(self.one, o, n) for o, n in _PAIRS]
        batched = adopt.rewrite_txn_ids(self.many, _PAIRS)
        self.assertEqual(batched, per_row)
        self.assertEqual(per_row, [True, True, False, False, True, True,
                                   True, False, True, True])
        self.assertEqual(_state(self.many), _state(self.one))

    def test_a_failure_inside_a_slice_moves_nothing_of_it(self):
        from unittest import mock
        import psycopg
        c = self.many
        cols, refs = adopt._txn_shape(c)
        broken = refs + (("no_such_child_table", "txn_id"),)
        before = _state(c)
        with mock.patch.object(adopt, "_txn_shape",
                               return_value=(cols, broken)):
            with self.assertRaises(psycopg.Error):
                adopt.rewrite_txn_ids(c, _PAIRS[:2])
        self.assertEqual(_state(c), before)


if __name__ == "__main__":
    unittest.main()
