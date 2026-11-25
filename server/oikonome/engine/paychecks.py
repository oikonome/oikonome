"""Which payers are paycheck payers — the one decision every reader of the
income kinds (paychecks / interest & dividends / other) asks.

A payer is the display merchant of its deposits folded by letters and
digits (`payer_key`). It is a paycheck payer when the household said so,
when the aggregator tagged any of its deposits as wages, when it matches
an active income bill, or when its deposits read as payroll: described as
pay, on a regular cadence, at a check's size (`classify_payer`). The
household's word beats every rule. specs/paychecks.md has the reasoning.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import statistics
import unicodedata

# The settings key holding the household's word per payer: {key: bool}.
CONFIG_KEY = "paycheck_payers"

WAGES_TAG = "INCOME_WAGES"
INTEREST_TAGS = frozenset({"INCOME_INTEREST_EARNED", "INCOME_DIVIDENDS"})
_INTEREST_WORDS = frozenset({"interest", "dividend", "dividends"})

# A description that names pay. Payroll processors name the employer's run
# rather than the employer, so their names count as payroll words too.
PAYROLL_RE = re.compile(
    r"\b(payroll|payrl|pyrl|salary|salaries|wages?|paychecks?|pay ?check"
    r"|direct ?dep(osit)?|dir ?dep|paychex|gusto|adp)\b", re.IGNORECASE)

# A payer key: letters and digits only, the fold the income-by-source list
# groups display names by. Bounded so a stored word stays small.
KEY_MAX = 120
_KEY_RE = re.compile(r"^[a-z0-9]{1,%d}$" % KEY_MAX)

# cadence bands, in days between deposits: weekly, twice a month or
# biweekly, monthly
_BANDS = ((5, 9), (12, 19), (26, 35))
RUN_GAP_DAYS = 62            # a longer gap ends a run of deposits
MIN_RUN = 4                  # deposits in a run before it is a cadence
REGULAR_SHARE = 0.7          # gaps in one band / deposits at a check's size
SIZE_BAND = (0.6, 1.6)       # a check's size against the run's median


def payer_key(name: str | None) -> str:
    """The payer a display name stands for: its ASCII letters and digits,
    lowercased (the income-by-source fold). A name with none of those, or
    one whose fold runs past KEY_MAX, is keyed by a digest of its
    Unicode-folded name instead, so every named payer has a key the door
    and a restore accept (valid_key). '' for no name."""
    fold = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if fold and len(fold) <= KEY_MAX:
        return fold
    whole = "".join(c for c in unicodedata.normalize("NFKC", name or "")
                    .casefold() if c.isalnum()) or (name or "").strip()
    if not whole:
        return ""
    return "u" + hashlib.sha256(whole.encode()).hexdigest()[:40]


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def overrides(cfg: dict) -> dict[str, bool]:
    """The household's word per payer key, as stored; anything not shaped
    {key: bool} is ignored."""
    raw = cfg.get(CONFIG_KEY)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, bool)}


def payroll_worded(name: str | None) -> bool:
    return bool(PAYROLL_RE.search(name or ""))


def _regular_run(run: list[tuple[dt.date, float]]) -> bool:
    if len(run) < MIN_RUN:
        return False
    gaps = [(b[0] - a[0]).days for a, b in zip(run, run[1:])]
    in_band = max(sum(1 for g in gaps if lo <= g <= hi) for lo, hi in _BANDS)
    if in_band < REGULAR_SHARE * len(gaps):
        return False
    mid = statistics.median(a for _d, a in run)
    if mid <= 0:
        return False
    sized = sum(1 for _d, a in run
                if SIZE_BAND[0] * mid <= a <= SIZE_BAND[1] * mid)
    return sized >= REGULAR_SHARE * len(run)


def classify_payer(deposits: list[tuple[dt.date, float, str]]) -> bool:
    """Whether one payer's deposits ([(date, amount, description)], any
    order) read as payroll: a description that names pay, and a run of at
    least MIN_RUN deposits (same-day deposits merged) whose gaps keep to one
    cadence band and whose amounts keep to a check's size."""
    if not any(payroll_worded(n) for _d, _a, n in deposits):
        return False
    by_day: dict[dt.date, float] = {}
    for d, a, _n in deposits:
        by_day[d] = by_day.get(d, 0.0) + float(a or 0)
    days = sorted(by_day.items())
    run: list[tuple[dt.date, float]] = []
    for d, a in days:
        if run and (d - run[-1][0]).days > RUN_GAP_DAYS:
            if _regular_run(run):
                return True
            run = []
        run.append((d, a))
    return _regular_run(run)


