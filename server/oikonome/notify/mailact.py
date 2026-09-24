"""Act from the daily email — the "Needs you" section at its foot.

The verdict email used to open with the alert strip and end there: every
line that asked for a decision ("12 proposed recurring changes awaiting
review", an uncategorized charge, a warning worth silencing) sent the
reader to sign in and find the page. This module puts those lines at the
BOTTOM of the mail, after the verdict and the timeline, and gives each one
a button that does the thing: confirm or refuse a proposed bill, put a
category on an uncategorized charge, mute an alert. One tap, a small page
that says what it is about to do, one more tap, done. No sign-in.

Who gets the section: the addresses that hold an OWNER or MEMBER account
on the household — the two roles that may edit the household's money in
the app (see web/permissions.py). A viewer's copy and a guest's copy (an
address on the recipient list with no account) carry no section and no
alerts at all: the alerts are the household's to-do list, and a reader who
cannot act on it is only being nagged. The role check runs twice — when
the section is rendered, and again when a token is presented — so a member
who was removed after the mail went out finds their buttons dead. At the
second check the account must also be in good standing, exactly as an
in-app write requires (web.app.write_refusal): a household frozen or held
read-only after the mail went out, or a hosted account with no second
factor, gets a page that says so and nothing is written.

The token is the same shape as the unsubscribe link (notify/unsubscribe):
a signed statement, no table. It names the tenant, the address the mail
went to, ONE action on ONE object with the expected value, and an expiry.
Anyone holding it can do that one thing, which is the trade every emailed
action link makes; a forwarded verdict can confirm one bill and nothing
else, and only for a week. The GET only shows the page and a button; the
change is the POST — a mail scanner that prefetches every link in a
message must not approve a bill (the same peek-then-act split as
/unsubscribe and /recipient-invite).

A stale token — the proposal already decided, the charge already
categorized (by anyone, under any category) or split, or changed in any
way since the mail went out, the alert already gone or its mute changed
since — says so instead of overwriting. That also makes a used category
or mute button spent: using it is itself a change after the mail went
out, so undoing it in the app does not re-arm the link.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid

log = logging.getLogger("oikonome.mailact")

PATH = "/act"
TTL_DAYS = 7
MAX_PROPOSALS = 5
MAX_UNCATEGORIZED = 5
MAX_OPTIONS = 3
ACTOR_ROLES = ("owner", "member")

_EPHEMERAL = secrets.token_bytes(32)


def _key() -> bytes:
    mk = os.environ.get("OIKONOME_MASTER_KEY") or ""
    seed = mk.encode() if mk else _EPHEMERAL
    return hashlib.sha256(seed + b"|email-act").digest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- tokens ------------------------------------------------------------

def token(tenant_id, email: str, action: str, target: str, value: str = "",
          *, now: float | None = None) -> str:
    """`<payload>.<signature>` — payload is the JSON list
    [tenant, address, action, target, value, expires-epoch]."""
    exp = int((now if now is not None else time.time()) + TTL_DAYS * 86400)
    payload = json.dumps(
        [str(uuid.UUID(str(tenant_id))), _norm(email), action, target,
         value, exp], separators=(",", ":")).encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()[:20]
    return _b64(payload) + "." + _b64(sig)


def parse(tok: str, *, now: float | None = None) -> dict | None:
    """The statement a genuine, unexpired token makes, else None. An
    expired token parses to None too — `expired()` tells the two apart for
    the page's wording."""
    got = _parse_any(tok)
    if got is None or got["exp"] < (now if now is not None else time.time()):
        return None
    return got


def expired(tok: str, *, now: float | None = None) -> bool:
    got = _parse_any(tok)
    return bool(got) and got["exp"] < (now if now is not None else time.time())


