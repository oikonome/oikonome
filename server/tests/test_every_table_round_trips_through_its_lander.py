"""Every table the export carries comes back through the restore as the
restore promises, for households drawn at random.

`roundtrip_builders` fills one synthetic household per seed: twins (one W-2
under each spouse, two identical charges under two ids, two merchants of
one name), NULL in every nullable column, right-to-left text, combining
marks, emoji, cells a spreadsheet reads as a formula, CR/LF, quotes and
commas inside cells, money at 0, a cent, a refund and the edge of any real
sum, and ids from pools a second household also draws, so a restore into
that household meets colliding ids. Each scenario is checked per seed; a
failure names its seed, and OIKONOME_RT_SEEDS runs more of them.

A  into an empty household: per table, the multiset of identities (what
   the restore counts as the same row, `roundtrip_builders.IDENTITY`)
   equals the source's, and every archived row has exactly one outcome.
B  the same archive again: nothing written, every row already present,
   the household unchanged.
C  into the household it came from: nothing written, nothing changed.
D  into another populated household: nothing either side held is lost,
   per identity the destination ends with max(source, destination) copies
   (per owner for income documents), no identity is held twice, and each
   W-2 wage figure the W-2s own equals the sum of its W-2s.
E  the export of A, restored, is A (a fixed point).
F  an archive without its manifest, or whose manifest names the oldest
   migration, still satisfies A and B.

Content the restore deliberately rewrites (a card number in free text, a
descriptor past the longest real one) is checked for idempotence only.

The golden digests (fixtures/restore_round_trip_digests.json) pin, per
fixed seed and scenario, the outcome tally and a hash of every restored
row; a refactor of the restore that changes either fails here. Regenerate
with OIKONOME_RT_REGEN=1 when a change in what lands is intended.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import pathlib
import time
import unittest
import uuid
import zipfile
from collections import Counter

from oikonome.db import tenancy
from oikonome.sync import archive_upgrades, export, restore
from oikonome.tenant_export import rls_tenant_tables
from oikonome.web.pages import EXPORT_SKIP

from . import roundtrip_builders as rb
from .util import _ensure_db

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / \
    "restore_round_trip_digests.json"
REGEN = os.environ.get("OIKONOME_RT_REGEN") == "1"

TABLES = tuple(rb.BUILDERS)
# rows a count cannot be compared on: the settings document, one row merged
# over the destination's
ROWS = tuple(t for t in TABLES if rb.IDENTITY[t][0] != "single")


def fresh_household():
    _ensure_db()
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, f"rt-{uuid.uuid4().hex[:8]}")
    finally:
        admin.close()
    return tenancy.tenant_connect(tid)


def archived_rows(data: bytes) -> dict[str, int]:
    z = zipfile.ZipFile(io.BytesIO(data))
    out = {}
    for name in z.namelist():
        t = name[:-4]
        if name.endswith(".csv") and t in ROWS:
            out[t] = sum(1 for _ in csv.DictReader(
                io.StringIO(z.read(name).decode("utf-8"))))
    return out


def rewrite_manifest(data: bytes, doc) -> bytes:
    """The archive with its manifest removed (`doc` None) or replaced."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename != archive_upgrades.MANIFEST:
                z.writestr(info.filename, src.read(info.filename))
        if doc is not None:
            z.writestr(archive_upgrades.MANIFEST, json.dumps(doc))
    return buf.getvalue()


def tally(out) -> dict:
    # the counters behind the summary's sentences too: which bound a value
    # passed, which optional value was left empty, what a column refused
    return {"written": {t: n for t, n in sorted(out.written.items()) if n},
            "refused": {f"{t}:{r}": n
                        for (t, r), n in sorted(out.refused.items())},
            "over_bound": dict(sorted(out.over_bound.items())),
            "emptied": dict(sorted(out.emptied_bound.items())),
            "misfits": dict(sorted(out.misfits.items()))}


def keyed(conn, table, key, *, landing=False):
    return Counter(key.ident(r)
                   for r in rb.key_rows(conn, table, key, landing=landing))


