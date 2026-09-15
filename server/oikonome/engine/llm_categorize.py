"""Merchant-level LLM categorization for transactions the aggregator puts
in generic buckets (GENERAL_MERCHANDISE / GENERAL_SERVICES / OTHER / null),
plus Amazon item categories/summaries and recurring-proposal tags.

The aggregator natively categorizes every transaction and is trusted
everywhere else — especially the flow categories (TRANSFER_*,
LOAN_PAYMENTS, INCOME) that drive spend exclusions, which the LLM is NEVER
allowed to touch (the FLOW-GUARD below is load-bearing: importer-assigned
rows from brokerage, retirement-plan and crypto importers have no
aggregator raw category, so the COALESCE reads them as generic 'OTHER'; without the guard
the LLM would reclassify them into counted spend). This only sharpens the
vague spend buckets.

Classifies UNIQUE MERCHANTS, not transactions: each merchant is classified
once into an aggregator primary category, cached in merchant_categories,
and applied to all matching rows (past and future) as plain SQL during
every sync — the LLM only runs when new unknown merchants appear.

Backend (pluggable; NO-OP when unconfigured — apply() of the cached map
still runs so migrated merchant_categories stay live). TWO config layers:

  1. TENANT CONFIG (wins when present) — keys in tenant_settings.config
     (budget.load_config), written by the setup wizard / POST /api/settings:
       llm_url      base URL of an OpenAI-compatible server
       llm_model    model name (required with llm_url)
       llm_api_key  optional bearer token, stored encrypted (db/crypto)
     When llm_url is set the tenant endpoint is used WHOLESALE: the env
     API key and OIKONOME_LLM_EXTRA_BODY are NOT sent (they belong to the
     operator's server, never to a tenant-configured one).
  2. ENV (operator-level default, used when the tenant has no llm_url):
       OIKONOME_LLM_URL         base URL of an OpenAI-compatible server
                                (e.g. http://127.0.0.1:8080 or
                                https://api.openai.com)
       OIKONOME_LLM_MODEL       model name (required with URL)
       OIKONOME_LLM_API_KEY     optional bearer token
       OIKONOME_LLM_EXTRA_BODY  optional JSON merged into each request body
                                (e.g. '{"chat_template_kwargs":
                                {"enable_thinking": false}}', which some
                                local servers need to keep replies short)

The model server's own lifecycle is host-side ops, not app code: the
endpoint is assumed up. Amazon item categories are derived from the
tenant's existing amazon_orders categories plus the 'Shopping' default,
minus 'Refunds'.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager

import httpx

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 600
# Largest completion body we will read. A tenant may point a backend at any
# public host, so the reply is untrusted input: without a ceiling one
# multi-GB "completion" is buffered whole into the web process. Real
# answers are a few KB; 512 KB leaves room for verbose reasoning fields.
MAX_RESPONSE_BYTES = 512 * 1024
# 8, not 25 — small local models lose the number→merchant mapping
# in long batches; short batches measurably improve accuracy (the cache is
# permanent, so quality beats throughput here)
BATCH_SIZE = 8

from .categories import (
    FLOW_SQL as _FLOW_SQL,
    GENERIC_SQL as _GENERIC_SQL,
    PLAID_TRUSTED_SQL as _PLAID_TRUSTED_SQL,
)

# Machine may refine a row when Plaid was vague (generic bucket) OR when
# Plaid confidence is not HIGH/VERY_HIGH (LOW/MEDIUM/missing). Sharp
# HIGH-confidence Plaid labels stay unless a user-merchant rule applies.
_REFINEABLE_SQL = f"""(
    COALESCE(t.raw #>> '{{personal_finance_category,primary}}',
             t.category_plaid, 'OTHER') IN {_GENERIC_SQL}
    OR UPPER(COALESCE(t.category_plaid_confidence,
                      t.raw #>> '{{personal_finance_category,confidence_level}}',
                      '')) NOT IN {_PLAID_TRUSTED_SQL}
)"""

# category rules key on the CANONICAL merchant, not the raw
# descriptor. One payee that arrives under several descriptors ("HARBOR
# PLAY BARN" and "GglPay HARBOR PLA…") is one merchant on every report but
# N unrelated keys to a rule engine keyed on the raw string, so a
# correction on one variant never reaches the others. Both sides now resolve through merchant_canonical
# (raw_merchant -> canonical); an unmapped string is its OWN canonical
# (COALESCE fallback), so merchants that were never deduped behave exactly as
# before. Because several raw variants collapse to one canonical, a lookup can
# now match MORE THAN ONE rule row — so every match aggregates and applies a
# category only when the rules are UNANIMOUS; a canonical whose rules disagree
# is left split (the same outcome per-string keys gave, for the small tail
# of canonicals with genuinely conflicting guesses — never an arbitrary
# winner).
#
# Only the RULE side needs a fragment: a rule is keyed by merchant STRING, so
# it resolves the same way wherever it is read (here, data.py's snapshot/undo).
# The transaction side has no fragment on purpose — a row's canonical is the
# DISPLAY string (outlet first, see merchant_dedup.DISPLAY_MERCHANT), resolved
# by join in apply()/pending_merchants. The removed transaction-side twin
# resolved on merchant_name alone and was the shape that let the two sides
# disagree about which rows a bulk change covers.
_RULE_CANON = (
    "COALESCE((SELECT mc2.canonical FROM merchant_canonical mc2 "
    "WHERE mc2.raw_merchant = m.merchant), m.merchant)")

# Spend-side primaries the LLM may assign. Flow categories
# (TRANSFER_*, LOAN_*, INCOME, BANK_FEES) are deliberately absent.
CATEGORIES = {
    "FOOD_AND_DRINK": "restaurants, fast food, coffee, bars, groceries",
    "GENERAL_MERCHANDISE": "retail stores, online shopping, clothing, electronics, superstores",
    "GENERAL_SERVICES": "auto repair, professional services, education, childcare, insurance",
    "TRANSPORTATION": "gas stations, parking, tolls, rideshare, transit",
    "MEDICAL": "doctors, dental, pharmacy, therapy, veterinary",
    "HOME_IMPROVEMENT": "hardware, furniture, contractors, home services, storage",
    "RENT_AND_UTILITIES": "rent, power, water, garbage, internet, phone",
    "ENTERTAINMENT": "streaming, events, hobbies, games, recreation",
    "TRAVEL": "hotels, flights, lodging, vacation",
    "PERSONAL_CARE": "salons, barbers, gyms, spas",
    "GOVERNMENT_AND_NON_PROFIT": "taxes, government fees, courts, donations",
}


class LLMError(Exception):
    pass


def _reply_shape(content: str) -> str:
    """How much a backend sent back, said without quoting any of it."""
    if not (content or "").strip():
        return "the reply was empty"
    return f"{len(content.encode('utf-8', 'replace'))} bytes came back"


def _unusable_reply(stage: str, content: str, why: str = "") -> LLMError:
    """The failure of a reply the parser could not use, phrased so it is
    safe to store and to show.

    The reply is the model's own text, and a backend that is
    misconfigured, over-quantized or handed the wrong chat template
    routinely answers by echoing its prompt back — and these prompts carry
    the household's merchant names, retailer item titles and
    typical charge amounts. This sentence is kept on the run record,
    rendered on the Doctor page for every member (viewers included),
    mailed in the daily digest, and attached to any feedback bundle that
    leaves the instance under an explicit "no transaction contents"
    promise. So it names WHICH parse failed and HOW MUCH came back, and
    nothing else: being free of household content has to hold by
    construction, because "a broken model will probably stay on topic" is
    not a control.

    The reply itself goes to the DEBUG log only, marked no_capture so the
    feedback ring buffer cannot pick it up — that buffer scrubs secret and
    address shapes, which is no help against a purchase description."""
    log.debug("unusable %s reply: %r", stage, (content or "")[:200],
              extra={"no_capture": True})
    return LLMError(f"the model's {stage} reply was not usable JSON"
                    + (f" ({why})" if why else "")
                    + f" — {_reply_shape(content)}")


def _json_object(content: str, stage: str) -> dict:
    """The JSON object embedded in a completion. Every parse in this module
    goes through here so that a reply nobody can use fails ONE way — as an
    _unusable_reply — instead of each caller inventing its own message (and
    one of them, the Amazon batch, letting a raw JSONDecodeError out)."""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise _unusable_reply(stage, content)
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        # a JSON decoder states what it expected and where; it never quotes
        # the document, so its own words carry no household content
        raise _unusable_reply(stage, content, str(e)[:100])


# ---- pluggable backend ---------------------------------------------------


# the tasks a backend can be routed to. "categorize" is the bulk/private
# work (merchant categorization, recurring-proposal tags, Amazon
# summaries); "assistant" the conversational page; "vision" receipts and
# paper documents. Unrouted roles keep the legacy resolution below.
ROLES = ("categorize", "assistant", "vision")


def resolve_role(cfg: dict, role: str = "categorize"):
    """Which config layer answers `role`. Returns (layer, entry):

      ("backend", <llm_backends entry>)  routed at a live named entry
      ("legacy",  None)                  tenant single-endpoint keys
      ("env",     None)                  operator env / bundled fallback
                                         (may still be unconfigured)

    The single source of truth for role→backend precedence, consumed by
    BOTH the execution path (_backend below) and the Settings "what uses
    what" display (api._settings_view) — they used to reimplement this
    chain independently and agreed only by hand-verification."""
    bid = str((cfg.get("llm_roles") or {}).get(role) or "")
    if bid and bid != "bundled":
        b = next((x for x in (cfg.get("llm_backends") or [])
                  if isinstance(x, dict) and x.get("id") == bid), None)
        if b and b.get("url"):
            return "backend", b
        bid = ""   # routed at an entry that no longer exists → legacy/env
    if bid != "bundled" and cfg.get("llm_url"):
        return "legacy", None
    return "env", None


def _backend(conn=None, role: str = "categorize") -> dict:
    """Effective backend for one task. Resolution, most specific first:

    1. cfg llm_roles[role] → the named entry in cfg llm_backends, or the
       literal "bundled" for the operator's env backend. Several backends
       can be live at once — a local model doing the bulk work while a
       cloud model answers the assistant.
    2. legacy single-endpoint keys (llm_url / llm_model / llm_api_key):
       tenant endpoint used WHOLESALE — it never receives the operator's
       env key or extra body; the layers never mix.
    3. OIKONOME_LLM_* env vars (the bundled Ollama on a standard install).

    Values are never logged — the key is a credential."""
    # "source" is provenance, not config: _chat re-guards TENANT-sourced
    # URLs at request time, but never second-guesses the operator's own
    # env/bundled backend — the standard install's default is
    # http://ollama:11434, a private compose hostname the hosted netguard
    # policy would (correctly, for tenant data) refuse.
    out = {"url": os.environ.get("OIKONOME_LLM_URL", ""),
           "model": os.environ.get("OIKONOME_LLM_MODEL", ""),
           "api_key": os.environ.get("OIKONOME_LLM_API_KEY", ""),
           "extra_body": os.environ.get("OIKONOME_LLM_EXTRA_BODY", ""),
           "vision_model": "", "source": "env"}
    if conn is None:
        return out
    from ..db import crypto
    from . import budget
    cfg = budget.load_config(conn)

    def _with_cfg_vision(d):
        # the legacy vision key rides along for the env/legacy/bundled
        # layers (how a standard install pairs a tenant-picked vision
        # model with the bundled endpoint); a NAMED backend carries only
        # its own vision_model so models never mix across backends
        if cfg.get("llm_vision_model"):
            d = {**d, "vision_model": cfg["llm_vision_model"]}
        return d

    layer, b = resolve_role(cfg, role)
    if layer == "backend":
        key = b.get("api_key") or ""
        eb = b.get("extra_body") or ""
        return {"url": b["url"], "model": b.get("model") or "",
                "api_key": crypto.decrypt(conn, key) if key else "",
                # server-shape tweaks (e.g. gemma's enable_thinking
                # off) ride with whoever provides the endpoint —
                # encrypted at rest like the key (plaintext legacy
                # rows pass through decrypt unchanged)
                "extra_body": crypto.decrypt(conn, eb) if eb else "",
                "vision_model": b.get("vision_model") or "",
                "source": "tenant"}
    if layer == "legacy":
        key = cfg.get("llm_api_key") or ""
        eb = cfg.get("llm_extra_body") or ""
        return _with_cfg_vision({
            "url": cfg["llm_url"], "model": cfg.get("llm_model") or "",
            "api_key": crypto.decrypt(conn, key) if key else "",
            # encrypted at rest like the key beside it (plaintext rows
            # written before that pass through decrypt unchanged)
            "extra_body": crypto.decrypt(conn, eb) if eb else "",
            "vision_model": "", "source": "tenant"})
    return _with_cfg_vision(out)


def configured(conn=None, role: str = "categorize") -> bool:
    return bool(_backend(conn, role)["url"])


def _prepare(be: dict, transport):
    """Vet the backend URL + build (base, transport, headers) for a chat POST.
    Shared by the categorizer (_chat) and the assistant (chat_tools) so the
    tenant-URL netguard applies to BOTH — a tenant-configured llm_url is
    re-checked at USE time (SimpleFIN's every-fetch guard) and, on hosted, the
    connect is DNS-pinned."""
    base = (be["url"] or "").rstrip("/")
    if not base:
        raise LLMError("no LLM backend configured (tenant llm_url config "
                       "or OIKONOME_LLM_URL env)")
    if be.get("source") == "tenant":
        from ..web.netguard import BlockedURL, check_url, pinned_transport
        try:
            check_url(base, what="the configured LLM endpoint")
        except BlockedURL as e:
            raise LLMError(str(e))
        if transport is None:
            transport = pinned_transport()
    headers = {}
    if be["api_key"]:
        headers["Authorization"] = f"Bearer {be['api_key']}"
    return base, transport, headers


def _merge_extra_body(body: dict, extra_body: str) -> None:
    """Fold a backend's extra_body JSON into the request body. Settings
    saves refuse anything but a JSON object now, but env values and rows
    stored before that check still exist — a non-object here must degrade
    to a warning, not crash every call routed through the backend (the
    old bare dict.update raised TypeError, which nothing caught)."""
    if not extra_body:
        return
    try:
        extra = json.loads(extra_body)
    except ValueError:
        extra = None
    if isinstance(extra, dict):
        body.update(extra)
    else:
        log.warning("LLM extra_body is not a JSON object; ignored")


def _read_completion(client: httpx.Client, url: str, body: dict,
                     headers: dict) -> dict:
    """POST a chat completion and return the parsed JSON, refusing any
    reply larger than MAX_RESPONSE_BYTES BEFORE it is buffered — the
    stream is abandoned at the cap, so a hostile or broken backend can't
    grow the process by the size of its answer."""
    with client.stream("POST", url, json=body, headers=headers) as r:
        r.raise_for_status()
        try:
            declared = int(r.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_RESPONSE_BYTES:
            raise LLMError("LLM reply too large")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise LLMError("LLM reply too large")
    return json.loads(bytes(buf))


def _chat(messages: list[dict], max_tokens: int, transport=None,
          backend: dict | None = None) -> str:
    """One OpenAI-compatible chat completion; returns the message content."""
    be = backend if backend is not None else _backend()
    base, transport, headers = _prepare(be, transport)
    body = {
        "model": be["model"],
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    _merge_extra_body(body, be["extra_body"])
    from ..web.netguard import BlockedURL
    try:
        with httpx.Client(transport=transport, timeout=REQUEST_TIMEOUT) as c:
            msg = _read_completion(c, f"{base}/v1/chat/completions", body,
                                   headers)["choices"][0]["message"]
    except BlockedURL as e:
        # fetch-time pin rejection (rebind caught mid-flight) reads like
        # the save-time rejection, not a transport crash
        raise LLMError(str(e))
    return msg.get("content") or msg.get("reasoning_content") or ""


def chat_tools(messages: list[dict], tools: list[dict], max_tokens: int,
               transport=None, backend: dict | None = None,
               timeout: float = REQUEST_TIMEOUT) -> dict:
    """OpenAI-compatible chat completion WITH tool-calling. Returns
    the raw assistant message dict (`content` + optional `tool_calls`). Same
    netguard + DNS-pin as _chat via _prepare."""
    be = backend if backend is not None else _backend()
    base, transport, headers = _prepare(be, transport)
    body = {
        "model": be["model"],
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "tools": tools,
        "tool_choice": "auto",
    }
    _merge_extra_body(body, be["extra_body"])
    from ..web.netguard import BlockedURL
    try:
        with httpx.Client(transport=transport, timeout=timeout) as c:
            return _read_completion(c, f"{base}/v1/chat/completions", body,
                                    headers)["choices"][0]["message"]
    except BlockedURL as e:
        raise LLMError(str(e))


# ---- selection -----------------------------------------------------------

from .merchant_sql import DISPLAY_MERCHANT as _DISPLAY, MC_JOIN as _MC_JOIN  # noqa: E402


def pending_merchants(conn, limit: int | None = None) -> list[dict]:
    """Unique spend merchants in generic aggregator buckets with no cached
    category, grouped and keyed by the CANONICAL merchant — the
    variants of one business are ONE pending merchant (classified once, with
    the canonical name as the model's input), a rule cached under ANY variant
    hides the whole canonical, and _classify_merchants/apply_seed therefore
    store new rules under the canonical key going forward. `raw_name` keeps a
    raw descriptor so seed prefix rules (TST* …) that canonicalization strips
    can still match."""
    from . import merchant_seed
    # Eligible when Plaid was generic OR low/medium confidence (max-accuracy
    # policy). Original PFC / category_plaid is used so a prior apply() that
    # rewrote category_primary does not hide the merchant from re-queue.
    q = f"""
        SELECT p.merchant, p.n, p.avg_amount, p.sample_name, p.raw_name
        FROM (
            SELECT {_DISPLAY} AS merchant,
                   COUNT(*) AS n, AVG(t.amount) AS avg_amount,
                   MAX(t.name) AS sample_name,
                   MAX(COALESCE(t.merchant_name, t.name)) AS raw_name,
                   SUM(t.amount) AS total,
                   MAX(t.merchant_id::text) AS mid
            FROM transactions t
            -- keyed AND grouped on the DISPLAY name — the merchant ROW's
            -- name first, then the alias, then the raw string, exactly
            -- merchant_sql.DISPLAY_MERCHANT: a candidate must resolve to
            -- the name the pages show, or the categorizer would learn a
            -- merchant nobody sees. Grouped on the alias string alone,
            -- a merchant whose row had been renamed or retargeted (the
            -- alias string lags: the resolver writes only merchant_id)
            -- was queued under a name apply() no longer resolves to it.
            -- It is also the key apply() resolves each row's rule by, so
            -- a rule cached here always reaches the rows it was learned
            -- from — grouped by the brand while apply() matched on the
            -- outlet, a fuel-arm row stayed uncategorized forever.
            {_MC_JOIN}
            WHERE t.removed = 0 AND t.amount > 0
              AND t.category_override IS NULL
              AND {_REFINEABLE_SQL}
              -- FLOW-GUARD: flow categories are importer-assigned spend
              -- exclusions (brokerage, retirement and crypto importer rows
              -- have no aggregator raw category); the LLM must never reclassify
              -- them (they'd become counted spend).
              AND COALESCE(t.category_primary, '') NOT IN {_FLOW_SQL}
              AND COALESCE(t.category_plaid, '') NOT IN {_FLOW_SQL}
              AND COALESCE(t.raw #>> '{{personal_finance_category,primary}}',
                           '') NOT IN {_FLOW_SQL}
              AND COALESCE(t.merchant_name, t.name) IS NOT NULL
            GROUP BY {_DISPLAY}
        ) p
        -- hashable anti-join, not a correlated NOT EXISTS: a rule covers
        -- the candidate when it resolves to the same merchant ROW (through
        -- its alias row, or a merchant of that exact name — the resolution
        -- apply() makes), or, for rows no merchant claims yet, when its
        -- string is the candidate's display name (and a correlated lookup
        -- per candidate is the quadratic shape apply(canon=...) exists to
        -- avoid)
        LEFT JOIN (SELECT DISTINCT COALESCE(mc2.merchant_id, named.id)::text AS mid,
                                   COALESCE(mc2.canonical, m.merchant) AS canon
                   FROM merchant_categories m
                   LEFT JOIN merchant_canonical mc2
                          ON mc2.raw_merchant = m.merchant
                   LEFT JOIN (SELECT DISTINCT ON (lower(mm2.name)) lower(mm2.name) AS lname, mm2.id
                                FROM merchants mm2 WHERE mm2.merged_into IS NULL
                               ORDER BY lower(mm2.name), (mm2.plaid_entity_id IS NULL)) named
                          ON named.lname = lower(m.merchant)) r
               ON (r.mid IS NOT NULL AND r.mid = p.mid) OR r.canon = p.merchant
        WHERE r.mid IS NULL AND r.canon IS NULL
        ORDER BY p.total DESC"""
    # bank mechanics never reach a categorizer — flow strings are
    # flow_classify()'s job, mechanics-only strings (bare checks/payments)
    # have no merchant to classify. Filtered in python (word patterns), then
    # capped, so the cap isn't wasted on rows we'd drop. Both the canonical
    # and the raw descriptor are screened — canonicalization must never
    # launder a flow/mechanics string into the LLM's view.
    rows = [dict(r) for r in conn.execute(q)
            if not merchant_seed.flow_match(r["merchant"])
            and not merchant_seed.skip_llm(r["merchant"])
            and not merchant_seed.flow_match(r["raw_name"])
            and not merchant_seed.skip_llm(r["raw_name"])]
    return rows[:int(limit)] if limit else rows


# ---- classification ------------------------------------------------------


def _prompt(merchants: list[dict]) -> list[dict]:
    catalog = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    listing = "\n".join(
        f"{i + 1}. {m['merchant']}"
        + (f" (descriptor: {m['sample_name'][:60]})"
           if m["sample_name"] and m["sample_name"] != m["merchant"] else "")
        + f" — {m['n']} charge(s), typical ${m['avg_amount']:.0f}"
        for i, m in enumerate(merchants))
    system = (
        "You categorize credit-card merchants for a personal budget. "
        "Assign each numbered merchant exactly one "
        f"category:\n{catalog}\n\n"
        "Rules: grocery stores and supermarkets are FOOD_AND_DRINK. "
        "Gas stations and convenience-store fuel are TRANSPORTATION. "
        "There is no pets category — pet stores and pet supplies are "
        "GENERAL_MERCHANDISE; only actual veterinary clinics are MEDICAL. "
        "Software, apps, VPNs, AI tools, and SaaS subscriptions are "
        "GENERAL_SERVICES unless they are clearly media streaming or games "
        "(ENTERTAINMENT). Shopping malls and thrift/antique stores are "
        "GENERAL_MERCHANDISE. Bike/sporting-goods shops are GENERAL_MERCHANDISE. "
        "Judge by what the business most likely is. Answer EVERY number, "
        "in order, using only categories from the list. "
        'Reply with JSON only: {"1": "<CATEGORY>", "2": "<CATEGORY>", ...}'
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": listing}]


def _parse(content: str, n: int) -> dict[int, str]:
    parsed = _json_object(content, "category")
    out = {}
    for k, v in parsed.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= n and isinstance(v, str) and v.strip().upper() in CATEGORIES:
            out[idx] = v.strip().upper()
    return out


def _classify_batch(merchants: list[dict], transport=None,
                    backend=None) -> dict[int, str]:
    content = _chat(_prompt(merchants), 30 * len(merchants) + 200,
                    transport=transport, backend=backend)
    return _parse(content, len(merchants))


# ---- apply (cheap SQL, runs every sync) -----------------------------------


def record_model_category(conn, merchant: str, category: str) -> None:
    """The classifier's answer for a merchant — written only where no
    PERSON has ruled. The batch is snapshotted before a call that can take
    minutes; a correction the household makes meanwhile arrives as a
    source='user' row, and an unguarded write would overwrite its category
    while the row kept saying 'user' — the machine's guess then running
    ledger-wide as if a person had chosen it."""
    conn.execute(
        """INSERT INTO merchant_categories (merchant, category_primary)
           VALUES (%s,%s)
           ON CONFLICT (tenant_id, merchant) DO UPDATE SET
               category_primary=EXCLUDED.category_primary,
               classified_at=now()
           WHERE merchant_categories.source <> 'user'""",
        (merchant, category))


def upsert_user_rule(conn, merchant: str, category: str) -> None:
    """Store a USER category correction keyed to the merchant's
    CANONICAL form, so one correction covers every descriptor variant of the
    same business (past + future), not just the descriptor the corrected
    transaction happened to carry. An un-deduped merchant is its own
    canonical. Last correction wins for a canonical (ON CONFLICT)."""
    r = conn.execute(
        "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
        (merchant,)).fetchone()
    canon = r["canonical"] if r else merchant
    # editing a rule re-enables it (disabled=false)
    conn.execute(
        """INSERT INTO merchant_categories (merchant, category_primary, source)
           VALUES (%s,%s,'user')
           ON CONFLICT (tenant_id, merchant) DO UPDATE SET
               category_primary=EXCLUDED.category_primary,
               source='user', classified_at=now(), disabled=false""",
        (canon, category))


def _touched(stats: dict | None) -> set:
    """The merchants a pass actually wrote, accumulated on `stats`
    so `categorize_new` can scope its apply to them. A plain set living on
    the stats dict every pass already carries — no new plumbing, and a caller
    that passes no stats (or an old one) still works, it just collects
    nothing and falls back to the full sweep."""
    if stats is None:
        return set()
    return stats.setdefault("touched", set())


def apply(conn, canon: str | None = None,
          canons: list[str] | None = None) -> int:
    """Push cached merchant_categories onto transactions.category_primary.

    Machine pass (seed + LLM cache): only rows that are refineable —
    generic Plaid buckets OR non-HIGH confidence — never flow, never
    override. User-merchant pass: may rewrite sharp HIGH Plaid too, because
    the person taught the merchant. category_plaid* columns are never
    written here.

    canon/canons limit the sweep to those merchants' rows. The full-table
    sweep evaluates a correlated merchant_categories aggregate per row —
    minutes on a large ledger — so interactive corrections (a single-row or
    merchant-wide recategorize) must pass the canonical they touched; only
    batch categorization (post-import/backfill) sweeps everything.

    On a ledger of tens of thousands of rows the unscoped sweep costs
    minutes to update ZERO rows, and `categorize_new` runs it at the end of
    every sync — which is what the header chip's "finishing up…" sits on.
    An EMPTY `canons` list means "nothing was touched, sweep nothing" and
    returns 0 without querying; `None` still means the full sweep.

    Entries may be canonicals or raw descriptors — a canonical is expanded to
    its raw variants and both forms go into the scope, so callers holding
    either (apply_seed caches canonicals, flow_classify matches raw names)
    can pass what they have."""
    scope_sql, scope_params = "", {}
    targets = (list(canons) if canons is not None
               else [canon] if canon is not None else None)
    if targets is not None:
        if not targets:
            return 0                  # nothing touched → nothing can change
        raws = [r["raw_merchant"] for r in conn.execute(
            "SELECT raw_merchant FROM merchant_canonical "
            "WHERE canonical = ANY(%s)", (targets,)).fetchall()]
        # an un-deduped merchant is its own canonical
        scope_params = {"scope_raws": sorted(set(raws) | set(targets))}
        # Two keys, deliberately: the canonical map is keyed on the DISPLAY
        # string (outlet first — a chain's fuel arm and every user rename
        # live there), while flow_classify hands us the base descriptor. A
        # scope matching only one of them silently updates nothing for the
        # other's rows, so match either.
        scope_sql = ("\n              AND (COALESCE(transactions.merchant_outlet, "
                     "transactions.merchant_name, transactions.name) "
                     "= ANY(%(scope_raws)s)"
                     "\n                   OR COALESCE(transactions.merchant_name, "
                     "transactions.name) = ANY(%(scope_raws)s))")
    # apply() uses bare column names (no t. alias) — expand refineable
    refineable = f"""(
        COALESCE(raw #>> '{{personal_finance_category,primary}}',
                 category_plaid, 'OTHER') IN {_GENERIC_SQL}
        OR UPPER(COALESCE(category_plaid_confidence,
                          raw #>> '{{personal_finance_category,confidence_level}}',
                          '')) NOT IN {_PLAID_TRUSTED_SQL}
    )"""
    # ONE pass over the rules, then a join — not a correlated
    # merchant_categories aggregate re-evaluated per transaction (twice: once
    # in SET, once in the EXISTS), each of whose canonical lookups is itself
    # a correlated subquery. That shape costs minutes to update ZERO rows on
    # a large ledger, at the end of every sync, and it is not a missing
    # index — adding one on merchant_canonical.raw_merchant barely moves it.
    #
    # `rules` is the unanimity gate lifted out whole: group the non-disabled
    # rules by canonical, keep only the canonicals whose rules agree, and the
    # single agreed category is MAX(). COUNT(DISTINCT …) ignores NULLs, so an
    # all-NULL group is dropped and a surviving group's MAX is never NULL —
    # the same rows the correlated form selected.
    def _rules_cte(user_only: bool) -> str:
        return f"""
        rules AS (
            -- keyed on the merchant ROW when the rule's string resolves to
            -- one (its alias row, else a merchant of that exact name), so
            -- every raw spelling under a merchant shares the rule and a
            -- rename/merge cannot detach it; on the canonical string only
            -- for a rule naming a merchant the ledger has never seen
            SELECT COALESCE(mid::text, canon) AS key,
                   MAX(canon) AS canon, MAX(mid::text) AS mid,
                   MAX(category_primary) AS cat,
                   -- who the rule came from, for the row's category_source
                   CASE WHEN bool_or(source = 'user') THEN 'rule_user'
                        WHEN bool_or(source = 'llm') THEN 'rule_llm'
                        WHEN bool_or(source = 'model') THEN 'rule_model'
                        ELSE 'rule_seed' END AS src
            FROM (SELECT m.category_primary, m.source,
                         COALESCE(mc2.canonical, m.merchant) AS canon,
                         COALESCE(mc2.merchant_id, named.id) AS mid
                    FROM merchant_categories m
                    LEFT JOIN merchant_canonical mc2
                           ON mc2.raw_merchant = m.merchant
                    -- the merchant row a rule's bare name resolves to,
                    -- resolved ONCE for all rules as a join: the same
                    -- lookup written as a correlated subquery ran a
                    -- sequential scan of every merchant per rule — six
                    -- seconds to recategorize three rows, because this
                    -- CTE is built whole even when the sweep is scoped
                    -- to one merchant. A live merchant beats a merged
                    -- one; among live ones the aggregator-backed row
                    -- wins, exactly the correlated form's ORDER BY.
                    LEFT JOIN (SELECT DISTINCT ON (lower(mm2.name)) lower(mm2.name) AS lname, mm2.id
                                 FROM merchants mm2
                                WHERE mm2.merged_into IS NULL
                                ORDER BY lower(mm2.name), (mm2.plaid_entity_id IS NULL)) named
                           ON named.lname = lower(m.merchant)
                   WHERE NOT m.disabled{" AND m.source = 'user'" if user_only else ""}
                     -- a rule may never NAME a flow category: transfers,
                     -- income and loan payments are decided per row by
                     -- the importer / flow_classify, and the user pass
                     -- below writes a rule's category over whatever the
                     -- aggregator said, so a flow-named rule (however it
                     -- got in — a rename, a replayed undo, an old row)
                     -- would turn a merchant's spend into transfers on
                     -- the next sync. Such a rule is simply not a rule.
                     AND COALESCE(m.category_primary, '') NOT IN {_FLOW_SQL}) x
            GROUP BY 1
            HAVING COUNT(DISTINCT category_primary) = 1)"""

    # the transaction's canonical, resolved once per row by join rather than
    # by a correlated lookup inside every rule comparison.
    #
    # Keyed on the DISPLAY string (outlet first), byte-for-byte the key
    # merchant_dedup's MC_JOIN uses and therefore the one the bulk
    # recategorize preview/undo snapshot counts rows by. Keyed on
    # merchant_name instead, the write and the snapshot resolved different
    # canonicals for the same row: a fuel-arm row (outlet "Acme Gas",
    # merchant_name "Acme") took the brand's rule while the snapshot
    # listed it under the outlet, so it was recategorized without an undo
    # entry and without being counted. The write must never match
    # differently than the page.
    cand_join = """
        FROM transactions
        LEFT JOIN merchant_canonical mc
               ON mc.raw_merchant = COALESCE(transactions.merchant_outlet,
                                             transactions.merchant_name,
                                             transactions.name)"""

    def _update(rows_where: str, user_only: bool) -> int:
        # TRUST GUARD (machine pass only). A machine rule —
        # LLM or seed alike — may reassign a row only when Plaid's
        # ORIGINAL primary was generic (GENERAL_*/OTHER — the buckets
        # refinement exists for) or agrees with it. Without the guard a
        # machine rule rewrites row after row that carries a concrete
        # aggregator label, mostly wrongly — rent → Transportation,
        # restaurants → Home Improvement, a burger stand → Rent &
        # Utilities. The seed layer is no more "curated" than the rest: it
        # matches bare brand words by substring, so a "Boost Espresso"
        # takes Boost Mobile's utilities label and a "Shell Bakery" takes a
        # fuel brand's. No machine source outranks an aggregator's concrete
        # label; only the USER pass may (they know their own merchant).
        guard = ("" if user_only else f"""
              AND (COALESCE(m.plaid0, 'OTHER') IN {_GENERIC_SQL}
                   OR m.cat = m.plaid0)""")
        cur = conn.execute(f"""
            WITH {_rules_cte(user_only)},
            cand AS (
                SELECT transactions.id AS id,
                       COALESCE(mc.canonical, transactions.merchant_outlet,
                                transactions.merchant_name,
                                transactions.name) AS canon,
                       transactions.merchant_id::text AS mid,
                       transactions.category_primary AS cur,
                       COALESCE(transactions.raw
                                    #>> '{{personal_finance_category,primary}}',
                                transactions.category_plaid) AS plaid0
                {cand_join}
                WHERE {rows_where}{scope_sql}),
            -- A row reaches its rule two ways, and BOTH are needed: through
            -- the merchant row, so every raw spelling under one merchant
            -- shares the rule; and through the canonical string, for the
            -- rows this sync has not resolved to a merchant yet. Keyed on
            -- the merchant alone, such a row matches nothing once the rule
            -- has resolved to a merchant, so a rule the user just wrote
            -- would not apply to the very transaction they wrote it from.
            --
            -- Two equi-joins UNIONed rather than one join ORing two key
            -- columns: an OR across different columns cannot use either
            -- index, and this runs over the whole ledger at the end of
            -- every sync. Then exactly ONE rule per row, chosen rather
            -- than stumbled upon — the user's rule first, then the
            -- merchant-keyed one, so a row matching both a merchant rule
            -- and a string rule cannot flip category between runs.
            match AS (
                SELECT DISTINCT ON (id) id, cur, plaid0, cat, src
                  FROM (
                    SELECT c.id, c.cur, c.plaid0, r.cat, r.src, r.key AS tie,
                           (r.src <> 'rule_user') AS machine,
                           (r.mid IS NULL) AS unkeyed
                      FROM cand c JOIN rules r ON r.mid = c.mid
                     WHERE c.mid IS NOT NULL
                    UNION ALL
                    SELECT c.id, c.cur, c.plaid0, r.cat, r.src, r.key,
                           (r.src <> 'rule_user'), (r.mid IS NULL)
                      FROM cand c JOIN rules r ON r.canon = c.canon
                     WHERE r.mid IS NULL OR c.mid IS NULL) u
                 ORDER BY id, machine, unkeyed, tie)
            UPDATE transactions SET category_primary = m.cat,
                                    category_source = m.src
            FROM match m
            WHERE transactions.id = m.id
              -- only rows that actually CHANGE, exactly as the old EXISTS did
              AND m.cat != COALESCE(m.cur, ''){guard}""", scope_params)
        return cur.rowcount

    with conn.transaction():
        # Machine pass: seed + LLM cache → primary when refineable + unanimous
        n = _update(f"""removed = 0 AND category_override IS NULL
              AND {refineable}
              -- the merchant's own MCC outranks a machine rule (a user
              -- rule, below, still wins)
              AND COALESCE(category_source, '') <> 'mcc'
              -- FLOW-GUARD: never clobber importer-assigned flow categories
              AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}
              AND COALESCE(category_plaid, '') NOT IN {_FLOW_SQL}""",
                    user_only=False)
        # USER corrections apply merchant-wide — past and future,
        # even over a sharp HIGH-confidence Plaid category (the user knows
        # their own merchant better than the aggregator), and over a FLOW
        # label too: a payment to a person through Venmo / Zelle / PayPal
        # is TRANSFER_OUT to the aggregator and "the babysitter" to the
        # household, and a rule that could not reach those rows left every
        # new one landing as a transfer no matter how many times the user
        # taught the merchant. The flow guard stays on the
        # MACHINE pass above — no model may turn a transfer into spend —
        # and a rule can never NAME a flow category (set_merchant_category
        # and rename_category refuse to write one, and the rules CTE above
        # ignores one that exists anyway), so a user rule only ever moves
        # rows INTO spend, on purpose. Per-row overrides still win.
        n += _update("removed = 0 AND category_override IS NULL",
                     user_only=True)
    return n


def categorize_check_payee(conn, txn_id: str, payee: str, memo: str = "",
                           transport=None) -> str | None:
    """Check-image parse → the transaction's category. A written check's
    ledger row is bank mechanics ("CHECK #1234" — skip_llm territory, no
    merchant to classify), but the payee read off the check's face IS the
    merchant. Feed that string through the SAME machinery a merchant
    descriptor uses: the cached rule (user rules included, canonical
    resolution, unanimity gate) wins outright; otherwise one LLM
    classification is cached under the payee so the next check to the same
    payee is free. The write respects everything apply() respects — never
    category_override, never flow rows, only generic aggregator buckets."""
    from . import merchant_seed
    payee = (payee or "").strip()
    if (not payee or merchant_seed.flow_match(payee)
            or merchant_seed.skip_llm(payee)):
        return None                       # flow strings stay flow_classify's
    txn = conn.execute(
        "SELECT amount FROM transactions WHERE id=%s AND removed=0",
        (txn_id,)).fetchone()
    if txn is None:
        return None
    c = conn.execute(
        "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
        (payee,)).fetchone()
    canon = c["canonical"] if c else payee
    r = conn.execute(
        f"""SELECT MAX(m.category_primary) AS cat FROM merchant_categories m
            WHERE {_RULE_CANON} = %s AND NOT m.disabled
            HAVING COUNT(DISTINCT m.category_primary) = 1""",
        (canon,)).fetchone()
    cat = r["cat"] if r else None
    if cat is None and configured(conn):
        try:
            # tenant CHAT backend (not the vision override the check parse
            # itself ran under) — this is a text classification
            result = _classify_batch(
                [{"merchant": payee, "n": 1,
                  "avg_amount": abs(txn["amount"] or 0.0),
                  "sample_name": f"check memo: {memo[:60]}" if memo else None}],
                transport=transport, backend=_backend(conn))
        except (LLMError, httpx.HTTPError) as e:
            log.warning("check payee classify failed: %s",
                        backend_failure(e, None))
            result = {}
        cat = result.get(1)
        if cat:
            # cache like _classify_merchants does (source defaults 'llm'),
            # keyed to the CANONICAL so every variant of the payee
            # shares the rule; never displace an existing rule
            conn.execute(
                """INSERT INTO merchant_categories (merchant, category_primary)
                   VALUES (%s,%s) ON CONFLICT (tenant_id, merchant)
                   DO NOTHING""", (canon, cat))
    if not cat:
        return None
    cur = conn.execute(f"""
        UPDATE transactions SET category_primary = %s, category_source = 'rule_llm'
        WHERE id = %s AND removed = 0 AND category_override IS NULL
          AND COALESCE(raw #>> '{{personal_finance_category,primary}}',
                       'OTHER') IN {_GENERIC_SQL}
          -- FLOW-GUARD: an importer-assigned flow row stays a flow row
          AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}""",
        (cat, txn_id))
    return cat if cur.rowcount else None


def flow_classify(conn, stats: dict | None = None) -> int:
    """Route bank-mechanics rows on category-less sources to flow
    categories BEFORE any categorizer runs — a credit-card payment filed as
    spend double-counts against the budget. Only rows with NO aggregator
    category (raw), no override, and no existing flow category; also purges
    any mis-cached spend entry for these strings (a prior LLM pass may have
    filed "Chase Credit Card" under merchandise)."""
    from . import merchant_seed
    rows = conn.execute(f"""
        SELECT DISTINCT COALESCE(merchant_name, name) AS merchant
        FROM transactions
        WHERE removed = 0 AND category_override IS NULL
          AND COALESCE(raw #>> '{{personal_finance_category,primary}}', '') = ''
          AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}""").fetchall()
    n = 0
    touched = _touched(stats)
    with conn.transaction():
        for r in rows:
            m = r["merchant"]
            fm = merchant_seed.flow_match(m)
            if not fm:
                continue
            touched.add(m)            # raw descriptor, scopes apply
            primary, detailed = fm
            conn.execute(
                "DELETE FROM merchant_categories "
                "WHERE merchant=%s AND source != 'user'", (m,))
            if primary == "TRANSFER_OUT":
                # direction follows the money: outgoing → TRANSFER_OUT,
                # incoming ("Transfer from savings") → TRANSFER_IN
                cur = conn.execute(f"""
                    UPDATE transactions SET
                        category_primary = CASE WHEN amount < 0
                            THEN 'TRANSFER_IN' ELSE 'TRANSFER_OUT' END,
                        category_detailed = NULL, category_source = 'flow'
                    WHERE removed = 0 AND category_override IS NULL
                      AND COALESCE(merchant_name, name) = %s
                      AND COALESCE(raw #>> '{{personal_finance_category,primary}}', '') = ''
                      AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}""",
                    (m,))
            else:
                cur = conn.execute(f"""
                    UPDATE transactions
                    SET category_primary = %s, category_detailed = %s,
                        category_source = 'flow'
                    WHERE removed = 0 AND category_override IS NULL
                      AND COALESCE(merchant_name, name) = %s
                      AND COALESCE(raw #>> '{{personal_finance_category,primary}}', '') = ''
                      AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}""",
                    (primary, detailed, m))
            n += cur.rowcount
    if stats is not None:
        stats["flow_classified"] = n
    return n


# institution names arrive spelled differently on each side —
# `items.institution_name` is "Acme"/"Northwind" while Plaid's
# counterparty is "Acme Bank"/"Northwind Bank, Inc.". Compare on letters
# and digits only, with the corporate suffixes dropped, so the two agree.
_INST_KEY = r"regexp_replace(lower(%s),'[^a-z0-9]|bank|inc|llc','','g')"


def mcc_classify(conn, stats: dict | None = None) -> int:
    """The merchant's registered line of business (MCC) as a category, for
    rows Plaid was unsure or generic about. Sits between Plaid's confident
    answer and the machine rules: it never touches an override, a flow row,
    a HIGH/VERY_HIGH-and-specific Plaid answer, a row a USER rule decides
    (the user pass runs after and wins), or a row the statement line itself
    labelled as a brand's pump (category_source='fuel' — direct evidence,
    which a registered line of business that says "grocery" for the
    warehouse does not outrank). Idempotent — stamps category_source='mcc'
    and skips rows already carrying it."""
    from . import mcc as _mcc
    n = 0
    # rows this pass overwrote before it learned to leave fuel alone: an
    # outlet (the ingest's own fuel stamp) carrying an MCC category is that
    # mistake, and the receipt still says pump — put the stamp back
    n += conn.execute(
        """UPDATE transactions
              SET category_primary = 'TRANSPORTATION',
                  category_detailed = 'TRANSPORTATION_GAS',
                  category_source = 'fuel'
            WHERE removed = 0 AND category_override IS NULL
              AND merchant_outlet IS NOT NULL AND category_source = 'mcc'""").rowcount
    rows = conn.execute(f"""
        SELECT id, mcc FROM transactions
         WHERE removed = 0 AND category_override IS NULL
           AND mcc IS NOT NULL
           AND COALESCE(category_source, '') NOT IN ('mcc', 'rule_user', 'fuel')
           AND COALESCE(category_primary, '') NOT IN {_FLOW_SQL}
           AND COALESCE(category_plaid, '') NOT IN {_FLOW_SQL}
           AND (COALESCE(raw #>> '{{personal_finance_category,primary}}',
                         'OTHER') IN {_GENERIC_SQL}
                OR COALESCE(category_plaid_confidence, '') NOT IN ('HIGH', 'VERY_HIGH'))
    """).fetchall()
    with conn.transaction():
        for r in rows:
            m = _mcc.category_for(r["mcc"])
            if not m:
                continue
            n += conn.execute(
                """UPDATE transactions
                      SET category_primary = %s, category_detailed = %s,
                          category_source = 'mcc'
                    WHERE id = %s AND category_override IS NULL
                      AND (category_primary IS DISTINCT FROM %s
                           OR COALESCE(category_source, '') <> 'mcc')""",
                (m[0], m[1], r["id"], m[0])).rowcount
    if stats is not None:
        stats["mcc_classified"] = stats.get("mcc_classified", 0) + n
    return n


def own_transfer_classify(conn, stats: dict | None = None) -> int:
    """Money moving between the owner's OWN accounts is a transfer, whatever
    the aggregator called it.

    A brokerage withdrawal deposited into the household's checking account
    arrives with the brokerage's payment-rail descriptor, and the aggregator
    may stamp it INCOME / INCOME_CONTRACTOR. Nothing downstream can undo
    that — the FLOW-GUARD bars seed/LLM from revisiting any flow row, so a
    wrong INCOME would be permanent and corrected by hand every time.

    The signal is structural, not textual: Plaid names a `financial_institution`
    counterparty, and we already know which institutions the household banks
    with. When that counterparty is an institution where they hold an
    INVESTMENT account, a deposit into their checking account is their own
    money moving, not earnings. Direction follows the sign, exactly like
    `flow_classify` and `flowmap.transfer_category`.

    Deliberately narrow:

    * the counterparty institution must hold an **investment** account. A
      depository-only match would catch a genuine account-opening bonus
      from your own bank, and "from investment to checking" is the case
      that is actually unambiguous.
    * `category_override` is never touched: hand corrections stay sacred.
    * rows already TRANSFER_*/LOAN_PAYMENTS are left alone; a card payment to
      an institution you also invest with is still a card payment. INCOME and
      spend categories ARE rewritten, because that is the defect.
    """
    # Imported here rather than at module scope because the shape guard is
    # a SQL fragment owned by the read layer and the engine must not depend
    # on the web package to load — the same idiom budget.py uses to reach
    # demoguard.
    from ..web.data import _as_array
    cur = conn.execute(f"""
        WITH own AS (
            SELECT DISTINCT {_INST_KEY % 'i.institution_name'} AS k
            FROM items i JOIN accounts a ON a.item_id = i.id
            WHERE COALESCE(i.status,'') <> 'archived'
              AND a.type = 'investment'
              AND COALESCE(i.institution_name,'') <> '')
        UPDATE transactions t SET
            category_primary = CASE WHEN t.amount < 0
                THEN 'TRANSFER_IN' ELSE 'TRANSFER_OUT' END,
            category_detailed = NULL, category_source = 'flow'
        FROM accounts a
        WHERE a.id = t.account_id
          AND t.removed = 0 AND t.category_override IS NULL
          AND a.type = 'depository'
          AND COALESCE(t.category_primary,'')
              NOT IN ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')
          AND EXISTS (
              -- Through the shape guard, because this is ONE update over
              -- the whole ledger: a counterparty list that is an object
              -- rather than a list raises, and the raise loses the entire
              -- pass for the household, not the one row it came from.
              SELECT 1 FROM jsonb_array_elements(
                       {_as_array("t.raw->'counterparties'")}) cp
              WHERE cp->>'type' = 'financial_institution'
                AND {_INST_KEY % "cp->>'name'"} IN (SELECT k FROM own))""")
    n = cur.rowcount
    if stats is not None:
        stats["own_transfers"] = n
    return n


def model_pass(conn, stats: dict | None = None,
               limit: int | None = None) -> int:
    """The local classifier's turn: runs AFTER the seed and BEFORE any
    LLM, over the same pending set. Confident predictions become
    merchant_categories rows under source='model'; user rules outrank
    them and apply()'s trust guard binds them like every machine source.
    Abstentions stay pending, so a configured LLM still sees exactly the
    merchants the model would not vouch for. No model file configured =
    a no-op, exactly like an unconfigured backend."""
    from . import model_categorize
    merchants = pending_merchants(conn, limit=limit)
    if not merchants:
        return 0
    # the tenant's learned overlay answers even when no shipped artifact
    # is mounted, so availability is decided inside classify(), per model
    srcs: dict = {}
    got = model_categorize.classify(merchants, conn=conn, stats=srcs)
    if stats is not None:
        stats["model_classified"] = len(got)
    touched = _touched(stats)
    n = 0
    with conn.transaction():
        for i, (cat, _conf) in got.items():
            m = merchants[i]
            if cat not in CATEGORIES:
                continue
            conn.execute(
                """INSERT INTO merchant_categories
                       (merchant, category_primary, source)
                   VALUES (%s,%s,'model')
                   ON CONFLICT (tenant_id, merchant) DO NOTHING""",
                (m["merchant"], cat))
            touched.add(m["merchant"])
            n += 1
    if n:
        # the two sources hold different bars. The per-source figures are
        # ADMISSION counts (what cleared each threshold in classify);
        # n is what was actually written after the known-category filter,
        # so the halves need not sum to n.
        log.info("model categorizer: wrote %d of %d pending merchants "
                 "(admitted: overlay %d at threshold %.2f, shipped %d at "
                 "threshold %.2f)", n, len(merchants),
                 srcs.get("overlay", 0), model_categorize.overlay_min_conf(),
                 srcs.get("shipped", 0), model_categorize.min_conf())
    return n


def apply_seed(conn, stats: dict | None = None) -> int:
    """Deterministic first pass — cache curated seed-rule matches
    (national chains, processor prefixes) so the LLM only sees the genuinely
    ambiguous tail. Runs even with NO backend configured: the seed rules
    (plus apply) give every install baseline categorization for free."""
    from . import merchant_seed
    n = 0
    touched = _touched(stats)
    merchants = pending_merchants(conn)
    with conn.transaction():
        for m in merchants:
            # m["merchant"] is the canonical; a processor prefix the
            # canonical form strips (TST* → Toast) still matches via the raw
            # descriptor, and the rule is cached under the canonical key
            cat = (merchant_seed.match(m["merchant"])
                   or merchant_seed.match(m.get("raw_name")))
            if cat:
                conn.execute(
                    """INSERT INTO merchant_categories
                           (merchant, category_primary, source)
                       VALUES (%s,%s,'seed')
                       ON CONFLICT (tenant_id, merchant) DO NOTHING""",
                    (m["merchant"], cat))
                touched.add(m["merchant"])       # canonical key
                n += 1
    if stats is not None:
        stats["seeded"] = n
    return n


def backend_failure(e: BaseException, backend: dict | None) -> str:
    """One readable sentence for a failed backend call, for the run record
    and the Doctor. A 404 from an OpenAI-compatible endpoint means the
    model is not loaded there (Ollama answers exactly that when a model's
    blobs are gone) — said as such, not as a status code."""
    model = (backend or {}).get("model") or "the configured model"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 404:
            return (f"the backend answered 404 — model '{model}' is not "
                    f"loaded there")
        return f"the backend answered HTTP {code}"
    if isinstance(e, httpx.TimeoutException):
        return "the backend timed out"
    if isinstance(e, httpx.TransportError):
        return f"the backend is unreachable ({type(e).__name__})"
    if isinstance(e, LLMError):
        # our own wording, written to carry no household content
        # (_unusable_reply says why that matters here)
        return str(e)[:200]
    # Anything else is an exception this module did not word, and text we
    # did not write may quote whatever it choked on — a prompt, a row, a
    # model's reply. The summary and tag passes catch Exception, so that is
    # a wide door. What comes through it is reported by CLASS; the message
    # goes to the debug log the feedback bundle cannot capture.
    log.debug("LLM backend failure detail: %s", e, extra={"no_capture": True})
    return f"the backend call failed ({type(e).__name__})"


def _note_failure(stats: dict, e: BaseException, backend: dict | None) -> str:
    """Record why a batch failed and return that same sentence, so the app
    log says exactly what the run record and the Doctor will say — the log
    ring rides along in feedback bundles too, and it is no place for the
    raw reply either."""
    stats["backend_error"] = backend_failure(e, backend)
    return stats["backend_error"]


def record_run(conn, stats: dict, backend: dict) -> None:
    """The nightly run's own verdict, kept in job_progress (id 'llm') so
    the Doctor and the alert strip can say the last run failed — a
    missing model otherwise fails every batch silently (each failure is
    a warning in the app log, which nobody reads at 4am) and the only
    symptom is Amazon summaries and categories that quietly stop.
    'error' only when the backend failed and NOTHING was produced: one bad
    batch among good ones is a hiccup, not a dead backend."""
    from .compat import jsonb
    produced = (stats.get("merchants_classified", 0)
                + stats.get("items_classified", 0)
                + stats.get("summaries_written", 0)
                + stats.get("tags_written", 0))
    failed = bool(stats.get("backend_error")) and produced == 0
    rec = {"error": stats.get("backend_error") if failed else None,
           # this record's error is a CLASSIFICATION of the failure, never
           # a piece of the backend's reply; last_run refuses to hand back
           # the errors of records written before that was true
           "error_classified": True,
           "model": backend.get("model"), "url": backend.get("url"),
           "produced": produced,
           "pending": (stats.get("pending_merchants", 0)
                       + stats.get("pending_items", 0)
                       + stats.get("pending_summaries", 0))}
    try:
        conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES ('llm', %s, %s::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   state=EXCLUDED.state, progress=EXCLUDED.progress,
                   started_at=now(), updated_at=now()""",
            ("error" if failed else "done", jsonb(rec)))
        conn.commit()
    except Exception as e:                                   # noqa: BLE001
        log.warning("llm run record not written: %s", e)


