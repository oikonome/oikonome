"""PostgreSQL cuts a number exactly as Python does.

The stored-name scrub selects rows in SQL and cuts them in Python, and a
migration cuts stored text in SQL alone; if the two read a name
differently, a migration leaves behind a number the next import cuts
again, and every id hashed over it stops matching. So the SQL is built
from the module's own patterns (card_number.sql_cut, sql_selector) and run
here over the shared fixture and random text.

Migrations 203 and 204 are frozen copies of an earlier cut. They are read
from their files, run, and held to the module: every text they cut, the
module cuts the same way, except the cases the fixture names as the
module's wider reading (FROZEN_DIFFERS); and what they stored for those,
the module cuts on to its own answer, except FROZEN_LEAVES.

Migration 206 pasted the module's cut as it stood then, which joined the
groups after the fourth with a back-reference. PostgreSQL back-tracks on a
back-reference, and on text dense with groups of four that cut took time
growing with the fourth power of the length. The module now spells each
separator out, so the SQL is linear; 206 is sealed and stays the record
of what ran, pinned below to a frozen copy and held to the module's
answers, except the separators the module has read since (TABBED: any
whitespace is a space now). The time the SQL cut takes on adversarial text is checked here
too, under a statement timeout, so a back-reference creeping back fails
fast instead of hanging the suite."""
import random
import re
import time
import unittest
from pathlib import Path

from oikonome.db import tenancy
from oikonome.sync import base, card_number

from .card_number_fixture import CASES, FROZEN_DIFFERS, FROZEN_LEAVES, TABBED
from .util import TEST_DB, _admin_dsn, _ensure_db

MIGRATIONS = (Path(base.__file__).resolve().parents[1] / "db" / "migrations")
FROZEN = ("203_grouped_card_numbers_stored_on_file_rows_are_cut.sql",
          "204_card_numbers_in_merchant_names_are_cut.sql")
# the migration that re-cuts what the frozen two left, with the module's
# cut as it stood then pasted in whole
CURRENT = "206_card_numbers_in_bills_offers_and_the_activity_log_are_cut.sql"

# the cut 206 pasted, frozen: the module's grouped pattern as it stood then
_FROZEN_206_GROUPED = (
    r"…?(?<!\d)(?:\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}([ ./-]{1,3})"
    r"(?:\d{4}\1)*(\d{4})|\d{4}[ ./-]{1,3}\d{6}[ ./-]{1,3}\d(\d{4}))(?!\d)")


def _frozen_206_cut_function() -> str:
    def one(e):
        return (f"regexp_replace(regexp_replace({e}, '{_FROZEN_206_GROUPED}', "
                f"'…\\2\\3', 'g'), '\\d{{4,}}(\\d{{4}})', '…\\1', 'g')")
    return ("CREATE OR REPLACE FUNCTION pg_temp.cut_numbers(v text)\n"
            "RETURNS text LANGUAGE sql IMMUTABLE AS $$\n"
            f"    SELECT {one(one('v'))}\n$$;")


_FROZEN_206_SELECTOR = (
    r"\d{8}|\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}"
    r"|\d{4}[ ./-]{1,3}\d{6}[ ./-]{1,3}\d{5}")

# text as dense with groups of four as text can be: a number every twenty
# characters, each followed by a group joined differently
_ADVERSARIAL = ("4000 0000 0000 0002-", "2023-2024 ", "1111.2222.3333.4444 / ")


def _function(sql: str, name: str) -> str:
    m = re.search(rf"CREATE OR REPLACE FUNCTION pg_temp\.{name}\(.*?\$\$;",
                  sql, re.S)
    assert m, name
    return m.group()


class SqlParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        cls.conn = tenancy.admin_connect(_admin_dsn(TEST_DB))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _sql_cut(self, texts):
        rows = self.conn.execute(
            f"SELECT {card_number.sql_cut('x.v')} AS c"
            "  FROM unnest(%s::text[]) WITH ORDINALITY x(v, n) ORDER BY x.n",
            (list(texts),)).fetchall()
        return [r["c"] for r in rows]

    def _selected(self, texts):
        rows = self.conn.execute(
            "SELECT x.v ~ %s AS hit"
            "  FROM unnest(%s::text[]) WITH ORDINALITY x(v, n) ORDER BY x.n",
            (card_number.sql_selector(), list(texts))).fetchall()
        return [r["hit"] for r in rows]

    def test_the_fixture_cuts_alike(self):
        texts = [t for t, _ in CASES]
        self.assertEqual(self._sql_cut(texts), [w for _, w in CASES])

    def test_the_selector_finds_every_number_in_sql(self):
        texts = [t for t, w in CASES if t != w]
        self.assertTrue(all(self._selected(texts)))
        # the name scrub and the migrate's household search read it
        self.assertEqual(base.NUMBERED_NAME_PATTERN, card_number.sql_selector())

    def test_random_text_cuts_alike(self):
        rnd = random.Random(20261106)
        alphabet = "0123456789" * 4 + " ./-…a\t\u00a0\n"
        texts = ["".join(rnd.choice(alphabet)
                         for _ in range(rnd.randint(1, 48)))
                 for _ in range(5000)]
        got = self._sql_cut(texts)
        bad = [(t, g, card_number.cut(t)) for t, g in zip(texts, got)
               if g != card_number.cut(t)]
        self.assertEqual(bad, [])
        sel = self._selected(texts)
        self.assertEqual([t for t, s, g in zip(texts, sel, got)
                          if g != t and not s], [])

    def test_the_function_a_migration_defines_is_the_cut(self):
        with self.conn.transaction():
            self.conn.execute(card_number.sql_cut_function("pg_temp.cut_u"))
            got = [self.conn.execute("SELECT pg_temp.cut_u(%s) AS c",
                                     (t,)).fetchone()["c"] for t, _ in CASES]
        self.assertEqual(got, [w for _, w in CASES])

    def test_migration_206_is_its_frozen_cut_and_agrees_with_the_module(self):
        """Pasted, not imported: a sealed file is the record of what ran.
        It is held to the frozen copy above, never to the module, so the
        module may move on; what it computes still matches the module on
        every case, so the rows it cut need no cutting again."""
        sql = (MIGRATIONS / CURRENT).read_text()
        self.assertEqual(_function(sql, "cut_numbers"),
                         _frozen_206_cut_function())
        self.assertIn(f"SELECT v ~ '{_FROZEN_206_SELECTOR}'",
                      _function(sql, "may_hold_number"))
        cases = [(t, w) for t, w in CASES if t not in TABBED]
        texts = [t for t, _ in cases]
        with self.conn.transaction():
            self.conn.execute(_function(sql, "cut_numbers"))
            self.conn.execute(_function(sql, "may_hold_number"))
            self.conn.execute(_function(sql, "bill_id"))
            rows = self.conn.execute(
                "SELECT pg_temp.cut_numbers(x.v) AS c,"
                "       pg_temp.may_hold_number(x.v) AS hit,"
                "       pg_temp.bill_id(pg_temp.cut_numbers(x.v)) AS id"
                "  FROM unnest(%s::text[]) WITH ORDINALITY x(v, n)"
                " ORDER BY x.n", (texts,)).fetchall()
        from oikonome.engine import bills
        self.assertEqual([r["c"] for r in rows], [w for _, w in cases])
        self.assertTrue(all(r["hit"] for t, r in zip(texts, rows)
                            if card_number.cut(t) != t))
        # a moved bill's id is the one bills._slug gives its new name
        self.assertEqual([r["id"] for r in rows],
                         [bills._slug(w) for _, w in cases])
        # what 206 left of the cases it does not read, the module cuts
        left = [t for t in TABBED if card_number.cut(t) == t]
        self.assertEqual(left, [])

    def test_the_selector_is_206s_with_whitespace_as_a_space(self):
        self.assertEqual(
            card_number.sql_selector(),
            _FROZEN_206_SELECTOR.replace(
                "[ ./-]", "[ \\t\\n\\v\\f\\r\\u00a0./-]"))

    def test_the_sql_cut_uses_no_back_reference(self):
        # a back-reference is what PostgreSQL back-tracks on
        self.assertIsNone(re.search(r"\\[1-9]", card_number.GROUPED_PATTERN))
        self.assertIsNone(re.search(r"\\[1-9]", card_number.LONG_PATTERN))

    def _timed_cut(self, text: str, budget_s: int) -> tuple[float, str]:
        with self.conn.transaction():
            self.conn.execute(f"SET LOCAL statement_timeout = '{budget_s}s'")
            t0 = time.monotonic()
            got = self.conn.execute(
                f"SELECT {card_number.sql_cut('%s::text')} AS c",
                (text,)).fetchone()["c"]
            return time.monotonic() - t0, got

    def test_the_sql_cut_takes_linear_time_on_dense_numbers(self):
        """Measured before the separators were spelled out: 1,600 dense
        characters took 4 s and 3,200 took 62 s. Now each is milliseconds,
        and doubling the text about doubles the time."""
        for unit in _ADVERSARIAL:
            small = unit * (3200 // len(unit))
            took, got = self._timed_cut(small, 5)
            self.assertEqual(got, card_number.cut(small))
            self.assertLess(took, 1.0, unit)
            big = unit * (64_000 // len(unit))
            t_big, got = self._timed_cut(big, 30)
            self.assertEqual(got, card_number.cut(big))
            t_double, _ = self._timed_cut(big * 2, 60)
            # linear doubles; the back-reference grew sixteen-fold
            self.assertLess(t_double, 4 * t_big + 0.2, (unit, t_big, t_double))

    def test_random_number_shaped_text_cuts_alike(self):
        """Text built from groups and separators, so the joining rule
        after the fourth group is exercised in every combination."""
        rnd = random.Random(20261107)
        seps = card_number.SEPARATORS + ("", "", "x", "\t", " \t",
                                         "\u00a0", "\r\n", "\t-")
        groups = ("1234", "5678", "56", "123456", "12345", "…", "0002")
        texts = ["".join(rnd.choice(groups) + rnd.choice(seps)
                         for _ in range(rnd.randint(1, 9)))
                 for _ in range(5000)]
        got = self._sql_cut(texts)
        self.assertEqual([(t, g) for t, g in zip(texts, got)
                          if g != card_number.cut(t)], [])

    def test_frozen_migrations_agree_with_the_module(self):
        texts = [t for t, _ in CASES]
        for name in FROZEN:
            sql = (MIGRATIONS / name).read_text()
            with self.conn.transaction():
                self.conn.execute(_function(sql, "cut_numbers"))
                sel = ("may_hold_grouped" if "may_hold_grouped" in sql
                       else "may_hold_number")
                self.conn.execute(_function(sql, sel))
                rows = self.conn.execute(
                    f"SELECT pg_temp.cut_numbers(x.v) AS c,"
                    f"       pg_temp.{sel}(x.v) AS hit,"
                    f"       x.v ~ %s AS mine"
                    "  FROM unnest(%s::text[]) WITH ORDINALITY x(v, n)"
                    " ORDER BY x.n",
                    (card_number.sql_selector(), texts)).fetchall()
            differs = set()
            for t, r in zip(texts, rows):
                want = card_number.cut(t)
                if r["c"] != t:
                    # what the migration cut, the module cuts too
                    self.assertNotEqual(want, t, (name, t[:60]))
                if r["c"] != want:
                    differs.add(t)
                if r["hit"]:
                    # what the migration selects, the module's selector does
                    self.assertTrue(r["mine"], (name, t[:60]))
            self.assertEqual(differs, FROZEN_DIFFERS, name)
            left = {t for t, r in zip(texts, rows) if t in differs
                    and card_number.cut(r["c"]) != card_number.cut(t)}
            self.assertEqual(left, FROZEN_LEAVES, name)


if __name__ == "__main__":
    unittest.main()