class Case:
    """One seed's households, built once and shared by every scenario.
    Each is held by its tenant id; a connection is opened when a scenario
    reads it and handed back by `release`."""

    def __init__(self, seed: int):
        self.seed = seed
        self._open: dict[str, object] = {}
        self.tids: dict[str, str] = {}
        src = self.open("src", fresh_household())
        rb.build(src, seed)
        self.data = export.build_zip(src)
        self.rows = archived_rows(self.data)
        self.src_digest = rb.household_digest(src, TABLES)
        a = self.open("a", fresh_household())
        self.a_result, self.a_out = restore.restore_outcomes(a, self.data)
        self.a_digest = rb.household_digest(a, TABLES)
        self._d = None
        self.release()

    def open(self, name, conn=None):
        if conn is not None:
            self.tids[name] = rb.tenant_of(conn)
            self._open[name] = conn
        if name not in self._open:
            self._open[name] = tenancy.tenant_connect(self.tids[name])
        return self._open[name]

    @property
    def src(self):
        return self.open("src")

    @property
    def a(self):
        return self.open("a")

    def d(self):
        """Scenario D's households, built on first use: the destination
        before and after, and the archive's merchants it folded."""
        if self._d is None:
            dest = self.open("dest", fresh_household())
            rb.build(dest, self.seed + 1000)
            before = {t: [keyed(dest, t, k) for k in rb.IDENTITY[t][1]]
                      for t in ROWS}
            before_owners = Counter(
                rb.key_rows(dest, "income_documents",
                            rb.Key(*rb._DOC, "owner")))
            before_amounts = {r["id"]: r["amount"] for r in dest.execute(
                "SELECT id, amount FROM transactions").fetchall()}
            unlinked = {r["id"] for r in dest.execute(
                "SELECT id FROM accounts WHERE item_id IS NULL").fetchall()}
            # the archive's merchants the destination holds under its own
            # id: the same aggregator entity, folded onto the live row
            live = {r["plaid_entity_id"]: r["id"] for r in dest.execute(
                "SELECT id::text AS id, plaid_entity_id FROM merchants"
                " WHERE plaid_entity_id IS NOT NULL").fetchall()}
            merchant_map = {
                r["id"]: live[r["plaid_entity_id"]] for r in self.src.execute(
                    "SELECT id::text AS id, plaid_entity_id FROM merchants"
                    " WHERE plaid_entity_id IS NOT NULL").fetchall()
                if r["plaid_entity_id"] in live
                and live[r["plaid_entity_id"]] != r["id"]}
            result, out = restore.restore_outcomes(dest, self.data)
            self._d = dict(before=before, before_owners=before_owners,
                           before_amounts=before_amounts,
                           unlinked=unlinked,
                           merchant_map=merchant_map, result=result, out=out,
                           digest=rb.household_digest(dest, TABLES))
        self._d["dest"] = self.open("dest")
        return self._d

    def release(self):
        for conn in self._open.values():
            conn.close()
        self._open.clear()