class PayerVerdicts:
    """Every payer's verdict, and the kind of any income row under it."""

    def __init__(self, paycheck: dict[str, bool], words: dict[str, bool],
                 bill_tokens: set[str], tokens_fn):
        self._paycheck = paycheck
        self.words = words
        self._bill_tokens = bill_tokens
        self._tokens = tokens_fn
        effective = {**paycheck, **words}
        self.fingerprint = hashlib.md5(
            "|".join(f"{k}:{int(v)}" for k, v in sorted(effective.items()))
            .encode(), usedforsecurity=False).hexdigest()

    def is_paycheck(self, payee: str | None, name: str | None = None,
                    detailed: str | None = None) -> bool:
        """Whether a deposit from `payee` (its display merchant) is pay.
        A payer outside the scan (a row the verdicts did not read) falls to
        the row's own tag and the income bills, the rules before the
        cadence."""
        key = payer_key(payee)
        if key in self.words:
            return self.words[key]
        if key in self._paycheck:
            return self._paycheck[key]
        return (detailed == WAGES_TAG
                or bool(self._bill_tokens & self._tokens(name or "")))

    def kind(self, payee: str | None, name: str | None,
             detailed: str | None) -> str:
        """paychecks / interest / other for one income row."""
        if self.is_paycheck(payee, name, detailed):
            return "paychecks"
        if detailed in INTEREST_TAGS:
            return "interest"
        if self._tokens(name or "") & _INTEREST_WORDS:
            return "interest"
        return "other"

    def source(self, payee: str | None) -> dict:
        """What a client prints beside an income-by-source row."""
        key = payer_key(payee)
        word = self.words.get(key)
        return {"key": key,
                "paycheck": (word if word is not None
                             else self._paycheck.get(key, False)),
                "override": (None if word is None
                             else "paycheck" if word else "never")}


def payer_verdicts(conn, *, memo: dict | None = None) -> PayerVerdicts:
    """THE answer to "is this payer a paycheck payer", for every payer that
    ever paid into a bank account (the ledger's income rows, the household's
    excluded accounts left out, the whole history). Read once per request
    when `memo` is passed."""
    if memo is not None and "paycheck_verdicts" in memo:
        return memo["paycheck_verdicts"]
    from . import budget, reporting
    cfg = budget.load_config(conn, memo=memo)
    words = overrides(cfg)
    bill_tokens = {k for k in (budget._key_token(r["payee"] or "")
                               for r in conn.execute(
        "SELECT payee FROM bills WHERE amount > 0 AND active = 1")) if k}
    by_payer: dict[str, list[tuple[dt.date, float, str]]] = {}
    tagged: set[str] = set()
    billed: set[str] = set()
    for r in reporting.income_payer_rows(conn):
        key = payer_key(r["payee"])
        by_payer.setdefault(key, []).append(
            (r["date"], float(r["amt"] or 0), r["name"] or ""))
        if r["d"] == WAGES_TAG:
            tagged.add(key)
        if bill_tokens and bill_tokens & budget._tokens(r["name"] or ""):
            billed.add(key)
    paycheck = {k: (k in tagged or k in billed or classify_payer(rows))
                for k, rows in by_payer.items()}
    out = PayerVerdicts(paycheck, words, bill_tokens, budget._tokens)
    if memo is not None:
        memo["paycheck_verdicts"] = out
    return out