def last_run(conn) -> dict | None:
    """{state, error, model, url, produced, pending, at} of the last
    recorded run, or None before any."""
    try:
        row = conn.execute(
            "SELECT state, progress, updated_at FROM job_progress "
            "WHERE id='llm'").fetchone()
    except Exception:                                        # noqa: BLE001
        return None
    if not row:
        return None
    from .compat import as_dict
    prog = as_dict(row["progress"]) or {}
    if prog.get("error") and not prog.get("error_classified"):
        # A record written before failures were classified quotes the
        # backend's raw reply, which can be the prompt echoed back —
        # household purchases, on the Doctor page and inside any feedback
        # bundle (see _unusable_reply). The next run overwrites it, but an
        # instance whose backend was switched off never has a next run, so
        # the old text is dropped on the way out rather than shown.
        prog["error"] = "the last run failed (no detail was recorded)"
    return {"state": row["state"], "at": row["updated_at"], **prog}


def _classify_merchants(conn, *, limit, batch_size, dry_run, stats,
                        transport=None, backend=None, progress=None) -> None:
    merchants = pending_merchants(conn, limit=limit)
    stats["pending_merchants"] = len(merchants)
    if progress and merchants:
        progress(0, len(merchants))
    for i in range(0, len(merchants), batch_size):
        batch = merchants[i:i + batch_size]
        try:
            result = _classify_batch(batch, transport=transport,
                                     backend=backend)
        except (LLMError, httpx.HTTPError) as e:
            log.warning("merchant batch failed: %s",
                        _note_failure(stats, e, backend))
            stats["unparseable"] += len(batch)
            result = None
        if result is not None:
            with conn.transaction():
                for idx, m in enumerate(batch, start=1):
                    cat = result.get(idx)
                    if cat is None:
                        stats["unparseable"] += 1
                        continue
                    stats["merchants_classified"] += 1
                    if not dry_run:
                        _touched(stats).add(m["merchant"])
                        record_model_category(conn, m["merchant"], cat)
        # progress counts ATTEMPTED merchants — a failed batch must still
        # advance it or the wizard's progress row sits frozen at 0/N while
        # every batch errors (the failure itself is reported via stats)
        if progress:
            progress(min(i + batch_size, len(merchants)), len(merchants))
        log.info("merchants: %d/%d", min(i + batch_size, len(merchants)),
                 len(merchants))


