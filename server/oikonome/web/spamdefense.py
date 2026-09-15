"""Spam defense for public email intake (signup, and any intake form an add-on adds).

Design:

- **Inline, instant:** every submission's email domain is checked against the
  merged public blocklist (disposable-email-domains + StopForumSpam toxic) and
  the operator's own blocks — a local, indexed lookup, no network. Obvious bots
  die here and get the same generic success as everyone (no enumeration).
- **Budgeted live check:** a domain not caught locally is scored via a
  StopForumSpam email lookup (+ a cheap DNS null-route check), under a tight
  timeout, cached per domain. Flagged → auto-blocked. Timeout / API down →
  *fail open* (allow) so a third-party outage never blocks a real signup.
- **Major providers are never domain-blocked** — gmail/outlook/proton/… hit an
  address-level block instead, so one bad request can't wall off a shared domain.

All state is control-plane (no tenant); every DB touch is on an admin
connection. Enforcement is on for hosted, off for self-host unless opted in.
"""
from __future__ import annotations

import json
import logging
import os
from ..envnum import env_flag

import httpx

from ..db import tenancy

log = logging.getLogger("oikonome.spam")

# ---- config --------------------------------------------------------------

DISPOSABLE_URL = ("https://raw.githubusercontent.com/disposable-email-domains/"
                  "disposable-email-domains/main/disposable_email_blocklist.conf")
SFS_TOXIC_URL = "https://www.stopforumspam.com/downloads/toxic_domains_whole.txt"
SFS_API_URL = "https://api.stopforumspam.org/api"

# Never domain-blocked; a spam decline on one of these blocks the address only.
_SAFELIST = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.net", "fastmail.com", "hey.com", "zoho.com", "yandex.com",
    "mail.com", "hushmail.com", "tutanota.com", "tuta.io",
})


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    # empty == unset: compose materializes every declared key, so an
    # unconfigured flag arrives as "" and must NOT read as "off"
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def enforced() -> bool:
    """On for hosted; off for self-host unless OIKONOME_SPAM_DEFENSE is set."""
    return _flag("OIKONOME_SPAM_DEFENSE", env_flag("OIKONOME_HOSTED"))


def _live_enabled() -> bool:
    """The live signals (DNS null-route + StopForumSpam lookup). On for hosted;
    OFF under DEV_MODE unless OIKONOME_SPAM_API is explicitly set, so the test
    suite stays hermetic (no network) while the list path still works. Set
    OIKONOME_SPAM_API=off to disable even in prod."""
    v = os.environ.get("OIKONOME_SPAM_API")
    if v is not None and v.strip():          # empty == unset (see _flag)
        return v.strip().lower() not in ("off", "0", "false", "no")
    return os.environ.get("OIKONOME_DEV") != "1"


def _budget_ms() -> int:
    try:
        return max(50, int(os.environ.get("OIKONOME_SPAM_API_BUDGET_MS")
                           or "400"))
    except ValueError:
        return 400


def _cache_ttl_days() -> int:
    try:
        return max(1, int(os.environ.get("OIKONOME_SPAM_CACHE_TTL_DAYS")
                          or "7"))
    except ValueError:
        return 7


def safelist() -> frozenset[str]:
    extra = os.environ.get("OIKONOME_SPAM_SAFELIST_EXTRA", "")
    if not extra.strip():
        return _SAFELIST
    more = {d.strip().lower() for d in extra.split(",") if d.strip()}
    return _SAFELIST | more


# ---- helpers -------------------------------------------------------------

def domain_of(email: str) -> str | None:
    """Normalize an email to its lowercase, punycode domain. None if malformed."""
    if not email or "@" not in email:
        return None
    dom = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not dom or "." not in dom:
        return None
    try:
        dom = dom.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return dom


def _admin_or(admin):
    """Return (admin_conn, opened_here). Callers close only what they opened."""
    if admin is not None:
        return admin, False
    return tenancy.admin_connect(), True


# ---- live signals (monkeypatched out in tests) ---------------------------

