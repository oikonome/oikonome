"""Every archive member a restore reads is landed in exactly one place:
by a declared lander (sync/landers.py) or by a loop still inline in
sync/restore.py. Moving a table to a lander moves its name from one side
to the other; a member read in both places, or in neither, fails here."""
import ast
import dataclasses
import inspect
import io
import pathlib
import re
import unittest
from unittest import mock
import zipfile

from oikonome.db import tenancy
from oikonome.engine import entities
from oikonome.sync import landers, restore

from .util import _ensure_db, add_txn, make_db


# The functions that land rows. Every other function of sync/restore.py
# that reads an archive member only plans from it (which ids move, which
# names a person gave) and lands nothing; each is named here with what it
# reads, so a member landed from a new helper is caught.
LANDING = ("_restore_zip", "_restore_recipient_invites")
PLANNERS = {
    "_rows": {"transactions"},
    "_resolve_cut_lineage": {"transactions"},
    "_moved_rows": {"transactions"},
    "_plan_keyed_txns": {"transactions"},
    "_archived_numbered_formers": {"account_adoptions", "accounts"},
    "_plan_rehashed_txns": {"transactions"},
    "_plan_foreign_keyed_txns": {"transactions"},
    "_plan_plain_by_content": {"transactions"},
    "_plan_comma_renames": {"accounts"},
    "_persons_names": {"merchant_canonical"},
    "_push_fed_items": {"accounts", "items"},
    "_is_export": {"transactions"},
}


def _reads(src: str) -> set[str]:
    read = set(re.findall(r'"([a-z_0-9]+)\.csv"', src))
    return read


def _members_by_function() -> dict[str, set[str]]:
    path = pathlib.Path(restore.__file__)
    src = path.read_text()
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            read = _reads(ast.get_source_segment(src, node))
            if read:
                out[node.name] = read
    return out


def _inline_members() -> set[str]:
    by_fn = _members_by_function()
    return set().union(*(by_fn.get(f, set()) for f in LANDING))


class EveryRestoredMemberHasOneLander(unittest.TestCase):

    def test_landers_and_the_inline_loops_split_the_restored_members(self):
        by_lander = {m.removesuffix(".csv")
                     for lander in landers.LANDERS.values()
                     for m in lander.members}
        inline = _inline_members()
        self.assertEqual(by_lander & inline, set(),
                         "a member is landed both by a lander and inline")
        self.assertEqual(by_lander | inline, set(restore.RESTORED_MEMBERS))

    def test_every_other_reader_of_a_member_only_plans(self):
        by_fn = _members_by_function()
        others = {f: m for f, m in by_fn.items() if f not in LANDING}
        self.assertEqual(others, PLANNERS,
                         "a function outside the landing ones reads an "
                         "archive member: say in PLANNERS what it plans")

    def test_a_lander_keeps_the_identity_the_restore_had(self):
        from .roundtrip_builders import FROZEN_IDENTITY
        for table, lander in landers.LANDERS.items():
            self.assertEqual(lander.identity.columns,
                             FROZEN_IDENTITY.get(table), table)

    def test_a_lander_is_keyed_by_its_table(self):
        for table, lander in landers.LANDERS.items():
            self.assertEqual(lander.table, table)

    def test_a_lander_identity_is_among_what_it_writes(self):
        for table, lander in landers.LANDERS.items():
            self.assertTrue(lander.identity.columns, table)
            self.assertLessEqual(set(lander.identity.columns),
                                 set(lander.columns), table)
            self.assertLessEqual(set(lander.defaults), set(lander.columns),
                                 table)

    def test_a_lander_names_only_context_that_exists(self):
        ctx_fields = {f.name for f in dataclasses.fields(landers.Ctx)}
        for table, lander in landers.LANDERS.items():
            self.assertLessEqual(set(lander.reads) | set(lander.writes),
                                 ctx_fields, table)


def _restore_steps():
    """What sets restore state, in the order it runs: ("prepare", field)
    for what Ctx.prepare sets, then per ORDER entry ("land", table) or
    ("step", name)."""
    steps = [("prepare", f) for f in landers.PREPARED]
    for entry in landers.ORDER:
        if isinstance(entry, landers.Step):
            steps.append(("step", entry.name))
        else:
            steps.append(("land", entry))
    steps += [("step", "finalize"), ("step", "after_commit")]
    return steps


def _step_writes(kind, name):
    if kind == "prepare":
        return {name}
    if kind == "step":
        return set(_STEPS[name].writes)
    return set(landers.LANDERS[name].writes)