# ---- Amazon item classification --------------------------------------------

MAX_ITEMS_PER_ROW = 6
MAX_TITLE_CHARS = 140


def amazon_categories(conn) -> list[str]:
    """Assignable Amazon item categories: the tenant's existing category set
    (whatever the collector's own rules assigned) + the 'Shopping' default,
    minus 'Refunds' (refund rows are never re-classified)."""
    cats = {r["category"] for r in conn.execute(
        "SELECT DISTINCT category FROM amazon_orders WHERE category != ''")}
    return sorted((cats | {"Shopping"}) - {"Refunds"})


def amazon_pending(conn, limit: int | None = None) -> list[dict]:
    q = ("SELECT dedup_key, payee, seller, memo, category, items_json "
         "FROM amazon_orders "
         "WHERE category_source != 'llm' AND is_refund = 0 ORDER BY date DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q)]


def _amazon_row_text(row: dict) -> str:
    items = row.get("items_json") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = []
    if not isinstance(items, list):
        # `or []` above answers "is it empty", not "is it a list": a dict is
        # truthy and slices with a TypeError, which would end the nightly
        # summary pass rather than skip one row
        items = []
    titles = [str(d.get("title", ""))[:MAX_TITLE_CHARS]
              for d in items[:MAX_ITEMS_PER_ROW]
              if isinstance(d, dict) and d.get("title")]
    parts = ["; ".join(titles) if titles else (row["memo"] or "")[:MAX_TITLE_CHARS]]
    if row["seller"]:
        parts.append(f"(seller: {row['seller'][:60]})")
    return " ".join(p for p in parts if p).strip() or "unknown item"


def _amazon_prompt(rows, categories: list[str]) -> list[dict]:
    catalog = "\n".join(f"- {c}" for c in categories)
    listing = "\n".join(f"{i + 1}. {_amazon_row_text(r)}" for i, r in enumerate(rows))
    system = (
        "You categorize Amazon purchases for a personal budget. "
        "Assign each numbered purchase exactly one category from this list:\n"
        f"{catalog}\n\n"
        "Rules: judge by what the product IS and its most likely use. "
        "Products for children/babies go to 'Kids & Baby' even if they are "
        "apparel or toys. Use 'Shopping' only when nothing else fits. "
        'Reply with JSON only: {"1": "<category>", "2": "<category>", ...}'
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": listing}]


def _classify_amazon_batch(rows, categories: list[str],
                           transport=None, backend=None) -> dict[int, str]:
    content = _chat(_amazon_prompt(rows, categories), 30 * len(rows) + 200,
                    transport=transport, backend=backend)
    canon = {c.lower(): c for c in categories}
    out = {}
    for k, v in _json_object(content, "purchase-category").items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(rows) and isinstance(v, str) and v.strip().lower() in canon:
            out[idx] = canon[v.strip().lower()]
    return out


def _classify_amazon(conn, *, limit, batch_size, dry_run, stats,
                     transport=None, backend=None) -> None:
    categories = amazon_categories(conn)
    rows = amazon_pending(conn, limit=limit)
    stats["pending_items"] = len(rows)
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            result = _classify_amazon_batch(batch, categories,
                                            transport=transport,
                                            backend=backend)
        except (LLMError, httpx.HTTPError, ValueError) as e:
            log.warning("amazon batch failed: %s",
                        _note_failure(stats, e, backend))
            stats["unparseable"] += len(batch)
            continue
        with conn.transaction():
            for idx, row in enumerate(batch, start=1):
                cat = result.get(idx)
                if cat is None:
                    stats["unparseable"] += 1
                    continue
                stats["items_classified"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE amazon_orders SET category=%s, category_source='llm' "
                        "WHERE dedup_key=%s", (cat, row["dedup_key"]))
        log.info("amazon items: %d/%d", min(i + batch_size, len(rows)), len(rows))


# ---- Amazon item summaries ("Amazon — kids play mat" in the email) ---------


def summaries_pending(conn, limit: int | None = None) -> list[dict]:
    """Matched orders (they appear in the email) without a summary yet.
    Recent first so the daily email is served before the backfill."""
    q = ("""SELECT o.dedup_key, o.payee, o.seller, o.memo, o.category, o.items_json
            FROM amazon_orders o
            JOIN amazon_matches m ON m.dedup_key = o.dedup_key
            LEFT JOIN amazon_summaries s ON s.dedup_key = o.dedup_key
            WHERE s.dedup_key IS NULL AND o.items_json IS NOT NULL
              AND o.items_json != '[]'::jsonb
            ORDER BY o.date DESC""")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q)]


