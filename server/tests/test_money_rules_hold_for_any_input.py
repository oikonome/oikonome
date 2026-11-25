"""The money rules that need no database, held for generated inputs
(specs/invariants-and-fuzzing.md, the pure money invariants).

* A split is accepted exactly when its parts are a valid partition of the
  charge, and an accepted split adds up to the charge to the cent. Any
  other input is a SplitError naming the part, never another exception.
* An envelope never spends past what is left of its pool, every claimed
  dollar is either inside the cap or overflow, the rows behind each figure
  add up to it, and overflow begins only once the pool is spent.
* A typed amount is either refused (400) or a finite number inside the
  money ceiling, and both separator conventions read back what was written.
* A retirement answer is either inside its range or no answer, for any JSON
  value — the cleaner the settings door and the restore share never raises.

Examples are derandomized (tests/props.py); OIKONOME_PROPERTY=long runs
many more, at random.
"""

from __future__ import annotations

import datetime as dt
import math
import unittest

from fastapi import HTTPException
from hypothesis import given
from hypothesis import strategies as st

from oikonome.engine import budget, splits
from oikonome.engine.categories import FLOW
from oikonome.sync import restore
from oikonome.web.fields import MONEY_MAX, Fields, money_float

from . import props  # noqa: F401  (loads the profile)

MAX_CENTS = int(splits.MAX_AMOUNT * 100)

# any JSON value, including what Python's reader also takes: NaN, the
# infinities and integers past float range
JSON_SCALARS = st.one_of(
    st.none(), st.booleans(),
    st.integers(), st.integers(min_value=10 ** 300, max_value=10 ** 400),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40))
# the numbers a number field meets: ordinary, at the edges of a double,
# and integers no double holds
NUMBERS = st.one_of(
    st.integers(-10 ** 6, 10 ** 6),
    st.integers(min_value=10 ** 300, max_value=10 ** 400),
    st.integers(max_value=-10 ** 300, min_value=-10 ** 400),
    st.floats(allow_nan=True, allow_infinity=True))
JSON = st.recursive(
    JSON_SCALARS,
    lambda inner: st.one_of(st.lists(inner, max_size=4),
                            st.dictionaries(st.text(max_size=8), inner,
                                            max_size=4)),
    max_leaves=12)

SPEND_CATS = [f"CATEGORY_{i}" for i in range(splits.MAX_PARTS + 2)]


def _cents(x: float) -> int:
    return int(round(x * 100))


@st.composite
def valid_splits(draw):
    """(row amount, parts): a charge and a partition of it into 2..MAX_PARTS
    nonzero parts of its sign, each under a distinct spending category."""
    n = draw(st.integers(2, splits.MAX_PARTS))
    cuts = draw(st.lists(st.integers(1, 10 ** 9), min_size=n, max_size=n))
    total = sum(cuts)
    sign = draw(st.sampled_from((1, -1)))
    cats = draw(st.permutations(SPEND_CATS))[:n]
    parts = [{"category": c, "amount": sign * k / 100}
             for c, k in zip(cats, cuts)]
    return sign * total / 100, parts


class ASplitIsAPartitionOfItsCharge(unittest.TestCase):

    @given(valid_splits())
    def test_a_valid_partition_is_accepted_and_adds_up_to_the_cent(self, case):
        row, parts = case
        out = splits.validate(row, parts)
        self.assertEqual([p["category"] for p in out],
                         [p["category"] for p in parts])
        self.assertEqual(sum(_cents(p["amount"]) for p in out), _cents(row))

    @given(valid_splits(), st.data())
    def test_any_one_flaw_is_refused_by_name(self, case, data):
        row, parts = case
        parts = [dict(p) for p in parts]
        i = data.draw(st.integers(0, len(parts) - 1))
        flaw = data.draw(st.sampled_from((
            "cent", "dup", "flow", "sign", "zero", "nan", "inf", "huge",
            "bool", "text", "missing", "blank")))
        p = parts[i]
        if flaw == "cent":
            p["amount"] = round(p["amount"] + 0.01, 2)
        elif flaw == "dup":
            p["category"] = parts[(i + 1) % len(parts)]["category"]
        elif flaw == "flow":
            p["category"] = data.draw(st.sampled_from(FLOW))
        elif flaw == "sign":
            p["amount"] = -p["amount"]
        elif flaw == "zero":
            p["amount"] = 0
        elif flaw == "nan":
            p["amount"] = math.nan
        elif flaw == "inf":
            p["amount"] = math.inf
        elif flaw == "huge":
            p["amount"] = 10 ** 400
        elif flaw == "bool":
            p["amount"] = True
        elif flaw == "text":
            p["amount"] = "12.50"
        elif flaw == "missing":
            del p["category"]
        else:
            p["category"] = "   "
        with self.assertRaises(splits.SplitError):
            splits.validate(row, parts)

    @given(st.floats(min_value=-1e12, max_value=1e12, allow_nan=False),
           JSON)
    def test_any_json_value_is_a_split_error_or_a_valid_split(self, row, parts):
        try:
            out = splits.validate(row, parts)
        except splits.SplitError:
            return
        self.assertEqual(sum(_cents(p["amount"]) for p in out), _cents(row))