def _step_reads(kind, name):
    if kind == "prepare":
        return set()
    if kind == "step":
        return set(_STEPS[name].reads)
    return set(landers.LANDERS[name].reads)


_STEPS = {e.name: e for e in (*landers.ORDER, landers.FINALIZE,
                               landers.AFTER_COMMIT)
          if isinstance(e, landers.Step)}


class EveryLanderRunsOnceInOrder(unittest.TestCase):

    def test_every_lander_is_in_the_order_exactly_once(self):
        called = [name for kind, name in _restore_steps() if kind == "land"]
        self.assertEqual(sorted(called), sorted(landers.LANDERS),
                         "a lander is declared but never landed, landed "
                         "twice, or the order names no lander")

    def test_the_restore_lands_through_the_order(self):
        src = inspect.getsource(restore._restore_zip)
        self.assertIn("_landers.land_all(", src)
        self.assertNotIn("land_one(", src)
        # the declared last steps are the ones the restore runs, in order
        self.assertLess(src.index("_landers.land_all("),
                        src.index("_landers.FINALIZE"))
        self.assertLess(src.index("_landers.FINALIZE"),
                        src.index("_landers.AFTER_COMMIT"))

    def test_every_step_reads_only_what_it_declares(self):
        """Each step run in a whole restore reads only the context fields it
        names (or writes): a reader the order test cannot see is one it
        cannot check."""
        from . import roundtrip_builders as rb
        from .test_every_table_round_trips_through_its_lander import (
            fresh_household)
        fields = {f.name for f in dataclasses.fields(landers.Ctx)}
        current: list = [None]
        seen: dict[str, set] = {}

        class TracingCtx(landers.Ctx):
            def __getattribute__(self, name):
                if name in fields and current[0] is not None:
                    seen.setdefault(current[0], set()).add(name)
                return object.__getattribute__(self, name)

        real = landers.run_step

        def traced(step, conn, ctx, out):
            current[0] = step.name
            try:
                return real(step, conn, ctx, out)
            finally:
                current[0] = None

        src = fresh_household()
        dest = fresh_household()
        try:
            rb.build(src, 1)
            from oikonome.sync import export
            data = export.build_zip(src)
            with mock.patch.object(landers, "Ctx", TracingCtx), \
                    mock.patch.object(landers, "run_step", traced):
                restore.restore_zip(dest, data)
        finally:
            src.close()
            dest.close()
        self.assertTrue(seen)
        for name, read in seen.items():
            step = _STEPS[name]
            self.assertLessEqual(read, set(step.reads) | set(step.writes),
                                 name)

    def test_what_a_lander_reads_is_set_before_it_runs(self):
        ready: set[str] = set()
        for kind, name in _restore_steps():
            missing = _step_reads(kind, name) - ready
            self.assertEqual(missing, set(),
                             f"{name} runs before what it reads is set")
            ready |= _step_writes(kind, name)

    def test_every_context_field_has_one_writer(self):
        """A field set in two places is read from whichever ran last: each
        snapshot has its own name, so a reader can never get another's."""
        writers: dict[str, list] = {}
        for kind, name in _restore_steps():
            for f in _step_writes(kind, name):
                writers.setdefault(f, []).append(name)
        self.assertEqual({f: w for f, w in writers.items() if len(w) > 1},
                         {})

    def test_every_written_field_is_a_context_field(self):
        fields = {f.name for f in dataclasses.fields(landers.Ctx)}
        for kind, name in _restore_steps():
            self.assertLessEqual(_step_writes(kind, name) | _step_reads(kind, name),
                                 fields, name)

    def test_an_unset_context_field_names_itself(self):
        ctx = landers.Ctx()
        with self.assertRaisesRegex(landers.ContextNotReady, "ctx.accounts"):
            ctx.holds(None, "account", "chk")
        with self.assertRaisesRegex(landers.ContextNotReady, "ctx.entities"):
            ctx.entity("x")