def _items_key(row: dict) -> str:
    """Order-insensitive fingerprint of what an order CONTAINED — the sorted
    lowercased item titles. Two orders with the same key are the same
    purchase repeated, and repeat purchases must carry the SAME summary:
    the model samples its output, so asking it twice about the identical
    jar of honey yields 'honey' one day and 'manuka honey' the next, and a
    ledger search then looks inconsistent for no reason. Empty when the
    order has no titled items (nothing safe to equate on)."""
    items = row.get("items_json") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = []
    titles = sorted(str(d.get("title", "")).strip().lower()
                    for d in items
                    if isinstance(d, dict) and str(d.get("title", "")).strip())
    return "\n".join(titles)


def _summary_prompt(rows) -> list[dict]:
    listing = "\n".join(f"{i + 1}. {_amazon_row_text(r)}" for i, r in enumerate(rows))
    system = (
        "You summarize Amazon purchases for a personal budget report. "
        "For each numbered purchase name every distinct item bought as a "
        "2-4 word plain-English phrase — lowercase, no brand names, no "
        "sizes/colors/counts — joined with ', '. Never write 'more' or "
        "'etc'; name each item. "
        "Examples: 'toddler pants, night light', 'bike computer mount', "
        "'dog food, chew toys, leash'. "
        'Reply with JSON only: {"1": "<summary>", "2": "<summary>", ...}'
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": listing}]


def _parse_freetext(content: str, n: int, stage: str = "text") -> dict[int, str]:
    """Like _parse but for free-text values (summaries, not categories).
    Returns 0-based indexes. `stage` only names the caller in a failure
    message, so a run record says which pass could not read its reply."""
    parsed = _json_object(content, stage)
    out = {}
    for k, v in parsed.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= n and isinstance(v, str) and v.strip():
            out[idx - 1] = v.strip()
    return out


def _summarize_amazon(conn, *, limit, batch_size, dry_run, stats,
                      transport=None, backend=None) -> None:
    rows = summaries_pending(conn, limit=limit)
    stats["pending_summaries"] = len(rows)
    if not rows:
        return

    def write(dedup_key: str, summary: str) -> None:
        if not dry_run:
            conn.execute(
                """INSERT INTO amazon_summaries (dedup_key, summary)
                   VALUES (%s, %s)
                   ON CONFLICT (tenant_id, dedup_key) DO UPDATE SET
                       summary=EXCLUDED.summary, created_at=now()""",
                (dedup_key, summary))
        stats["summaries_written"] = stats.get("summaries_written", 0) + 1

    # Identical orders share one summary (see _items_key): first, the text
    # an earlier run already produced for the same item set…
    known: dict[str, str] = {}
    for r in conn.execute(
            """SELECT o.items_json, s.summary FROM amazon_summaries s
               JOIN amazon_orders o ON o.dedup_key = s.dedup_key"""):
        k = _items_key(dict(r))
        if k:
            known.setdefault(k, r["summary"])
    groups: dict[str, list[dict]] = {}
    singles: list[dict] = []              # untitled items: nothing to equate on
    for r in rows:
        if _items_key(r):
            groups.setdefault(_items_key(r), []).append(r)
        else:
            singles.append(r)
    with conn.transaction():
        for k in [k for k in groups if k in known]:
            for r in groups.pop(k):
                write(r["dedup_key"], known[k])
                stats["summaries_reused"] = stats.get("summaries_reused", 0) + 1
    # …then the model is asked ONCE per item set it has never seen, and the
    # answer fans out to every pending duplicate of that set
    ask = [g[0] for g in groups.values()] + singles
    for start in range(0, len(ask), batch_size):
        chunk = ask[start:start + batch_size]
        try:
            content = _chat(_summary_prompt(chunk), 60 * len(chunk) + 200,
                            transport=transport, backend=backend)
            results = _parse_freetext(content, len(chunk), "summary")
        except Exception as e:                   # noqa: BLE001 — batch isolation
            log.warning("summary batch failed: %s",
                        _note_failure(stats, e, backend))
            continue
        with conn.transaction():
            for idx, summary in results.items():
                summary = (summary or "").strip().strip('."')[:200]
                if not summary:
                    stats["unparseable"] += 1
                    continue
                k = _items_key(chunk[idx])
                for row in (groups.get(k) or [chunk[idx]]) if k else [chunk[idx]]:
                    write(row["dedup_key"], summary)
                if k:
                    known.setdefault(k, summary)


# ---- recurring-proposal tags (advisory) -------------------------------------

PROPOSAL_TAGS = ("subscription", "bill", "habit", "one-off streak")


def proposals_pending_tags(conn, limit: int | None = None) -> list[dict]:
    q = ("""SELECT id, payee, amount, frequency, "interval", bill_type, evidence
            FROM bill_proposals
            WHERE status = 'pending' AND kind = 'add' AND llm_tag IS NULL
            ORDER BY created_at""")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q)]