def _sfs_lookup(email: str, ip: str | None, timeout_s: float) -> dict | None:
    """StopForumSpam email lookup (aggregates domain reputation). Returns the
    parsed 'email' block, or None on any error/timeout (→ fail open). Callers
    pass a SYNTHETIC probe address, never the real user's email."""
    params = {"email": email, "json": ""}
    if ip:
        params["ip"] = ip
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.get(SFS_API_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("SFS lookup failed (fail-open): %s", type(e).__name__)
        return None
    if not data.get("success"):
        return None
    return data.get("email") or {}


def _live_verdict(domain: str, ip: str | None) -> tuple[str, dict]:
    """Compute a live verdict for a domain not on any list, via a StopForumSpam
    lookup on a SYNTHETIC probe address (SFS returns domain-level reputation
    for any local-part, so we never send the real user's email to a third
    party). Returns (verdict, score) where verdict is 'spam'|'clean'|'unknown';
    'unknown' (timeout / API off) means fail open — do not cache. The whole
    call is bounded by OIKONOME_SPAM_API_BUDGET_MS; there is no un-timeout'd
    DNS resolve on the request path."""
    score: dict = {}
    res = _sfs_lookup(f"probe@{domain}", ip, _budget_ms() / 1000.0)
    if res is None:
        return "unknown", score
    score = {"blacklisted": res.get("blacklisted"),
             "confidence": res.get("confidence"),
             "frequency": res.get("frequency")}
    spam = bool(res.get("blacklisted")) or (res.get("confidence") or 0) >= 50
    return ("spam" if spam else "clean"), score


# ---- DB-backed checks ----------------------------------------------------

def _address_blocked(admin, email: str) -> bool:
    return admin.execute("SELECT 1 FROM spam_addresses_blocked WHERE email=%s",
                         (email,)).fetchone() is not None


def _in_blocked(admin, domain: str) -> bool:
    """The operator's own block list (manual / decline / auto)."""
    return admin.execute("SELECT 1 FROM spam_domains_blocked WHERE domain=%s",
                         (domain,)).fetchone() is not None


def _in_public(admin, domain: str) -> bool:
    """The merged upstream public feed."""
    return admin.execute("SELECT 1 FROM spam_domains_public WHERE domain=%s",
                         (domain,)).fetchone() is not None


def _cached_verdict(admin, domain: str) -> str | None:
    row = admin.execute(
        "SELECT verdict FROM spam_reputation_cache WHERE domain=%s "
        "AND checked_at > now() - make_interval(days => %s)",
        (domain, _cache_ttl_days())).fetchone()
    return row["verdict"] if row else None


def _cache_put(admin, domain: str, verdict: str, score: dict) -> None:
    admin.execute(
        "INSERT INTO spam_reputation_cache (domain, verdict, score, source, "
        "checked_at) VALUES (%s,%s,%s,'sfs',now()) "
        "ON CONFLICT (domain) DO UPDATE SET verdict=EXCLUDED.verdict, "
        "score=EXCLUDED.score, checked_at=now()",
        (domain, verdict, json.dumps(score)))


def record_drop(admin, surface: str, domain: str | None, reason: str) -> None:
    admin.execute(
        "INSERT INTO spam_drops (domain, surface, reason) VALUES (%s,%s,%s)",
        (domain, surface, reason))


def auto_block(admin, domain: str, email: str, reason: str,
               source: str = "auto", added_by: str = "system") -> None:
    """Block the whole domain unless it's a major provider — those get an
    address-level block so gmail/outlook/… are never walled off."""
    if domain in safelist():
        admin.execute(
            "INSERT INTO spam_addresses_blocked (email, reason, source, "
            "added_by) VALUES (%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            (email, reason, source, added_by))
    else:
        admin.execute(
            "INSERT INTO spam_domains_blocked (domain, reason, source, "
            "added_by) VALUES (%s,%s,%s,%s) ON CONFLICT (domain) DO NOTHING",
            (domain, reason, source, added_by))


def recipient_blocked(email: str, admin=None) -> bool:
    """True if a transactional email to this address would be wasted — the
    address is blocked, or its domain is on our block/public list. LIST-ONLY
    (no live API / DNS) so it adds no latency or dependency to the send path;
    the live check already ran at intake. Never raises → False (send) on any
    error, so a bug can't silently swallow real onboarding mail. Major
    providers are never treated as blocked here."""
    if not enforced():
        return False
    domain = domain_of(email)
    if domain is None:
        return False
    conn, opened = _admin_or(admin)
    try:
        if _address_blocked(conn, email):
            return True
        if domain in safelist():
            return False
        return _in_blocked(conn, domain) or _in_public(conn, domain)
    except Exception:                                   # noqa: BLE001
        log.exception("recipient_blocked check failed (send anyway)")
        return False
    finally:
        if opened:
            conn.close()


def check(surface: str, email: str, ip: str | None = None,
          admin=None) -> dict:
    """The intake gate. Returns {"blocked": bool, "reason": str|None}.

    Blocked submissions are logged to spam_drops and, when the live check
    flags a fresh domain, auto-blocked. Never raises — on any internal error
    it fails open (blocked=False) so real users are never lost to a bug."""
    if not enforced():
        return {"blocked": False, "reason": None}
    domain = domain_of(email)
    if domain is None:
        return {"blocked": False, "reason": None}   # normal validation handles it
    conn, opened = _admin_or(admin)
    try:
        if _address_blocked(conn, email):
            record_drop(conn, surface, domain, "address")
            return {"blocked": True, "reason": "address"}
        # An explicit operator/decline/auto block is honored even for a major
        # provider — if the operator blocked a domain, they meant it (this is
        # before the safelist gate so a manual block is never shadowed by it).
        if _in_blocked(conn, domain):
            record_drop(conn, surface, domain, "blocked")
            return {"blocked": True, "reason": "blocked"}
        if domain in safelist():
            return {"blocked": False, "reason": None}
        if _in_public(conn, domain):
            # surface it in the operator's own blocked-domains view too
            auto_block(conn, domain, email, "matched public blocklist")
            record_drop(conn, surface, domain, "public_list")
            return {"blocked": True, "reason": "public_list"}
        verdict = _cached_verdict(conn, domain)
        if verdict is None and _live_enabled():
            verdict, score = _live_verdict(domain, ip)
            if verdict in ("spam", "clean"):
                _cache_put(conn, domain, verdict, score)
        if verdict == "spam":
            auto_block(conn, domain, email, "live check")
            record_drop(conn, surface, domain, "live")
            return {"blocked": True, "reason": "live"}
        return {"blocked": False, "reason": None}
    except Exception:                                   # noqa: BLE001
        log.exception("spam check failed (fail-open) for surface=%s", surface)
        return {"blocked": False, "reason": None}
    finally:
        if opened:
            conn.close()


# ---- console operations --------------------------------------------------

def manual_block(admin, value: str, added_by: str, source: str = "manual",
                 reason: str | None = None) -> str:
    """Block a domain or a full email address (auto-detected). Returns which."""
    value = value.strip().lower().rstrip(".")
    if "@" in value:
        admin.execute(
            "INSERT INTO spam_addresses_blocked (email, reason, source, "
            "added_by) VALUES (%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            (value, reason, source, added_by))
        return "address"
    admin.execute(
        "INSERT INTO spam_domains_blocked (domain, reason, source, added_by) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (domain) DO NOTHING",
        (value, reason, source, added_by))
    return "domain"


def unblock(admin, value: str) -> None:
    value = value.strip().lower().rstrip(".")
    admin.execute("DELETE FROM spam_domains_blocked WHERE domain=%s", (value,))
    admin.execute("DELETE FROM spam_addresses_blocked WHERE email=%s", (value,))


def stats(admin=None) -> dict:
    conn, opened = _admin_or(admin)
    try:
        meta = conn.execute(
            "SELECT public_count, last_fetched_at FROM blocklist_meta "
            "WHERE id").fetchone() or {}
        blocked = conn.execute(
            "SELECT count(*) AS n FROM spam_domains_blocked").fetchone()["n"]
        addrs = conn.execute(
            "SELECT count(*) AS n FROM spam_addresses_blocked").fetchone()["n"]
        drops = conn.execute(
            "SELECT count(*) AS n FROM spam_drops").fetchone()["n"]
        recent = conn.execute(
            "SELECT domain, source, reason, added_at FROM spam_domains_blocked "
            "ORDER BY added_at DESC LIMIT 50").fetchall()
        return {"public_count": meta.get("public_count", 0),
                "last_fetched_at": meta.get("last_fetched_at"),
                "blocked_domains": blocked, "blocked_addresses": addrs,
                "drops": drops, "recent_blocks": recent,
                "enforced": enforced()}
    finally:
        if opened:
            conn.close()


# ---- public-list refresh (daily job + manual button) ---------------------

def _fetch_list(url: str) -> list[str]:
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        out = []
        for line in r.text.splitlines():
            d = line.strip().lower().rstrip(".")
            if d and not d.startswith("#") and "." in d:
                out.append(d)
        return out


REFRESH_LOCK_KEY = "oikonome:blocklist-refresh"


def refresh_lists(admin=None) -> dict:
    """Fetch both public feeds, merge/dedupe, drop safelisted domains, and
    bulk-replace spam_domains_public in one transaction. A failed fetch keeps
    the previous list (never wipe on error)."""
    conn, opened = _admin_or(admin)
    locked = False
    try:
        # Two doors reach this rebuild — the daily cron and the admin
        # console's run-job button — and two concurrent DELETE-then-INSERT
        # passes over spam_domains_public would duplicate-key each other
        # and roll one back after it already fetched both feeds. Same
        # skip-not-wait TRY lock the other dual-door jobs use; the session
        # lock is released in the finally (it outlives the transaction).
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (REFRESH_LOCK_KEY,)).fetchone()["ok"]
        if not locked:
            log.info("blocklist refresh already running — skipping")
            return {"ok": False, "skipped": "already-running",
                    "errors": {}, "count": None}
        sources = {"disposable": DISPOSABLE_URL, "sfs_toxic": SFS_TOXIC_URL}
        merged: dict[str, set[str]] = {}
        got_any = False
        errors = {}
        for name, url in sources.items():
            try:
                for d in _fetch_list(url):
                    merged.setdefault(d, set()).add(name)
                got_any = True
            except (httpx.HTTPError, ValueError) as e:   # keep old on failure
                errors[name] = type(e).__name__
                log.warning("blocklist source %s failed: %s", name, e)
        if not got_any:
            return {"ok": False, "errors": errors, "count": None}
        safe = safelist()
        rows = [(d, sorted(s)) for d, s in merged.items() if d not in safe]
        with conn.transaction():
            # DELETE (not TRUNCATE) so concurrent check() reads aren't blocked
            # on an ACCESS EXCLUSIVE lock during the rebuild — MVCC serves the
            # old rows until this commits.
            conn.execute("DELETE FROM spam_domains_public")
            conn.cursor().executemany(
                "INSERT INTO spam_domains_public (domain, sources) "
                "VALUES (%s, %s)", rows)
            conn.execute(
                "UPDATE blocklist_meta SET last_fetched_at=now(), "
                "public_count=%s, source_state=%s WHERE id",
                (len(rows), json.dumps({"errors": errors})))
            # retention: keep the drop log bounded
            conn.execute("DELETE FROM spam_drops WHERE created_at < "
                         "now() - interval '90 days'")
        return {"ok": True, "count": len(rows), "errors": errors}
    finally:
        if locked:
            try:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                             (REFRESH_LOCK_KEY,))
            except Exception:                               # noqa: BLE001
                pass                    # connection already dead; lock went with it
        if opened:
            conn.close()