class EveryExactIdentityIsAUniqueKey(unittest.TestCase):
    """A lander's identity is what the round-trip test checks the restore
    against, so it is checked here against the catalogue: an Exact one is
    a unique key of its table, and a ByKey one says why it cannot be."""

    def test_identities_against_the_catalogue(self):
        _ensure_db()
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(tenancy.ADMIN_DSN, row_factory=dict_row) as c:
            for table, lander in landers.LANDERS.items():
                if isinstance(lander.presence, (landers.ByKey,
                                                landers.Counted,
                                                landers.Group)):
                    self.assertTrue(lander.presence.why, table)
                    continue
                keys = [set(r["cols"]) for r in c.execute(
                    """SELECT array_agg(a.attname::text) AS cols
                         FROM pg_index i
                         JOIN pg_attribute a ON a.attrelid = i.indrelid
                                            AND a.attnum = ANY(i.indkey)
                        WHERE i.indrelid = %s::regclass AND i.indisunique
                          AND i.indpred IS NULL
                        GROUP BY i.indexrelid""", (table,)).fetchall()]
                self.assertIn({"tenant_id", *lander.identity.columns}, keys,
                              f"{table}: no unique key on its identity")


def _archive(rows: str) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("networth_recorded.csv", rows)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


class ByKeyPresence(unittest.TestCase):
    """A table whose identity no unique index states is held by a lookup
    on its identity columns, and a blank cell in one matches a blank."""

    def setUp(self):
        self.conn = make_db()
        self.lander = dataclasses.replace(
            landers.LANDERS["networth_recorded"],
            identity=landers.columns("month", "source"),
            presence=landers.ByKey(why="test only"))

    def tearDown(self):
        self.conn.close()

    def _land(self):
        out = restore._Outcomes()
        landers.land_one(self.conn, _archive(
            "month,total,source\n2031-01,100.0,\n2031-02,200.0,books\n"),
            landers.Ctx(), out, self.lander)
        return out

    def test_the_lookup_names_the_household_beside_row_security(self):
        self.assertIn(f"WHERE tenant_id = {landers.TENANT} AND ",
                      self.lander.sql)

    def test_a_second_landing_finds_both_rows_held(self):
        first = self._land()
        self.assertEqual(first.written, {"networth_recorded": 2})
        again = self._land()
        self.assertEqual(again.written, {"networth_recorded": 0})
        self.assertEqual(again.refused, {("networth_recorded", "present"): 2})
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM networth_recorded").fetchone()["n"], 2)