def _proposal_prompt(rows) -> list[dict]:
    def desc(r):
        ev = r["evidence"] or {}
        if isinstance(ev, str):
            ev = json.loads(ev or "{}")
        if r["bill_type"] == "envelope":
            cpm = ev.get("charges_per_month")
            if isinstance(cpm, dict) and cpm:       # stored per-month {ym: n}
                counts = list(cpm.values())
                cpm = f"{min(counts)}-{max(counts)}"
            cad = (f"~${r['amount']:,.0f}/month across "
                   f"{cpm or 'several'} charges")
        else:
            iv = r["interval"] or 1
            cad = f"${r['amount']:,.0f} every {iv if iv > 1 else ''} {(r['frequency'] or '').lower()}"
        return f"{r['payee']} — {cad}"
    listing = "\n".join(f"{i + 1}. {desc(r)}" for i, r in enumerate(rows))
    system = (
        "You judge candidate recurring charges for a personal budget. For each "
        "numbered merchant pattern answer what it most likely is: "
        "'subscription' (fixed-price service billed on a schedule), "
        "'bill' (utility/insurance/medical obligation), "
        "'habit' (voluntary repeat purchases, e.g. coffee runs), or "
        "'one-off streak' (coincidental repeats, not truly recurring). "
        'Reply with JSON only: {"1": "<label>", "2": "<label>", ...}'
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": listing}]


def _tag_proposals(conn, *, limit, dry_run, stats, transport=None,
                   backend=None) -> None:
    rows = proposals_pending_tags(conn, limit=limit)
    stats["pending_tags"] = len(rows)
    for start in range(0, len(rows), 20):
        chunk = rows[start:start + 20]
        try:
            content = _chat(_proposal_prompt(chunk), 10 * len(chunk) + 200,
                            transport=transport, backend=backend)
            results = _parse_freetext(content, len(chunk), "proposal-tag")
        except Exception as e:                   # noqa: BLE001 — batch isolation
            log.warning("proposal-tag batch failed: %s",
                        _note_failure(stats, e, backend))
            continue
        with conn.transaction():
            for idx, tag in results.items():
                tag = (tag or "").strip().lower()
                if tag not in PROPOSAL_TAGS:
                    stats["unparseable"] += 1
                    continue
                if not dry_run:
                    conn.execute("UPDATE bill_proposals SET llm_tag=%s WHERE id=%s",
                                 (tag, chunk[idx]["id"]))
                stats["tags_written"] = stats.get("tags_written", 0) + 1


# ---- entry point -----------------------------------------------------------


# Sync-time merchant cap: an hourly sweep sees a handful of new merchants,
# but the FIRST run after enabling an LLM inherits the whole backlog — on a
# CPU-only local model that could stall the hourly sync (and the onboarding
# force-sync the user is watching) for a very long time. pending_merchants
# orders by total spend DESC, so the cap takes the merchants that matter
# most; the uncapped nightly run() mops up the tail.
SYNC_MERCHANT_LIMIT = 100


def categorize_new(conn, *, limit: int | None = SYNC_MERCHANT_LIMIT,
                   batch_size: int = BATCH_SIZE, transport=None,
                   progress=None) -> dict:
    """Sync-time categorization: classify NEW unknown spend
    merchants (up to `limit`, biggest spend first) and apply the cached
    merchant map — nothing else. Meant to run at the END of every sync so
    freshly pulled rows (especially sparse SimpleFIN merchants the aggregator
    leaves uncategorized/generic) are categorized within the hour instead of
    only overnight.

    The merchant pass runs here (unique unknown merchants only, cached), and
    so does the Amazon chain — classify, order↔txn match, item summaries — so
    a charge that syncs mid-day shows its items by the next page/email render
    instead of waiting for the nightly sweep. All three are pending-gated, so
    tenants without Amazon orders pay one EXISTS probe. Recurring-proposal
    tags depend on nightly detection output and stay in run. apply runs
    regardless of backend so the cached map (flow-guarded) sharpens the new
    rows even when no LLM is configured."""
    backend = _backend(conn)
    stats = {"pending_merchants": 0, "merchants_classified": 0,
             "pending_items": 0, "items_classified": 0,
             "seeded": 0, "flow_classified": 0, "unparseable": 0,
             "own_transfers": 0,
             "rows_updated": 0, "configured": bool(backend["url"])}
    flow_classify(conn, stats)                  # bank mechanics → flow
    own_transfer_classify(conn, stats)          # own-institution → transfer
    mcc_classify(conn, stats)                   # the merchant's own MCC next
    apply_seed(conn, stats)                     # deterministic rules next
    model_pass(conn, stats, limit=limit)        # local classifier next
    if backend["url"] and pending_merchants(conn, limit=1):
        # Skip-if-held, like the Amazon chain below: the nightly sweep (or
        # another sync door) is already billing the LLM for these exact
        # merchants, and classifying them twice pays twice for the same
        # answer. Every step is pending-gated, so the next pass mops up.
        with merchant_classify_lock(conn) as got:
            if got:
                _classify_merchants(conn, limit=limit, batch_size=batch_size,
                                    dry_run=False, stats=stats,
                                    transport=transport, backend=backend,
                                    progress=progress)
    if conn.execute("SELECT 1 FROM amazon_orders LIMIT 1").fetchone():
        # classify before matching so a fresh category reaches the
        # "Amazon - <category>" override in the same pass; matching before
        # summaries because only matched orders get summarized. Skip-if-held:
        # the nightly sweep runs this same chain under the shared lock, and
        # every step is pending-gated, so the next hourly pass catches up.
        with amazon_chain_lock(conn) as got:
            if got:
                if backend["url"] and amazon_pending(conn, limit=1):
                    _classify_amazon(conn, limit=limit, batch_size=20,
                                     dry_run=False, stats=stats,
                                     transport=transport, backend=backend)
                from . import amazon_match
                amazon_match.run_match(conn)
                if backend["url"] and summaries_pending(conn, limit=1):
                    _summarize_amazon(conn, limit=limit, batch_size=20,
                                      dry_run=False, stats=stats,
                                      transport=transport, backend=backend)
    if conn.execute("SELECT 1 FROM costco_receipts LIMIT 1").fetchone():
        # receipts carry their categories from the collector; only the
        # match itself runs here, under the same lock as the Amazon chain
        with amazon_chain_lock(conn) as got:
            if got:
                from . import costco_match
                costco_match.run_match(conn)
    # still the FULL sweep, deliberately. Scoping this to the merchants the
    # run touched breaks this function's contract
    # (`test_applies_cached_map_when_unconfigured`: a cached rule must
    # reach a newly pulled row even with no LLM), and the honest candidate
    # set that would preserve it is most of the ledger's merchants, so it
    # buys nothing. The cost is the query SHAPE — a correlated
    # merchant_categories aggregate per row — not the scope, and not a
    # missing index. Fixing it needs apply() rewritten as a join; until
    # then the tail at least NAMES itself to whoever is watching.
    stats["rows_updated"] = apply(conn)
    stats.pop("touched", None)                  # internal; not a stat
    return stats


_AMAZON_LOCK = "hashtext('oikonome:amazon:' || current_setting('app.tenant_id'))"


@contextmanager
def amazon_chain_lock(conn, *, wait: bool = False):
    """Single-flight for the Amazon classify → match → summarize chain.

    The chain runs from TWO doors — the hourly/webhook sync (categorize_new,
    under the sync advisory lock) and the nightly sweep (worker, under the
    nightly lock) — and those locks never contend with each other.
    run_match rebuilds amazon_matches with a DELETE-then-reinsert from a
    pre-transaction snapshot, and the summarizer samples an LLM, so two
    concurrent chains overwrite each other's matches with stale snapshots
    and write divergent summaries for the same items-group. This shared
    per-tenant lock closes that: the sync door skips when it's held
    (wait=False — every step is idempotent and pending-gated, the next pass
    redoes it), the nightly door waits (wait=True — it is the
    reconciliation pass and must not silently skip). Session-level, so the
    finally-unlock matters on pooled connections."""
    if wait:
        conn.execute(f"SELECT pg_advisory_lock({_AMAZON_LOCK})")
        got = True
    else:
        got = conn.execute(
            f"SELECT pg_try_advisory_lock({_AMAZON_LOCK}) AS ok"
        ).fetchone()["ok"]
    try:
        yield got
    finally:
        if got:
            conn.execute(f"SELECT pg_advisory_unlock({_AMAZON_LOCK})")


_MERCHANT_LOCK = \
    "hashtext('oikonome:llmclassify:' || current_setting('app.tenant_id'))"


@contextmanager
def merchant_classify_lock(conn, *, wait: bool = False):
    """Single-flight for the merchant LLM classification pass.

    The pass runs from THREE doors — the hourly/webhook sync and a file
    import's background thread (both via categorize_new) and the nightly
    sweep (run) — and the sync and nightly advisory locks never contend
    with each other. Each door independently SELECTs the same
    not-yet-classified merchants and POSTs them to the tenant's LLM
    backend, so an overlap bills the same batch twice (or doubles the
    local-compute cost on self-hosted Ollama). Same shape as
    amazon_chain_lock: the sync-side doors skip when it's held
    (wait=False — classification is idempotent and pending-gated, the
    next pass catches up), the nightly door waits (wait=True — it is the
    mop-up pass and must not silently skip). Session-level, so the
    finally-unlock matters on pooled connections."""
    if wait:
        conn.execute(f"SELECT pg_advisory_lock({_MERCHANT_LOCK})")
        got = True
    else:
        got = conn.execute(
            f"SELECT pg_try_advisory_lock({_MERCHANT_LOCK}) AS ok"
        ).fetchone()["ok"]
    try:
        yield got
    finally:
        if got:
            conn.execute(f"SELECT pg_advisory_unlock({_MERCHANT_LOCK})")


def run(conn, *, limit: int | None = None, batch_size: int = BATCH_SIZE,
        dry_run: bool = False, transport=None) -> dict:
    """Classify pending Amazon items AND unknown merchants (+ summaries +
    proposal tags). No-op when no backend is configured — but apply() of the
    cached merchant map (cheap SQL, flow-guarded) runs regardless, so
    migrated merchant_categories keep sharpening rows. Backend resolved
    ONCE here: tenant config wins over env (see _backend)."""
    backend = _backend(conn)
    stats = {"pending_items": 0, "items_classified": 0,
             "pending_merchants": 0, "merchants_classified": 0,
             "seeded": 0, "flow_classified": 0,
             "pending_summaries": 0, "summaries_written": 0,
             "pending_tags": 0, "tags_written": 0,
             "unparseable": 0, "rows_updated": 0, "own_transfers": 0,
             "configured": bool(backend["url"])}
    if not dry_run:
        flow_classify(conn, stats)              # bank mechanics → flow
        own_transfer_classify(conn, stats)      # own-institution → transfer
        mcc_classify(conn, stats)               # the merchant's own MCC next
        apply_seed(conn, stats)                 # deterministic rules next
        model_pass(conn, stats, limit=limit)    # local classifier next
    if backend["url"]:
        if amazon_pending(conn, limit=1):
            _classify_amazon(conn, limit=limit, batch_size=20,
                             dry_run=dry_run, stats=stats, transport=transport,
                             backend=backend)
        if pending_merchants(conn, limit=1):
            # Wait, don't skip: this is the mop-up pass. _classify_merchants
            # re-reads pending_merchants under the lock, so a sync that
            # classified everything while we waited leaves nothing to bill.
            with merchant_classify_lock(conn, wait=True):
                _classify_merchants(conn, limit=limit, batch_size=batch_size,
                                    dry_run=dry_run, stats=stats,
                                    transport=transport, backend=backend)
        if summaries_pending(conn, limit=1):
            _summarize_amazon(conn, limit=limit, batch_size=20,
                              dry_run=dry_run, stats=stats,
                              transport=transport, backend=backend)
        if proposals_pending_tags(conn, limit=1):
            _tag_proposals(conn, limit=limit, dry_run=dry_run, stats=stats,
                           transport=transport, backend=backend)
        if not dry_run:
            record_run(conn, stats, backend)
    if not dry_run:
        # the nightly run keeps the FULL unscoped sweep — it is the
        # reconciliation the per-sync scoped apply() deliberately leaves to it,
        # and nothing is watching a progress chip at this hour.
        stats["rows_updated"] = apply(conn)
    stats.pop("touched", None)                  # internal; not a stat
    return stats
