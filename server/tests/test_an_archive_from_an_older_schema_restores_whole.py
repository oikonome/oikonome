"""An archive an older install wrote restores whole, and once
(specs/invariants-and-fuzzing.md, R7).

`archive_eras` rewrites a generated household's export into the shape an
install at an older migration wrote: the tables and columns it had then,
the bill tables under their old names, a set-aside merchant name spelled
the old way, and a manifest naming that migration. Restored into an empty
household, such an archive

* finishes, every archived row with exactly one outcome, and refuses
  nothing but what the destination rebuilds (round-trip scenario A);
* loses nothing: every row the source holds is there under its identity,
  wherever the era's columns can name it, and the restore adds rows only
  where it derives them on purpose for an archive that old (`DERIVED`);
* restored again, writes nothing and changes nothing (scenario B);
* repays no charge past what it cost and claims no deposit past what it
  brought in (the restore cuts such links), and
  breaks none of the other money invariants of the concurrency harness (a
  split sums to its row) that the source household kept: the builders
  write rows directly, and a source that carries one odd row restores it
  as it is.

By default one household and two eras: the one before the bill rename
(most tables missing, both renamed members) and the one with the overruled
flag. OIKONOME_PROPERTY=long runs every era in which the archive's shape
changed, for every seeded household (`props.seeds()`); a failure names the
seed and the era.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import archive_upgrades as au
from oikonome.sync import export, restore

from . import archive_eras as ae
from . import props
from . import roundtrip_builders as rb
from .concurrency import money_invariants
from .test_every_table_round_trips_through_its_lander import (
    ROWS, TABLES, fresh_household)

# Rows a restore derives for an archive older than a migration, on
# purpose (archive_upgrades), so the destination may hold more of them
# than the source did. Nothing the source held may be missing.
DERIVED = {
    ("merchant_categories", "035_canonical_category_backfill.sql"):
        "the household's own category rules are rebuilt from its overrides",
    ("merchant_canonical", "097_merchants.sql"):
        "with no merchant ids in the archive, every name its rows carry is "
        "resolved afresh and the resolution recorded",
}

def charges_repaid_past_cost(conn) -> list[str]:
    """Charges whose paybacks (budget.REIMB_RECEIVED) exceed their cost."""
    return [f"{r['id']} repaid {r['got']} of {r['cost']}" for r in conn.execute(
        f"""SELECT t.id, ABS(t.amount) AS cost, {budget.REIMB_RECEIVED} AS got
              FROM transactions t
             WHERE EXISTS (SELECT 1 FROM reimbursements r
                            WHERE r.expense_id = t.id AND r.partial = 1)
               AND {budget.REIMB_RECEIVED} > ABS(t.amount) + 0.005
             ORDER BY t.id""").fetchall()]


def deposits_claimed_past_amount(conn) -> list[str]:
    """Deposits whose links (partial links plus the charges fully paired to
    them, web.data._DEPOSIT_LEFT) claim more than they brought in."""
    from oikonome.web.data import _DEPOSIT_LEFT
    return [f"{r['id']} claimed past its amount by {-r['left']:.2f}"
            for r in conn.execute(
        f"""SELECT t.id, {_DEPOSIT_LEFT} AS left
              FROM transactions t
             WHERE EXISTS (SELECT 1 FROM reimbursements r
                            WHERE r.reimburse_id = t.id AND r.partial = 1)
               AND ABS(t.amount) > 0.005
               AND {_DEPOSIT_LEFT} < -0.005
             ORDER BY t.id""").fetchall()]


DEFAULT_ERAS = (ae.before(ae.BILLS_RENAME), ae.OVERRULED_ADDED)

_OLD_NAMES = {"recurring": "bills", "recurring_proposals": "bill_proposals"}


def _member(z: zipfile.ZipFile, table: str) -> str | None:
    """The member holding `table`'s rows, under its old name if need be."""
    for name in (table, *(o for o, n in _OLD_NAMES.items() if n == table)):
        if f"{name}.csv" in z.namelist():
            return f"{name}.csv"
    return None


def _records(z: zipfile.ZipFile, member: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(z.read(member).decode("utf-8"),
                                       newline="")))


def _rows(data: bytes) -> dict[str, int]:
    """Rows per table, an old member counted under the table it became,
    the way the restore counts them."""
    z = zipfile.ZipFile(io.BytesIO(data))
    out = {}
    for table in ROWS:
        member = _member(z, table)
        if member:
            out[table] = sum(1 for _ in csv.DictReader(io.StringIO(
                z.read(member).decode("utf-8"))))
    return out


def _readable(table: str, data: bytes, era: str) -> bool:
    """Can the era's columns name the table's rows: no column the identity
    reads was stripped from the archive."""
    z = zipfile.ZipFile(io.BytesIO(data))
    member = _member(z, table)
    if member is None:
        return False
    head = (_records(z, member) or [[]])[0]
    _created, added = ae.history()
    gone = {c for (t, c), m in added.items()
            if t == table and au._order(era) < au._order(m)} - set(head)
    _mode, keys = rb.IDENTITY[table]
    words = set(re.findall(r"[a-z_][a-z0-9_]*", " ".join(keys[0].cols)))
    return not (words & gone)


