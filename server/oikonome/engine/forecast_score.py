"""The cash forecast grades itself (specs/forecast-grading.md).

Every night `record` keeps what the forecast said (forecast_nights: the
checking account and its balance, each scenario's predicted balance 7 to 60
days ahead, its low point, the card payments it assumed, the pace and, for
INPUTS_KEPT_NIGHTS, the inputs that cannot be rebuilt later). When the day a
prediction pointed at arrives, that night's own starting balance is the real
balance: `graded_pairs` compares them, `summarise` says how close the
forecast has landed, `band` draws the chart's shaded band from the
forecast's own past misses, and `miss` explains one miss part by part.

Only "your usual" (the headline walk) is graded for accuracy. "Pay all cards
now" and "pay on the due date" are what-ifs: they are compared with the card
payments the household really made (`card_paying`), never graded.

Everything here but `record`, `current`, `rows`, `ledger_facts` and `miss`
is pure: rows in, figures out. Amounts in examples are invented.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass

from . import budget, forecast
from .compat import as_date

log = logging.getLogger(__name__)

HORIZONS = (7, 14, 30, 45, 60)
# the inputs the drill reads are kept this many nights (it explains the
# misses of the last 90 target days at the 30-day horizon); rows this many
SCORE_INPUTS_NIGHTS = 120
NIGHTS_KEPT = 400
# a checking balance pulled this long before the night is fresh
FRESH_HOURS = 36

# the track-record sentence: the longest of these horizons with enough
# graded results spread over enough days, in the last WINDOW_DAYS
SENTENCE_HORIZONS = (30, 14, 7)
WINDOW_DAYS = 90
MIN_PAIRS = 30
MIN_SPAN_DAYS = 45
# under this many separate stretches of the horizon, the sentence says the
# weeks its results rest on instead of claiming a period
INDEPENDENT_MIN = 6
# the "checks itself" note shows for this long after the first night
NOTE_DAYS = 60
# a lean is said when this share of the 30-day misses of the last
# BIAS_DAYS share a sign and the median is at least BIAS_MIN dollars and
# BIAS_INCOME_SHARE of the plan's monthly income
BIAS_DAYS = 60
BIAS_SHARE = 0.70
BIAS_MIN = 100.0
BIAS_INCOME_SHARE = 0.05
# the band: the 10th to 90th percentile of the misses whose day came in the
# last BAND_DAYS, the last BAND_RECENT_DAYS counted twice
BAND_DAYS = 180
BAND_RECENT_DAYS = 90
BAND_RECENT_WEIGHT = 2.0
# the pooled misses' 5th and 95th percentiles: overlapping misses are
# fewer separate results than they look, and this is the level that held
# the next weeks' balances 8 times in 10 on the benchmark's households
# (specs/forecast-grading.md, "How the band was chosen")
BAND_Q = (0.05, 0.95)
# the growth fit: |miss| ~ h^a from the median |miss| at these horizons
BAND_FIT_HORIZONS = (7, 14, 30)
BAND_FIT_MIN = 10
BAND_POWER_CLAMP = (0.3, 1.0)
BAND_POWER_DEFAULT = 0.5
# the band replaces the heavier line once this many misses are graded,
# this many of them at 7 days (about five weeks of nights)
BAND_MIN_PAIRS = 30
BAND_MIN_SHORT = 30
# under this many separate stretches of a horizon's history the band is
# floored at the heavier line's distance from the walk (band)
BAND_FLOOR_INDEPENDENT = 6
# a band narrower than this many dollars is floored the same way
BAND_MIN_WIDTH = 1.0
# "not counting money you moved between your own accounts": when taking
# the own-account transfers out of the misses halves their size or more
TRANSFER_SHARE = 0.5
# how a card payment out of checking is recognised (the rule
# payments_on_their_way and the timeline use)
CARD_PAYMENT = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
# real card payments within this of the usual's are "about as assumed"
CARD_GAP_SAME = 50.0
TRANSFERS = ("TRANSFER_IN", "TRANSFER_OUT")


def _c(x) -> int:
    """Dollars to whole cents."""
    return int(round(float(x) * 100))


# --- what is kept each night ------------------------------------------------

def _walk_at(w: dict, today: dt.date) -> dict:
    by = {d: v for d, v in w.get("series") or []}
    at = []
    for h in HORIZONS:
        v = by.get((today + dt.timedelta(days=h)).isoformat())
        at.append(None if v is None else _c(v))
    lo = w.get("min_date")
    neg = w.get("negative_date")
    out = {"at": at, "min": _c(w.get("min") or 0),
           "min_day": (as_date(lo) - today).days if lo else None,
           "neg_day": (as_date(neg) - today).days if neg else None}
    if "outflows" in w:
        outs = [(as_date(d), a) for d, a, _cid in w["outflows"]]
        out["cards_out"] = [
            _c(sum(a for d, a in outs if d <= today + dt.timedelta(days=h)))
            for h in HORIZONS]
    return out


def _now_cards_out(fc: dict, today: dt.date) -> list[int]:
    """What paying every card today assumes leaves checking for the cards
    by each horizon: today's card debt, then every dollar the usual walk
    charges to a card (the pace's card shares, the envelope drips on a
    card, the bills a card pays) on the day it is charged."""
    charges = [(as_date(d), a) for d, a, _cid in
               (fc.get("pace_usual") or {}).get("charges") or []]
    out = []
    for h in HORIZONS:
        end = today + dt.timedelta(days=h)
        out.append(_c(fc["card_debt"] + sum(a for d, a in charges
                                            if d <= end)))
    return out


def _event_kind(label: str, amount: float) -> str:
    if label.startswith("Card payment on its way"):
        return "on_way"
    if amount > 0:
        return "paycheck"
    # the walk's own savings events (a monthly plan, a month-end sweep),
    # named by the suffix the walk gives them — never a payee that happens
    # to say "savings"
    if label.endswith(("(savings plan)", "(sweep savings)")):
        return "sweep"
    return "bill"


def compact(fc: dict, today: dt.date) -> dict:
    """The night's row from a forecast built for `today` (pure): `walks`
    per scenario (balances at each horizon and the low point, in cents,
    days as offsets from `today`), `pace`, and the `inputs` the drill
    reads."""
    walks = {"usual": _walk_at(fc["pace_usual"], today),
             # "Pay all cards today": one payoff, then the usual pattern
             # (pace_today); a forecast without that walk falls back to
             # cash after card balances
             "now": _walk_at(fc.get("pace_today") or fc["pace_now"], today),
             "due": _walk_at(fc["pace_due"], today),
             "low": _walk_at(fc["pace_low"], today),
             # checking less every dollar owed on cards that day — the
             # quiet warning's walk, not a scenario
             "after_cards": _walk_at(fc["pace_now"], today)}
    if "cards_out" not in walks["now"]:
        walks["now"]["cards_out"] = _now_cards_out(fc, today)
    walks["usual"]["after"] = _c(fc.get("cards_after_horizon") or 0)
    heavy = fc.get("heavy_factor") or {}
    pace = {"daily": round(float(fc["pace_daily"]), 4),
            "basis": (fc.get("pace_basis") or {}).get("basis"),
            "days": (fc.get("pace_basis") or {}).get("days"),
            "shares": {k: round(float(v), 4)
                       for k, v in (fc.get("pace_shares") or {}).items()},
            "heavy": heavy.get("factor")}
    usual = fc["pace_usual"]
    events = [[(as_date(d) - today).days, _c(a), p, _event_kind(p, a), None]
              for d, a, p in usual.get("checking_events") or []]
    events += [[(as_date(d) - today).days, _c(a), p, "card_bill", card]
               for d, a, p, card in usual.get("charged_bills") or []]
    inputs = {"cards": [{"card": t["card"], "mode": t["mode"],
                         "lag": t.get("lag")}
                        for t in fc.get("card_timing") or []],
              "card_debt": _c(fc.get("card_debt") or 0),
              "events": events,
              "one_offs": [[str(o.get("txn_id")), _c(o["amount"]),
                            o.get("payee")]
                           for o in fc.get("one_offs") or []]}
    return {"walks": walks, "pace": pace, "inputs": inputs}


def record(conn, today: dt.date, *, memo: dict | None = None) -> bool:
    """Keep tonight's forecast: one forecast_nights row for the household's
    day `today`, built exactly as Today builds it. The first record of a
    day stands (a second run lands nothing). No checking account, or one
    whose balance nobody has set: no row. Then the grading as of tonight is
    stored in the row (`score`), and rows past their keep are trimmed.
    Whether a row landed."""
    # An account move (a reconnect in the web process, a restore-merge)
    # can commit between the build and the insert: the night would land
    # on the id the move just deleted, after the move carried the nights,
    # and nothing would carry it again. So the insert holds every account
    # the night names (FOR KEY SHARE, which waits for a move's FOR UPDATE
    # and then finds a moved row gone), and a night naming one that is
    # gone is built again from the moved accounts — once: a second move
    # inside one build is no night tonight, and the next step keeps it.
    for _try in (0, 1):
        cfg = budget.load_config(conn, memo=memo)
        acct, _bal, _avail, unset = forecast.checking_pick(conn, cfg)
        if acct is None or unset:
            return False
        fc = forecast.build(conn, today=today, memo=memo)
        if fc.get("no_checking"):
            return False
        row = compact(fc, today)
        named = sorted({acct} | {k for k in row["pace"]["shares"]
                                 if k != forecast.CHECKING})
        with conn.transaction():
            held = {r["id"] for r in conn.execute(
                "SELECT id FROM accounts WHERE id = ANY(%s) "
                "ORDER BY id FOR KEY SHARE", (named,)).fetchall()}
            if held != set(named):
                landed = moved = None
            else:
                moved = False
                landed = conn.execute(
                    """INSERT INTO forecast_nights
                           (as_of, model, checking_id, start_checking, fresh,
                            walks, pace, inputs)
                       SELECT %s, %s, a.id, %s,
                              COALESCE(a.updated_at >= now()
                                       - make_interval(hours => %s), false)
                              OR EXISTS (SELECT 1 FROM sync_log s
                                         WHERE s.item_id = a.item_id
                                           AND s.error IS NULL
                                           AND s.ran_at >= now()
                                               - make_interval(hours => %s)),
                              %s::jsonb, %s::jsonb, %s::jsonb
                         FROM accounts a WHERE a.id = %s
                       ON CONFLICT (tenant_id, as_of) DO NOTHING
                       RETURNING as_of""",
                    (today, forecast.FORECAST_MODEL, float(fc["checking"]),
                     FRESH_HOURS, FRESH_HOURS, json.dumps(row["walks"]),
                     json.dumps(row["pace"]), json.dumps(row["inputs"]),
                     acct)).fetchone()
        if moved is False:
            break
        # the checkout's memo holds the settings and facts read before
        # the move; the rebuild reads them again
        memo = None
    trim(conn, today)
    if landed is None:
        return False
    try:
        store_score(conn, today, cfg=cfg)
    except Exception as e:                       # noqa: BLE001
        log.warning("forecast score not stored: %s", e)
    return True


# the household-local hour from which a day's night is kept (the hourly
# step's, jobs/worker.FORECAST_LOCAL_HOUR, and a Today build's when the
# step has not run yet)
RECORD_LOCAL_HOUR = 4


def record_if_due(conn, today: dt.date) -> bool:
    """Keep today's night now when the household's clock has passed
    RECORD_LOCAL_HOUR and it has none (the first build of a day, before
    the hourly step reaches it). Whether a row landed."""
    from .. import localtime
    cfg = budget.load_config(conn)
    now = localtime.now_local(cfg)
    if now.date() != today or now.hour < RECORD_LOCAL_HOUR:
        return False
    if conn.execute("SELECT 1 FROM forecast_nights WHERE as_of = %s",
                    (today,)).fetchone():
        return False
    return record(conn, today)


def trim(conn, today: dt.date) -> None:
    """The keep: inputs go after SCORE_INPUTS_NIGHTS nights, rows after
    NIGHTS_KEPT. Idempotent."""
    conn.execute("UPDATE forecast_nights SET inputs = NULL "
                 "WHERE inputs IS NOT NULL AND as_of < %s",
                 (today - dt.timedelta(days=SCORE_INPUTS_NIGHTS),))
    conn.execute("DELETE FROM forecast_nights WHERE as_of < %s",
                 (today - dt.timedelta(days=NIGHTS_KEPT),))


# --- how it is graded -------------------------------------------------------

@dataclass(frozen=True)
class Pair:
    as_of: dt.date
    h: int
    target: dt.date
    predicted: float
    actual: float
    error: float           # actual − predicted
    rebuilt: bool
    model: str


SKIP_REASONS = ("stale", "account_changed", "restore", "offline")


def rows(conn, *, upto: dt.date | None = None) -> list[dict]:
    """The household's kept nights, oldest first (the columns grading
    reads). With `upto`, only the keep's own window (NIGHTS_KEPT nights
    through `upto`): a restored row dated outside it is no night."""
    q = ("SELECT as_of, model, checking_id, start_checking, fresh, "
         "restored, walks FROM forecast_nights")
    args: tuple = ()
    if upto is not None:
        q += " WHERE as_of <= %s AND as_of >= %s"
        args = (upto, upto - dt.timedelta(days=NIGHTS_KEPT))
    return [dict(r) for r in conn.execute(q + " ORDER BY as_of",
                                          args).fetchall()]


def kept_cents(walks, key: str, field: str) -> list[int | None]:
    """A kept night's `walks[key][field]` — one entry per HORIZONS, whole
    cents or None — read whatever JSON shape the row carries: a restored
    row whose walk is a list, or whose entries are strings, is no
    forecast there, and its entries are None. Every reader of a kept
    night's walks reads them through this."""
    w = walks.get(key) if isinstance(walks, dict) else None
    v = w.get(field) if isinstance(w, dict) else None
    v = v if isinstance(v, list) else []
    out: list[int | None] = []
    for i in range(len(HORIZONS)):
        x = v[i] if i < len(v) else None
        ok = (isinstance(x, (int, float)) and not isinstance(x, bool)
              and math.isfinite(x))
        out.append(int(round(x)) if ok else None)
    return out