class ByKeyTypedCells(unittest.TestCase):
    """ByKey over uuid, date, numeric and NULL identity cells: a second
    archive naming the same obligations under fresh ids lands nothing."""

    def setUp(self):
        self.conn = make_db()
        self.eid = str(entities.create_entity(
            self.conn, name="Invented Works LLC", structure="sole_prop",
            business_start_date="2030-01-01")["id"])
        self.lander = dataclasses.replace(
            landers.LANDERS["compliance_obligation"],
            identity=landers.columns("entity_id", "due_date", "fee", "url"),
            presence=landers.ByKey(why="test only"))

    def tearDown(self):
        self.conn.close()

    def _land(self, ids):
        rows = "id,entity_id,title,due_date,fee,url\n" + "".join(
            f"{i},{self.eid.upper()},{t},2031-04-15,{fee},{url}\n"
            for i, (t, fee, url) in zip(ids, (
                ("Annual report", "", ""),
                ("Franchise tax", "12.50", "https://example.com/pay"))))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("compliance_obligation.csv", rows)
        out = restore._Outcomes()
        ctx = landers.Ctx(entities={self.eid})
        landers.land_one(self.conn, zipfile.ZipFile(io.BytesIO(
            buf.getvalue())), ctx, out, self.lander)
        return out

    def test_typed_and_blank_identity_cells_are_found_held(self):
        first = self._land([
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002"])
        self.assertEqual(first.written, {"compliance_obligation": 2})
        again = self._land([
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004"])
        self.assertEqual(again.written, {"compliance_obligation": 0})
        self.assertEqual(again.refused,
                         {("compliance_obligation", "present"): 2})
        # the upper-case archive id lands as the business it names
        self.assertEqual({r["e"] for r in self.conn.execute(
            "SELECT entity_id::text AS e FROM compliance_obligation")},
            {self.eid})


class BatchesLandWhole(unittest.TestCase):
    """More rows than one batch holds all land, and a row a column refuses
    inside a batch costs only that row."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _land(self):
        n = landers.BATCH + 7
        bad = landers.BATCH // 2
        rows = "id,source,row_count\n" + "".join(
            # a count past an INTEGER is a value the column refuses
            f"b{i},csv,{10 ** 12 if i == bad else i}\n" for i in range(n))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("import_batches.csv", rows)
        out = restore._Outcomes()
        landers.land_one(self.conn, zipfile.ZipFile(io.BytesIO(
            buf.getvalue())), landers.Ctx(), out,
            landers.LANDERS["import_batches"])
        return n, out

    def test_every_batch_lands_and_a_misfit_costs_one_row(self):
        n, out = self._land()
        self.assertEqual(out.written, {"import_batches": n - 1})
        self.assertEqual(out.refused, {("import_batches", "malformed"): 1})
        self.assertEqual(sum(out.misfits.values()), 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM import_batches").fetchone()["n"],
            n - 1)
        _, again = self._land()
        self.assertEqual(again.refused, {("import_batches", "present"): n - 1,
                                         ("import_batches", "malformed"): 1})


class ReplacePresence(unittest.TestCase):
    """A held row gives way to the archived one only when the lander's
    condition holds; otherwise the archived row counts as present and the
    held one stays as it was. A condition may read a value beyond the
    written columns, passed after them."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO networth_recorded (month, total, source) VALUES"
            " ('2031-01', 1, 'machine'), ('2031-02', 2, 'person'),"
            " ('2031-03', 3, 'machine')")
        self.lander = dataclasses.replace(
            landers.LANDERS["networth_recorded"],
            parse=lambda r, ctx, out: {
                "month": r["month"], "total": float(r["total"]),
                "source": r.get("source") or None,
                "floor": float(r["floor"])},
            presence=landers.Exact(replace=landers.Replace(
                set=(("total", "EXCLUDED.total"),
                     ("source", "EXCLUDED.source")),
                when="EXCLUDED.source = 'person'"
                     " AND networth_recorded.source <> 'person'"
                     " AND networth_recorded.total < %s::float8",
                when_params=("floor",))))

    def tearDown(self):
        self.conn.close()

    def test_only_the_condition_replaces_a_held_row(self):
        out = restore._Outcomes()
        landers.land_one(self.conn, _archive(
            "month,total,source,floor\n"
            "2031-01,10,person,5\n"     # a machine's row: replaced
            "2031-02,20,person,5\n"     # a person's own: kept
            "2031-03,30,person,2\n"     # past the floor: kept
            "2031-04,40,machine,5\n"),  # not held: written
            landers.Ctx(), out, self.lander)
        self.assertEqual(out.written, {"networth_recorded": 2})
        self.assertEqual(out.refused, {("networth_recorded", "present"): 2})
        self.assertEqual(
            {r["month"]: (r["total"], r["source"]) for r in self.conn.execute(
                "SELECT month, total, source FROM networth_recorded")},
            {"2031-01": (10.0, "person"), "2031-02": (2.0, "person"),
             "2031-03": (3.0, "machine"), "2031-04": (40.0, "machine")})

    def test_a_replace_lander_names_its_conflict_key(self):
        sql = self.lander.sql
        self.assertIn("ON CONFLICT (tenant_id, month) DO UPDATE SET "
                      "total = EXCLUDED.total, source = EXCLUDED.source "
                      "WHERE EXCLUDED.source = 'person'", sql)


class ACellItsColumnCannotHoldIsRefusedBeforeAnyQuery(unittest.TestCase):
    """A lander asks the household about a row (is this receipt here,
    which transaction is this) with the row's own cells. One its column
    cannot hold would end the whole restore at that query, so the driver
    judges those cells first: only that row is refused."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _land(self, table, csv_text):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{table}.csv", csv_text)
        out = restore._Outcomes()
        ctx = landers.Ctx(conn=self.conn)
        landers.land_one(self.conn, zipfile.ZipFile(io.BytesIO(
            buf.getvalue())), ctx, out, landers.LANDERS[table])
        return out

    def test_a_receipt_and_a_line_with_no_uuid_cost_only_themselves(self):
        import base64
        good = "00000000-0000-4000-8000-0000000000aa"
        image = base64.b64encode(b"\x89PNG invented bytes").decode()
        out = self._land("receipts", "id,image,mime\n"
                         f"abc,{image},image/png\n{good},{image},image/png\n")
        self.assertEqual(out.written, {"receipts": 1})
        self.assertEqual(out.refused, {("receipts", "malformed"): 1})
        out = self._land("receipt_items", "receipt_id,line,amount\n"
                         f"abc,1,2.50\n{good},1,2.50\n")
        self.assertEqual(out.written, {"receipt_items": 1})
        self.assertEqual(out.refused, {("receipt_items", "malformed"): 1})

    def test_a_parent_its_column_cannot_hold_is_not_here(self):
        ctx = landers.Ctx(conn=self.conn)
        self.assertFalse(ctx.holds(self.conn, "receipt", "abc"))


class ByKeyLooksForWhatItWrites(unittest.TestCase):
    """An optional identity cell the write leaves empty is looked for
    empty, so a second landing finds the row the first one wrote."""

    def test_an_emptied_identity_cell_is_found_held(self):
        conn = make_db()
        try:
            lander = dataclasses.replace(
                landers.LANDERS["holdings"],
                parse=lambda r, ctx, out: {
                    "account_id": r["account_id"], "symbol": r["symbol"],
                    "name": None, "quantity": None, "price": None,
                    # a value the parse made, not a cell the driver saw
                    "value": None, "as_of": "not a time", "raw": None},
                identity=landers.columns("account_id", "symbol", "as_of"),
                presence=landers.ByKey(why="test only"))
            for _ in range(2):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    z.writestr("holdings.csv",
                               "account_id,symbol\nchk,INV\n")
                out = restore._Outcomes()
                landers.land_one(conn, zipfile.ZipFile(io.BytesIO(
                    buf.getvalue())), landers.Ctx(accounts={"chk"}), out,
                    lander)
            self.assertEqual(out.refused, {("holdings", "present"): 1})
            self.assertEqual(conn.execute(
                "SELECT count(*) AS n FROM holdings").fetchone()["n"], 1)
        finally:
            conn.close()


class ALanderDeclaresOnlyWhatCanBeWritten(unittest.TestCase):

    def _computed(self, **computed):
        return dataclasses.replace(landers.LANDERS["entity_membership"],
                                   computed=computed)

    def test_a_computed_value_is_declared_once_and_whole(self):
        expr = ("%s AND NOT EXISTS (SELECT 1 FROM entity_membership"
                " WHERE entity_id = %s AND is_household)")
        for bad in (
                # not a column the lander writes
                {"nowhere": ("%s", ("nowhere",))},
                # declared a second way
                {"created_at": ("%s", ("created_at",))},
                # one placeholder short
                {"is_household": (expr, ("is_household",))},
                # reads a cell nothing judges before the write
                {"is_household": (expr, ("is_household", "member_name"))}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._computed(**bad)
        self._computed(is_household=(expr, ("is_household", "entity_id")))

    def test_replace_needs_a_conflict_key(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(
                landers.LANDERS["networth_recorded"],
                presence=landers.Exact(any_key=True, replace=landers.Replace(
                    set=(("total", "EXCLUDED.total"),), when="TRUE")))


def _zip(**members) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in members.items():
            z.writestr(f"{name}.csv", text)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


class AJournalRowWithoutItsTimeIsTheSameRenameAtAnyTime(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _land(self, text):
        out = restore._Outcomes()
        ctx = landers.Ctx(conn=self.conn, cut_aliases=set(),
                          alias_where=lambda raw: (False, True))
        landers.land_one(self.conn, _zip(merchant_renames=text), ctx, out,
                         landers.LANDERS["merchant_renames"])
        return out

    def test_blank_times_are_found_held_in_the_archive_and_after(self):
        text = ("id,raw_merchant,from_canonical,to_canonical,at\n"
                "1,INVENTED RAW,Old Name,New Name,\n"
                "2,INVENTED RAW,Old Name,New Name,\n")
        first = self._land(text)
        self.assertEqual(first.written, {"merchant_renames": 1})
        self.assertEqual(first.refused, {("merchant_renames", "present"): 1})
        again = self._land(text)
        self.assertEqual(again.written, {"merchant_renames": 0})
        self.assertEqual(again.refused, {("merchant_renames", "present"): 2})


class ASplitIsRefusedWhole(unittest.TestCase):
    """Every refusal of a split counts each of its parts."""

    def setUp(self):
        self.conn = make_db()
        for t in ("s-store", "s-held", "s-bad"):
            add_txn(self.conn, "2031-03-01", -30.0, "INVENTED SHOP",
                    txn_id=t)
        self.conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount,"
            " source) VALUES ('s-held', 1, 'GROCERIES', -10, 'user'),"
            " ('s-held', 2, 'HOME_IMPROVEMENT', -20, 'user')")

    def tearDown(self):
        self.conn.close()

    def test_rebuilt_present_and_malformed_count_every_part(self):
        out = restore._Outcomes()
        landers.land_one(self.conn, _zip(transaction_splits=(
            "txn_id,line,category,amount,source\n"
            "s-store,1,GROCERIES,-10,amazon\ns-store,2,HOME_IMPROVEMENT,-20,"
            "amazon\n"
            "s-held,1,GROCERIES,-15,user\ns-held,2,HOME_IMPROVEMENT,-15,user\n"
            # three parts that do not add up to the charge
            "s-bad,1,GROCERIES,-1,user\ns-bad,2,HOME_IMPROVEMENT,-1,user\n"
            "s-bad,3,TRAVEL,-1,user\n")), landers.Ctx(conn=self.conn), out,
            landers.LANDERS["transaction_splits"])
        self.assertEqual(out.refused, {
            ("transaction_splits", "rebuilt"): 2,
            ("transaction_splits", "present"): 2,
            ("transaction_splits", "malformed"): 3})

    def test_a_part_its_column_cannot_hold_refuses_the_group_first(self):
        self.conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount,"
            " source) VALUES ('s-store', 1, 'GROCERIES', -10, 'amazon'),"
            " ('s-store', 2, 'HOME_IMPROVEMENT', -20, 'amazon')")
        out = restore._Outcomes()
        lander = landers.LANDERS["transaction_splits"]
        lander.write(self.conn, None, out, lander, [{"txn_id": "s-store", "lines": [
            {"category": "GROCERIES\x00", "amount": -10.0},
            {"category": "HOME_IMPROVEMENT", "amount": -20.0}]}])
        self.assertEqual(out.refused, {("transaction_splits", "malformed"): 2})
        # the store's parts are still there
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transaction_splits"
            " WHERE txn_id = 's-store' AND source = 'amazon'"
        ).fetchone()["n"], 2)


class ADocumentWithAnUnreadableYearOrIdCostsOnlyItself(unittest.TestCase):

    def test_an_unreadable_year_or_id_is_malformed(self):
        conn = make_db()
        try:
            out = restore._Outcomes()
            ctx = landers.Ctx(conn=conn, pre_226=False, seeded_years=set())
            landers.land_one(conn, _zip(income_documents=(
                "id,year,form,payer,primary_amount\n"
                "x1,2024,1099-DIV,Invented Fund,10\n"
                "1,twenty,1099-DIV,Invented Fund,10\n"
                "2,2024,1099-DIV,Invented Fund,11\n")), ctx, out,
                landers.LANDERS["income_documents"])
            self.assertEqual(out.written, {"income_documents": 1})
            self.assertEqual(out.refused,
                             {("income_documents", "malformed"): 2})
        finally:
            conn.close()


class AnIncomeYearThatIsNoNumberCostsOnlyItsRow(unittest.TestCase):

    def test_an_unreadable_year_is_malformed(self):
        conn = make_db()
        try:
            out = restore._Outcomes()
            ctx = landers.Ctx(conn=conn)
            landers.land_one(conn, _zip(income_annual=(
                "year,wages\ntwenty,10\n2024,10\n")), ctx, out,
                landers.LANDERS["income_annual"])
            self.assertEqual(out.written, {"income_annual": 1})
            self.assertEqual(out.refused,
                             {("income_annual", "malformed"): 1})
        finally:
            conn.close()

    def test_a_non_finite_year_refuses_the_archive(self):
        # no genuine export writes one (restore._f), here as anywhere
        conn = make_db()
        try:
            with self.assertRaises(restore._ArchiveRefused):
                landers.land_one(conn, _zip(income_annual=(
                    "year,wages\ninf,10\n2024,10\n")), landers.Ctx(
                        conn=conn), restore._Outcomes(),
                    landers.LANDERS["income_annual"])
        finally:
            conn.close()


class ALanderDeclaresHowItWrites(unittest.TestCase):

    def test_a_custom_write_takes_no_per_row_results(self):
        for extra in ({"landed": lambda ctx, got: None}, {"spellings": True}):
            with self.subTest(extra=list(extra)), \
                    self.assertRaises(ValueError):
                dataclasses.replace(landers.LANDERS["transaction_splits"],
                                    **extra)

    def test_a_lander_that_allocates_ids_writes_one_row_at_a_time(self):
        self.assertTrue(landers.LANDERS["income_documents"].allocates_ids)
        with self.assertRaises(ValueError):
            dataclasses.replace(landers.LANDERS["income_documents"], batch=2)


class ALinkGroupIsRenumberedOnlyWhereARowLanded(unittest.TestCase):

    def test_a_refused_link_touches_no_group(self):
        conn = make_db()
        try:
            out = restore._Outcomes()
            ctx = landers.Ctx(conn=conn)
            landers.land_one(conn, _zip(account_links=(
                "group_id,account_id,home_rank\n"
                "00000000-0000-4000-8000-000000000001,chk,0\n"
                # a rank the column cannot hold: refused at the write
                "00000000-0000-4000-8000-000000000002,card,99999999999\n")),
                ctx, out, landers.LANDERS["account_links"])
            self.assertEqual(out.written, {"account_links": 1})
            self.assertEqual(ctx.touched_groups,
                             {"00000000-0000-4000-8000-000000000001"})
        finally:
            conn.close()


class TheSpineLandsAsBefore(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _ledger_ctx(self):
        ctx = landers.Ctx(conn=self.conn, carried=None, entities=set(),
                          known_merchants=set(), merchant_map={})
        return ctx

    def test_two_archived_merchants_on_one_entity_fold_into_the_first(self):
        out = restore._Outcomes()
        ctx = landers.Ctx(conn=self.conn)
        landers.land_one(self.conn, _zip(merchants=(
            "id,name,plaid_entity_id\n"
            "00000000-0000-4000-8000-0000000000a1,Invented Grocer,ent_x\n"
            "00000000-0000-4000-8000-0000000000a2,Invented Grocer,ent_x\n")),
            ctx, out, landers.LANDERS["merchants"])
        self.assertEqual(out.written, {"merchants": 1})
        self.assertEqual(out.refused, {("merchants", "merged"): 1})

    def test_a_merchant_lander_must_write_one_row_at_a_time(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(landers.LANDERS["merchants"], batch=2)

    def test_an_archive_without_merchants_still_knows_the_merchants_here(self):
        ctx = landers.Ctx(conn=self.conn)
        landers.land_one(self.conn, _zip(items="id\n"), ctx,
                         restore._Outcomes(), landers.LANDERS["merchants"])
        self.assertEqual(ctx.known_merchants, set())
        self.assertEqual(ctx.merchant_map, {})

    def test_land_returns_one_result_per_row_refused_ones_too(self):
        out = restore._Outcomes()
        sql = landers.LANDERS["import_batches"].sql
        got = out.land(self.conn, "import_batches", sql, [
            ("b1", "csv", None, None, 1, None),
            ("b2", "csv", None, None, 10 ** 12, None),
            ("b1", "csv", None, None, 1, None)])
        self.assertEqual(got, ["b1", None, None])
        self.assertEqual(out.last_misfits,
                         [None, "a row_count that is not a valid integer",
                          None])

    def _ledger(self, text):
        out = restore._Outcomes()
        ctx = self._ledger_ctx()
        landers.land_one(self.conn, _zip(transactions=text), ctx, out,
                         landers.LANDERS["transactions"])
        return ctx, out

    def test_a_persons_category_reaches_a_held_row_only_from_a_row_that_fits(
            self):
        add_txn(self.conn, "2031-03-01", -5.0, "INVENTED SHOP", txn_id="h1")
        add_txn(self.conn, "2031-03-01", -5.0, "INVENTED SHOP", txn_id="h2")
        ctx, out = self._ledger(
            "id,account_id,date,amount,name,category_override,"
            "override_source,removed\n"
            "h1,chk,2031-03-01,-5,INVENTED SHOP,TRAVEL,user,0\n"
            # a removed flag past an INTEGER: the write refuses the row
            "h2,chk,2031-03-01,-5,INVENTED SHOP,TRAVEL,user,99999999999\n")
        got = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions"
            " WHERE id IN ('h1', 'h2')")}
        self.assertEqual(got, {"h1": "TRAVEL", "h2": None})

    def test_an_id_twice_across_a_batch_counts_its_repair_once(self):
        n = landers.LANDERS["transactions"].batch
        rows = ["id,account_id,date,amount,name"]
        rows += [f"b{i},chk,2031-03-01,-1,INVENTED SHOP" for i in range(n - 1)]
        rows += ["twin,chk,2031-03-01,,INVENTED SHOP",
                 "twin,chk,2031-03-01,,INVENTED SHOP"]
        ctx, out = self._ledger("\n".join(rows) + "\n")
        self.assertEqual(out.written, {"transactions": n})
        self.assertEqual(out.repaired,
                         {("transactions", "amount_repaired"): 1})
        self.assertEqual(out.refused, {("transactions", "present"): 1})


class TheAlertLogLandsAsBefore(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _land(self, text):
        out = restore._Outcomes()
        landers.land_one(self.conn, _zip(alerts_log=text),
                         landers.Ctx(conn=self.conn), out,
                         landers.LANDERS["alerts_log"])
        return out

    def test_an_alert_held_under_both_spellings_is_muted_whole_once(self):
        from oikonome.sync import card_number
        whole = "Invented card 4111111111111111 ran low"
        cut = card_number.cut(whole)
        self.assertNotEqual(cut, whole)
        for msg in (whole, cut):
            self.conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen, active, dismissed) VALUES ('low_balance',"
                " 'warn', %s, '2031-01-01', '2031-01-02', 1, 0)", (msg,))
        out = self._land("kind,severity,message,first_seen,last_seen,"
                         "active,dismissed\nlow_balance,warn,"
                         f"{whole},2031-01-01,2031-01-02,1,1\n")
        self.assertEqual(out.written, {"alerts_log": 1})
        self.assertEqual([r["dismissed"] for r in self.conn.execute(
            "SELECT dismissed FROM alerts_log")], [1, 1])

    def test_an_alert_twice_in_one_archive_lands_once(self):
        row = "low_balance,warn,Invented alert,2031-01-01,2031-01-02,1,1\n"
        out = self._land("kind,severity,message,first_seen,last_seen,"
                         "active,dismissed\n" + row + row)
        self.assertEqual(out.written, {"alerts_log": 1})
        self.assertEqual(out.refused, {("alerts_log", "present"): 1})
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM alerts_log").fetchone()["n"], 1)

    def test_the_round_trip_compares_the_alert_log(self):
        import json
        import pathlib
        from . import roundtrip_builders as rb
        self.assertIn("alerts_log", rb.BUILDERS)
        golden = json.loads((pathlib.Path(__file__).parent / "fixtures"
                             / "restore_round_trip_digests.json").read_text())
        empty = rb.hashlib.sha256(b"").hexdigest()[:20]
        for seed, doc in golden.items():
            for scenario in ("A", "D"):
                self.assertNotEqual(doc[scenario]["rows"].get("alerts_log",
                                                               empty), empty,
                                    f"{seed} {scenario}")


class TheRestoreFinishesAfterItsCommit(unittest.TestCase):

    def setUp(self):
        from .test_every_table_round_trips_through_its_lander import (
            fresh_household)
        from .util import write_config
        self.src, self.dest = make_db(), fresh_household()
        write_config(self.src)

    def tearDown(self):
        self.src.close()
        self.dest.close()

    def _archive(self, drop=()):
        from oikonome.sync import export
        data = export.build_zip(self.src)
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in src.namelist():
                if not any(name.endswith(d) for d in drop):
                    z.writestr(name, src.read(name))
        return buf.getvalue()

    def test_an_archive_without_settings_still_finishes(self):
        counts = restore.restore_zip(
            self.dest, self._archive(drop=("tenant_settings.csv",)))
        self.assertNotIn("settings", counts)
        self.assertEqual(self.dest.execute(
            "SELECT count(*) AS n FROM accounts").fetchone()["n"], 2)

    def test_a_moved_ai_route_is_told_after_the_commit(self):
        from oikonome.engine import budget
        from .util import write_config
        cfg = budget.load_config(self.src)
        cfg["llm_vision_model"] = "invented-model-b"
        budget.save_config(self.src, cfg)
        write_config(self.dest)
        cfg = budget.load_config(self.dest)
        cfg["llm_vision_model"] = "invented-model-a"
        budget.save_config(self.dest, cfg)
        told = []

        def tell(conn):
            # after the commit: nothing of the restore is still open
            told.append(conn.info.transaction_status)
        with mock.patch.object(restore, "_tell_the_owner_the_ai_route_moved",
                               tell):
            restore.restore_zip(self.dest, self._archive())
        import psycopg
        self.assertEqual(told, [psycopg.pq.TransactionStatus.IDLE])

    def test_a_failing_route_notice_keeps_the_summary(self):
        from oikonome.engine import budget
        from .util import write_config
        cfg = budget.load_config(self.src)
        cfg["llm_vision_model"] = "invented-model-b"
        budget.save_config(self.src, cfg)
        write_config(self.dest)
        cfg = budget.load_config(self.dest)
        cfg["llm_vision_model"] = "invented-model-a"
        budget.save_config(self.dest, cfg)
        raised = []

        def boom(conn):
            raised.append(True)
            raise RuntimeError("mail relay down")
        with mock.patch.object(restore, "_tell_the_owner_the_ai_route_moved",
                               boom):
            counts = restore.restore_zip(self.dest, self._archive())
        self.assertEqual(raised, [True])
        self.assertIn("accounts", counts)


if __name__ == "__main__":
    unittest.main()