class AnOlderArchiveRestoresWhole(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()

    def source(self, seed):
        """The seed's household (open: the caller closes it) and export."""
        conn = fresh_household()
        rb.build(conn, seed)
        return conn, export.build_zip(conn)

    def eras(self, data) -> list[str]:
        if not props.LONG:
            return list(DEFAULT_ERAS)
        return [e for e in (ae.before(m) for m in
                            ae.shape_changes(ae.era_members(data))) if e]

    def test_an_older_archive_lands_whole_and_once(self):
        seeds = props.seeds() if props.LONG else (1,)
        for seed in seeds:
            # one source open at a time: the tenant pool is small
            src, data = self.source(seed)
            try:
                for era in self.eras(data):
                    with self.subTest(seed=seed, era=era):
                        self.check(seed, src, ae.era_archive(data, era), era)
            finally:
                src.close()

    def check(self, seed, src, data, era):
        what = f"seed {seed}, era {era}"
        rows = _rows(data)
        dest = fresh_household()
        try:
            _result, out = restore.restore_outcomes(dest, data)
            for table, n in rows.items():
                if table not in ROWS or table in rb.CONTROL:
                    continue
                refused = sum(v for (t, _), v in out.refused.items()
                              if t == table)
                self.assertEqual(out.written.get(table, 0) + refused, n,
                                 f"{what}: {table} archived {n} rows")
            self.assertEqual({k: v for k, v in out.refused.items()
                              if k[1] != "rebuilt"}, {},
                             f"{what}: refused rows of a clean archive")
            for table in ROWS:
                if table not in rows or not _readable(table, data, era):
                    continue
                want = rb.identities(src, table, landing=True)
                got = rb.identities(dest, table)
                self.assertEqual(
                    want - got, rb.Counter(),
                    f"{what}: {table} lost rows the archive carried")
                derived = any(t == table and au._order(era) < au._order(m)
                              for t, m in DERIVED)
                if not derived:
                    self.assertEqual(
                        got - want, rb.Counter(),
                        f"{what}: {table} gained rows nothing archived")
            # a charge is held to its cost, and a deposit to its amount,
            # whatever the source carried
            self.assertEqual(charges_repaid_past_cost(dest), [], what)
            self.assertEqual(deposits_claimed_past_amount(dest), [], what)
            kept = set(money_invariants(src))
            self.assertEqual([m for m in money_invariants(dest)
                              if m not in kept], [], what)
            digest = rb.household_digest(dest, TABLES)
            _result, out = restore.restore_outcomes(dest, data)
            self.assertEqual({t: n for t, n in out.written.items()
                              if n and t in ROWS}, {},
                             f"{what} again: wrote rows")
            self.assertEqual({k: v for k, v in out.refused.items()
                              if k[1] not in ("present", "rebuilt")}, {},
                             f"{what} again: reported more than "
                             "'already present'")
            self.assertEqual(rb.household_digest(dest, TABLES), digest,
                             f"{what} again: changed the household")
        finally:
            dest.close()

    def test_the_era_parser_sees_every_column_that_dates_an_archive(self):
        """Each column `FLOOR_COLUMNS` dates an archive by is one the
        parser finds added by that migration: a migration spelled in a way
        the parser misses fails here, not as an era left unshaped."""
        _created, added = ae.history()
        for migration, member, column in au.FLOOR_COLUMNS:
            self.assertEqual(added.get((member[:-4], column)), migration,
                             f"{member} {column}")

    def test_an_era_archive_is_shaped_as_its_install_wrote_it(self):
        src, data = self.source(1)
        src.close()
        old = ae.era_archive(data, ae.before(ae.BILLS_RENAME))
        members = ae.era_members(old)
        self.assertIn("recurring", members)
        self.assertIn("recurring_proposals", members)
        self.assertNotIn("bills", members)
        self.assertNotIn("merchants", members)
        self.assertNotIn("transaction_splits", members)
        z = zipfile.ZipFile(io.BytesIO(old))
        self.assertEqual(json.loads(z.read(au.MANIFEST))["schema"],
                         ae.before(ae.BILLS_RENAME))
        self.assertIsNone(au.archive_floor(z))
        flagged = ae.era_archive(data, ae.OVERRULED_ADDED)
        head = _records(zipfile.ZipFile(io.BytesIO(flagged)),
                        "transactions.csv")[0]
        self.assertIn("merchant_name_overruled", head)
        self.assertNotIn("merchant_name_set_aside", head)

    def test_zz_the_default_run_stays_cheap(self):
        if props.LONG:
            self.skipTest("long mode")
        self.assertLess(time.monotonic() - self.started, 15)


if __name__ == "__main__":
    unittest.main()