def _kept_events(inputs) -> list:
    """A kept night's input events — each exactly [offset (int), cents
    (int), label (str), kind (str), card (str or null)] — the malformed
    ones (any other JSON a restored row carries) left out."""
    evs = inputs.get("events") if isinstance(inputs, dict) else None
    return [e for e in evs if isinstance(e, list) and len(e) == 5
            and isinstance(e[0], int) and not isinstance(e[0], bool)
            and isinstance(e[1], int) and not isinstance(e[1], bool)
            and isinstance(e[2], str) and isinstance(e[3], str)
            and (e[4] is None or isinstance(e[4], str))
            ] if isinstance(evs, list) else []


def graded_pairs(nights: list[dict], today: dt.date,
                 moves=None) -> tuple[list[Pair], dict]:
    """Every (night, horizon) whose target day has been recorded, graded
    against the target night's own starting balance, and the skips by
    reason (pure). A target night that was not kept is not graded
    (offline). `moves` is accepted for older callers and not read."""
    by_day = {as_date(r["as_of"]): r for r in nights}
    pairs: list[Pair] = []
    skips = {k: 0 for k in SKIP_REASONS}
    last = today - dt.timedelta(days=1)
    for r in nights:
        d0 = as_date(r["as_of"])
        at = kept_cents(r["walks"], "usual", "at")
        for i, h in enumerate(HORIZONS):
            t = d0 + dt.timedelta(days=h)
            if t > last or at[i] is None:
                continue
            if not r["fresh"]:
                skips["stale"] += 1
                continue
            tgt = by_day.get(t)
            rebuilt = False
            if tgt is not None:
                if tgt["checking_id"] != r["checking_id"]:
                    skips["account_changed"] += 1
                    continue
                if bool(tgt["restored"]) != bool(r["restored"]):
                    skips["restore"] += 1
                    continue
                if not tgt["fresh"]:
                    skips["stale"] += 1
                    continue
                actual = float(tgt["start_checking"])
            else:
                # a night not kept is not rebuilt: the next night's balance
                # holds charges still pending that post later dated after
                # the missing day, so walking it back by the posted rows
                # between is wrong whenever a hold straddles the gap
                skips["offline"] += 1
                continue
            predicted = at[i] / 100
            pairs.append(Pair(d0, h, t, predicted, actual,
                              round(actual - predicted, 2), rebuilt,
                              r["model"]))
    # a night that was never kept made no forecasts: each of its horizons
    # whose day has come counts as offline too
    if by_day:
        # within the keep only: a night older than it was trimmed, not lost
        d = max(min(by_day), last - dt.timedelta(days=NIGHTS_KEPT))
        while d <= last:
            if d not in by_day:
                skips["offline"] += sum(
                    1 for h in HORIZONS if d + dt.timedelta(days=h) <= last)
            d += dt.timedelta(days=1)
    return pairs, skips