@st.composite
def envelope_month(draw):
    """Envelopes (merchant-identity matched) with whole-cent pools, an
    annual prior use for some, and rows dated across a month."""
    n_env = draw(st.integers(1, 4))
    envelopes = [{"payee": f"Envelope {i}", "pool": draw(
        st.integers(0, 50_000)) / 100, "ids": {f"m{i}"}, "match_cat": None,
        "off": False} for i in range(n_env)]
    prior = {e["payee"]: draw(st.integers(0, 60_000)) / 100
             for e in envelopes if draw(st.booleans())}
    n_rows = draw(st.integers(0, 25))
    rows = []
    for j in range(n_rows):
        mid = draw(st.sampled_from([f"m{i}" for i in range(n_env + 1)]
                                   + [None]))
        rows.append({
            "txn_id": f"t{j}", "merchant_id": mid,
            "date": dt.date(2026, 7, draw(st.integers(1, 31))),
            "amount": draw(st.integers(1, 40_000)) / 100,
            "payee": "Store", "name": "Store", "category": "",
            "is_food": draw(st.booleans()), "pending": 0})
    return envelopes, prior, rows


class AnEnvelopeNeverSpendsPastItsPool(unittest.TestCase):

    @given(envelope_month())
    def test_the_pool_bounds_the_spend_and_every_dollar_is_counted(self, case):
        envelopes, prior, rows = case
        matched, states = budget.match_envelopes(rows, envelopes, set(),
                                                 prior_used=prior)
        by_id = {r["txn_id"]: r for r in rows}
        order = sorted(range(len(rows)), key=lambda i: rows[i]["date"])
        for stt in states:
            e = stt["bill"]
            left = max(0.0, e["pool"] - prior.get(e["payee"], 0.0))
            claimed = [rows[i] for i in order if i in matched
                       and rows[i]["merchant_id"] in e["ids"]]
            self.assertGreaterEqual(stt["used"], -1e-9)
            self.assertLessEqual(stt["used"], left + 1e-6)
            self.assertAlmostEqual(stt["used"] + stt["overflow"],
                                   sum(r["amount"] for r in claimed), places=6)
            slack = 0.005 * (len(claimed) + 1)
            self.assertLessEqual(
                abs(sum(r["amount"] for r in stt["rows"]) - stt["used"]), slack)
            self.assertLessEqual(
                abs(sum(r["amount"] for r in stt["overflow_rows"])
                    - stt["overflow"]), slack)
            # each claimed row is whole once, or two slices adding up to it
            inside = {r["txn_id"]: r["amount"] for r in stt["rows"]}
            over = {r["overflow_of"]: r["amount"]
                    for r in stt["overflow_rows"]}
            for r in claimed:
                got = inside.get(r["txn_id"], 0.0) + over.get(r["txn_id"], 0.0)
                self.assertAlmostEqual(got, by_id[r["txn_id"]]["amount"],
                                       places=6)
            # overflow begins only when the pool is spent: after the first
            # row that overflows, every later claimed row overflows whole
            spilled = False
            for r in claimed:
                if spilled:
                    self.assertNotIn(r["txn_id"], inside)
                if r["txn_id"] in over:
                    spilled = True
        # a row no envelope wants is left to the variable budgets
        for i, r in enumerate(rows):
            wanted = any(r["merchant_id"] in e["ids"] for e in envelopes)
            self.assertEqual(i in matched, wanted)


def _grouped(cents: int, thousands: str, decimal: str) -> str:
    whole, frac = divmod(abs(cents), 100)
    digits = f"{whole:,}".replace(",", thousands)
    return f"{'-' if cents < 0 else ''}{digits}{decimal}{frac:02d}"


class ATypedAmountIsRefusedOrAFiniteAmount(unittest.TestCase):

    @given(st.one_of(NUMBERS, JSON))
    def test_any_json_value_is_a_400_or_a_bounded_finite_number(self, v):
        for read in (lambda: money_float(v, "amount"),
                     lambda: Fields({"amount": v}).money("amount")):
            try:
                got = read()
            except HTTPException as e:
                self.assertEqual(e.status_code, 400)
                continue
            if got is None:            # absent, null and "" are no amount
                continue
            self.assertIsInstance(got, float)
            self.assertTrue(math.isfinite(got))
            self.assertLessEqual(abs(got), MONEY_MAX)

    @given(st.integers(-MAX_CENTS, MAX_CENTS))
    def test_both_separator_conventions_read_back_what_was_written(self, c):
        want = c / 100
        for text in (_grouped(c, ",", "."), _grouped(c, ".", ","),
                     _grouped(c, "", ".")):
            self.assertEqual(money_float(text, "amount"), want, text)

    @given(st.one_of(NUMBERS, JSON), st.integers(-50, 50), st.integers(0, 100))
    def test_a_whole_number_field_is_a_400_or_inside_its_range(self, v, lo, w):
        try:
            got = Fields({"n": v}).int_in("n", lo, lo + w, text_ok=True,
                                          truncate=True)
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)
            return
        if got is not None:
            self.assertIsInstance(got, int)
            self.assertTrue(lo <= got <= lo + w)


class ARetirementAnswerIsInRangeOrNoAnswer(unittest.TestCase):

    @given(st.one_of(NUMBERS, JSON))
    def test_any_json_value_is_an_answer_in_range_or_none(self, v):
        for key in restore.RET_WIZARD_ANSWERS:
            got = restore.ret_wizard_answer(key, v)
            if got is None:
                continue
            lo, hi = restore.RET_WIZARD_RANGES[key]
            self.assertTrue(math.isfinite(got) and lo <= got <= hi)
            if key in restore.RET_WHOLE_ANSWERS:
                self.assertTrue(float(got).is_integer())


if __name__ == "__main__":
    unittest.main()