def _parse_any(tok: str) -> dict | None:
    try:
        p64, s64 = (tok or "").split(".", 1)
        payload, sig = _unb64(p64), _unb64(s64)
    except (ValueError, TypeError):
        return None
    want = hmac.new(_key(), payload, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(sig, want):
        return None
    try:
        tid, email, action, target, value, exp = json.loads(payload)
        return {"tenant_id": str(uuid.UUID(str(tid))), "email": _norm(email),
                "action": str(action), "target": str(target),
                "value": str(value), "exp": int(exp)}
    except (ValueError, TypeError):
        return None


def url(base: str, tenant_id, email: str, action: str, target: str,
        value: str = "") -> str:
    return f"{base}{PATH}?token={token(tenant_id, email, action, target, value)}"


# ---- who may act -------------------------------------------------------

def actors(tenant_id: str) -> set[str]:
    """The addresses that may act from the mail: owner and member accounts
    on the tenant, lowercased. On a hosted instance only a VERIFIED account
    counts — the same proof of mailbox control the recipient list itself
    requires."""
    from ..db import tenancy
    from ..envnum import env_flag
    proof = " AND verified_at IS NOT NULL" if env_flag("OIKONOME_HOSTED") else ""
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute(
            "SELECT email FROM users WHERE tenant_id=%s AND role = ANY(%s)"
            + proof, (tenant_id, list(ACTOR_ROLES))).fetchall()
    finally:
        admin.close()
    return {_norm(r["email"]) for r in rows}


def _standing(tok: dict) -> dict | None:
    """None when the token's address may act right now, else the page's
    answer: `forbidden` when it no longer holds an owner/member account
    (verified, on hosted), `blocked` when it does but the account may not
    write — the household frozen or read-only, or a hosted account with no
    second factor. The standing gates are web.app's own, called rather
    than restated, so the mail cannot drift from the app on what a
    household in that state may do."""
    from ..db import tenancy
    from ..envnum import env_flag
    from ..web.app import write_refusal
    proof = (" AND u.verified_at IS NOT NULL"
             if env_flag("OIKONOME_HOSTED") else "")
    admin = tenancy.admin_connect()
    try:
        who = admin.execute(
            "SELECT u.id AS user_id, u.tenant_id, t.status AS tenant_status, "
            "(u.totp_secret IS NOT NULL) AS has_totp, "
            "u.second_factor_waived FROM users u "
            "JOIN tenants t ON t.id = u.tenant_id "
            "WHERE u.tenant_id=%s AND lower(btrim(u.email))=%s "
            "AND u.role = ANY(%s)" + proof + " LIMIT 1",
            (tok["tenant_id"], tok["email"], list(ACTOR_ROLES))).fetchone()
    finally:
        admin.close()
    if who is None:
        return {"state": "forbidden"}
    refused = write_refusal({**who, "tenant_id": str(who["tenant_id"])})
    if refused is not None:
        code, message = refused
        return {"state": "blocked", "code": code, "sentence": message}
    return None


# ---- what there is to act on -------------------------------------------

def _label(stored: str) -> str:
    # the SPA's catLabel: the stored key with its underscores opened up
    return (stored or "").replace("_", " ")


def _suggest(conn, row: dict, house_top: list[str]) -> list[tuple[str, str]]:
    """Up to MAX_OPTIONS (stored key, label) for an uncategorized row: what
    this merchant's other charges are filed under, then what the household
    files most of its spending under. A few likely answers, never the
    whole picker — the picker is in the app, behind "other…"."""
    from ..engine import categories as cat
    picks: list[str] = []

    def take(c):
        if c and c != "?" and c not in picks and c not in cat.FLOW:
            picks.append(c)
    mid = row.get("merchant_id")
    if mid:
        for r in conn.execute(
                "SELECT COALESCE(category_override, category_primary) AS c, "
                "COUNT(*) AS n FROM transactions WHERE removed=0 AND "
                "merchant_id=%s AND COALESCE(category_override, "
                "category_primary) IS NOT NULL GROUP BY 1 ORDER BY n DESC "
                "LIMIT %s", (mid, MAX_OPTIONS)).fetchall():
            take(r["c"])
    for c in house_top:
        if len(picks) >= MAX_OPTIONS:
            break
        take(c)
    for c in cat.PLAID_SPEND:
        if len(picks) >= MAX_OPTIONS:
            break
        take(c)
    return [(c, _label(c)) for c in picks[:MAX_OPTIONS]]


def gather(conn, d: dict) -> dict:
    """Everything the section can offer, computed ONCE per household per
    send; `render` then cuts a copy per recipient. `d` is the gathered
    verdict (report.gather) — the recent rows and the alerts come from it,
    so the section describes the same day the mail does."""
    from ..engine import bills
    from ..engine import categories as cat
    from ..engine.compat import as_dict
    from ..web.pages import _cadence_label, _proposal_summary
    out: dict = {"proposals": [], "more_proposals": 0,
                 "uncategorized": [], "alerts": list(d.get("alerts") or [])}
    try:
        pend = bills.pending_proposals(conn)
        for p in pend[:MAX_PROPOSALS]:
            ev = as_dict(p["evidence"]) or {}
            income = bool(ev.get("income"))
            if p["kind"] == "add":
                yes, no = (("Confirm income", "Not income") if income
                           else ("Confirm bill", "Not a bill"))
            else:
                yes, no = "Apply", "Keep as is"
            out["proposals"].append({
                "pid": p["id"], "kind": p["kind"], "payee": p["payee"],
                "amount": p["amount"], "income": income,
                "bill_payee": ev.get("bill_payee"),
                "cadence": _cadence_label(p["frequency"], p["interval"]),
                "summary": (bills.merchant_offer_summary(conn, p)
                            if p["kind"] == "merchant"
                            else _proposal_summary(p)),
                "yes": yes, "no": no})
        out["more_proposals"] = max(0, len(pend) - MAX_PROPOSALS)
    except Exception:                                      # noqa: BLE001
        log.exception("mailact: proposals unavailable")
    try:
        uncat = [r for r in d.get("yesterday_rows") or []
                 if (r.get("stored_category") or r.get("category") or "?")
                 == "?"]
        if uncat:
            house_top = [r["c"] for r in conn.execute(
                "SELECT COALESCE(category_override, category_primary) AS c, "
                "COUNT(*) AS n FROM transactions WHERE removed=0 AND "
                "date >= CURRENT_DATE - 90 AND COALESCE(category_override, "
                "category_primary) = ANY(%s) GROUP BY 1 ORDER BY n DESC "
                "LIMIT 6", (list(cat.PLAID_SPEND),)).fetchall()]
            for r in uncat[:MAX_UNCATEGORIZED]:
                out["uncategorized"].append({
                    "txn_id": r["txn_id"], "date": r["date"],
                    "payee": r.get("payee"), "amount": r["amount"],
                    "options": _suggest(conn, r, house_top)})
    except Exception:                                      # noqa: BLE001
        log.exception("mailact: uncategorized rows unavailable")
    return out


# ---- the section, per recipient ----------------------------------------

def render(acts: dict, tenant_id: str, email: str) -> tuple[str, str]:
    """`(plain, html)` of the "Needs you" section for ONE actor's copy of
    the mail, or ("", "") when there is nothing to act on. The caller has
    already decided this address may act (see `actors`).

    Buttons need a link, and a link needs a mailable base (report.
    mailable_base — a LAN URL in recurring mail gets it filtered). Without
    one the section still carries the alerts as text, the way the mail
    always did, and the lists that exist only to be tapped are left out."""
    from ..web import todayview
    from ..web.report import mailable_base
    base = mailable_base()
    who = _norm(email)
    props = acts.get("proposals") or []
    uncat = acts.get("uncategorized") or []
    alerts = acts.get("alerts") or []
    if not base:
        props, uncat = [], []
    if not (props or uncat or alerts):
        return "", ""

    def link(action, target, value=""):
        return url(base, tenant_id, who, action, target, value) if base else ""

    ctx = {
        "app_base": base,
        "proposals": [{**p, "yes_url": link("bill", p["pid"], "approve"),
                       "no_url": link("bill", p["pid"], "reject")}
                      for p in props],
        "more_proposals": acts.get("more_proposals") or 0,
        "uncategorized": [{**u, "options": [
            {"label": lab, "url": link("cat", u["txn_id"], key)}
            for key, lab in u["options"]]} for u in uncat],
        "alerts": [{**a, "mute_url": link("mute", a["kind"], a["message"])}
                   for a in alerts],
        "moneyc": todayview._moneyc,
    }
    html = todayview._inline(
        todayview._env.get_template("email_needs_you.html").render(**ctx))

    plain = ["", "Needs you" + (" — one tap each, no sign-in" if base else "")]
    if props:
        plain.append("Proposed bills:")
        for p in ctx["proposals"]:
            plain.append(f"  {p['payee']} · {todayview._moneyc(p['amount'])} "
                         f"· {p['cadence']}")
            plain.append(f"    {p['yes']}: {p['yes_url']}")
            plain.append(f"    {p['no']}: {p['no_url']}")
        if ctx["more_proposals"]:
            plain.append(f"  {ctx['more_proposals']} more on the Bills page: "
                         f"{base}/app/bills")
    if uncat:
        plain.append("Uncategorized:")
        for u in ctx["uncategorized"]:
            plain.append(f"  {todayview.fmt_date(u['date'])} · {u['payee']} · "
                         f"{todayview._moneyc(-u['amount'])}")
            for o in u["options"]:
                plain.append(f"    {o['label']}: {o['url']}")
    if alerts:
        plain.append("Alerts:")
        for a in ctx["alerts"]:
            pre = {"bad": "!! ", "warn": "!! ", "good": "++ "}.get(
                a["severity"], "-- ")
            plain.append("  " + pre + a["message"])
            if a["mute_url"]:
                plain.append(f"    mute: {a['mute_url']}")
    return "\n".join(plain), html


def attach(plain: str, html: str, extra_plain: str, extra_html: str
           ) -> tuple[str, str]:
    """The section goes INSIDE the mail's two-level wrapper (`…</div></div>`)
    so it inherits the mail's background — the same seam the unsubscribe
    footer uses, and it must be attached BEFORE that footer so the footer
    stays last."""
    if not (extra_plain or extra_html):
        return plain, html
    tail = "</div></div>"
    if html.rstrip().endswith(tail):
        cut = html.rstrip()
        html = cut[:-len(tail)] + extra_html + tail
    else:
        html = html + extra_html
    return plain.rstrip("\n") + "\n" + extra_plain + "\n", html


# ---- the page: describe (GET) and apply (POST) -------------------------

def _money(v) -> str:
    from ..web.todayview import _moneyc
    return _moneyc(abs(float(v or 0)))


def _issued(tok: dict):
    import datetime as dt
    return dt.datetime.fromtimestamp(tok["exp"] - TTL_DAYS * 86400,
                                     dt.timezone.utc)


def _cat_moot(conn, tok: dict, r: dict) -> dict | None:
    """Why a 'file under' button no longer applies, or None. The button
    was minted for an UNCATEGORIZED charge, so anything that has touched
    the charge since the mail went out makes it stale: a category on it
    (whoever set it, whatever it is), a split into parts, or any logged
    category or split change — the last also spends a used button, since
    filing from the mail is itself such a change, so clearing the charge
    afterwards does not re-arm the link. Comparing only against the
    button's own value would let a week-old mailed guess silently replace
    a later, deliberate decision."""
    v = tok["value"]
    if conn.execute("SELECT 1 FROM transaction_splits WHERE txn_id=%s "
                    "LIMIT 1", (tok["target"],)).fetchone():
        return {"state": "already",
                "sentence": f"{r['payee']} was split since the email went "
                            "out, and its parts carry their own categories. "
                            "Open the charge in the app to change them."}
    now = r["category_override"] or r["category_primary"]
    if now and now != "?":
        if now == v:
            return {"state": "already",
                    "sentence": f"{r['payee']} is already filed under "
                                f"{_label(v)}."}
        return {"state": "already",
                "sentence": f"{r['payee']} is now filed under {_label(now)} "
                            "— it was filed after the email went out. Open "
                            "the charge in the app to change it."}
    if conn.execute(
            "SELECT 1 FROM activity_log WHERE target=%s AND kind IN "
            "('category','split') AND at >= %s LIMIT 1",
            (tok["target"], _issued(tok))).fetchone():
        return {"state": "already",
                "sentence": f"{r['payee']} was changed after the email went "
                            "out. Open the charge in the app to file it."}
    return None


def _mute_moot(tok: dict, r: dict | None) -> dict | None:
    """Why a mute button no longer applies, or None. Beyond the alert
    being gone or already muted: its mute changed after the mail went out
    (a trigger on alerts_log stamps every flip). Muting from the mail is itself such
    a change, so a used button is spent — restoring the alert in the app
    is a decision to see it again, and replaying the week-old link must
    not hide it a second time."""
    if not r or not r["active"]:
        return {"state": "already",
                "sentence": "That alert has already cleared."}
    if r["dismissed"]:
        return {"state": "already",
                "sentence": "That alert is already muted."}
    if r["mute_changed_at"] and r["mute_changed_at"] >= _issued(tok):
        return {"state": "already",
                "sentence": "That alert's mute was changed after the email "
                            "went out, so this button is spent. Mute it from "
                            "the app if you still want it hidden."}
    return None


def describe(tok: dict) -> dict:
    """What the button on the page will do, in one sentence, read from the
    live data — or the state that makes it moot (`already`, `forbidden`,
    `unknown`, `blocked`). Writes nothing."""
    from ..db import tenancy
    moot = _standing(tok)
    if moot is not None:
        return moot
    try:
        conn = tenancy.tenant_connect(tok["tenant_id"])
    except Exception:                                      # noqa: BLE001
        log.exception("mailact: could not open tenant %s", tok["tenant_id"])
        return {"state": "failed"}
    try:
        a, t, v = tok["action"], tok["target"], tok["value"]
        if a == "bill":
            p = conn.execute("SELECT payee, amount, frequency, interval, "
                             "kind, status, evidence FROM bill_proposals "
                             "WHERE id=%s", (t,)).fetchone()
            if not p:
                return {"state": "unknown"}
            if p["status"] != "pending":
                return {"state": "already",
                        "sentence": f"{p['payee']} was already decided "
                                    f"({p['status']})."}
            from ..engine.compat import as_dict
            from ..web.pages import _cadence_label
            income = bool((as_dict(p["evidence"]) or {}).get("income"))
            what = ("income" if income else "bill") if p["kind"] == "add" \
                else "recurring change"
            cad = _cadence_label(p["frequency"], p["interval"])
            if v == "approve":
                return {"state": "confirm", "button": "Confirm",
                        "sentence": f"Track {p['payee']} {_money(p['amount'])} "
                                    f"as a {cad} {what}?"}
            return {"state": "confirm", "button": "Not a bill"
                    if what == "bill" else "Keep as is",
                    "sentence": f"Drop the proposed {cad} {what} "
                                f"{p['payee']} {_money(p['amount'])}?"}
        if a == "cat":
            r = conn.execute(
                "SELECT COALESCE(merchant_name, name) AS payee, amount, "
                "category_override, category_primary FROM transactions "
                "WHERE id=%s AND removed=0", (t,)).fetchone()
            if not r:
                return {"state": "unknown"}
            moot = _cat_moot(conn, tok, r)
            if moot is not None:
                return moot
            return {"state": "confirm", "button": f"File under {_label(v)}",
                    "sentence": f"File {r['payee']} {_money(r['amount'])} "
                                f"under {_label(v)}? This charge only — no "
                                "rule is written for the merchant."}
        if a == "mute":
            r = conn.execute(
                "SELECT dismissed, active, mute_changed_at FROM alerts_log "
                "WHERE kind=%s AND message=%s ORDER BY last_seen DESC "
                "LIMIT 1", (t, v)).fetchone()
            moot = _mute_moot(tok, r)
            if moot is not None:
                return moot
            return {"state": "confirm", "button": "Mute this alert",
                    "sentence": f"Mute “{v}”? It stays out of the email and "
                                "the Today page while the condition lasts; "
                                "the alert log keeps it."}
        return {"state": "unknown"}
    finally:
        conn.close()


def apply(tok: dict) -> dict:
    """Do the one thing the token names. Returns {state, sentence}: `done`,
    `already` (nothing left to do — never overwrites a later decision),
    `forbidden` (the address no longer holds an owner/member account),
    `blocked` (it does, but may not write right now — with the HTTP `code`
    the app would answer), `unknown`, or `failed`."""
    from ..db import tenancy
    from ..engine import budget
    moot = _standing(tok)
    if moot is not None:
        return moot
    try:
        conn = tenancy.tenant_connect(tok["tenant_id"])
    except Exception:                                      # noqa: BLE001
        log.exception("mailact: could not open tenant %s", tok["tenant_id"])
        return {"state": "failed"}
    try:
        # a demo tenant sends no mail, so no button was ever minted for
        # one; a token that claims otherwise is refused, not honoured
        if budget.load_config(conn).get("demo_mode"):
            return {"state": "forbidden"}
        a, t, v = tok["action"], tok["target"], tok["value"]
        if a == "bill":
            if v not in ("approve", "reject"):
                return {"state": "unknown"}
            from ..engine import bills
            r = bills.apply_proposal(conn, t, v)
            if r.get("error"):
                if "already-decided" in r["error"]:
                    return {"state": "already",
                            "sentence": "That proposal was already decided."}
                return {"state": "failed", "sentence": r["error"]}
            who = r.get("approved") or r.get("rejected") or t
            # the log names the address the button was mailed to: that is
            # who pressed it
            from ..web.api import _log_proposal
            _log_proposal(conn, tok["email"], t, v, r)
            if v == "approve":
                try:
                    from ..engine import alerts
                    if not bills.pending_proposals(conn):
                        alerts.retire(conn, "proposals")
                except Exception:                          # noqa: BLE001
                    pass
                return {"state": "done",
                        "sentence": f"{who} is now tracked. It shows on the "
                                    "Bills page and in tomorrow's email."}
            return {"state": "done",
                    "sentence": f"{who} will not be proposed again."}
        if a == "cat":
            from ..web import data
            from ..engine import activity as _activity
            # the check and the write hold the row: two copies of the mail
            # tapped at once must not both see it uncategorized
            with conn.transaction():
                r = conn.execute(
                    "SELECT COALESCE(merchant_name, name) AS payee, "
                    "category_override, category_primary FROM transactions "
                    "WHERE id=%s AND removed=0 FOR UPDATE", (t,)).fetchone()
                if not r:
                    return {"state": "unknown"}
                moot = _cat_moot(conn, tok, r)
                if moot is not None:
                    return moot
                data.set_category(conn, t, v, scope="one")
                _activity.record(
                    conn, tok["email"], "category", "set", target=t,
                    label=_activity.txn_label(conn, t),
                    detail={"before": r["category_override"], "after": v,
                            "scope": "one", "via": "email"})
            return {"state": "done",
                    "sentence": f"{r['payee']} is filed under {_label(v)}. "
                                "To teach the merchant, open the charge in "
                                "the app."}
        if a == "mute":
            from ..engine import alerts
            # dismiss() reports rows MATCHED, not rows changed — a second
            # tap on the same button would otherwise say "muted" again
            r = conn.execute(
                "SELECT dismissed, active, mute_changed_at FROM alerts_log "
                "WHERE kind=%s AND message=%s ORDER BY last_seen DESC "
                "LIMIT 1", (t, v)).fetchone()
            moot = _mute_moot(tok, r)
            if moot is not None:
                return moot
            alerts.dismiss(conn, t, v)
            return {"state": "done",
                    "sentence": "Muted. It stays out of the email and the "
                                "Today page while the condition lasts."}
        return {"state": "unknown"}
    except Exception:                                      # noqa: BLE001
        log.exception("mailact: %s failed for tenant %s", tok["action"],
                      tok["tenant_id"])
        return {"state": "failed"}
    finally:
        conn.close()