def weighted_quantile(vals: list[float], weights: list[float],
                      q: float) -> float:
    """The q-quantile of `vals` with `weights` (the lower of two values
    when the weight splits exactly between them; pure)."""
    pts = sorted(zip(vals, weights))
    total = sum(w for _v, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= q * total - 1e-9:
            return v
    return pts[-1][0]


def plain_up(x: float) -> float:
    """A figure rounded UP to a plain one: $10 steps under $200, $50 under
    $1,000, $100 above."""
    step = 10 if x < 200 else 50 if x < 1000 else 100
    # never "within $0": the smallest figure said is $10
    return max(10.0, float(math.ceil(x / step - 1e-9) * step))


def _p80(vals: list[float]) -> float:
    return weighted_quantile(vals, [1.0] * len(vals), 0.80)


def summarise(pairs: list[Pair], today: dt.date, *, income: float = 0.0,
              first_night: dt.date | None = None,
              skips: dict | None = None, transfers: dict | None = None,
              card_paid: dict | None = None,
              cards_out: dict | None = None,
              recorded: int | None = None) -> dict:
    """How close the forecast has landed (pure): the stored `score`.
    `transfers[(as_of, h)]` is the net of own-account transfers out of
    checking over the pair's days (positive = money out),
    `card_paid[(as_of, h)]` the card payments that really left checking,
    `cards_out[(as_of, h)]` each scenario's assumed card payments
    ({"usual": $, "now": $, "due": $})."""
    win0 = today - dt.timedelta(days=WINDOW_DAYS)
    out = {"as_of": today.isoformat(), "horizon": None, "n": 0,
           "span_days": 0, "within": None, "transfers_out": False,
           "typical_horizon": None, "typical": None, "typical_weeks": None,
           "window": None, "bias": None, "quantiles": {}, "coverage": None,
           "card_paying": None,
           "skips": dict(skips or {k: 0 for k in SKIP_REASONS}),
           # nights actually kept behind today (the band's floor reads
           # it): a gap is no history
           "history_days": (recorded if recorded is not None
                            else (today - first_night).days
                            if first_night else 0),
           "first_night": first_night.isoformat() if first_night else None}
    # each horizon's misses of the window, own-account transfers taken out
    # when they explain half the misses or more (the same for every
    # horizon, so the sentence and its typical figure agree)
    stats = {}
    for h in SENTENCE_HORIZONS:
        ps = [p for p in pairs if p.h == h and p.target > win0]
        if len(ps) < MIN_PAIRS:
            continue
        span = (max(p.target for p in ps) - min(p.target for p in ps)).days
        if span < MIN_SPAN_DAYS:
            continue
        errs = [abs(p.error) for p in ps]
        adj = None
        if transfers:
            adj = [abs(p.error + transfers.get((p.as_of, p.h), 0.0))
                   for p in ps]
            if not (sum(errs) > 0 and sum(adj) <= TRANSFER_SHARE * sum(errs)):
                adj = None
        use = adj if adj is not None else errs
        stats[h] = {"n": len(ps), "span": span, "use": use,
                    "transfers": adj is not None,
                    # the separate h-day stretches the misses cover:
                    # overlapping misses are not independent results
                    "independent": round((span + h) / h, 1)}
    # a frequency ("4 times in 5") only for a horizon with enough separate
    # stretches; a longer horizon without them says its typical miss
    freq = next((h for h in SENTENCE_HORIZONS if h in stats
                 and stats[h]["independent"] >= INDEPENDENT_MIN), None)
    typ = next((h for h in SENTENCE_HORIZONS if h in stats
                and (freq is None or h > freq)), None)
    if freq is not None:
        st = stats[freq]
        out.update(horizon=freq, n=st["n"], span_days=st["span"],
                   within=plain_up(_p80(st["use"])),
                   window="3 months" if st["span"] >= 75 else "6 weeks",
                   independent=st["independent"],
                   transfers_out=st["transfers"])
    if typ is not None:
        st = stats[typ]
        med = sorted(st["use"])[len(st["use"]) // 2]
        out.update(typical_horizon=typ, typical=plain_up(med),
                   typical_weeks=math.ceil((st["span"] + 1) / 7),
                   typical_independent=st["independent"])
        out["transfers_out"] = out["transfers_out"] or st["transfers"]
    # a lean is said for the longest horizon whose misses of the last
    # BIAS_DAYS cover INDEPENDENT_MIN separate stretches: thirty 30-day
    # misses over 60 days are about three, not thirty
    b0 = today - dt.timedelta(days=BIAS_DAYS)
    for bh in SENTENCE_HORIZONS:
        bp = [p for p in pairs if p.h == bh and p.target > b0]
        if len(bp) < MIN_PAIRS:
            continue
        span = (max(p.target for p in bp) - min(p.target for p in bp)).days
        if (span + bh) / bh < INDEPENDENT_MIN:
            continue
        bs = sorted(p.error for p in bp)
        n = len(bs)
        med = bs[n // 2] if n % 2 else (bs[n // 2 - 1] + bs[n // 2]) / 2
        same = max(sum(1 for e in bs if e > 0),
                   sum(1 for e in bs if e < 0)) / n
        if (same >= BIAS_SHARE
                and abs(med) >= max(BIAS_MIN, BIAS_INCOME_SHARE * income)):
            out["bias"] = {"median": round(med, 2), "horizon": bh,
                           "share_same_sign": round(same, 3)}
        break
    out["band"] = band_pool(pairs, today)
    out["quantiles"] = band_quantiles(pairs, today)
    q30 = out["quantiles"].get("30")
    if q30:
        cov = [p for p in pairs if p.h == 30 and p.target > win0]
        if cov:
            out["coverage"] = round(sum(
                1 for p in cov if q30[0] <= p.error <= q30[1]) / len(cov), 3)
    if card_paid is not None and cards_out:
        out["card_paying"] = card_paying(pairs, today, card_paid, cards_out)
    return out


def growth_power(pairs: list[Pair]) -> float:
    """How fast this household's misses grow with the horizon: the power
    `a` of |miss| ~ h^a, fitted to the median |miss| at 7, 14 and 30 days
    (BAND_FIT_HORIZONS, each with BAND_FIT_MIN misses), held in
    BAND_POWER_CLAMP; BAND_POWER_DEFAULT (a random walk) when it cannot be
    fitted (pure)."""
    med = {}
    for h in BAND_FIT_HORIZONS:
        sub = sorted(abs(p.error) for p in pairs if p.h == h)
        if len(sub) >= BAND_FIT_MIN:
            m = sub[len(sub) // 2]
            med[h] = max(m, 0.01)
    if len(med) < 2:
        return BAND_POWER_DEFAULT
    xs = [math.log(h) for h in med]
    ys = [math.log(v) for v in med.values()]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return min(BAND_POWER_CLAMP[1], max(BAND_POWER_CLAMP[0], a))


def band_pool(pairs: list[Pair], today: dt.date) -> dict | None:
    """The band's one statistic, decided here (specs/forecast-grading.md,
    "The band"): every graded miss whose day came in the last BAND_DAYS,
    at every horizon, divided by the household's own growth h^a
    (growth_power) and pooled — short horizons hold many more separate
    stretches than long ones, so the pool is stable where one horizon's
    overlapping misses are not — and its BAND_Q quantiles, the last
    BAND_RECENT_DAYS counted twice, the current model's misses alone once
    they number BAND_MIN_PAIRS. {"z": [lo, hi], "power": a, "n": misses}
    or None under BAND_MIN_PAIRS misses or BAND_MIN_SHORT 7-day ones (pure).
    """
    b0 = today - dt.timedelta(days=BAND_DAYS)
    r0 = today - dt.timedelta(days=BAND_RECENT_DAYS)
    ps = [p for p in pairs if p.target > b0]
    newest = max((p.as_of, p.model) for p in pairs)[1] if pairs else None
    mine = [p for p in ps if p.model == newest]
    if len(mine) >= BAND_MIN_PAIRS:
        ps = mine
    if (len(ps) < BAND_MIN_PAIRS
            or sum(1 for p in ps if p.h == HORIZONS[0]) < BAND_MIN_SHORT):
        return None
    a = growth_power(ps)
    z = [p.error / p.h ** a for p in ps]
    w = [BAND_RECENT_WEIGHT if p.target > r0 else 1.0 for p in ps]
    # unrounded: the band is z × k^a, rounded once at the end (band)
    return {"z": [weighted_quantile(z, w, BAND_Q[0]),
                  weighted_quantile(z, w, BAND_Q[1])],
            "power": a, "n": len(ps)}


def band_quantiles(pairs: list[Pair], today: dt.date) -> dict:
    """{horizon: [lo, hi] | None}: the pooled band (band_pool) at each
    horizon, z × h^a (pure)."""
    pool = band_pool(pairs, today)
    if pool is None:
        return {str(h): None for h in HORIZONS}
    a = pool["power"]
    return {str(h): [round(pool["z"][0] * h ** a, 2),
                     round(pool["z"][1] * h ** a, 2)] for h in HORIZONS}


def card_paying(pairs: list[Pair], today: dt.date, card_paid: dict,
                cards_out: dict) -> dict | None:
    """How the household's real card payments compared with the three
    scenarios' over the last WINDOW_DAYS of 30-day targets (pure): which
    scenario came closest most often, and the median (real − usual)."""
    win0 = today - dt.timedelta(days=WINDOW_DAYS)
    ps = [p for p in pairs if p.h == 30 and p.target > win0
          and (p.as_of, p.h) in card_paid and (p.as_of, p.h) in cards_out]
    if len(ps) < MIN_PAIRS:
        return None
    wins = {"usual": 0, "now": 0, "due": 0}
    gaps = []
    for p in ps:
        real = card_paid[(p.as_of, p.h)]
        sc = cards_out[(p.as_of, p.h)]
        best = min(("usual", "now", "due"),
                   key=lambda k: (abs(real - sc.get(k, 0.0)), k != "usual"))
        wins[best] += 1
        gaps.append(real - sc.get("usual", 0.0))
    gaps.sort()
    med = gaps[len(gaps) // 2]
    return {"closest": max(wins, key=lambda k: (wins[k], k == "usual")),
            "usual_gap": round(med, 2), "n": len(ps)}


# --- what grading reads from the ledger ------------------------------------

# a transfer is the household's own money moving when its other leg is on
# another of the household's accounts: the opposite amount within this
# many days (the rows a feed posts on both sides of one move)
OWN_TRANSFER_DAYS = 3


def own_transfer_ids(conn, account: str, since: dt.date,
                     until: dt.date, *,
                     partner_until: dt.date | None = None) -> set[str]:
    """The checking account's transfer rows dated in (since, until] that
    are the household's own money moving: each paired one to one with a
    transfer row on ANOTHER of the household's accounts — both legs
    transfer-typed, the opposite amount, within OWN_TRANSFER_DAYS — the
    nearest day first, then the partner's day and id. A payment to another
    person (no leg of the household's on the other side) is not, nor is a
    refund that happens to be the opposite amount. `partner_until` caps
    the other legs' dates (default `until` + OWN_TRANSFER_DAYS): a fact
    as of yesterday reads no leg dated today."""
    pad = dt.timedelta(days=OWN_TRANSFER_DAYS)
    legs = conn.execute(
        """SELECT id, date, ROUND(amount * 100) AS c FROM transactions
            WHERE account_id = %s AND removed = 0 AND pending = 0
              AND category_primary = ANY(%s) AND date > %s AND date <= %s
            ORDER BY date, id""",
        (account, list(TRANSFERS), since, until)).fetchall()
    if not legs:
        return set()
    others = conn.execute(
        """SELECT id, date, ROUND(amount * 100) AS c FROM transactions
            WHERE account_id <> %s AND removed = 0 AND pending = 0
              AND category_primary = ANY(%s) AND date >= %s AND date <= %s
            ORDER BY date, id""",
        (account, list(TRANSFERS), since - pad,
         partner_until or until + pad)).fetchall()
    by_amount: dict[int, list] = {}
    for o in others:
        by_amount.setdefault(int(o["c"]), []).append(o)
    cands = []
    for leg in legs:
        d0 = as_date(leg["date"])
        for o in by_amount.get(-int(leg["c"]), ()):
            gap = abs((as_date(o["date"]) - d0).days)
            if gap <= OWN_TRANSFER_DAYS:
                cands.append((gap, as_date(o["date"]), str(o["id"]),
                              d0, str(leg["id"])))
    cands.sort()
    used, out = set(), set()
    for _g, _d, oid, _d0, lid in cands:
        if oid in used or lid in out:
            continue
        used.add(oid)
        out.add(lid)
    return out


# the window the Today page reads the household's own-account transfers
# over (todayview.transfers_note), to the end of yesterday
OWN_TRANSFER_NOTE_DAYS = 60


def recent_own_transfers(conn, account: str, today: dt.date) -> dict:
    """`account`'s own-account transfers (own_transfer_ids' pairs) over the
    OWN_TRANSFER_NOTE_DAYS days to the end of yesterday, each direction
    apart: {"out": money that left for the household's other accounts,
    "in": money that came back}. Every read stops at yesterday, the other
    legs included — a learned fact (specs/today-figures.md, "Own-account
    transfers are not in the forecast")."""
    until = today - dt.timedelta(days=1)
    since = until - dt.timedelta(days=OWN_TRANSFER_NOTE_DAYS)
    own = own_transfer_ids(conn, account, since, until, partner_until=until)
    out = back = 0.0
    if own:
        for r in conn.execute(
                """SELECT amount FROM transactions
                    WHERE account_id = %s AND id = ANY(%s)
                    ORDER BY date, id""", (account, sorted(own))).fetchall():
            a = float(r["amount"])
            if a > 0:
                out += a
            else:
                back -= a
    return {"out": round(out, 2), "in": round(back, 2)}


def kept_own_transfers(conn, cfg: dict, account: str,
                       today: dt.date) -> dict:
    """recent_own_transfers, kept with the day's learned facts
    (forecast.learned): read once per household per day, not on every
    Today load and email."""
    return forecast.learned(
        conn, cfg, today, f"own_transfers:{account}",
        lambda: recent_own_transfers(conn, account, today))


def _day_sums(conn, account: str, since: dt.date, until: dt.date) -> dict:
    """{date: (all, own-account transfers, card payments)} of the account's
    posted rows dated in (since, until] (positive = money out); a transfer
    counts as the household's own only when paired (own_transfer_ids)."""
    own = own_transfer_ids(conn, account, since, until)
    out: dict = {}
    for r in conn.execute(
            """SELECT id, date, amount, category_detailed FROM transactions
                WHERE account_id = %s AND removed = 0 AND pending = 0
                  AND date > %s AND date <= %s ORDER BY date, id""",
            (account, since, until)).fetchall():
        d = as_date(r["date"])
        a = float(r["amount"])
        t, m, c = out.get(d, (0.0, 0.0, 0.0))
        out[d] = (t + a, m + (a if str(r["id"]) in own else 0.0),
                  c + (a if r["category_detailed"] == CARD_PAYMENT else 0.0))
    return {d: tuple(round(x, 2) for x in v) for d, v in out.items()}


class _Ledger:
    """Prefix sums of each checking account's posted rows, read once."""

    def __init__(self, conn, nights: list[dict], today: dt.date):
        self.conn, self.today = conn, today
        self.first = min((as_date(r["as_of"]) for r in nights),
                         default=today)
        self._by: dict[str, dict] = {}

    def _sums(self, account: str) -> dict:
        if account not in self._by:
            self._by[account] = _day_sums(self.conn, account,
                                          self.first - dt.timedelta(days=1),
                                          self.today)
        return self._by[account]

    def between(self, account: str, t: dt.date, u: dt.date, k: int) -> float:
        s = self._sums(account)
        return round(sum(v[k] for d, v in s.items() if t < d <= u), 2)

    def moves(self, account: str, t: dt.date, u: dt.date) -> float:
        return self.between(account, t, u, 0)


def current(conn, today: dt.date, *, cfg: dict | None = None) -> dict | None:
    """The grading as of `today` from the kept nights: the stored score of
    the newest night when it was computed tonight, else computed again
    (a restore, an upgrade). None when nothing was ever recorded."""
    newest = conn.execute(
        "SELECT as_of, score, (SELECT count(*) FROM forecast_nights) AS n, "
        "(SELECT min(as_of) FROM forecast_nights) AS first "
        "FROM forecast_nights ORDER BY as_of DESC LIMIT 1").fetchone()
    if newest is None:
        return None
    sc = newest["score"]
    if sc and sc.get("as_of") == today.isoformat():
        return sc
    # no night kept today yet (before the hour, or a restore): computed
    # once per household per day and kept beside the day's learned facts
    cfg = cfg if cfg is not None else budget.load_config(conn)
    return forecast.learned(
        conn, cfg, today,
        f"score:{newest['as_of']}:{newest['n']}:{newest['first']}",
        lambda: compute_score(conn, today, cfg=cfg))


def on_kept_accounts(conn, nights: list[dict], pairs: list[Pair],
                     skips: dict) -> list[Pair]:
    """`pairs` less those whose night names a checking account the
    household no longer has, each counted in `skips` as account_changed.
    A reconnect carries the nights to the account's new id
    (adopt.ACCOUNT_REFS); a night kept before that did not, and its
    ledger reads as empty — no card payment, no transfer — which is no
    record of $0, so such a pair is not graded."""
    named = {r["checking_id"] for r in nights}
    live = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s)",
        (sorted(named),)).fetchall()}
    if live >= named:
        return pairs
    by_day = {as_date(r["as_of"]): r for r in nights}
    kept = [p for p in pairs if by_day[p.as_of]["checking_id"] in live]
    skips["account_changed"] += len(pairs) - len(kept)
    return kept


def compute_score(conn, today: dt.date, *,
                  cfg: dict | None = None) -> dict | None:
    nights = rows(conn, upto=today)
    if not nights:
        return None
    cfg = cfg if cfg is not None else budget.load_config(conn)
    led = _Ledger(conn, nights, today)
    pairs, skips = graded_pairs(nights, today, moves=led.moves)
    pairs = on_kept_accounts(conn, nights, pairs, skips)
    by_day = {as_date(r["as_of"]): r for r in nights}
    transfers, card_paid, cards_out = {}, {}, {}
    for p in pairs:
        r = by_day[p.as_of]
        key = (p.as_of, p.h)
        transfers[key] = led.between(r["checking_id"], p.as_of, p.target, 1)
        card_paid[key] = led.between(r["checking_id"], p.as_of, p.target, 2)
        i = HORIZONS.index(p.h)
        cards_out[key] = {
            k: (kept_cents(r["walks"], k, "cards_out")[i] or 0) / 100
            for k in ("usual", "now", "due")}
    try:
        # the plan's monthly income, as the month decides it
        # (specs/honest-figures.md, "The plan's income is one figure")
        income = float(budget.month_status(conn, today, memo={})
                       .get("income") or 0.0)
    except Exception:                            # noqa: BLE001
        income = 0.0
    return summarise(pairs, today, income=income,
                     first_night=as_date(nights[0]["as_of"]), skips=skips,
                     transfers=transfers, card_paid=card_paid,
                     cards_out=cards_out,
                     recorded=sum(1 for r in nights
                                  if as_date(r["as_of"]) < today))


def store_score(conn, today: dt.date, *, cfg: dict | None = None) -> None:
    sc = compute_score(conn, today, cfg=cfg)
    if sc is not None:
        conn.execute("UPDATE forecast_nights SET score = %s::jsonb "
                     "WHERE as_of = %s", (json.dumps(sc), today))


# --- the band on the chart --------------------------------------------------

def offsets(score: dict | None, days: int) -> list[tuple[float, float]] | None:
    """[(lo, hi)] for walk days 1..days (pure): the pooled band
    (band_pool) as z × k^a on day k; None until the band qualifies. A
    score stored before the pooled band carries no "band" and draws none
    (the next night's score does)."""
    pool = (score or {}).get("band")
    if not pool:
        return None
    (zl, zh), a = pool["z"], pool["power"]
    return [(zl * k ** a, zh * k ** a) for k in range(1, days + 1)]


def band(series: list, score: dict | None, *,
         heavy: list | None = None) -> dict | None:
    """A walk's shaded band from the forecast's own misses (pure):
    {"lo": series, "hi": series}, every day of the chart, or None before
    the misses qualify. While day k has fewer than BAND_FLOOR_INDEPENDENT
    separate k-day stretches of kept history behind it
    ((history_days − k) ÷ k), the band is never narrower than `heavy`
    (the same walk in a heavier month) sits below the walk that day, and
    as far above: a few months of overlapping long misses are too few to
    stand for the weeks ahead, and the heavier line is what the household
    saw before.

    `series` is the walk's own, starting at today (forecast.build): its
    first point is a balance already known and has no width; point k is
    k days ahead and takes day k's width."""
    offs = offsets(score, max(len(series) - 1, 0))
    if offs is None:
        return None
    hist = (score or {}).get("history_days") or 0
    gap = {}
    if heavy:
        hv = dict(heavy)
        gap = {d: max(0.0, v - hv[d]) for d, v in series if d in hv}
    lo, hi = [], []
    for k, (d, v) in enumerate(series):
        if k == 0:
            lo.append((d, round(v, 2)))
            hi.append((d, round(v, 2)))
            continue
        a, b = offs[k - 1]
        # thin history, or misses that never moved (a band of no width
        # says nothing): never narrower than the heavier line
        if d in gap and ((hist - k) / k < BAND_FLOOR_INDEPENDENT
                         or b - a < BAND_MIN_WIDTH):
            a, b = min(a, -gap[d]), max(b, gap[d])
        lo.append((d, round(v + a, 2)))
        hi.append((d, round(v + b, 2)))
    return {"lo": lo, "hi": hi}


# --- the panel and the drill ------------------------------------------------

def _money(x: float) -> str:
    from ..web import charts
    return charts._money(x)


def _mmdd(d: dt.date) -> str:
    return d.strftime("%m/%d")


WALK_FORWARD_MIN = 10


def recorded_days(by_day: dict, first: dt.date, upto: dt.date) -> int:
    """Nights actually kept from `first` through `upto`: a gap is no
    history, so it adds no separate stretch."""
    return sum(1 for d in by_day if first <= d <= upto)


def walk_forward_coverage(nights: list[dict], pairs: list[Pair],
                          today: dt.date, h: int = 30
                          ) -> tuple[float | None, int, float]:
    """How often the band held, measured honestly (pure): for each h-day
    miss whose day came in the last WINDOW_DAYS, the band as it stood the
    night that forecast was made — from the misses known then only, drawn
    as Today draws it (band_pool, the heavier-line floor) — against the
    real balance. (share, checks, separate stretches the checks cover);
    share None under WALK_FORWARD_MIN checks."""
    by_day = {as_date(r["as_of"]): r for r in nights}
    first = min(by_day) if by_day else today
    win0 = today - dt.timedelta(days=WINDOW_DAYS)
    i = HORIZONS.index(h)
    held = n = 0
    days = []
    for p in pairs:
        if p.h != h or p.target <= win0:
            continue
        known = [x for x in pairs if x.target < p.as_of]
        q = band_quantiles(known, p.as_of).get(str(h))
        if not q:
            continue
        lo, hi = q
        walks = by_day[p.as_of]["walks"]
        u = kept_cents(walks, "usual", "at")
        low = kept_cents(walks, "low", "at")
        hist = recorded_days(by_day, first, p.as_of)
        if ((hist - h) / h < BAND_FLOOR_INDEPENDENT or hi - lo < BAND_MIN_WIDTH) \
                and None not in (u[i], low[i]):
            g = max(0.0, (u[i] - low[i]) / 100)
            lo, hi = min(lo, -g), max(hi, g)
        n += 1
        days.append(p.target)
        held += lo <= p.error <= hi
    stretches = (((max(days) - min(days)).days + h) / h) if days else 0.0
    if n < WALK_FORWARD_MIN:
        return None, n, stretches
    return round(held / n, 3), n, stretches


def panel(conn, today: dt.date) -> dict:
    """"How has the forecast done?": the last WINDOW_DAYS of 30-day
    predictions against the real balance, the band's coverage, how the
    household's card payments compared with the scenarios, and why days
    were not graded — every word decided here."""
    nights = rows(conn, upto=today)
    led = _Ledger(conn, nights, today)
    pairs, skips = graded_pairs(nights, today, moves=led.moves)
    pairs = on_kept_accounts(conn, nights, pairs, skips)
    sc = current(conn, today) or {}
    win0 = today - dt.timedelta(days=WINDOW_DAYS)
    hist = [{"day": p.target.isoformat(), "made": p.as_of.isoformat(),
             "predicted": p.predicted, "actual": p.actual,
             "error": p.error, "rebuilt": p.rebuilt}
            for p in pairs if p.h == 30 and p.target > win0]
    # the coverage of the longest horizon whose checks cover
    # INDEPENDENT_MIN separate stretches; a frequency for fewer is not one
    cov = cov_text = None
    for ch in SENTENCE_HORIZONS:
        c, checks, stretches = walk_forward_coverage(nights, pairs, today,
                                                     ch)
        if c is not None and stretches >= INDEPENDENT_MIN:
            cov = c
            cov_text = (f"Checked against later days, the real balance "
                        f"{ch} days ahead landed inside the shaded band "
                        f"{round(c * 10)} times in 10 ({checks} checks).")
            break
    cp = sc.get("card_paying")
    if cp:
        gap = cp["usual_gap"]
        if abs(gap) < CARD_GAP_SAME:
            cp_text = ("Lately you have paid your cards about as the "
                       "forecast assumed.")
        elif gap > 0:
            cp_text = (f"Lately you have paid your cards about "
                       f"{_money(plain_up(gap))} sooner than the forecast "
                       "assumed — closer to paying them all at once.")
        else:
            cp_text = (f"Lately you have paid your cards about "
                       f"{_money(plain_up(-gap))} later than the forecast "
                       "assumed — closer to the due dates.")
    else:
        cp_text = None
    words = {"stale": "your checking balance had not been updated",
             "account_changed": "your checking account changed",
             "restore": "they fall on both sides of a restore",
             "offline": "the app did not record those nights"}
    skips_text = [f"{n} forecasts not graded: {words[k]}."
                  for k, n in skips.items() if n]
    models, last = [], None
    for r in nights:
        if last is not None and r["model"] != last:
            models.append(as_date(r["as_of"]).isoformat())
        last = r["model"]
    return {"history": hist, "coverage": cov, "coverage_text": cov_text,
            "card_paying_text": cp_text, "skips_text": skips_text,
            "model_changes": models,
            "empty_text": (None if hist else
                           "No 30-day forecast has been graded yet.")}


# the panel and the drill, kept per household while its kept nights and
# its day are the same (the doors are read again on every open, and the
# grading under them changes only with a night or a new day)
_PANEL_KEPT = forecast._Kept(forecast.KEPT_HOUSEHOLDS)


def _nights_stamp(conn, today: dt.date) -> tuple:
    r = conn.execute(
        "SELECT count(*) AS n, max(as_of) AS d, max(recorded_at) AS t "
        "FROM forecast_nights").fetchone()
    return (today, r["n"], r["d"], r["t"],
            forecast.history_stamp(conn, budget.load_config(conn), today)[1])


def _kept(conn, today: dt.date, key: tuple, compute):
    tid = forecast._tenant(conn)
    stamp = _nights_stamp(conn, today)
    hit = _PANEL_KEPT.get(tid)
    if hit is not None and hit[0] == stamp and key in hit[1]:
        return hit[1][key]
    val = compute()
    entry = hit if hit is not None and hit[0] == stamp else (stamp, {})
    entry[1][key] = val
    _PANEL_KEPT.put(tid, entry)
    return val


def panel_kept(conn, today: dt.date) -> dict:
    """panel, computed once per household per day and kept night."""
    return _kept(conn, today, ("panel",), lambda: panel(conn, today))


def drillable(day: dt.date, horizon: int, today: dt.date) -> bool:
    """Whether "why the forecast missed" can exist for target `day` at
    `horizon`: a graded horizon (HORIZONS), a day already past, and a
    forecast night inside the inputs keep (SCORE_INPUTS_NIGHTS). Anything
    else is "not graded" before a query, and is never kept."""
    as_of = day - dt.timedelta(days=horizon)
    return (horizon in HORIZONS and day < today
            and as_of >= today - dt.timedelta(days=SCORE_INPUTS_NIGHTS))


def miss_kept(conn, day: dt.date, horizon: int, today: dt.date):
    """miss, computed once per household per day and kept night; a miss
    that cannot exist (drillable) is answered None and not kept."""
    if not drillable(day, horizon, today):
        return None
    return _kept(conn, today, ("miss", day, horizon),
                 lambda: miss(conn, day, horizon, today))


def _payee_of(label: str) -> str:
    """The payee an event's label names ("Rivertown Water (estimate)")."""
    return label.split(" (", 1)[0]


def matched_owners(conn, since: dt.date, until: dt.date,
                   today: dt.date) -> dict[str, tuple[str, str]]:
    """{txn id: ("bills" | "paychecks", payee)} for the rows dated in
    (since, until]: each row the bill matcher gave a bill's occurrence
    (the same pass the month status marks a bill paid by,
    budget.month_occurrence_pass, every month the window touches), and
    each deposit an income series claims (budget._claim_income_rows, the
    forecast's own paycheck claims)."""
    from . import bills as _bills
    cfg = budget.load_config(conn)
    out: dict[str, tuple[str, str]] = {}
    claims = budget._claim_income_rows(
        _bills._income_rows(conn, today),
        budget._income_claimants(conn, []))
    for payee, deps in claims.items():
        for r in deps:
            d = as_date(r["date"])
            if since < d <= until:
                out[str(r["txn_id"])] = ("paychecks", payee)
    sched = budget.drop_card_pay_bills(
        conn, budget._recurring_bills(
            conn, caps=cfg.get("occurrence_caps"),
            disabled=cfg.get("disabled_bills")), cfg=cfg)
    import calendar as _cal
    for y, m in budget.months_spanned(since + dt.timedelta(days=1), until):
        end = dt.date(y, m, _cal.monthrange(y, m)[1])
        closed = end < today
        occ = budget.month_occurrence_pass(conn, sched,
                                           end if closed else today,
                                           cfg=cfg, closed=closed)
        for o in occ["candidates"]:
            for r in [o.get("txn")] + list(o.get("companions") or []):
                if r is None:
                    continue
                d = as_date(r["date"])
                if since < d <= until:
                    out.setdefault(str(r["txn_id"]),
                                   ("bills", o["bill"]["payee"]))
    return out


PARTS = ("paychecks", "bills", "card_payments", "spending", "transfers",
         "other", "pending")
PART_WORDS = {"paychecks": "Paychecks", "bills": "Bills from checking",
              "card_payments": "Card payments", "spending": "Spending",
              "transfers": "Money moved between accounts",
              "other": "Refunds, interest and other",
              "pending": "Timing of pending charges"}


def miss(conn, day: dt.date, horizon: int, today: dt.date) -> dict | None:
    """"Why the forecast missed" for target `day` at `horizon`: the miss
    (real − predicted) split into parts that add up to it to the cent, and
    the five largest causes in words. None when that forecast was not kept
    with its inputs, or its miss was not graded."""
    if not drillable(day, horizon, today):
        return None
    as_of = day - dt.timedelta(days=horizon)
    row = conn.execute(
        "SELECT as_of, checking_id, start_checking, walks, inputs "
        "FROM forecast_nights WHERE as_of = %s", (as_of,)).fetchone()
    if row is None or row["inputs"] is None:
        return None
    nights = rows(conn, upto=today)
    led = _Ledger(conn, nights, today)
    pairs, _sk = graded_pairs(nights, today, moves=led.moves)
    pairs = on_kept_accounts(conn, nights, pairs, _sk)
    pair = next((p for p in pairs if p.as_of == as_of and p.h == horizon),
                None)
    if pair is None:
        return None
    i = HORIZONS.index(horizon)
    start = _c(row["start_checking"])
    predicted, actual = _c(pair.predicted), _c(pair.actual)
    evs = [e for e in _kept_events(row["inputs"]) if 0 < e[0] <= horizon
           and e[3] != "card_bill"]
    pred = {k: 0 for k in PARTS}
    for _d, cents, _label, kind, _a in evs:
        pred[{"paycheck": "paychecks", "bill": "bills",
              "on_way": "card_payments",
              "sweep": "transfers"}.get(kind, "bills")] += cents
    pred["card_payments"] += -(kept_cents(row["walks"], "usual",
                                          "cards_out")[i] or 0)
    pred["spending"] = (predicted - start) - sum(pred.values())
    # each bill and paycheck as the forecast expected it, by payee (a
    # label is the payee, an estimate says so after it)
    labels = {k: [(_payee_of(e[2]), e[1]) for e in evs if e[3] == k]
              for k in ("paycheck", "bill")}
    owners = matched_owners(conn, as_of, day, today)
    act = {k: 0 for k in PARTS}
    by_label: dict[str, int] = {}
    singles: list[dict] = []
    for r in conn.execute(
            """SELECT id, date, amount, COALESCE(merchant_name, name) AS payee,
                      name, category_primary, category_detailed
                 FROM transactions
                WHERE account_id = %s AND removed = 0 AND pending = 0
                  AND date > %s AND date <= %s ORDER BY date, id""",
            (row["checking_id"], as_of, day)).fetchall():
        cents = -_c(r["amount"])          # positive = money in
        owner = owners.get(str(r["id"]))
        if owner is not None:
            # the row the bill matcher gave a bill, or the deposit an
            # income series claims: that bill's, that paycheck's
            kind, payee = owner
            by_label[payee] = by_label.get(payee, 0) + cents
        elif r["category_detailed"] == CARD_PAYMENT:
            kind = "card_payments"
        elif r["category_primary"] in TRANSFERS:
            kind = "transfers"
        elif cents > 0 and r["category_primary"] == "INCOME":
            # a deposit no paycheck series claims: income the forecast
            # did not expect
            kind = "paychecks"
            singles.append({"kind": "income", "cents": cents,
                            "payee": r["payee"] or r["name"] or "",
                            "date": as_date(r["date"])})
        else:
            kind = "spending" if cents < 0 else "other"
        act[kind] += cents
        if kind in ("spending", "transfers", "other"):
            singles.append({"kind": kind, "cents": cents,
                            "payee": r["payee"] or r["name"] or "",
                            "date": as_date(r["date"])})
    act["pending"] = (actual - start) - sum(act.values())
    parts = [{"kind": k, "label": PART_WORDS[k], "predicted": pred[k] / 100,
              "actual": act[k] / 100, "diff": (act[k] - pred[k]) / 100}
             for k in PARTS]
    # the parts close by construction: the predicted spending and the
    # actual pending are each side's remainder
    items = []
    for k in ("paycheck", "bill"):
        # one line per payee: every occurrence expected in the window
        # against everything of it that arrived
        expected: dict[str, int] = {}
        for lab, cents in labels[k]:
            expected[lab] = expected.get(lab, 0) + cents
        for lab, cents in expected.items():
            got = by_label.get(lab, 0)
            d = got - cents
            if abs(d) < 100:
                continue
            if k == "paycheck":
                text = (f"{lab} arrived {_money(abs(d) / 100)} "
                        f"{'higher' if d > 0 else 'lower'} than expected."
                        if got else f"{lab} had not arrived by "
                        f"{_mmdd(day)}.")
            elif got == 0:
                text = f"{lab} had not been paid by {_mmdd(day)}."
            else:
                text = (f"{lab} came in {_money(abs(d) / 100)} "
                        f"{'lower' if d > 0 else 'higher'} than scheduled.")
            items.append({"text": text, "amount": d / 100, "date": None})
    dc = act["card_payments"] - pred["card_payments"]
    if abs(dc) >= 100:
        paid = -act["card_payments"] / 100
        items.append({"text": (
            f"Card payments: {_money(paid)} paid by {_mmdd(day)}, "
            f"{_money(abs(dc) / 100)} "
            f"{'less' if dc > 0 else 'more'} than the forecast assumed."),
            "amount": dc / 100, "date": None})
    for s in singles:
        if s["kind"] == "spending" and -s["cents"] >= 20000:
            items.append({"text": (
                f"{s['payee']} {_money(-s['cents'] / 100)} on "
                f"{_mmdd(s['date'])} — more than the usual pace allowed "
                "for in one charge."),
                "amount": s["cents"] / 100, "date": s["date"].isoformat()})
        elif s["kind"] == "income" and s["cents"] >= 10000:
            items.append({"text": (
                f"{s['payee']} {_money(s['cents'] / 100)} on "
                f"{_mmdd(s['date'])} — income the forecast did not "
                "expect."),
                "amount": s["cents"] / 100, "date": s["date"].isoformat()})
        elif s["kind"] == "transfers" and abs(s["cents"]) >= 5000:
            way = "moved out" if s["cents"] < 0 else "moved in"
            items.append({"text": (
                f"{_money(abs(s['cents']) / 100)} {way} on "
                f"{_mmdd(s['date'])} ({s['payee']}) — the forecast does "
                "not yet plan transfers."),
                "amount": s["cents"] / 100, "date": s["date"].isoformat()})
    if abs(act["pending"]) >= 100:
        items.append({"text": (
            f"{_money(abs(act['pending']) / 100)} of the difference is "
            "charges and deposits still pending or not yet in the ledger "
            "on that day."), "amount": act["pending"] / 100, "date": None})
    items.sort(key=lambda it: -abs(it["amount"]))
    return {"day": day.isoformat(), "made": as_of.isoformat(),
            "horizon": horizon, "predicted": predicted / 100,
            "actual": actual / 100, "error": (actual - predicted) / 100,
            "rebuilt": pair.rebuilt, "parts": parts, "items": items[:5]}