class EveryTableRoundTrips(unittest.TestCase):
    maxDiff = None
    cases: dict[int, Case] = {}

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        cls.cases = {}

    @classmethod
    def tearDownClass(cls):
        for c in cls.cases.values():
            c.release()

    def case(self, seed) -> Case:
        if seed not in self.cases:
            self.cases[seed] = Case(seed)
        return self.cases[seed]

    @contextlib.contextmanager
    def using(self, seed):
        """The seed's case, its connections handed back afterwards: the
        tenant pool is small, and eight seeds' households are not."""
        c = self.case(seed)
        try:
            yield c
        finally:
            c.release()

    # -- shared promises --------------------------------------------------

    def assert_one_outcome_each(self, seed, rows, out, what):
        """Every archived row is written, or refused under one reason."""
        for table, n in rows.items():
            refused = sum(v for (t, _), v in out.refused.items()
                          if t == table)
            written = out.written.get(table, 0)
            if table in rb.CONTROL:
                # a control-plane member lands answers only (see LANDS):
                # the rest are no outcome at all, by design
                self.assertLessEqual(written + refused, n, f"seed {seed}")
                continue
            self.assertEqual(
                written + refused, n,
                f"seed {seed} {what}: {table} archived {n} rows, wrote "
                f"{written}, refused {refused}")

    def assert_lands_whole(self, seed, src, dest, out, rows, what):
        """Scenario A: the destination holds the source's identities, and
        every row the restore does not land has its stated outcome."""
        for table in ROWS:
            want = rb.identities(src, table, landing=True)
            got = rb.identities(dest, table)
            self.assertEqual(
                got, want,
                f"seed {seed} {what}: {table} — missing "
                f"{sorted((want - got).elements())[:3]}, extra "
                f"{sorted((got - want).elements())[:3]}")
        self.assert_one_outcome_each(seed, rows, out, what)
        rebuilt = {}
        for table, cond in rb.LANDS.items():
            if table in rb.CONTROL:
                continue
            n = src.execute(f"SELECT count(*) AS n FROM {table}"
                            f" WHERE NOT ({cond})").fetchone()["n"]
            if n:
                rebuilt[(table, "rebuilt")] = n
        self.assertEqual(out.refused, rebuilt,
                         f"seed {seed} {what}: a clean archive into an empty "
                         "household refuses only what the destination "
                         "rebuilds")

    def assert_writes_nothing(self, seed, out, result, rows, what):
        written = {t: n for t, n in out.written.items()
                   if n and t in ROWS}
        self.assertEqual(written, {}, f"seed {seed} {what}: wrote rows")
        self.assertEqual({k: v for k, v in out.refused.items()
                          if k[1] not in ("present", "rebuilt")}, {},
                         f"seed {seed} {what}: reported more than "
                         "'already present'")
        self.assertEqual(out.repaired, {}, f"seed {seed} {what}")
        self.assert_one_outcome_each(seed, rows, out, what)

    # -- the coverage guard ---------------------------------------------

    def test_every_exported_table_has_a_builder_or_a_reason(self):
        conn = fresh_household()
        try:
            exported = (set(rls_tenant_tables(conn)) - EXPORT_SKIP
                        | set(export.CONTROL_TABLES))
        finally:
            conn.close()
        self.assertEqual(set(rb.BUILDERS) | set(rb.RECORDS_ONLY), exported,
                         "a table the export carries needs a builder in "
                         "roundtrip_builders.BUILDERS, or a reason in "
                         "RECORDS_ONLY")
        self.assertFalse(set(rb.BUILDERS) & set(rb.RECORDS_ONLY))
        self.assertEqual(set(rb.IDENTITY), set(rb.BUILDERS),
                         "every built table says what its identity is")
        self.assertEqual(set(rb.BUILDERS) - restore.RESTORED_MEMBERS, set(),
                         "a built table the restore never loads belongs in "
                         "RECORDS_ONLY")

    def test_every_built_table_is_filled_in_the_source(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                for table in TABLES:
                    n = rb.query(c.src, table,
                                 f"SELECT count(*) AS n FROM {table}")[0]["n"]
                    self.assertGreater(n, 0, f"seed {seed}: {table} is empty, so "
                                             "nothing checks it")
                    if table in ROWS:
                        self.assertTrue(
                            rb.identities(c.src, table, landing=True),
                            f"seed {seed}: no row of {table} lands")

    def test_every_nullable_column_is_null_somewhere_in_the_source(self):
        """The NULL row of each table is there, so a column the restore
        cannot take blank shows up as a refused row in A."""
        never = []
        with self.using(rb.seeds()[0]) as c:
            for table in TABLES:
                if table in rb.CONTROL or table == "tenant_settings":
                    continue
                for col in sorted(rb.nullable(c.src, table)):
                    if col in (rb.KEEP_NOT_NULL.get(table, set())
                               | rb.FILLED_BY_SETTLE.get(table, set())):
                        continue
                    if not c.src.execute(
                            f"SELECT 1 FROM {table} WHERE {col} IS NULL"
                            " LIMIT 1").fetchone():
                        never.append(f"{table}.{col}")
        self.assertEqual(never, [], "nullable columns no source row "
                                    "leaves NULL")

    # -- the scenarios ---------------------------------------------------

    def test_a_into_an_empty_household(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                self.assert_lands_whole(seed, c.src, c.a, c.a_out, c.rows, "A")
                self.assert_w2_figures_are_their_sums(seed, c.a, "A")

    def test_b_the_same_archive_again_writes_nothing(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                result, out = restore.restore_outcomes(c.a, c.data)
                self.assert_writes_nothing(seed, out, result, c.rows, "B")
                self.assertEqual(rb.household_digest(c.a, TABLES), c.a_digest,
                                 f"seed {seed} B: the household changed")

    def test_c_into_its_own_household_writes_nothing(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                result, out = restore.restore_outcomes(c.src, c.data)
                self.assert_writes_nothing(seed, out, result, c.rows, "C")
                self.assertEqual(rb.household_digest(c.src, TABLES), c.src_digest,
                                 f"seed {seed} C: the household changed")

    def test_d_into_another_household_loses_nothing(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                d = c.d()
                self.assert_one_outcome_each(seed, c.rows, d["out"], "D")
                for table in ROWS:
                    mode, keys = rb.IDENTITY[table]
                    check = {"one": self._d_one, "counted": self._d_counted,
                             "group": self._d_group}[mode]
                    check(seed, c, d, table, keys)
                self.assert_w2_figures_are_their_sums(seed, d["dest"], "D")

    def _d_one(self, seed, c, d, table, keys):
        dest = d["dest"]
        after = [keyed(dest, table, k) for k in keys]
        lost = []
        for row in rb.key_rows(c.src, table, keys[0], landing=True):
            k0 = keys[0].ident(keys[0].follow(row, d["merchant_map"])
                               if keys[0].follow else row)
            if k0 is None or k0 in after[0]:
                continue
            lost.append(keys[0].ident(row))
        # a row held under another of its keys is held: the alternative
        # keys say which destination row took its place
        if lost and len(keys) > 1:
            lost = self._held_under_another_key(c, d, table, keys, lost,
                                                after)
        if lost and table == "merchants":
            lost = self._not_pruned(c, d, lost)
        if lost and table == "account_adoptions":
            # the destination's own account under that id has no
            # connection, and an adoption is a connection's history: the
            # restore reports the row orphaned
            lost = [k for k in lost if k[0] not in d["unlinked"]]
        self.assertEqual(lost, [], f"seed {seed} D: {table} lost archived "
                                   "rows")
        dest_lost = sorted((d["before"][table][0] - after[0]).elements())
        self.assertEqual(dest_lost, [], f"seed {seed} D: {table} lost the "
                                        "destination's own rows")
        for k, got in zip(keys, after):
            twice = {v: n for v, n in got.items() if n > 1}
            self.assertEqual(twice, {}, f"seed {seed} D: {table} holds an "
                                        f"identity twice under {k.cols}")

    def _not_pruned(self, c, d, lost):
        """The archived merchants of `lost` the restore had no reason to
        drop. The identity pass that ends every restore removes a merchant
        nothing points at (merchant_identity.prune_empty), as it does
        nightly; an archived merchant every one of whose charges, aliases
        and 1099 marks the destination already held under that row's own
        key lands with nothing pointing at it, and goes."""
        held_txns = set(d["before_amounts"])
        held_aliases = {k[0] for k in d["before"]["merchant_canonical"][0]}
        held_marks = {k[0] for k in d["before"]["vendor_1099"][0]}
        still = []
        for (mid,) in lost:
            def ids(sql):
                return {next(iter(r.values())) for r in c.src.execute(
                    sql, (mid,)).fetchall()}
            pointed = (
                ids("SELECT id FROM transactions WHERE merchant_id::text = %s")
                - held_txns,
                ids("SELECT raw_merchant FROM merchant_canonical"
                    " WHERE merchant_id::text = %s") - held_aliases,
                ids("SELECT id::text FROM vendor_1099"
                    " WHERE merchant_id::text = %s") - held_marks)
            if any(pointed):
                still.append((mid,))
        return still

    def _held_under_another_key(self, c, d, table, keys, lost, after):
        """The rows of `lost` no other of the table's keys holds either."""
        # every column aliased: a column two keys share must come back
        # twice, and a row read as a dict keeps one of each name
        cols = [f"{col} AS c{i}" for i, col in enumerate(
            col for k in keys for col in k.cols)] + [
            f"({k.where or 'true'}) AS covered_{i}" for i, k in enumerate(keys)]
        sql = (f"SELECT {', '.join(cols)} FROM {table}"
               + (f" WHERE {rb.LANDS[table]}" if table in rb.LANDS else ""))
        still = []
        for r in rb.query(c.src, table, sql):
            vals = [rb._norm(v) for v in r.values()]
            at, parts = 0, []
            for k in keys:
                parts.append(tuple(vals[at:at + len(k.cols)]))
                at += len(k.cols)
            covered = vals[at:]
            ident = keys[0].ident(parts[0])
            if ident not in lost:
                continue
            held = False
            for k, part, got, ok in zip(keys[1:], parts[1:], after[1:],
                                        covered[1:]):
                if not ok:
                    continue
                part = k.follow(part, d["merchant_map"]) if k.follow else part
                if k.ident(part) in got:
                    held = True
            if not held:
                still.append(ident)
        return still

    def _d_counted(self, seed, c, d, table, keys):
        dest = d["dest"]
        src = keyed(c.src, table, keys[0], landing=True)
        before = d["before"][table][0]
        after = keyed(dest, table, keys[0])
        want = Counter({k: max(src[k], before[k])
                        for k in set(src) | set(before)})
        self.assertEqual(after, want, f"seed {seed} D: {table} — each held "
                                      "copy stands for one archived copy")
        owners = Counter(rb.key_rows(dest, table,
                                     rb.Key(*keys[0].cols, "owner")))
        shrank = {k: (n, owners[k]) for k, n in d["before_owners"].items()
                  if owners[k] < n}
        self.assertEqual(shrank, {}, f"seed {seed} D: {table} — a person's "
                                     "own documents went")

    def _d_group(self, seed, c, d, table, keys):
        """A charge keeps the split the destination gave it; one with
        none takes the archive's, when the archive's adds up to the
        charge the destination holds under that id."""
        dest = d["dest"]
        src = keyed(c.src, table, keys[0], landing=True)
        before = d["before"][table][0]
        after = keyed(dest, table, keys[0])
        src_amounts = {r["id"]: r["amount"] for r in c.src.execute(
            "SELECT id, amount FROM transactions").fetchall()}
        want = Counter()
        for k in set(src) | set(before):
            txn = k[0]
            if before[k]:
                want[k] = before[k]
            elif (txn in d["before_amounts"]
                  and d["before_amounts"][txn] != src_amounts.get(txn)):
                continue        # another charge under the same id
            else:
                want[k] = src[k]
        self.assertEqual(after, want, f"seed {seed} D: {table}")

    def test_e_the_export_of_a_restore_restores_to_it(self):
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                again = export.build_zip(c.a)
                e = fresh_household()
                try:
                    _result, out = restore.restore_outcomes(e, again)
                    self.assertEqual(out.refused, {},
                                     f"seed {seed} E: refused rows")
                    self.assertEqual(rb.household_digest(e, TABLES), c.a_digest,
                                     f"seed {seed} E: not a fixed point")
                finally:
                    e.close()

    def test_f_an_archive_without_its_manifest_restores_the_same(self):
        oldest = archive_upgrades.migration_names()[0]
        for seed in rb.seeds():
            with self.subTest(seed=seed), self.using(seed) as c:
                for what, doc in (("F (no manifest)", None),
                                  ("F (oldest manifest)",
                                   {"format": archive_upgrades.MANIFEST_FORMAT,
                                    "schema": oldest})):
                    data = rewrite_manifest(c.data, doc)
                    f = fresh_household()
                    try:
                        _result, out = restore.restore_outcomes(f, data)
                        self.assert_lands_whole(seed, c.src, f, out, c.rows, what)
                        digest = rb.household_digest(f, TABLES)
                        result, out = restore.restore_outcomes(f, data)
                        self.assert_writes_nothing(seed, out, result, c.rows,
                                                   what + " again")
                        self.assertEqual(rb.household_digest(f, TABLES), digest,
                                         f"seed {seed} {what} again: changed")
                    finally:
                        f.close()

    def test_rewritten_content_lands_once(self):
        """A card number in free text and a descriptor past the longest
        real one are rewritten on the way in, so the destination differs
        from the archive; a second restore of it still writes nothing, and
        neither does a restore into the household the archive came from,
        which holds the uncut spelling: the cut one is the same row."""
        for seed in rb.seeds()[:2]:
            with self.subTest(seed=seed):
                src = fresh_household()
                dest = fresh_household()
                try:
                    h = rb.build(src, seed)
                    rb.rewritten_rows(h)
                    data = export.build_zip(src)
                    rows = archived_rows(data)
                    restore.restore_outcomes(dest, data)
                    digest = rb.household_digest(dest, TABLES)
                    result, out = restore.restore_outcomes(dest, data)
                    self.assert_writes_nothing(seed, out, result, rows,
                                               "rewritten, again")
                    self.assertEqual(rb.household_digest(dest, TABLES),
                                     digest, f"seed {seed}: changed")
                    own = rb.household_digest(src, TABLES)
                    result, out = restore.restore_outcomes(src, data)
                    self.assert_writes_nothing(seed, out, result, rows,
                                               "rewritten, into itself")
                    self.assertEqual(rb.household_digest(src, TABLES), own,
                                     f"seed {seed}: its own archive changed "
                                     "the household")
                finally:
                    src.close()
                    dest.close()

    # -- the W-2 promise --------------------------------------------------

    def assert_w2_figures_are_their_sums(self, seed, conn, what):
        """A wage figure the W-2s own (empty, or still what they last
        wrote) is the sum of that person's read W-2s, and so is the record
        of what they wrote."""
        rows = conn.execute(
            """SELECT y.year, a.primary_wages, a.primary_wages_w2,
                      a.spouse_wages, a.spouse_wages_w2,
                      (SELECT round(SUM(primary_amount)::numeric, 2)
                         FROM income_documents d
                        WHERE d.year = y.year AND form = 'W-2'
                          AND owner = 'primary' AND primary_amount <> 0) AS p,
                      (SELECT round(SUM(primary_amount)::numeric, 2)
                         FROM income_documents d
                        WHERE d.year = y.year AND form = 'W-2'
                          AND owner = 'spouse' AND primary_amount <> 0) AS s
                 FROM (SELECT DISTINCT year FROM income_documents
                        WHERE form = 'W-2') y
                 LEFT JOIN income_annual a ON a.year = y.year""").fetchall()
        for r in rows:
            for fig, mark, total in (("primary_wages", "primary_wages_w2",
                                      "p"),
                                     ("spouse_wages", "spouse_wages_w2", "s")):
                if r[fig] is not None and r[fig] != r[mark]:
                    continue        # another writer's figure: left alone
                got = None if r[mark] is None else round(r[mark], 2)
                want = None if r[total] is None else float(r[total])
                self.assertEqual(got, want, f"seed {seed} {what}: {r['year']}"
                                            f" {fig} is not its W-2s' sum")

    # -- golden digests ---------------------------------------------------

    def test_golden_digests(self):
        observed = {}
        for seed in rb.FIXED_SEEDS:
            with self.using(seed) as c:
                d = c.d()
                observed[str(seed)] = {
                    "source": {"rows": c.src_digest},
                    "A": {"tally": tally(c.a_out), "rows": c.a_digest},
                    "D": {"tally": tally(d["out"]), "rows": d["digest"]},
                }
        if REGEN:
            GOLDEN.write_text(json.dumps(observed, indent=1, sort_keys=True)
                              + "\n")
            return
        self.assertTrue(GOLDEN.exists(), "no golden digests: run with "
                                         "OIKONOME_RT_REGEN=1")
        golden = json.loads(GOLDEN.read_text())
        for seed, want in golden.items():
            for scenario, doc in want.items():
                got = observed[seed][scenario]
                self.assertEqual(got.get("tally"), doc.get("tally"),
                                 f"seed {seed} {scenario}: outcomes moved")
                moved = sorted(t for t in doc["rows"]
                               if got["rows"].get(t) != doc["rows"][t])
                self.assertEqual(moved, [], f"seed {seed} {scenario}: what "
                                            "landed in these tables changed")

    def test_zz_the_seeds_run_inside_the_suite_budget(self):
        """Runs last (unittest orders by name): the whole class, setup
        included, stays inside a minute for the fixed seeds."""
        if rb.seeds() != rb.FIXED_SEEDS:
            self.skipTest("extra seeds requested")
        self.assertLess(time.monotonic() - self.started, 60)


class ARestoreTakesRenameIdsFromTheSequence(unittest.TestCase):
    """A restore takes fresh merchant_renames ids from the instance-wide
    sequence; a destination household built beside it holds its own. With
    the sequence sitting just under where a fixture's ids would be, the
    restore must still land every rename."""

    def test_the_sequence_just_under_the_fixtures_ids(self):
        from oikonome.db import tenancy
        src = fresh_household()
        dest = fresh_household()
        try:
            rb.build(src, 7)
            rb.build(dest, 1007)
            data = export.build_zip(src)
            admin = tenancy.admin_connect()
            try:
                admin.execute("SELECT setval('merchant_renames_id_seq', "
                              "899)")
            finally:
                admin.close()
            _result, out = restore.restore_outcomes(dest, data)
            self.assertEqual(
                {k: v for k, v in out.refused.items()
                 if k[0] == "merchant_renames"}, {})
        finally:
            src.close()
            dest.close()


if __name__ == "__main__":
    unittest.main()
