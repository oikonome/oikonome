"""Settings, recipients, outbound resume and the SMTP test.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import os
import re
from fastapi import Depends, HTTPException

from . import demoguard, mailguard, permissions, settings_exposure
from .. import ext
from ..engine import activity as _activity
from ..envnum import env_flag
from .api_common import (_BODY_ROWS_MAX, _conn, router, _scalar_text,
                         _setup_mode_of, _today, _user)
from .fields import (Fields, iso_date as _iso_date, json_fields,
                     json_fields_or_empty as _json_fields_or_empty,
                     MONEY_MAX as _MONEY_MAX, typed_money as _typed_money)
from .schemas import documented
from .schemas.settings import (OutboundResume, OutboundResumed,
                               RecipientResend, RecipientResent,
                               SettingsSave, SettingsView, SmtpTested)
from .security import limit


# The self-host fat-finger guard on plaid_enrich_cap: an extra zero must
# not authorise a spend nobody meant to approve, but a self-hoster is
# spending against THEIR OWN Plaid account, so the number is theirs.
ENRICH_CAP_SELFHOST_MAX = 100000


def _enrich_cap_ceiling() -> int:
    """Highest monthly Plaid Enrich row cap a TENANT may write here.

    Enrich is billed per row to whoever owns the Plaid credentials. On hosted
    that is the operator — `_no_byo_on_hosted` makes sure a tenant cannot
    bring its own keys — so the cap is not a preference there, it is the
    operator's spend ceiling, and a tenant that can raise its own ceiling has
    no ceiling. Hosted tenants may lower it (0 = off) and never raise it past
    the ceiling the installed gate reports; self-host keeps the full
    range. The installed gate may lift a whole deployment without a code
    change — except that 0 here means OFF, never "uncapped": the whole
    point of this number is that money cannot be spent without a ceiling."""
    if not env_flag("OIKONOME_HOSTED"):
        return ENRICH_CAP_SELFHOST_MAX
    cap = ext.gate.enrich_rows_cap()
    return ENRICH_CAP_SELFHOST_MAX if cap is None else cap


def _no_smtp_byo_on_hosted() -> None:
    """In hosted mode the operator's mail server sends the mail, so a tenant
    does not point the app at its own relay. The reason is not tidiness: a
    tenant-set relay WINS over the operator's env in resolve_smtp, so the
    daily verdict — the product — would leave through a server the operator
    does not run, cannot see, and cannot debug when it silently stops. Blocked
    server-side and not merely hidden, for the same reason as above.
    Self-host configures its own mail by design and is unaffected."""
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "in hosted mode the operator's mail server sends your email "
                 "— there is no mail server to configure here")


def _relay_default_from(relay_host: str) -> str | None:
    """The From for a relay saved with neither a username nor a From:
    no-reply at the host name in OIKONOME_BASE_URL, else at the relay's
    own host name. None when neither is a name (an IP address is not a
    mail domain)."""
    import ipaddress
    from urllib.parse import urlsplit
    for host in ((urlsplit(os.environ.get("OIKONOME_BASE_URL", "")).hostname
                  or ""), relay_host or ""):
        host = host.strip().strip("[]").lower()
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        addr = f"no-reply@{host}"
        if mailguard.valid_address(addr):
            return addr
    return None


# ---- settings (SPA Settings page) -------------------------------------------

_SETTINGS_FIELDS = ("food_monthly", "other_monthly", "custom_buckets",
                    "budgeted_income_monthly", "dynamic_variable_budget",
                    "email_recipients", "email_muted",
                    "email_send_hour_utc",
                    "checking_account_id",
                    # retirement / income-scenario keys
                    "birthdate", "ss_estimates", "retirement_saving",
                    "income_biweekly_saving", "income_biweekly_not_saving",
                    # read-only in the SPA: the Accounts 'excl' toggle + the
                    # recurring tweaks — each is WRITTEN by
                    # its own feature, only READ here. manual_assets is
                    # writable (Net Worth page editor).
                    "excluded_accounts", "occurrence_caps", "disabled_bills",
                    "dismissed_hints", "manual_assets",
                    # smart-categorization LLM backend (the same keys as the setup
                    # wizard). llm_api_key and llm_extra_body are
                    # deliberately NOT here: both are credentials, so they are
                    # write-only — GET reports only llm_api_key_set /
                    # llm_extra_body_set.
                    "llm_url", "llm_model",
                    "llm_vision_model", "taxdocs_allow_remote_llm",
                    # provider key presence for the Connections
                    # card (secrets stay write-only)
                    "plaid_client_id", "plaid_env",
                    "mx_client_id", "mx_env",
                    # the owner's UI theme: auto/light/dark (absent = the
                    # dark default)
                    "theme",
                    # default landing page per client (absent = Today):
                    # a web route / a mobile tab name
                    "home_web", "home_mobile",
                    # the household's IANA zone — every scheduled hour is
                    # kept in it (absent = the instance's OIKONOME_TZ)
                    "timezone",
                    # Today hero face (absent = simple): "advanced" shows
                    # the full tile/goal anatomy inline. The toggle lives
                    # on the page and governs the page alone — the daily
                    # email's face is the separate email_schedule.daily
                    # .summary setting (decoupled)
                    "today_view",
                    # SMTP delivery via the web UI (password is
                    # write-only — GET reports only smtp_password_set)
                    "smtp_host", "smtp_port", "smtp_user", "smtp_from",
                    "smtp_starttls",
                    # feedback pipeline on/off (absent = on)
                    "feedback_enabled",
                    # merchant logos on the ledger / merchants / spending /
                    # email (absent = on; False = the minimal look)
                    "merchant_logos",
                    # Plaid's recurring streams as a bill-detection
                    # cross-check (absent = on; False = off)
                    "plaid_recurring",
                    # rows/month ceiling on Plaid Enrich (absent = the
                    # connector's default). Enrich is billed per row, so the
                    # ceiling is the whole cost control.
                    "plaid_enrich_cap",
                    # (merchant_strip_cities is deliberately absent: no
                    # client renders it, so it is an operator knob set in
                    # the stored tenant settings — neither shown here nor
                    # taken by the save. merchant_dedup.cities_for still
                    # reads a stored value.)
                    # {daily,monthly,yearly} local-time schedule
                    "email_schedule",
                    # the Net Worth page's investment fee estimate and
                    # money-in anchors (Settings → Connections → Investment
                    # fees & basis): {institution fragment: yearly fraction},
                    # [account masks that pay no advisory fee], {account
                    # mask: dollars put in}
                    "advisory_fees", "advisory_fee_exempt_masks",
                    "investment_basis_override",
                    # how far back the ledger reaches (1/3/6/12 months;
                    # absent = everything a source has): shortens a new bank
                    # link's backfill and trims file imports
                    "history_months",
                    # how the household set up its bills: "auto" (the
                    # finder proposes) or "manual" (typed in by hand), and
                    # whether the nightly finder proposes new bills and
                    # income at all (amount drift is not affected)
                    "setup_mode", "bill_detection_auto")


# resolved-address cache for the locality verdict below: a settings GET
# classifies up to half a dozen endpoint URLs (bundled + backends + one per
# role), usually sharing one or two hosts, and getaddrinfo on a dead name
# can block for seconds. Short TTL — a name that starts resolving publicly
# must flip the verdict promptly.
_LLM_DNS_TTL = 120.0
_LLM_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}


def _llm_is_local(url: str) -> bool:
    """Whether an LLM endpoint stays on this machine/network: loopback,
    RFC-1918, or a compose-network service. Anything else is a cloud
    endpoint the data leaves the instance to reach.

    Judged by ADDRESS, not by what the name looks like — the verdict tells
    the user their data stays home, and clients surface it beside consent
    decisions, so "ai.internal" or a dotless vanity host that resolves to
    the public internet must not read as local. An IP literal is classified
    directly and a hostname is resolved; every resolved address must be
    non-globally-routable, per netguard's shared address policy (which also
    folds IPv4-mapped IPv6 spellings to their embedded v4 form). The name
    heuristic (localhost / .local / dotless service name) applies
    only to names that do not resolve at all — an mDNS or compose name is
    often invisible from this process, and an unresolvable endpoint cannot
    send data anywhere; the moment it resolves publicly the verdict flips.
    """
    from urllib.parse import urlsplit
    import ipaddress
    import time
    from . import netguard
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if not host:
        return False
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        now = time.monotonic()
        hit = _LLM_DNS_CACHE.get(host)
        if hit is not None and now - hit[0] < _LLM_DNS_TTL:
            ips = hit[1]
        else:
            ips = netguard._resolved_ips(host)
            if len(_LLM_DNS_CACHE) >= 256:
                _LLM_DNS_CACHE.clear()
            _LLM_DNS_CACHE[host] = (now, ips)
        if not ips:
            return (host == "localhost" or host.endswith(".local")
                    or "." not in host)
    for ip in ips:
        try:
            # netguard's hosted policy raises exactly when an address is
            # NOT globally routable (after IPv4-mapped folding) — reused
            # here as the classifier so the two never drift.
            netguard._check_ip(ip, what="llm endpoint", hosted=True)
        except netguard.BlockedURL:
            continue                  # non-global: stays on this network
        return False                  # any public address means off-box
    return True


def _keep_masked_llm_url(sent: str, cfg: dict) -> str:
    """A saved LLM endpoint URL with any masked credential restored from
    the stored config (GET masks them — see _settings_view), or a 400 when
    the mask no longer matches anything stored."""
    from ..engine.llm_categorize import unredact_url
    stored = [str(cfg.get("llm_url") or "")] + [
        str(b.get("url") or "") for b in (cfg.get("llm_backends") or [])
        if isinstance(b, dict)]
    try:
        return unredact_url(sent, stored)
    except ValueError as e:
        raise HTTPException(400, str(e))


# Removed members' addresses, kept only as digests so the
# tenant config never goes on holding a departed person's address in the
# clear. Capped: it only has to outlive the settings pages that were open
# when the removal happened.
_REMOVED_KEY = "email_removed_members"
_REMOVED_CAP = 50


def _address_digest(email: str) -> str:
    import hashlib
    return hashlib.sha256(
        str(email or "").strip().lower().encode()).hexdigest()


def remember_removed_address(cfg: dict, email: str) -> None:
    """Note (by digest) that this address left the household, inside the
    caller's config transaction, so a stale settings page cannot quietly
    put it back on the mail list."""
    if not str(email or "").strip():
        return
    d = _address_digest(email)
    seen = [x for x in (cfg.get(_REMOVED_KEY) or []) if x != d]
    cfg[_REMOVED_KEY] = (seen + [d])[-_REMOVED_CAP:]


def _drop_stale_readds(conn, cfg: dict, prev: set, recips: list,
                       added) -> list:
    """Every client saves the WHOLE recipient list from a copy it loaded
    earlier. A page loaded before a member was removed still holds that
    member's address, and its next unrelated save would write them back
    onto the household's mail. So an address that was not on the stored
    list and belongs to no one in the household survives only if the
    person actually added it in this edit: clients name their adds in
    `email_recipients_added`. A client too old to send that field is held
    to the narrower rule — it cannot restore an address whose owner was
    removed from the household."""
    new = [r for r in recips if r.strip().lower() not in prev]
    if not new:
        return recips
    members = {str(r["email"]).strip().lower() for r in conn.execute(
        "SELECT email FROM users WHERE tenant_id = "
        "current_setting('app.tenant_id')::uuid").fetchall()}
    tomb = set(cfg.get(_REMOVED_KEY) or [])
    if isinstance(added, (list, str)):
        meant = {a.strip().lower()
                 for a in mailguard.parse_recipients(added)}
        drop = {r.strip().lower() for r in new
                if r.strip().lower() not in members
                and r.strip().lower() not in meant}
        # an address someone deliberately adds back is theirs to add
        if tomb and meant:
            keep_tomb = [x for x in cfg.get(_REMOVED_KEY) or []
                         if x not in {_address_digest(m) for m in meant}]
            if keep_tomb:
                cfg[_REMOVED_KEY] = keep_tomb
            else:
                cfg.pop(_REMOVED_KEY, None)
    else:
        drop = {r.strip().lower() for r in new
                if r.strip().lower() not in members
                and _address_digest(r) in tomb}
    return [r for r in recips if r.strip().lower() not in drop]



def _apply_recipient_deltas(conn, cfg: dict, body: Fields,
                            user: dict) -> None:
    """An add or a remove names the one address it changes instead of
    posting the whole list. A whole list is a copy loaded earlier: saved
    from a second device, it silently drops whatever the first device
    added since. Applied to the STORED list inside the caller's config
    transaction, so two edits made at once both land.

    Without a whole list in the body, `email_recipients_added` is the add
    itself; alongside one it stays the hint `_drop_stale_readds` reads.
    An added address passes the same rules as one in a whole list: valid,
    under the cap, and on hosted a member of this household."""
    deltas = {}
    for key in ("email_recipients_added", "email_recipients_removed"):
        if not body.present(key):
            continue
        _msg = f"{key} must be a list of addresses"
        sent = body.text_or_list(key, msg=_msg)
        if sent is None:
            raise HTTPException(400, _msg)
        deltas[key] = mailguard.parse_recipients(sent, cap=None)
    if not deltas:
        return
    added = deltas.get("email_recipients_added") or []
    removed = {a.strip().lower()
               for a in deltas.get("email_recipients_removed") or []}
    if demoguard.people_hidden(user):
        # the shared demonstration login is shown only its own row, so
        # the only entry it can mean to take off is its own
        mine = str(user.get("email") or "").strip().lower()
        removed &= {mine}
    stored = mailguard.parse_recipients(cfg.get("email_recipients") or [],
                                        cap=None)
    have = {r.strip().lower() for r in stored}
    fresh = [a for a in added
             if a.strip().lower() not in have
             and a.strip().lower() not in removed]
    mailguard.check_recipients(conn, fresh)
    mailguard.refuse_owner_added(user["tenant_id"], fresh)
    out = [r for r in stored if r.strip().lower() not in removed] + fresh
    if len(out) > mailguard.MAX_RECIPIENTS:
        raise HTTPException(
            400, f"that is {len(out)} recipients — the limit is "
                 f"{mailguard.MAX_RECIPIENTS}. Remove some and save again.")
    if added and cfg.get(_REMOVED_KEY):
        # an address someone deliberately adds back is theirs to add
        meant = {_address_digest(a) for a in added}
        keep = [x for x in cfg[_REMOVED_KEY] if x not in meant]
        if keep:
            cfg[_REMOVED_KEY] = keep
        else:
            cfg.pop(_REMOVED_KEY, None)
    if out:
        cfg["email_recipients"] = out
    else:
        cfg.pop("email_recipients", None)

def _apply_email_pref(cfg: dict, change, user: dict) -> None:
    """One address, one kind, on or off — applied to the stored choices
    inside the caller's config transaction, so two devices changing two
    boxes at once both land. The owner decides for anyone on the mail;
    everyone else, and the shared demonstration login (which is shown no
    one else's row), only for their own address."""
    from ..notify import email_prefs
    if not isinstance(change, dict):
        raise HTTPException(
            400, "email_prefs_set must be {address, kind, on}")
    kind, on = change.get("kind"), change.get("on")
    if kind not in email_prefs.KINDS:
        raise HTTPException(
            400, f"kind must be one of {', '.join(email_prefs.KINDS)}")
    if not isinstance(on, bool):
        raise HTTPException(400, "on must be true or false")
    boxes = mailguard.parse_recipients(
        change.get("address") if isinstance(change.get("address"), str)
        else "", cap=None)
    if len(boxes) != 1:
        raise HTTPException(400, "address must be one email address")
    address = boxes[0].strip().lower()
    mine = str(user.get("email") or "").strip().lower()
    if address != mine and (user.get("role") != "owner"
                            or demoguard.people_hidden(user)):
        raise HTTPException(
            403, "you can only change which emails you receive yourself")
    try:
        email_prefs.set_pref(cfg, address, kind, on,
                             cap=mailguard.MAX_RECIPIENTS)
    except OverflowError as e:
        raise HTTPException(400, str(e))


# The settings lists every client saves WHOLE from a copy it read earlier.
# Two devices that each edit a different entry and save would otherwise
# lose the first one's edit, so the read publishes a version of each and a
# save may send it back (see _refuse_moved_lists).
_VERSIONED_LISTS = ("savings_goals", "custom_buckets", "income_scenarios",
                    "email_schedule", "llm_backends", "llm_roles",
                    "manual_assets")


def _list_version(cfg: dict, key: str) -> str:
    """An opaque version of one stored list: a digest of its canonical
    JSON. Every save door stores an emptied list as no key at all, so
    empty and missing read alike and "never set" has a version that holds
    still."""
    import hashlib
    import json as _json
    raw = _json.dumps(cfg.get(key) or None, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _list_versions(cfg: dict) -> dict:
    return {k: _list_version(cfg, k) for k in _VERSIONED_LISTS}


def _refuse_moved_lists(cfg: dict, body: Fields) -> None:
    """Refuse, before anything in this save is written, a whole list whose
    stored copy moved since the client read the version it sends. Only a
    list that is both in the body and named in `list_versions` is
    compared: a client that sends no versions replaces the list as it
    always did, and a version for a list it is not saving says nothing
    about this save. Raised inside the settings lock, so the refusal rolls
    the whole save back and the reply's versions are the stored ones."""
    _msg = "list_versions must map each list to the version read"
    sent = body.opaque("list_versions", msg=_msg)
    if sent is None:
        return
    if not all(isinstance(v, str) for v in sent.values()):
        raise HTTPException(400, _msg)
    moved = [k for k in _VERSIONED_LISTS
             if body.present(k) and k in sent
             and sent[k] != _list_version(cfg, k)]
    if moved:
        raise HTTPException(409, {
            "error": "settings_moved", "keys": moved,
            "list_versions": _list_versions(cfg)})


def _settings_view(cfg: dict) -> dict:
    out = {k: cfg.get(k) for k in _SETTINGS_FIELDS}
    out["list_versions"] = _list_versions(cfg)
    # Publish the toggle the budget engine actually obeys: a household
    # whose config never stored the key runs with dynamic budgeting ON.
    # Sent raw it would arrive as null, read as off, and the next bucket
    # save would post that false back and switch the feature off unasked.
    out["dynamic_variable_budget"] = bool(
        cfg.get("dynamic_variable_budget", True))
    # the same for the setup choices: what the worker and the importers
    # obey, never a null a client has to guess the meaning of
    from ..sync.base import history_months_of
    out["history_months"] = history_months_of(cfg)
    out["setup_mode"] = _setup_mode_of(cfg)
    out["bill_detection_auto"] = cfg.get("bill_detection_auto") is not False
    # publish the schedule the worker actually runs, so no client has to
    # invent a default for a household that never saved one. Here, not in
    # one handler: the read and the save reply both build from this view,
    # and clients seed their cache from either.
    from ..notify.schedule import effective_email_schedule
    out["email_schedule"] = effective_email_schedule(cfg)
    # business-wizard step marks — always present so the SPA renders one
    # shape whether or not a business has ever been set up
    out["biz_wizard_steps"] = cfg.get("biz_wizard_steps") or {}
    out["ret_wizard_steps"] = cfg.get("ret_wizard_steps") or {}
    out["ret_wizard_answers"] = cfg.get("ret_wizard_answers") or {}
    # scenarios in migrated list form (the legacy pair maps to two
    # entries) + savings goals — always present so the SPA renders one shape
    from ..engine import budget as _budget
    scen, active = _budget.income_scenarios(cfg)
    # each with the month its take-home makes, by the plan's own
    # conversion: the card prints it and works nothing out
    out["income_scenarios"] = [
        {**s, "monthly": _budget.scenario_monthly(s)} for s in scen]
    out["active_scenario"] = active
    out["savings_goals"] = cfg.get("savings_goals") or []
    # never echo the LLM API key (stored encrypted) — presence only
    out["llm_api_key_set"] = bool(cfg.get("llm_api_key"))
    # the legacy single endpoint's extra_body is the same credential as the
    # per-backend one below (it rides into every request body, so an
    # auth-in-body vendor's org token lives there) and gets the same
    # treatment: presence flag only, with "" keeping the field's declared
    # shape for clients. The save path reads "" as keep-stored, so this
    # masked echo can never wipe the value on a round trip.
    out["llm_extra_body"] = ""
    out["llm_extra_body_set"] = bool(cfg.get("llm_extra_body"))
    out["smtp_password_set"] = bool(cfg.get("smtp_password"))
    out["plaid_secret_set"] = bool(cfg.get("plaid_secret"))
    # what a credential rotation paused, for the banner (None = nothing)
    from ..notify import outbound_pause as _outbound
    out["outbound_paused"] = _outbound.paused(cfg)
    # Hosted: the host owns mail and the aggregator, so these are not the
    # tenant's to see. Withheld rather than blanked — a blank field reads
    # as "unset, go ahead and fill me in", which is exactly the invitation
    # the 403 then refuses. The SPA renders no card when they are absent.
    # Assigned BEFORE the withhold loop: assigned after, the loop's pop
    # would be a dead no-op and this field would escape the hosted
    # withholding invariant, rendering an MX credential card the
    # platform's API refuses.
    out["mx_api_key_set"] = bool(cfg.get("mx_api_key"))
    import os as _os
    # The AI panel's whole truth, per task: which backend is actually
    # answering each role. Without this, empty fields read as "no AI"
    # while the env-provided bundled Ollama does all the work. Hosted
    # never reveals the env fallback — that is the operator's
    # infrastructure. Stored api keys never leave as anything but a flag.
    _hosted = env_flag("OIKONOME_HOSTED")
    _env_url = _os.environ.get("OIKONOME_LLM_URL", "").strip()
    _env_model = _os.environ.get("OIKONOME_LLM_MODEL", "").strip()
    # endpoint URLs leave masked: a password or key embedded in one is a
    # credential like api_key beside it, and this view is read by every
    # member, viewers included. The save path restores the stored value
    # when the masked form comes back unchanged.
    from ..engine.llm_categorize import redact_url as _redact_url
    if out.get("llm_url"):
        out["llm_url"] = _redact_url(str(out["llm_url"]))
    out["llm_bundled"] = ({"url": _redact_url(_env_url), "model": _env_model,
                           "local": _llm_is_local(_env_url)}
                          if _env_url and not _hosted else None)
    _bks = [b for b in (cfg.get("llm_backends") or []) if isinstance(b, dict)]
    out["llm_backends"] = [
        {"id": str(b.get("id")), "name": b.get("name") or "",
         "url": _redact_url(b.get("url") or ""),
         "model": b.get("model") or "",
         "vision_model": b.get("vision_model") or "",
         # extra_body rides into every request body, so it can carry
         # credentials — masked like api_key beside it: presence flag
         # only, the empty string keeps the field's declared shape for
         # clients. The save path treats "" as keep-stored, so this
         # masked echo can never wipe the value on round trip.
         "extra_body": "",
         "extra_body_set": bool(b.get("extra_body")),
         "api_key_set": bool(b.get("api_key")),
         "local": _llm_is_local(b.get("url") or "")}
        for b in _bks]
    from ..engine.llm_categorize import ROLES as _LLM_ROLES
    from ..engine.llm_categorize import resolve_role as _resolve_role
    from ..engine.llm_categorize import live_roles as _live_roles
    out["llm_roles"] = {k: v for k, v in _live_roles(cfg).items()
                        if k in _LLM_ROLES}
    _own_url = str(cfg.get("llm_url") or "").strip()
    _cfg_vision = str(cfg.get("llm_vision_model") or "").strip()

    def _role_view(role: str):
        # the layer comes from the SAME resolver the execution path uses
        # (llm_categorize._backend) — reimplementing the precedence chain
        # here would agree with it only by hand-verification.
        # Here it is dressed for display: names and locality added, keys
        # and the operator's env fallback (on hosted) withheld.
        layer, b = _resolve_role(cfg, role)
        if layer == "backend":
            model = (b.get("vision_model") or b.get("model")
                     if role == "vision" else b.get("model")) or ""
            return {"source": "backend", "name": b.get("name") or "",
                    "url": _redact_url(b["url"]), "model": model,
                    "local": _llm_is_local(b["url"])}
        if layer == "legacy":                  # legacy single endpoint
            model = ((_cfg_vision or cfg.get("llm_model"))
                     if role == "vision" else cfg.get("llm_model")) or ""
            return {"source": "tenant", "url": _redact_url(_own_url),
                    "model": str(model),
                    "local": _llm_is_local(_own_url)}
        if _env_url and not _hosted:           # bundled, chosen or fallback
            model = ((_cfg_vision or _env_model)
                     if role == "vision" else _env_model)
            return {"source": "bundled", "url": _redact_url(_env_url),
                    "model": model,
                    "local": _llm_is_local(_env_url)}
        return None

    out["llm_effective"] = {
        r: (None if v is None else {**v, "label": model_words(v["model"])})
        for r, v in ((r, _role_view(r)) for r in _LLM_ROLES)}
    # the local trained classifier categorizes FIRST (seed rules → model →
    # LLM); when it's active the card must say so, or the Categorization
    # row reads as if the LLM owns work it only sees the abstentions of
    from ..engine import model_categorize as _mc
    out["categorizer_active"] = _mc.available()
    if env_flag("OIKONOME_HOSTED"):
        for _k in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                   "smtp_starttls", "smtp_password_set",
                   "plaid_client_id", "plaid_env", "plaid_secret_set",
                   "mx_client_id", "mx_env", "mx_api_key_set"):
            out.pop(_k, None)
    return out


def _recipient_status(tenant_id, configured: list[str],
                      muted: list[str] | None = None,
                      cfg: dict | None = None, *,
                      admin=None) -> list[dict]:
    """Who receives the scheduled mail, and whether it reaches them.

    A plain list of addresses can only tell you what you typed. It cannot say
    that an address has never been confirmed, or that the provider has been
    refusing it for three days — and that is precisely the information the
    person adding a recipient needs, because the failure mode is silence.

    The OWNER leads the list and is marked non-removable: they receive the
    mail by construction (see `worker._recipients`), and a control that
    pretends otherwise would silently replace the owner instead of adding to
    them.

    Per address:
    owner — always mailed, cannot be removed here
    member — holds an account on this tenant. Hosted REQUIRES this; on
    self-host a free-form address is legitimate and simply
    has no account to be verified against.
    verified — the account confirmed its address. null when there is no
    account, because "unverified" would be a claim we have no
    standing to make about someone else's mailbox.
    delivery — the provider says mail to it is failing; the cadence is
    HELD until it is fixed.
    prefs — which kinds of mail this address receives
    (`email_prefs.KINDS`), as the worker reads them; a mute
    still stops all of it.
    invite — `invited` (asked, no answer yet), `accepted`,
    `declined`, `expired`, or null for an address that has
    never been asked. This is the one that decides whether
    mail flows: only `accepted` is mailed. The owner needs it
    because "I added them and nothing happened" and "I added
    them and they haven't answered" look identical otherwise.
"""
    from ..db import tenancy as _ten
    from ..notify import delivery as _delivery
    from ..notify import recipient_invites as _invites
    from ..notify import email_prefs as _email_prefs
    rows, owner_email = [], None
    try:
        # users is a control-plane table the app role reads on every
        # request (session lookup) — the pooled connection serves this
        # read too; a fresh admin connect per Settings load would buy nothing
        cc = _ten.control_connect()
        try:
            members = {r["email"].lower(): r for r in cc.execute(
                "SELECT email, verified_at, role FROM users WHERE "
                "tenant_id=%s", (tenant_id,)).fetchall()}
        finally:
            cc.close()
    except Exception:                              # noqa: BLE001
        import logging
        logging.getLogger("oikonome.api").exception("recipient status failed")
        return []
    for m in members.values():
        if m["role"] == "owner":
            owner_email = m["email"]
            break
    # WITH NO LIST CONFIGURED the worker mails every verified user on the
    # tenant (`_recipients_raw`'s fallback) — not just the owner. Listing
    # the owner alone would show a three-person household "you" while all
    # three receive the balances every morning. The card's own promise is
    # "who receives the scheduled mail"; it has to mean it.
    implied = ([m["email"] for m in members.values() if m["verified_at"]]
               if not configured else [])
    ordered, seen = [], set()
    for e in ([owner_email] if owner_email else []) + list(configured) \
            + implied:
        if not e or e.lower() in seen:
            continue
        seen.add(e.lower())
        ordered.append(e)
    # ONE query for every address's delivery state, not one per failing
    # address on top of the set lookup.
    failing = _delivery.states(ordered)
    # on the admin connection the caller holds, when it holds one (a
    # member's own row is built from several such reads on one connect)
    invites = (_invites.states(tenant_id, ordered) if admin is None
               else _invites.states_on(admin, tenant_id, ordered))
    # a restored config can carry email_muted in any JSON shape — a
    # non-list here 500's every Settings load for the tenant, and
    # Settings is the page you'd fix it from
    muted_set = ({str(m).lower() for m in muted}
                 if isinstance(muted, list) else set())
    # A delivery row is keyed on the ADDRESS, instance-wide: the failure
    # it records may have come from another household's mail. Its details
    # (bounce vs complaint, the provider's own words about the mailbox)
    # belong to whoever that mailbox answers to. This household sees them
    # for an address it has a standing with — one of its own accounts, or
    # a recipient who accepted its invitation — and for any other address
    # only that mail to it is held. A single-household box has no one else
    # whose record it could be, so the tenant count is asked only when an
    # address actually carries a row — and then once for the whole grid,
    # since each ask is an admin connection.
    # The rule itself lives in one place so Resend and this grid cannot
    # drift apart (recipient_invites.delivery_detail_visible).
    from ..notify import recipient_invites as _invites
    multi_tenant = None
    for e in ordered:
        m = members.get(e.lower())
        state = failing.get(e.lower())
        inv = invites.get(e.lower())
        if state and multi_tenant is None:
            multi_tenant = _invites.multi_household()
        if state and not _invites.delivery_detail_visible(
                tenant_id, e, member=bool(m),
                invite_state=inv and inv.get("state"),
                multi_tenant=multi_tenant):
            state = {"state": "bouncing", "reason": None}
        is_owner = bool(owner_email and e.lower() == owner_email.lower())
        rows.append({
            "email": e,
            "owner": is_owner,
            # asked not to receive the scheduled mail (per-person opt-out;
            # the owner can mute themselves too)
            "muted": e.lower() in muted_set,
            "prefs": _email_prefs.row(cfg or {}, e),
            "member": bool(m),
            "verified": (bool(m["verified_at"]) if m else None),
            "delivery": (state["state"] if state else None),
            "delivery_reason": (state["reason"] if state else None),
            # the owner is mailed by construction and is never invited to
            # their own household's email — reporting a null invite state
            # for them would put a "not invited" badge on the one row that
            # can never need one
            # A member receiving by the no-list fallback was never invited
            # and never needs to be — saying "not invited" about them would
            # be the same wrong badge the owner row must never get.
            "invite": (None if (is_owner or (not configured and m))
                       else (inv["state"] if inv else None)),
            # why this address is on the list at all
            "via": ("owner" if is_owner
                    else "member" if (not configured and m) else "invited"),
            "invited_at": (None if is_owner or not inv else
                           (inv["invited_at"].isoformat()
                            if inv["invited_at"] else None)),
        })
    return rows


def _owner_admin(user: dict):
    """One admin connection for an owner's settings read or save, shared
    by the recipient grid and who each cadence reaches — or None (anyone
    else's read builds one row and opens what it needs)."""
    if user.get("role") != "owner":
        return None
    from ..db import tenancy as _ten
    return _ten.admin_connect()


def _email_reach(conn, user: dict, cfg: dict, admin=None) -> dict | None:
    """Who each cadence reaches (notify/reach), for the owner — the
    schedule and its recipients are theirs to read. Never fails the read
    it rides: a lookup that could not be made is simply left out."""
    if user.get("role") != "owner":
        return None
    from ..notify import reach
    try:
        return reach.cadence_reach(conn, user["tenant_id"], cfg, admin=admin)
    except Exception:                                    # noqa: BLE001
        import logging
        logging.getLogger("oikonome.api").exception("email reach failed")
        return None


def _status_rows(user: dict, cfg: dict, admin=None) -> list[dict]:
    """The recipient grid as this caller's view carries it, built from the
    settings `cfg` the door already read, with NO tenant connection held:
    every door calls it after closing its own, so a read or a save checks
    out one tenant connection at a time.

    The owner gets the whole grid. Anyone else is cut to their own row by
    `_hide_people`, so only that row is built: with no list configured,
    the grid's fallback rows; on a configured list, the caller's own
    mailbox row (`_own_list_row`); off it, none."""
    configured = cfg.get("email_recipients") or []
    muted = cfg.get("email_muted") or []
    if user.get("role") == "owner" or not configured:
        return _recipient_status(user["tenant_id"], configured, muted, cfg,
                                 admin=admin)
    mine = str(user.get("email") or "").strip().lower()
    if not mine or mine not in mailguard.stored_mailboxes(configured)[0]:
        return []
    try:
        row = _own_list_row(user, cfg)
    except Exception:                                    # noqa: BLE001
        # the card then reads as on a server that sends no row
        # (`_hide_people` drops the list facts), never a failed page
        import logging
        logging.getLogger("oikonome.api").exception(
            "own recipient row failed")
        return []
    return [row] if row else []


def _own_list_row(user: dict, cfg: dict) -> dict | None:
    """The caller's own status row, for a non-owner whose mailbox the
    configured list names, however the entry naming it was stored.

    Whether they are mailed is the mailer's own answer
    (`worker._recipient_lists`, mute and per-kind choices aside: the card
    reads those from /api/me), so the split, the consent of the mailbox or
    of a one-mailbox entry, and the holds are not re-spelled here. The rest
    of the row (delivery, verification, prefs, the mailbox's own
    invitation) is the grid's, for the caller's login address.

    `mailed` is the mailer's answer; when it mails them the invite reads
    `accepted`, whichever record holds the consent. `not_mailed` says why
    it does not: `held` (they said yes, and a hold such as the owner's
    credential rotation keeps them off), else their invitation's state —
    `invited`, `declined`, `expired` or `not_invited`."""
    mine = str(user.get("email") or "").strip().lower()
    if not mine:
        return None
    # One admin connection for the whole row: the mailer's list, its
    # invitation read, the grid's and the entry-consent read below each
    # opened their own, and this runs on every member's settings read (an
    # unpooled connect is a fresh round trip). A refused connect raises to
    # `_status_rows`, which answers with no row.
    from ..db import tenancy as _ten
    admin = _ten.admin_connect()
    try:
        return _own_list_row_on(admin, user, cfg, mine)
    finally:
        admin.close()


def _own_list_row_on(admin, user: dict, cfg: dict, mine: str) -> dict | None:
    """`_own_list_row` on the admin connection it opened."""
    from ..jobs import worker
    # the worker reads the settings through a connection; the door's own
    # is closed by now, so this is the one tenant checkout held
    conn = _conn(user)
    try:
        mailed = mine in {str(e).lower() for e in worker._recipient_lists(
            conn, user["tenant_id"], honour_mute=False, admin=admin)[0]}
    finally:
        conn.close()
    rows = _recipient_status(user["tenant_id"], [user["email"]],
                             cfg.get("email_muted") or [], cfg, admin=admin)
    row = next((r for r in rows if not r.get("owner")
                and str(r.get("email") or "").lower() == mine), None)
    if row is None:
        return None
    row = {**row, "mailed": mailed, "not_mailed": None}
    if mailed:
        row["invite"] = "accepted"
        return row
    state = row.get("invite")
    if not state:
        # a consent recorded on a one-mailbox entry (`Name <me>`) stands in
        # for the mailbox's own, as the mailer reads it; asked only when
        # the mailbox itself was never asked
        from ..notify import recipient_invites as _ri
        stored = cfg.get("email_recipients") or []
        raws = [str(e).strip().lower() for e in
                (stored if isinstance(stored, list) else [stored])
                if [m.lower() for m in
                    mailguard.split_mailboxes([e])[0]] == [mine]]
        raws = [r for r in raws if r != mine]
        if raws:
            st = _ri.states_on(admin, user["tenant_id"], raws)
            state = next((st[r]["state"] for r in raws if r in st), None)
    row["not_mailed"] = ("held" if state == "accepted"
                         else state or "not_invited")
    return row


def _hide_people(view: dict, user: dict) -> dict:
    """Cut the settings view's recipient lists to the caller's own row
    when people are hidden from it. Every door that returns the view calls
    this one helper, so the read and the write answer alike."""
    if user.get("role") != "owner" and "email_recipients" in view:
        # Cut to their own entry below, the roster can no longer tell a
        # member whether a list is set or whether it names them (an entry
        # joining their mailbox with someone else's is withheld, yet the
        # worker splits it and mails them). So state both, read from the
        # whole stored list the way the worker reads it: any stored value
        # is a configured list, and its mailboxes are what is mailed.
        stored = view.get("email_recipients")
        me = str(user.get("email") or "").strip().lower()
        view["email_list_configured"] = bool(stored)
        view["email_on_list"] = bool(stored) and bool(me) and (
            me in mailguard.stored_mailboxes(stored)[0])
    if demoguard.people_hidden(user):
        for k in ("email_recipients", "email_muted"):
            if k in view:
                view[k] = demoguard.own_addresses_only(user, view.get(k))
        if "email_recipient_status" in view:
            mine = set(demoguard.own_addresses_only(
                user, [r.get("email") for r in
                       view["email_recipient_status"]]))
            view["email_recipient_status"] = [
                r for r in view["email_recipient_status"]
                if r.get("email") in mine]
        # the rotation's record names the paused recipients too; a record
        # left naming nothing the caller may see is no record for it
        paused = view.get("outbound_paused")
        if isinstance(paused, dict):
            paused = {**paused, "recipients": demoguard.own_addresses_only(
                user, paused.get("recipients"))}
            # and the members a recovery removed, by address
            if "removed_members" in paused:
                gone = [m for m in (paused.get("removed_members") or [])
                        if isinstance(m, dict)]
                mine = set(demoguard.own_addresses_only(
                    user, [m.get("email") for m in gone]))
                paused["removed_members"] = [
                    m for m in gone if m.get("email") in mine]
            view["outbound_paused"] = (
                paused if (paused["smtp"] or paused["llm"]
                           or paused["recipients"]
                           or paused.get("aggregator")
                           or paused.get("removed_members")) else None)
    if user.get("role") != "owner":
        # each member's mail switches (muted, which kinds they take, whether
        # their address bounces, their invite) are the owner's to see, as
        # on the roster; everyone else reads their own row and no other
        mine = str(user.get("email") or "").strip().lower()
        if "email_recipients" in view:
            # the roster of who the household's money is mailed to is the
            # owner's; anyone else reads the one entry that is theirs alone
            # (an entry naming several mailboxes names someone else too).
            # A member's save never carries this list — its keys are the
            # owner's and are dropped from the body — so the entries cut
            # here are never written back over.
            view["email_recipients"] = [
                e for e in (view.get("email_recipients") or [])
                if isinstance(e, str) and [
                    m.strip().lower() for m in
                    mailguard.parse_recipients(e, cap=None)] == [mine]]
        paused = view.get("outbound_paused")
        if isinstance(paused, dict) and paused.get("recipients"):
            # the rotation's record of the held recipients is the same
            # roster under another key; a record left holding nothing the
            # caller may see is no record for it
            paused = {**paused, "recipients": [
                r for r in paused.get("recipients") or []
                if str(r or "").strip().lower() == mine]}
            view["outbound_paused"] = (
                paused if (paused.get("smtp") or paused.get("llm")
                           or paused["recipients"]
                           or paused.get("aggregator")
                           or paused.get("removed_members")) else None)
        if "email_muted" in view:
            muted = view.get("email_muted") or []
            if isinstance(muted, str):
                muted = [x for x in muted.split(",")]
            view["email_muted"] = [m for m in muted
                                   if str(m or "").strip().lower() == mine]
        if "email_recipient_status" in view:
            view["email_recipient_status"] = [
                r for r in (view.get("email_recipient_status") or [])
                if str(r.get("email") or "").strip().lower() == mine]
            if view.get("email_on_list") and not view[
                    "email_recipient_status"]:
                # on the list but no row could be built for them: say
                # nothing rather than half, and the card reads as it does
                # on a server that never sent these facts
                view.pop("email_list_configured", None)
                view.pop("email_on_list", None)
    paused = view.get("outbound_paused")
    if (isinstance(paused, dict) and paused.get("removed_members")
            and user.get("role") != "owner"):
        # who an owner's recovery removed is the owner's to review; anyone
        # else reading the household's settings learns how many, not who
        paused = dict(paused)
        paused["removed_members_count"] = len(paused.pop("removed_members"))
        view["outbound_paused"] = paused
    paused = view.get("outbound_paused")
    if (isinstance(paused, dict) and "aggregator_keys" in paused
            and (user.get("role") != "owner"
                 or demoguard.people_hidden(user))):
        # which Plaid or MX client the bank-link hold is about is the
        # owner's to judge (they confirm or remove it); anyone else learns
        # that new links are held, not which client
        paused = dict(paused)
        paused.pop("aggregator_keys")
        view["outbound_paused"] = paused
    return view


def _merge_unseen_people(body: Fields, cfg: dict, user: dict) -> Fields:
    """The shared demonstration login is shown only its own row of the
    recipient and mute lists, and both clients save those lists whole — so
    its save carries only what it saw. Put back every stored entry it was
    not shown before the save reads the body, or one toggle of its own
    stops everyone else's mail and clears their mutes. Its own row is
    whatever it posted. Answers the body with the lists merged."""
    if not demoguard.people_hidden(user):
        return body
    mine = str(user.get("email") or "").strip().lower()
    for key, stored in (
            ("email_recipients",
             mailguard.parse_recipients(cfg.get("email_recipients") or [])),
            ("email_muted", list(cfg.get("email_muted") or []))):
        if not body.present(key):
            continue
        posted = body.text_or_list(
            key, msg=f"{key} must be a list of addresses")
        if isinstance(posted, str):
            posted = mailguard.parse_recipients(posted)
        if not isinstance(posted, list):
            continue                    # a null: the save reads it below
        unseen = [a for a in stored if str(a).strip().lower() != mine]
        kept = {str(u).strip().lower() for u in unseen}
        if key == "email_muted":
            # a mute is the one entry here that needs no step-up: an
            # address it was never shown, posted on this list, would
            # silence that member's mail. Only its own mute is its to set.
            posted = demoguard.own_addresses_only(user, posted)
        body = body.with_value(key, unseen + [
            p for p in posted if str(p).strip().lower() not in kept])
    return body



def model_words(model: str) -> str:
    """A model id in words a person reads, for the places that name the
    model without being its editor (the Settings side menu): the part
    after any "org/" prefix, its separators as spaces, each lower-case
    word capitalised, and a run of bare numbers read as a version
    ("vendor-family-5-5" -> "Vendor Family 5.5"). A word written with
    capitals is the maker's own spelling and is kept."""
    tail = str(model or "").strip().rsplit("/", 1)[-1]
    out: list[str] = []
    for tok in (t for t in re.split(r"[-_:\s]+", tail) if t):
        if re.fullmatch(r"\d+(\.\d+)*", tok) and out and \
                re.fullmatch(r"\d+(\.\d+)*", out[-1]):
            out[-1] += "." + tok
        else:
            out.append(tok.capitalize() if tok.islower() else tok)
    return " ".join(out)

@router.get("/settings")
@documented(response=SettingsView)
def settings_get(user: dict = Depends(_user())):
    from ..engine import budget
    admin = _owner_admin(user)
    try:
        conn = _conn(user)
        try:
            # sealed credentials (the relay login) read back as typed
            cfg = settings_exposure.readable(conn, budget.load_config(conn))
            reach = _email_reach(conn, user, cfg, admin)
        finally:
            conn.close()
        view = _settings_view(cfg)
        view["email_recipient_status"] = _status_rows(user, cfg, admin)
    finally:
        if admin is not None:
            admin.close()
    if reach is not None:
        view["email_reach"] = reach
    _hide_people(view, user)
    view = settings_exposure.view_for(user.get("role"), view)
    if demoguard.is_demo(user["tenant_id"]):
        # Feedback is OFF on a demo and cannot be switched on: the tab
        # would invite bug reports about synthetic
        # data from anonymous visitors, and its Doctor bundle is instance
        # detail. Reported false regardless of stored config, so the SPA
        # hides the tab; the submit endpoint denies demos outright, and
        # settings writes are refused, so nothing can flip it back.
        view["feedback_enabled"] = False
        view["demo_read_only"] = True
    return view


# Demo lockdown. A demo instance is read-only wholesale (see settings_set)
# — the shared, publicly printed credentials mean any write is one visitor
# reconfiguring the instance for every later visitor. This set is kept
# because it names the keys that must NEVER be writable on a demo even if
# the blanket rule is ever relaxed.
_DEMO_DOOR_KEYS = settings_exposure.DEMO_DOOR_KEYS


def _secret_values(conn, cfg: dict) -> set[str]:
    """Every stored LLM credential in plaintext: the single-endpoint key
    and extra body plus each backend's. Decrypted because a save can
    re-encrypt an unchanged value (the legacy fold-in wraps a plaintext
    key on its way into the backend list), and a fresh ciphertext of the
    same key is not a new credential."""
    from ..db import crypto
    stored = [cfg.get("llm_api_key"), cfg.get("llm_extra_body")]
    for b in cfg.get("llm_backends") or []:
        if isinstance(b, dict):
            stored += [b.get("api_key"), b.get("extra_body")]
    return {crypto.decrypt(conn, v) or "" for v in stored if v} - {""}


def _llm_urls(cfg: dict) -> set[str]:
    urls = {str(cfg.get("llm_url") or "").strip()}
    for b in cfg.get("llm_backends") or []:
        if isinstance(b, dict):
            urls.add(str(b.get("url") or "").strip())
    return urls - {""}


def _outbound_repointed(conn, before: dict, after: dict) -> list[str]:
    """What this save points somewhere NEW that the household's data or
    its sign-in mail travels to. Judged on the stored documents before and
    after the save, not on the body, so the SPA re-posting the whole
    document unchanged repoints nothing; and a removal or a clear is never
    a repoint, because it only takes a destination away.

    Each group is a destination a stolen session could otherwise swap for
    its own: the mail relay carries the owner's password-reset link, the
    LLM endpoint and its credential (which, on a proxy, picks whose account
    logs the prompts) receive every transaction the categorize pass reads,
    the tax-document consent widens what that endpoint is sent, and a new
    daily-mail recipient is a copy of the household's money every morning.
    """
    changed = []

    def _relay(c: dict) -> tuple:
        # unset fields read as the values the sender uses for them, so a
        # client filling in the defaults on a re-save is not a repoint
        # a stored port that does not parse still has to compare — this
        # runs on every settings save, including the one that fixes it
        try:
            port = int(c.get("smtp_port") or 587)
        except (TypeError, ValueError, OverflowError):
            port = str(c.get("smtp_port"))
        # an empty From sends as the relay account when that is an
        # address (report.resolve_smtp), so filling it in is no repoint
        from . import report as _report
        user_s = _report.relay_user(conn, c)
        from_s = str(c.get("smtp_from") or "") or (
            user_s if mailguard.valid_address(user_s) else "")
        return (str(c.get("smtp_host") or ""), port, user_s, from_s,
                c.get("smtp_starttls", True) is not False,
                str(c.get("smtp_password") or ""))
    if after.get("smtp_host") and _relay(before) != _relay(after):
        changed.append("smtp")
    if _llm_urls(after) - _llm_urls(before):
        changed.append("llm_url")
    llm_secret_keys = ("llm_api_key", "llm_extra_body", "llm_backends")
    if any(before.get(k) != after.get(k) for k in llm_secret_keys) and (
            _secret_values(conn, after) - _secret_values(conn, before)):
        changed.append("llm_api_key")
    if after.get("taxdocs_allow_remote_llm") and not before.get(
            "taxdocs_allow_remote_llm"):
        changed.append("taxdocs_allow_remote_llm")
    if (mailguard.stored_mailboxes(after.get("email_recipients"))[0]
            - mailguard.stored_mailboxes(before.get("email_recipients"))[0]):
        changed.append("email_recipients")
    return changed


def _scheduled_channels_switched_on(before: dict, after: dict) -> list[str]:
    """The scheduled text and push channels this save switches ON, as
    "<cadence>.<channel>" — read from the effective schedules the worker
    runs before and after, so re-saving what is already on switches nothing
    on and switching one off is never listed. A scheduled text or push goes,
    every day or month, to the phone the owner verified and to every
    member's devices: a recurring send to people the caller may never have
    been shown."""
    from ..notify.schedule import effective_email_schedule
    was, now = effective_email_schedule(before), effective_email_schedule(after)
    on = []
    for cad, entry in now.items():
        if not isinstance(entry, dict):
            continue
        prior = was.get(cad) if isinstance(was.get(cad), dict) else {}
        on += [f"{cad}.{ch}" for ch in ("sms", "push")
               if entry.get(ch) and not prior.get(ch)]
    return on


# Investment fee / basis settings: a household names a handful of
# institutions and accounts, so the cap only stops a runaway client.
_INVEST_MAX_ROWS = 50


def _invest_mask(v, field: str) -> str:
    """One account mask as stored: the tail of the account number, which
    is all an aggregator reports and what the net-worth engine compares
    against. Plaid's masks are 2-4 characters and some banks put letters
    in them, so letters are accepted and stored upper-case — the case the
    feeds report them in, so the engine's exact comparison still matches.
    Blank comes back blank for the caller to skip."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise HTTPException(
            400, f"{field}: an account mask must be letters or digits")
    m = str(v).strip().upper()
    if m and not (m.isascii() and m.isalnum() and 2 <= len(m) <= 8):
        raise HTTPException(
            400, f"{field}: an account mask is the account's last 2 to 8 "
                 "letters or digits")
    return m


# The most AI backends one household may route between — one ceiling for
# the settings save and the bundle import.
from ..sync.connections_bundle import _MAX_BACKENDS as _MAX_LLM_BACKENDS  # noqa: E402


def _check_supplied_endpoints(chost, curl, smtp_host, llm_url, backends):
    """The SSRF check on every endpoint a settings body supplies, in one
    place so the lookup budget around it covers all of them."""
    if smtp_host:
        chost(smtp_host, what="the SMTP host")
    if llm_url:
        curl(llm_url, what="llm_url")
    for u in backends:
        if u:
            curl(u, what="llm backend url")


def _yes_word(fields: Fields, key: str) -> bool:
    """A consent switch the clients send as true/false and a script may
    send as one of the words it has always been read as ("1", "true",
    "yes", "on", any case). Anything else, null included, is off; a list
    or an object is a 400 naming the field."""
    kind = fields.json_type(key)
    if kind == "bool":
        return bool(fields.flag(key))
    if kind in ("string", "number"):
        return (fields.text_or_int(key, default="",
                                   msg=f"{key} must be true or false")
                .lower() in ("1", "true", "yes", "on"))
    if kind in (None, "null"):
        return False
    raise HTTPException(400, f"{key} must be true or false")


# One save per few seconds, sustained, from one address is well past any
# person at the settings page or the wizard (each step posts once); the
# limit is the backstop for a script, not a pace a person can reach.
@router.post("/settings",
             dependencies=[Depends(limit("settings_save", 300, 600))])
@documented(response=SettingsView, body=SettingsSave)
def settings_set(user: dict = Depends(_user()),
                 body: Fields = Depends(json_fields)):
    # The demo instance is READ-ONLY: its credentials
    # are shared and printed publicly, so ANY settings write is one visitor
    # reconfiguring the instance for everyone who comes after. Refusing only
    # the "door" keys (_DEMO_DOOR_KEYS — mail/LLM endpoints) is not enough:
    # the whole endpoint is closed. The SPA greys the page out to match, and
    # this is the backstop that makes that true whatever is POSTed.
    demoguard.deny(user)
    # an older store build re-proves in the body rather than through the
    # elevation window; read before the role filter, which knows nothing
    # of these keys
    _proof = {k: _scalar_text(body, k)
              for k in ("password", "totp_code", "recovery_code")}
    # A member edits the household's money, not its plumbing: the mail
    # relay, the daily-email recipient list and the AI endpoint are owner
    # fields. They are DROPPED from the body rather than refused, because
    # the SPA posts the whole settings document on every save — refusing
    # would mean a member could never change a budget either.
    # The role filter decides which KEYS a role may save, never their
    # values; what is left is read only through the typed accessors.
    fields = Fields(permissions.settings_for(user["role"], body.as_dict()))

    def _refuse_member_change(key, requested, stored):
        # the owner-only keys a member is refused for changing rather than
        # having them dropped (permissions.OWNER_REFUSED_SETTINGS)
        why = permissions.refused_setting_change(user["role"], key,
                                                 requested, stored)
        if why:
            raise HTTPException(403, why)
    # The shape of every list whose entries cost a lookup is judged HERE,
    # before a pooled connection is taken and before any name is resolved:
    # a body is up to a megabyte, and a list refused only after each of its
    # entries was looked up would hold a connection and a worker for as
    # long as the resolver takes over all of them.
    # a blank list is none, as `or []` always read it
    _bk_in = fields.opaque_rows("llm_backends", _BODY_ROWS_MAX, blank_ok=True)
    if _bk_in is not None and len(_bk_in) > _MAX_LLM_BACKENDS:
        raise HTTPException(
            400, f"llm_backends: {_MAX_LLM_BACKENDS} backends max")
    if _bk_in is not None and not all(isinstance(e, dict) for e in _bk_in):
        raise HTTPException(400, "each llm backend must be an object")
    from ..engine import budget
    conn = _conn(user)
    # set inside the lock when the wizard's connect step is left; the pin
    # itself takes the same lock, so it runs after this one is let go
    pin_primary_after = False
    try:
        # The whole 500-line body is one read-modify-write
        # of the settings document, so a second save that read before
        # this one committed would overwrite every field this
        # request touched. The lock is held for the WHOLE body — the
        # validation raises inside it abort the transaction, which is
        # intended: a 400 on field nine
        # must not persist fields one through eight.
        #
        # EXCEPT the network validation. netguard's
        # getaddrinfo has no timeout — a blackholed domain stalls glibc
        # for ~10-40s — and running it inside the row lock while holding a
        # pooled tenant connection would let ten concurrent saves serialize into
        # minutes of a starved pool. Caller-supplied values are resolved
        # HERE, before any lock; the in-lock sites re-check only values
        # that came from the stored config (legacy rows).
        # (hosted skips the smtp half: the 403 owns that path and
        # must answer first — no DNS is ever done for a refused write)
        _sup_smtp = ("" if env_flag("OIKONOME_HOSTED")
                     else fields.text("smtp_host"))
        _sup_llm = fields.text("llm_url")
        # multi-backend AI list: every entry's URL is body-supplied, so all
        # of them are resolved and guarded here, above the lock, like
        # llm_url — the in-lock code never re-checks body values
        _sup_bk = [str(e.get("url") or "").strip() for e in _bk_in or []]
        if _sup_smtp or _sup_llm or any(_sup_bk):
            from .netguard import BlockedURL as _BURL
            from .netguard import check_host as _chost
            from .netguard import check_url as _curl
            from .netguard import lookup_budget as _lbudget
            # every name this body can carry, and a few seconds in all: a
            # slow resolver is not multiplied by the number of endpoints
            try:
                with _lbudget(_MAX_LLM_BACKENDS + 2, seconds=6.0):
                    _check_supplied_endpoints(_chost, _curl, _sup_smtp,
                                              _sup_llm, _sup_bk)
            except _BURL as e:
                raise HTTPException(400, str(e))
        with budget.config_txn(conn) as cfg:
            # first, before anything below mutates cfg: a whole list saved
            # from a copy that has moved since is refused with nothing
            # written
            _refuse_moved_lists(cfg, fields)
            # who was already on the list, so the save can tell an ADD
            # from a re-save of the same people. Captured before anything mutates
            # cfg — a diff taken after the write would invite everyone, every
            # time any unrelated setting was saved.
            # Compared as MAILBOXES: the client re-posts the stored list and
            # the save splits `Name <a>` / `a;b` entries, so raw strings here
            # would make every such entry look new and drop it.
            _prev_entries = cfg.get("email_recipients")
            if isinstance(_prev_entries, list):
                _prev_entries = list(_prev_entries)
            _prev_recips, _prev_asked = mailguard.stored_mailboxes(
                _prev_entries)
            fields = _merge_unseen_people(fields, cfg, user)
            # the whole document before the save, so the activity log can
            # name the KEYS that changed (never a value)
            import copy as _copy
            _cfg_was = _copy.deepcopy(cfg)
            _smtp_from_missing = _smtp_user_given = False
            def _num(v, field):
                # A blank box is an answer of 0. A JSON null is not: it is
                # what a client's NaN becomes on the wire, so a field the
                # person typed and the client could not read would be
                # stored as 0 without a word. No client sends null to mean
                # "clear" — clearing is a blank or a 0 — so refuse it.
                if v is None:
                    raise HTTPException(400, f"{field} must be a number")
                if v == "":
                    return 0.0
                import math
                try:
                    n = _typed_money(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{field} must be a number")
                # a literal NaN/Infinity would ride into the jsonb config and make
                # the whole save 500 (Postgres rejects non-finite in jsonb) — 400.
                if not math.isfinite(n):
                    raise HTTPException(400, f"{field} must be a finite number")
                # finite alone is not enough: two asset values near the
                # float ceiling overflow the net-worth sum (see _MONEY_MAX)
                if abs(n) > _MONEY_MAX:
                    raise HTTPException(400, f"{field} is too large")
                return n

            def _stated(v):
                """Did the caller actually supply a number? `0` is an answer;
                a missing key or a blank field is not."""
                return v is not None and str(v).strip() != ""

            # The money knobs: a text box's string ("" is an answer of 0)
            # or a number; a null is refused, never stored as 0
            # (Fields.money_given)
            for k in ("food_monthly", "other_monthly"):
                if fields.present(k):
                    cfg[k] = max(0.0, fields.money_given(k))
            # a person saving any part of the plan — the planner, the
            # Budget page, the wizard's step — is the household having
            # one of its own, which the first-run seed never is
            if any(fields.present(k) for k in (
                    "food_monthly", "other_monthly", "custom_buckets",
                    "budgeted_income_monthly")):
                budget.mark_saved_by_user(cfg)
            if fields.present("budgeted_income_monthly"):
                v = fields.money_given("budgeted_income_monthly",
                                       label="income")
                if v > 0:
                    cfg["budgeted_income_monthly"] = v
                else:
                    cfg.pop("budgeted_income_monthly", None)
            if fields.present("dynamic_variable_budget"):
                cfg["dynamic_variable_budget"] = bool(fields.flag(
                    "dynamic_variable_budget", default=False))
            if fields.present("plaid_enrich_cap"):
                # rows/month a person is willing to have Plaid Enrich bill
                # for. Clamped rather than free: 0 turns it off, and the
                # upper bound stops a typo'd extra zero from authorising a
                # spend nobody meant to approve.
                # digits from a text box and a fractional number cut to its
                # whole part are what int() always took; null and blank were
                # always refused
                _cap_msg = "plaid_enrich_cap must be an integer"
                cap = fields.int_in("plaid_enrich_cap", text_ok=True,
                                    truncate=True, msg=_cap_msg)
                if cap is None:
                    raise HTTPException(400, _cap_msg)
                cfg["plaid_enrich_cap"] = max(0, min(cap,
                                                     _enrich_cap_ceiling()))
            # A stored cap above the ceiling — an operator may lower the ceiling at
            # any time — would keep authorising that spend forever. Any save past this
            # point brings it back under the ceiling, whether or not this
            # request was the one that touched the key.
            _cap_now = cfg.get("plaid_enrich_cap")
            if _cap_now is not None:
                try:
                    _ceiling = _enrich_cap_ceiling()
                    if int(_cap_now) > _ceiling:
                        cfg["plaid_enrich_cap"] = _ceiling
                except (TypeError, ValueError, OverflowError):
                    cfg.pop("plaid_enrich_cap", None)   # corrupt → default
            if fields.present("wizard_done"):
                cfg["wizard_done"] = bool(fields.flag("wizard_done",
                                                      default=False))
            if fields.present("wizard_steps"):
                # explicit per-step marks for the strict wizard —
                # merge semantics so each step posts only its own state.
                # Handed whole to the loop below, which checks every mark.
                raw = fields.opaque(
                    "wizard_steps", blank_ok=True,
                    msg="wizard_steps must be an object") or {}
                steps = dict(cfg.get("wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("appearance", "connect", "sync", "import",
                                 "bills", "budgets", "email", "finish"):
                        raise HTTPException(400, f"unknown wizard step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["wizard_steps"] = steps
                # leaving the connect step with one checking account in:
                # the forecast's anchor is obvious, pin it now so the
                # Today checklist's step closes with the wizard's
                if raw.get("connect") in ("done", "skipped"):
                    pin_primary_after = True
            if fields.present("biz_wizard_steps"):
                # The BUSINESS setup wizard's per-step marks. Separate key from
                # wizard_steps on purpose: they are different flows with
                # different steps, and one tenant can have finished onboarding
                # while never having set up a business (or the reverse).
                #
                # Why persisted at all: completion held in React state is
                # forgotten on refresh — the wizard cannot resume, and it
                # prompts "continue setup" forever because nothing records
                # that it is done. Same merge semantics as above: each
                # step posts only its own mark, null forgets it.
                raw = fields.opaque(
                    "biz_wizard_steps", blank_ok=True,
                    msg="biz_wizard_steps must be an object") or {}
                steps = dict(cfg.get("biz_wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("what", "when", "accounts", "startup", "finish"):
                        raise HTTPException(400, f"unknown business step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["biz_wizard_steps"] = steps
            if fields.present("ret_wizard_steps"):
                # Retirement setup's per-step marks. Third flow, third key: a
                # tenant can finish onboarding, never run a business, and be
                # halfway through retirement planning — one shared key could not
                # express that. Same merge semantics as the two above.
                raw = fields.opaque(
                    "ret_wizard_steps", blank_ok=True,
                    msg="ret_wizard_steps must be an object") or {}
                steps = dict(cfg.get("ret_wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("age", "spend", "saving", "assumptions", "done"):
                        raise HTTPException(400, f"unknown retirement step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["ret_wizard_steps"] = steps
            if (fields.present("ret_wizard_answers")
                    or fields.present("ret_wizard_ssage")
                    or fields.present("ret_wizard_roth_mo")):
                # what was typed at each retirement step, so the wizard
                # resumes on another device with the person's numbers
                # rather than the defaults. Merged per key like the marks;
                # null forgets one answer.
                # one rule for this door and the restore, which bypasses it
                from ..sync.restore import (
                    RET_ANSWERS_MARK as _RET_MARK,
                    RET_WIZARD_ANSWERS as _RET_WIZARD_ANSWERS,
                    ret_marked_answers as _ret_marked,
                    ret_wizard_answer as _ret_wizard_answer,
                    ret_wizard_refusal as _ret_refusal)
                raw = dict(fields.opaque(
                    "ret_wizard_answers", blank_ok=True,
                    msg="ret_wizard_answers must be an object") or {})
                # The claim age, the Roth saving and the format mark ride BESIDE the
                # answers, as top-level fields an older server ignores: it
                # refuses a whole save over an answer key it does not know.
                # `ret_answers_v: 2` says a current client typed (or
                # confirmed) these, so they become inputs to the
                # projection; an older client's save (its prefilled boxes)
                # is neither an input nor shown again.
                if fields.present("ret_wizard_ssage"):
                    ss = fields.number(
                        "ret_wizard_ssage",
                        msg=_ret_refusal("ssage"))
                    raw["ssage"] = (int(ss) if ss is not None
                                    and ss.is_integer() else ss)
                # the Roth saving rides beside them for the same reason
                if fields.present("ret_wizard_roth_mo"):
                    ro = fields.number(
                        "ret_wizard_roth_mo", msg=_ret_refusal("roth_mo"))
                    raw["roth_mo"] = (int(ro) if ro is not None
                                      and ro.is_integer() else ro)
                fmt = fields.number("ret_answers_v",
                                    msg="ret_answers_v must be 2")
                if fmt is not None and fmt != 2:
                    raise HTTPException(400, "ret_answers_v must be 2")
                answers = dict(cfg.get("ret_wizard_answers") or {})
                marked = set(_ret_marked(answers))
                answers.pop(_RET_MARK, None)
                for k, v in raw.items():
                    # the marks the read shows inside the answers are this
                    # door's own record of which answers a current client
                    # saved; a body that sends them back (the document as
                    # read) changes nothing by it — `ret_answers_v` alone
                    # decides what a save marks
                    if k == _RET_MARK:
                        continue
                    if k not in _RET_WIZARD_ANSWERS:
                        raise HTTPException(
                            400, f"unknown retirement answer: {str(k)[:40]}")
                    if v is None or v == "":
                        answers.pop(k, None)
                        marked.discard(k)
                        continue
                    n = _ret_wizard_answer(k, v)
                    if n is None:
                        raise HTTPException(400, _ret_refusal(k))
                    # An older client re-sends what it pre-filled; the same
                    # value keeps the confirmation another device gave it.
                    same = answers.get(k) == n
                    answers[k] = n
                    if fmt == 2:
                        marked.add(k)
                    elif not same:
                        marked.discard(k)
                marked &= set(answers)
                if marked:
                    answers[_RET_MARK] = sorted(marked)
                if answers:
                    cfg["ret_wizard_answers"] = answers
                else:
                    cfg.pop("ret_wizard_answers", None)
            if fields.present("email_schedule"):
                # {daily:{on,hour}, monthly:{on,hour}, yearly:{on,hour}} —
                # hours LOCAL (OIKONOME_TZ). (The weekly report is
                # retired; a stored `weekly` entry is dropped
                # on the next save and ignored by the worker.)
                # Handed whole to the cadence reader below, which checks
                # each entry it uses.
                raw = fields.opaque("email_schedule",
                                    msg="email_schedule must be an object")
                if raw in (None, {}):
                    cfg.pop("email_schedule", None)   # legacy daily behavior
                else:
                    clean = {}
                    # A cadence the body leaves out keeps what the worker
                    # runs for it now (the effective schedule, so a
                    # household that never saved one keeps its default
                    # daily email): a client saving only the monthly card
                    # must not switch the daily email off by omission.
                    # Turning one off is an explicit {"on": false}.
                    from ..notify.schedule import effective_email_schedule
                    _was_sched = effective_email_schedule(cfg)
                    # each cadence (yearly included) carries
                    # per-channel flags — `on` stays email (back-compat),
                    # sms/push are additive
                    for cad in ("daily", "monthly", "yearly"):
                        if cad not in raw:
                            if isinstance(_was_sched.get(cad), dict):
                                raw = {**raw, cad: _was_sched[cad]}
                        c = raw.get(cad) or {}
                        if not isinstance(c, dict):
                            raise HTTPException(400, f"{cad} must be an object")
                        try:
                            hour = int(c.get("hour", 7))
                        except (TypeError, ValueError, OverflowError):
                            raise HTTPException(400, "hour must be a number")
                        if not 0 <= hour <= 23:
                            raise HTTPException(400, "hour must be 0-23")
                        entry = {"on": bool(c.get("on")), "hour": hour}
                        if c.get("sms"):
                            entry["sms"] = True
                        if c.get("push"):
                            entry["push"] = True
                        # daily only: the summary email — just the verdict
                        # pane, not the full report
                        if cad == "daily" and c.get("summary"):
                            entry["summary"] = True
                        clean[cad] = entry
                    cfg["email_schedule"] = clean
            if fields.present("merchant_logos"):
                if fields.flag("merchant_logos", default=True, blank_ok=True):
                    cfg.pop("merchant_logos", None)      # absent = on
                else:
                    cfg["merchant_logos"] = False
            if fields.present("plaid_recurring"):
                if fields.flag("plaid_recurring", default=True, blank_ok=True):
                    cfg.pop("plaid_recurring", None)     # absent = on
                else:
                    cfg["plaid_recurring"] = False
            if fields.present("feedback_enabled"):
                if fields.flag("feedback_enabled", default=True, blank_ok=True):
                    cfg.pop("feedback_enabled", None)   # absent = on
                else:
                    cfg["feedback_enabled"] = False
            if fields.present("timezone"):
                # An IANA name, or empty to follow the instance again. A
                # zone this host cannot load is refused rather than stored:
                # the sweep would silently fall back to the instance clock
                # and the page would keep promising an hour nobody keeps.
                from .. import localtime
                z = fields.text("timezone")
                # A client adopting the device's zone on its own says so.
                # That adoption is only right while the household still
                # follows the instance default: two phones in two places,
                # or a phone restoring an old snapshot, must not overwrite
                # a zone someone chose by hand in the meantime.
                if fields.flag("timezone_adopted") and localtime.valid_zone(
                        cfg.get("timezone")):
                    raise HTTPException(
                        409, "the household already keeps its own time zone")
                if not z:
                    cfg.pop("timezone", None)
                elif not localtime.valid_zone(z):
                    raise HTTPException(400, f"unknown time zone: {z[:40]}")
                else:
                    cfg["timezone"] = z
            # default home page per client — a closed vocabulary, because
            # the clients redirect to it blind at launch and an arbitrary
            # route would strand the app on a 404 every open
            from .. import homepages
            for key, allowed in homepages.VOCAB.items():
                if fields.present(key):
                    v = fields.text(key)
                    if not v or v in ("/", "index"):
                        cfg.pop(key, None)          # absent = Today
                    elif v not in allowed:
                        raise HTTPException(400, f"{key}: unknown page")
                    else:
                        cfg[key] = v
            if fields.present("theme"):
                # "auto" (follow the OS) is a choice of its own and is
                # stored as one: both clients read an absent theme as the
                # dark default, so storing auto as absence would turn it
                # back into dark on the next reconcile. Only an empty
                # value clears the key, back to that default.
                t = fields.one_of("theme", ("auto", "light", "dark"),
                                  msg="theme must be auto, light or dark")
                if not t:
                    cfg.pop("theme", None)
                else:
                    cfg["theme"] = t
            if fields.present("history_months"):
                # a whole number of months from the offered choices, or
                # null for everything. Strict: "6" or 6.5 is a client bug,
                # and guessing would quietly narrow what the next import
                # or bank link keeps.
                from ..sync.base import HISTORY_MONTHS, history_months_of
                _hm_msg = "history_months must be 1, 3, 6, 12 or null"
                # "" is the blank choice, everything, as null is
                hm = fields.int_in("history_months", blank_ok=True,
                                   msg=_hm_msg)
                if hm is not None and hm not in HISTORY_MONTHS:
                    raise HTTPException(400, _hm_msg)
                # owner-only and refused rather than dropped (see
                # permissions.OWNER_REFUSED_SETTINGS); a stored value outside
                # the choices reads as null, as the settings GET publishes it
                _refuse_member_change("history_months", hm,
                                      history_months_of(cfg))
                if hm is None:
                    cfg.pop("history_months", None)
                else:
                    cfg["history_months"] = hm
            if fields.present("bill_detection_auto"):
                _bda_msg = "bill_detection_auto must be true or false"
                v = fields.flag("bill_detection_auto", msg=_bda_msg)
                if v is None:                    # sent as null
                    raise HTTPException(400, _bda_msg)
                _refuse_member_change(
                    "bill_detection_auto", v,
                    cfg.get("bill_detection_auto") is not False)
                cfg["bill_detection_auto"] = v
            if fields.present("setup_mode"):
                sm = fields.one_of("setup_mode", ("auto", "manual"),
                                   msg="setup_mode must be auto or manual")
                was = _setup_mode_of(cfg)
                _refuse_member_change("setup_mode", sm or "auto", was)
                if not sm:
                    cfg.pop("setup_mode", None)       # absent = auto
                else:
                    cfg["setup_mode"] = sm
                    # someone who enters their bills by hand has said they
                    # do not want the finder's offers every morning; the
                    # body may still say otherwise in the same save. Only
                    # on the move to manual: a save echoing the stored
                    # choice must not undo detection the owner turned back
                    # on since
                    if sm == "manual" and was != "manual" \
                            and not fields.present("bill_detection_auto"):
                        cfg["bill_detection_auto"] = False
            if fields.present("today_view"):
                tv = fields.one_of(
                    "today_view", ("summary", "detail", "advanced", "simple"),
                    default="summary",
                    msg="today_view must be summary or detail")
                # advanced/simple are accepted as aliases
                tv = {"advanced": "detail", "simple": "summary"}.get(tv, tv)
                if tv not in ("summary", "detail"):
                    raise HTTPException(400,
                                        "today_view must be summary or detail")
                if tv == "summary":
                    cfg.pop("today_view", None)   # absent = the default face
                else:
                    cfg["today_view"] = tv
            if any(fields.present(k) for k in (
                    "smtp_host", "smtp_port", "smtp_user", "smtp_password",
                    "smtp_from", "smtp_starttls")):
                _no_smtp_byo_on_hosted()
                # web-configured email delivery. host anchors the
                # group — empty host clears everything (env fallback returns);
                # password is write-only and encrypted at rest, empty = keep.
                from ..db import crypto
                # a host sent as null or "" clears the relay; left out,
                # the stored one stands
                host = (fields.text("smtp_host")
                        if fields.present("smtp_host")
                        else str(cfg.get("smtp_host") or "").strip())
                if host:
                    # a tenant-supplied SMTP host is an SSRF primitive
                    # on hosted (probe internal nets on port 25/587) — same
                    # netguard policy the LLM URL gets. Self-host keeps LAN
                    # relays; only the metadata endpoint is blocked there.
                    # A body-supplied host was already resolved ABOVE
                    # the lock — only a stored legacy value re-checks here.
                    if not _sup_smtp:
                        from .netguard import BlockedURL, check_host
                        try:
                            check_host(host, what="the SMTP host")
                        except BlockedURL as e:
                            raise HTTPException(400, str(e))
                    cfg["smtp_host"] = host
                    # digits typed in a box are a port; null, "" and 0 are
                    # the submission port, as they always were
                    if fields.present("smtp_port"):
                        port = fields.int_in(
                            "smtp_port", text_ok=True, truncate=True,
                            msg="smtp_port must be a number") or 587
                    else:
                        try:
                            port = int(cfg.get("smtp_port") or 587)
                        except (TypeError, ValueError, OverflowError):
                            port = 587
                    if not 1 <= port <= 65535:
                        raise HTTPException(400, "smtp_port out of range")
                    cfg["smtp_port"] = port
                    from . import report as _report
                    if fields.present("smtp_user"):
                        cfg["smtp_user"] = _report.seal_relay_user(
                            conn, cfg.get("smtp_user"),
                            fields.text("smtp_user"))
                    if fields.present("smtp_from"):
                        cfg["smtp_from"] = fields.text("smtp_from")
                    if not cfg.get("smtp_from"):
                        # An empty From would send as a made-up local
                        # address that an authenticated relay refuses (the
                        # sender is not one the account owns) or the
                        # recipient's DMARC check drops, and the daily
                        # email would stop with nothing saying why. The
                        # relay account is almost always the mailbox, so it
                        # stands in, as the setup wizard already does.
                        # Without one the save is refused, after the
                        # step-up check below so an unproven session learns
                        # nothing from the order of the answers.
                        relay_user = _report.relay_user(conn, cfg)
                        if mailguard.valid_address(relay_user):
                            cfg["smtp_from"] = relay_user
                        elif not relay_user and (
                                default_from := _relay_default_from(host)):
                            # an unauthenticated LAN relay, which is all
                            # the setup wizard asks for: it accepts any
                            # sender, so a no-reply address on the
                            # instance's own name (or the relay's) is one
                            # it and the recipient can both read
                            cfg["smtp_from"] = default_from
                        else:
                            _smtp_from_missing = True
                            _smtp_user_given = bool(relay_user)
                    if fields.present("smtp_starttls"):
                        # `resolve_smtp` reads this key, so it needs a writer:
                        # without one, configuring SMTP in the UI silently forces
                        # STARTTLS and the documented OIKONOME_SMTP_STARTTLS
                        # escape hatch is unreachable — a LAN relay on plain 25
                        # reachable from docker/.env but not from the app that
                        # tells you to use the app.
                        cfg["smtp_starttls"] = bool(fields.flag(
                            "smtp_starttls", default=False))
                    # write-only: blank keeps the stored one; the reader
                    # names the field and never the value
                    _smtp_pw = fields.text("smtp_password")
                    if _smtp_pw:
                        cfg["smtp_password"] = crypto.encrypt(conn, _smtp_pw)
                else:
                    for k in ("smtp_host", "smtp_port", "smtp_user",
                              "smtp_password", "smtp_from", "smtp_starttls"):
                        cfg.pop(k, None)
            if fields.present("custom_buckets"):
                # full-list replace — the SPA edits the whole set and
                # saves it back, so add/edit/delete are all this one write;
                # an empty list, null or "" clears it
                lst = fields.opaque_rows("custom_buckets", _BODY_ROWS_MAX,
                                         blank_ok=True)
                if not lst:
                    cfg.pop("custom_buckets", None)
                else:
                    from ..engine.budget import RESERVED_BUCKET_NAMES
                    # The ONE settings list with no length cap.
                    # ~20k minimal buckets fit the 1 MB body limit, and every
                    # verdict computation then runs _claim_custom per spend
                    # row over 20k un-memoised matchers — quadratic, tenant-
                    # wide, from one save. 40 is far above any real plan
                    # (mailguard's neighbours cap at 20-30); a hard 400, not
                    # a silent slice, so the SPA can say why.
                    if len(lst) > 40:
                        raise HTTPException(
                            400, "too many budget categories (max 40)")
                    cleaned, seen = [], set()
                    for e in lst:
                        if not isinstance(e, dict):
                            raise HTTPException(400, "each bucket must be an object")
                        name = str(e.get("name") or "").strip()
                        if not name:
                            raise HTTPException(
                                400, "each bucket needs a name (1-40 chars)")
                        if len(name) > 40:
                            raise HTTPException(
                                400, f"bucket '{name[:40]}…': name must be "
                                     "1-40 characters")
                        if name.lower() in RESERVED_BUCKET_NAMES:
                            raise HTTPException(400, f"'{name}' is a reserved name")
                        if name.lower() in seen:
                            raise HTTPException(400, f"duplicate bucket '{name}'")
                        parent = e.get("parent")
                        if parent not in ("food", "other"):
                            raise HTTPException(
                                400, f"bucket '{name}': parent must be food or other")
                        cats = e.get("categories") or []
                        merch = e.get("merchants") or []
                        if not isinstance(cats, list) or not isinstance(merch, list):
                            raise HTTPException(
                                400, f"bucket '{name}': categories and merchants "
                                     "must be lists")
                        cats = [str(c).strip() for c in cats if str(c).strip()]
                        merch = [str(m).strip() for m in merch if str(m).strip()]
                        # Same cap discipline: bounded element counts and
                        # per-string lengths (matchers re-tokenise every one
                        # against every spend row)
                        if len(cats) > 40 or len(merch) > 60:
                            raise HTTPException(
                                400, f"bucket '{name}': too many matchers "
                                     "(max 40 categories / 60 merchants)")
                        if any(len(s) > 120 for s in cats + merch):
                            raise HTTPException(
                                400, f"bucket '{name}': matcher too long "
                                     "(max 120 chars)")
                        # a matcher-less bucket is a valid plan
                        # placeholder (the wizard's add-your-own row) — it
                        # claims nothing until categories/merchants are added
                        # in Settings, and its bar just shows $0 actual
                        seen.add(name.lower())
                        cleaned.append({
                            "name": name, "parent": parent,
                            "monthly": max(0.0, _num(e.get("monthly", 0),
                                                     f"bucket '{name}' monthly")),
                            "categories": cats, "merchants": merch})
                    cfg["custom_buckets"] = cleaned
            # A phone sends the one-address delta AND the whole list from
            # its copy, because a server older than the delta keys knows
            # only the list; here the delta wins, so the stale copy never
            # drops an address another device added. The delta wins only
            # when it actually names an address: older phones and web tabs
            # always send an empty `email_recipients_added` beside the whole
            # list, and for them the shortened list IS the edit — taking an
            # empty delta over it would silently keep the removed address
            _delta_keys = ("email_recipients_added", "email_recipients_removed")
            if fields.present("email_recipients") and any(
                    fields.text_or_list(
                        k, msg=f"{k} must be a list of addresses")
                    for k in _delta_keys):
                fields = fields.without("email_recipients")
            if fields.present("email_recipients"):
                # a list or one string of addresses; null clears. Any other
                # kind would reach parse_recipients' `.split`
                _recips_in = fields.text_or_list(
                    "email_recipients",
                    msg="email_recipients must be a list of addresses")
                recips = mailguard.parse_recipients(_recips_in)
                # parse_recipients CAPS the list — a defensive limit that
                # belongs there, since it also guards the invite fan-out.
                # Silently truncating would report success while the address
                # the person just typed was gone and still on their screen.
                # Say no instead of dropping it quietly.
                submitted = mailguard.parse_recipients(_recips_in, cap=None)
                if len(submitted) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(submitted)} recipients — the "
                             f"limit is {mailguard.MAX_RECIPIENTS}. Remove "
                             f"some and save again.")
                # hosted member-only recipients — shared with the
                # Jinja /settings/email door via mailguard so the
                # two paths can't drift.
                recips = _drop_stale_readds(
                    conn, cfg, _prev_recips, recips,
                    fields.text_or_list(
                        "email_recipients_added",
                        msg="email_recipients_added must be a list of "
                            "addresses"))
                mailguard.check_recipients(conn, recips)
                if recips:
                    cfg["email_recipients"] = recips
                else:
                    cfg.pop("email_recipients", None)
            else:
                _apply_recipient_deltas(conn, cfg, fields, user)
            if fields.present("email_muted") and any(
                    fields.text_or_list(
                        k, msg=f"{k} must be a list of addresses")
                    for k in ("email_muted_add", "email_muted_remove")):
                # same rule as the recipient list: a delta that names an
                # address wins over the fallback copy an older server would
                # have used; an empty one leaves the whole list in charge
                fields = fields.without("email_muted")
            if fields.present("email_muted"):
                # per-person opt-out from the scheduled mail, the owner
                # included. Stored lowercased — the worker filters
                # case-insensitively. No membership check on purpose: a
                # mute for an address that later leaves the list is inert,
                # never harmful.
                _muted_raw = fields.text_or_list(
                    "email_muted", msg="email_muted must be a list of addresses")
                # same cap-and-refuse as email_recipients above: without
                # a cap, one request could park
                # a ~1MB list in tenant config that every later
                # load_config — and every settings write's row-locked
                # before/after snapshot — then pays for. There is nothing
                # to mute beyond the recipient list, so the same limit is
                # the honest one.
                muted_in = mailguard.parse_recipients(_muted_raw, cap=None)
                if len(muted_in) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(muted_in)} muted addresses — "
                             f"the limit is {mailguard.MAX_RECIPIENTS}.")
                muted_in = sorted({m.lower() for m in muted_in})
                if muted_in:
                    cfg["email_muted"] = muted_in
                else:
                    cfg.pop("email_muted", None)
            # A toggle names the one address it changes instead of posting
            # the whole list: two members muting themselves from two phones,
            # or one phone working from a cached copy, must not un-mute
            # each other. Applied to the stored list inside this write.
            for key, add in (("email_muted_add", True),
                             ("email_muted_remove", False)):
                if not fields.present(key):
                    continue
                _msg = f"{key} must be a list of addresses"
                _sent = fields.text_or_list(key, msg=_msg)
                if _sent is None:
                    raise HTTPException(400, _msg)
                delta = {m.lower() for m in
                         mailguard.parse_recipients(_sent, cap=None)}
                # the shared demonstration login is shown only its own
                # address, so its own is the only mute it can mean to
                # change: anyone else's would silence (or un-silence) a
                # member it was never shown
                delta = set(demoguard.own_addresses_only(user, delta))
                have = {str(m).lower() for m in (cfg.get("email_muted") or [])}
                have = have | delta if add else have - delta
                if len(have) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(have)} muted addresses — "
                             f"the limit is {mailguard.MAX_RECIPIENTS}.")
                if have:
                    cfg["email_muted"] = sorted(have)
                else:
                    cfg.pop("email_muted", None)
            if fields.present("email_prefs_set"):
                # one {address, kind, on}, checked whole by the pref reader
                _apply_email_pref(cfg, fields.opaque(
                    "email_prefs_set",
                    msg="email_prefs_set must be {address, kind, on}"), user)
            if fields.present("email_send_hour_utc"):
                # digits, or a number cut to its whole hour, held to 0-23;
                # null and "" were never an hour
                _hour = fields.int_in("email_send_hour_utc", text_ok=True,
                                      truncate=True, msg="hour must be 0-23")
                if _hour is None:
                    raise HTTPException(400, "hour must be 0-23")
                # read after the schedule above: once one is saved the old
                # hour does nothing, and storing it would say it had
                from ..notify.schedule import legacy_hour_counts
                if legacy_hour_counts(cfg):
                    cfg["email_send_hour_utc"] = max(0, min(23, _hour))
            if fields.present("birthdate"):
                bd = fields.date(
                    "birthdate",
                    msg="birthdate must be an ISO date (YYYY-MM-DD)")
                if bd is None:
                    cfg.pop("birthdate", None)      # cleared → age falls back
                else:
                    if bd >= _today(conn):
                        raise HTTPException(400, "birthdate must be in the past")
                    cfg["birthdate"] = bd.isoformat()
            if fields.present("retirement_saving"):
                cfg["retirement_saving"] = bool(fields.flag(
                    "retirement_saving", default=False))
            for k in ("income_biweekly_saving", "income_biweekly_not_saving"):
                if fields.present(k):
                    v = fields.money_given(k)
                    if v > 0:
                        cfg[k] = round(v, 2)
                    else:
                        # empty/zero clears the scenario key; with either cleared,
                        # active_monthly_income falls back to the static
                        # budgeted_income_monthly knob (else that knob is dead)
                        cfg.pop(k, None)
            # named income scenarios (one active). Saving the list form
            # supersedes the legacy pair — drop it so there's one source.
            if fields.present("income_scenarios"):
                # the cleaner below keeps the first 20; "" or null is none
                scen_in = fields.opaque_rows(
                    "income_scenarios", _BODY_ROWS_MAX, blank_ok=True) or []
                scen, names = [], set()
                for s in scen_in[:20]:
                    if not isinstance(s, dict):
                        raise HTTPException(400, "each scenario must be an object")
                    name = str(s.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every scenario needs a name")
                    if name.lower() in names:
                        raise HTTPException(400, f"duplicate scenario '{name}'")
                    names.add(name.lower())
                    take = _num(s.get("take_home", 0), "take_home")
                    if take <= 0:
                        raise HTTPException(
                            400, f"scenario '{name}' needs a take-home > 0")
                    cadence = str(s.get("cadence") or "biweekly").strip().lower()
                    from ..engine.budget import CADENCE_MONTHLY
                    if cadence not in CADENCE_MONTHLY:
                        raise HTTPException(
                            400, "cadence must be one of "
                                 + ", ".join(sorted(CADENCE_MONTHLY)))
                    scen.append({"name": name, "take_home": round(take, 2),
                                 "cadence": cadence})
                if scen:
                    active = (_scalar_text(fields, "active_scenario")
                              or str(cfg.get("active_scenario") or "")
                              ).strip()
                    if active not in {s["name"] for s in scen}:
                        active = scen[0]["name"]
                    cfg["income_scenarios"], cfg["active_scenario"] = scen, active
                else:
                    for k in ("income_scenarios", "active_scenario"):
                        cfg.pop(k, None)
                for k in ("income_biweekly_saving", "income_biweekly_not_saving",
                          "retirement_saving"):
                    cfg.pop(k, None)
            elif fields.present("active_scenario"):
                from ..engine.budget import income_scenarios as _scen_fn
                scen, _ = _scen_fn(cfg)
                active = _scalar_text(fields, "active_scenario").strip()
                if active and active not in {s["name"] for s in scen}:
                    raise HTTPException(400, f"no scenario named '{active}'")
                if scen:
                    # switching persists the migrated list form as a side effect
                    cfg["income_scenarios"] = scen
                    cfg["active_scenario"] = active or scen[0]["name"]
                    for k in ("income_biweekly_saving",
                              "income_biweekly_not_saving", "retirement_saving"):
                        cfg.pop(k, None)
            # savings goals with targets (ledger-driven progress)
            if fields.present("savings_goals"):
                goals_in = fields.opaque_rows(
                    "savings_goals", _BODY_ROWS_MAX, blank_ok=True) or []
                out_goals, names = [], set()
                # the stored goals by name, for the match-words rule below:
                # both editors and the balance planner post the WHOLE list,
                # so an untouched legacy row rides along on every save
                stored_goals = {
                    str(sg.get("name") or "").strip().lower(): sg
                    for sg in (cfg.get("savings_goals") or [])
                    if isinstance(sg, dict)}
                for g in goals_in[:30]:
                    if not isinstance(g, dict):
                        raise HTTPException(400, "each goal must be an object")
                    name = str(g.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every goal needs a name")
                    if name.lower() in names:
                        raise HTTPException(400, f"duplicate goal '{name}'")
                    names.add(name.lower())
                    target = _num(g.get("target", 0), "target")
                    plan = round(max(0.0, _num(g.get("monthly_plan", 0),
                                               "monthly_plan")), 2)
                    # open-ended saving (a monthly plan with no
                    # target yet) is valid — the wizard's default Savings
                    # bucket starts this way.
                    # so is an explicit $0. The planner writes its
                    # goal row on every save, zero included, because the row's
                    # presence is what remembers "I'm not planning savings" —
                    # drop it and the next visit re-prefills the leftover. A
                    # BLANK field is still an unanswered one, so the form that
                    # sends "" keeps its error.
                    if target <= 0 and plan <= 0 and not _stated(
                            g.get("monthly_plan")):
                        raise HTTPException(
                            400, f"goal '{name}' needs a target or a "
                                 "monthly plan")
                    # contribution mode: 'monthly' (fixed commitment, the
                    # default) or 'sweep' (month-end, surplus-capped).
                    # 'monthly'/absent normalizes to no key so existing
                    # configs and old clients round-trip byte-identical.
                    mode = str(g.get("mode") or "").strip().lower()
                    if mode not in ("", "monthly", "sweep"):
                        raise HTTPException(
                            400, "mode must be 'monthly' or 'sweep'")
                    # the match words are a list: an object or a number
                    # cannot be sliced (a 500), and a bare string would be
                    # sliced into single letters that match everything
                    tokens_in = g.get("tokens") or []
                    if not isinstance(tokens_in, list):
                        raise HTTPException(
                            400, f"goal '{name}': tokens must be a list")
                    goal = {"name": name, "target": round(max(0.0, target), 2),
                            "monthly_plan": plan,
                            "account_id": str(g.get("account_id") or "") or None,
                            "tokens": [str(t).strip()[:60]
                                       for t in tokens_in[:8]
                                       if str(t).strip()],
                            "start_balance": round(
                                _num(g.get("start_balance", 0),
                                     "start_balance"), 2)}
                    # Match words with no account cannot be measured: they
                    # match both legs of a transfer and nothing says which
                    # side is the fund, so the goal would read as plan-only
                    # forever. Refuse the shape on the way in, except for a
                    # stored goal posted back with the same (absent) account
                    # and the same words: refusing that row would lock every
                    # later save of the list, the planner's included.
                    if goal["tokens"] and goal["account_id"] is None:
                        was = stored_goals.get(name.lower())
                        was_tokens = ([str(t).strip()[:60]
                                       for t in (was.get("tokens") or [])[:8]
                                       if str(t).strip()]
                                      if was and isinstance(
                                          was.get("tokens"), list) else None)
                        if not (was and not was.get("account_id")
                                and was_tokens == goal["tokens"]):
                            raise HTTPException(
                                400, f"goal '{name}': match words need an "
                                     "account — pick the account the money "
                                     "lands in, or clear the match words")
                    if mode == "sweep":
                        goal["mode"] = "sweep"
                    td = str(g.get("target_date") or "").strip()
                    if td:
                        goal["target_date"] = _iso_date(
                            td, "target_date must be YYYY-MM-DD").isoformat()
                    out_goals.append(goal)
                if out_goals:
                    cfg["savings_goals"] = out_goals
                else:
                    cfg.pop("savings_goals", None)
                    cfg.pop("savings_goal_state", None)
            if fields.present("manual_assets"):
                # property/vehicle/mortgage entries edited on the Net
                # Worth page. An entry keeps only what the net-worth report
                # reads: nothing revalues an asset on its own, so any other
                # key would be stored unchecked. A value edit arrives with
                # as_of cleared and gets stamped today.
                # the cleaner keeps the first 30; "" or null is none
                assets_in = fields.opaque_rows(
                    "manual_assets", _BODY_ROWS_MAX, blank_ok=True) or []
                # acquired_on is the month the item enters the net-worth
                # trend: the owner's "owned since", else the day it was
                # first recorded. A value edit restamps as_of and must not
                # move it, so a row sent without one keeps the stored
                # row's of the same name (a client that predates the field
                # never sends it), and a new row is acquired today.
                from ..engine.compat import asset_day
                stored_acq = {}
                for was in cfg.get("manual_assets") or []:
                    if not isinstance(was, dict):
                        continue
                    key = str(was.get("name") or "").strip()[:60].lower()
                    for k in ("acquired_on", "as_of"):
                        try:
                            day = asset_day(was[k]) if was.get(k) else None
                        except (TypeError, ValueError):
                            day = None
                        if day is not None:
                            stored_acq.setdefault(key, day)
                            break
                out_assets = []
                for a in assets_in[:30]:
                    if not isinstance(a, dict):
                        raise HTTPException(400, "each asset must be an object")
                    name = str(a.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every asset needs a name")
                    # as_of is read back as a date by the net-worth report,
                    # so a string that is not one
                    # must stop here as a 400 rather than surface there later
                    as_of = _today(conn)
                    if a.get("as_of"):
                        try:
                            as_of = asset_day(a["as_of"])
                        except (TypeError, ValueError):
                            raise HTTPException(
                                400, "asset as_of must be YYYY-MM-DD")
                    acquired = stored_acq.get(name.lower(), _today(conn))
                    if a.get("acquired_on"):
                        try:
                            acquired = asset_day(a["acquired_on"])
                        except (TypeError, ValueError):
                            raise HTTPException(
                                400, "asset acquired_on must be YYYY-MM-DD")
                    entry = {"name": name,
                             "kind": str(a.get("kind") or "other").strip()[:30],
                             "value": round(_num(a.get("value", 0), "value"), 2),
                             "as_of": as_of.isoformat(),
                             "acquired_on": acquired.isoformat()}
                    out_assets.append(entry)
                if out_assets:
                    cfg["manual_assets"] = out_assets
                else:
                    cfg.pop("manual_assets", None)
            if fields.present("ss_estimates"):
                est = fields.opaque(
                    "ss_estimates", blank_ok=True,
                    msg="ss_estimates must be an object {age: monthly $}")
                if not est:
                    cfg.pop("ss_estimates", None)
                else:
                    sched = {}
                    for age_k, amt in est.items():
                        try:
                            a = int(age_k)
                        except (TypeError, ValueError):
                            raise HTTPException(400,
                                                "ss_estimates ages must be integers")
                        if not 62 <= a <= 70:
                            raise HTTPException(400,
                                                "ss_estimates ages must be 62-70")
                        sched[str(a)] = max(0.0, _num(amt, f"ss_estimates[{a}]"))
                    cfg["ss_estimates"] = sched     # JSON keys are strings
            # The three investment keys are owner settings, like every
            # other knob in the Connections section (the bank-connection
            # ones are owner-only on the server too). Refused rather than
            # dropped: no client shows these to a member, so only a direct
            # call sends them, and a silent drop would answer it "saved".
            _invest_keys = ("advisory_fees", "advisory_fee_exempt_masks",
                            "investment_basis_override")
            if user.get("role") != "owner" and any(
                    fields.present(k) or fields.present(k + "_base")
                    for k in _invest_keys):
                raise HTTPException(403, "owner only")

            def _invest_base_check(key: str) -> None:
                """Each section replaces its stored key whole, so a tab
                that loaded the section before another device changed it
                would put back its stale copy of the rows it never
                touched. A save that names the section as it was read
                (`<key>_base`) is refused when the stored section has
                moved since; a save without a base replaces as before."""
                if not fields.present(key + "_base"):
                    return
                _msg = f"{key}_base must be the section as last read"
                # the section as it was read: an object, a list, or null
                base = (fields.opaque_rows(key + "_base", _BODY_ROWS_MAX,
                                           msg=_msg)
                        if fields.json_type(key + "_base") == "list"
                        else fields.opaque(key + "_base", msg=_msg))

                def _norm(v):
                    if not v:
                        return None
                    if isinstance(v, list):
                        return sorted(str(x) for x in v)
                    if isinstance(v, dict):
                        return {str(k): v[k] for k in v}
                    return v
                if _norm(base) != _norm(cfg.get(key)):
                    raise HTTPException(
                        409, "These settings were changed on another device "
                             "since this page loaded. The current ones are "
                             "shown now — make your change again.")

            for _k in _invest_keys:
                if fields.present(_k):
                    _invest_base_check(_k)
            if fields.present("advisory_fees"):
                # {institution-name fragment: yearly rate as a fraction}.
                # The fee estimate matches the fragment against institution
                # names with LIKE, so it is stored lowercase and trimmed —
                # a blank fragment would match every institution and
                # charge the rate on the whole portfolio. A rate is a
                # fraction (0.0025 = 0.25%); one above 1 is a percentage
                # typed into the wrong box and would estimate a fee larger
                # than the balance, so it is refused rather than guessed at.
                fees_in = fields.opaque(
                    "advisory_fees", blank_ok=True,
                    msg="advisory_fees must be an object "
                        "{institution: yearly rate}")
                if not fees_in:
                    cfg.pop("advisory_fees", None)
                else:
                    if len(fees_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many advisory fees")
                    fees_out = {}
                    for frag, rate in fees_in.items():
                        frag = str(frag or "").strip().lower()
                        if not frag:
                            raise HTTPException(
                                400, "every advisory fee needs an institution")
                        if len(frag) > 60:
                            raise HTTPException(
                                400, "advisory fee institution is too long")
                        if not _stated(rate):
                            raise HTTPException(
                                400, f"advisory fee for {frag} needs a rate")
                        r = _num(rate, "advisory fee rate")
                        if not 0 <= r <= 1:
                            raise HTTPException(
                                400, "advisory fee rate must be a fraction "
                                     "between 0 and 1 (0.0025 = 0.25%)")
                        fees_out[frag] = r
                    if fees_out:
                        cfg["advisory_fees"] = fees_out
                    else:
                        cfg.pop("advisory_fees", None)
            if fields.present("advisory_fee_exempt_masks"):
                # the last characters of self-directed accounts at a
                # fee-charging institution. Compared exactly against the
                # account's mask, so anything that is not a mask could never
                # match and would leave the account silently charged.
                # a typed "1234, 5678" or a list of masks
                masks_in = fields.text_or_list(
                    "advisory_fee_exempt_masks",
                    msg="advisory_fee_exempt_masks must be a list of "
                        "account masks")
                if isinstance(masks_in, str):
                    # separators at either end split off empty pieces that
                    # are not masks and must not count against the cap
                    masks_in = [p for p in re.split(r"[,;\s]+", masks_in)
                                if p]
                if not masks_in:
                    cfg.pop("advisory_fee_exempt_masks", None)
                else:
                    # refused before the scan, like the other two maps: a
                    # body near the size limit carries a six-figure list,
                    # and checking it entry by entry would hold the
                    # household's config row locked while it ran
                    if len(masks_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many exempt accounts")
                    # dict.fromkeys drops repeats in one pass and keeps the
                    # order they were typed in
                    masks_out = list(dict.fromkeys(
                        m for m in (_invest_mask(x, "advisory_fee_exempt_masks")
                                    for x in masks_in) if m))
                    if masks_out:
                        cfg["advisory_fee_exempt_masks"] = masks_out
                    else:
                        cfg.pop("advisory_fee_exempt_masks", None)
            if fields.present("investment_basis_override"):
                # {account mask: dollars actually put in} — replaces the
                # feed's cost basis where in-kind transfers arrived with a
                # near-zero one. Money in cannot be negative; zero is a
                # real answer (an account funded only by transfers in).
                bo_in = fields.opaque(
                    "investment_basis_override", blank_ok=True,
                    msg="investment_basis_override must be an object "
                        "{account mask: dollars}")
                if not bo_in:
                    cfg.pop("investment_basis_override", None)
                else:
                    if len(bo_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many basis overrides")
                    bo_out = {}
                    for m, amt in bo_in.items():
                        m = _invest_mask(m, "investment_basis_override")
                        if not m:
                            raise HTTPException(
                                400, "every basis override needs an "
                                     "account mask")
                        if not _stated(amt):
                            raise HTTPException(
                                400, f"basis override for {m} needs an amount")
                        v = _num(amt, "basis override")
                        if v < 0:
                            raise HTTPException(
                                400, "basis override must not be negative")
                        bo_out[m] = round(v, 2)
                    if bo_out:
                        cfg["investment_basis_override"] = bo_out
                    else:
                        cfg.pop("investment_basis_override", None)
            if any(fields.present(k)
                   for k in ("llm_url", "llm_model", "llm_api_key")):
                # smart-categorization LLM backend — same rules as the setup
                # wizard: URL+model together, key optional (encrypted at rest,
                # never echoed or logged); empty llm_url clears all three.

                from ..db import crypto
                # left out, the stored value stands; null or "" clears it
                url = (fields.text("llm_url") if fields.present("llm_url")
                       else str(cfg.get("llm_url") or "").strip())
                url = _keep_masked_llm_url(url, cfg)
                model = (fields.text("llm_model")
                         if fields.present("llm_model")
                         else str(cfg.get("llm_model") or "").strip())
                if url:
                    # Body-supplied URLs resolved above the lock;
                    # only a stored legacy value re-checks here
                    if not _sup_llm:
                        from . import netguard
                        try:
                            netguard.check_url(url, what="llm_url")
                        except netguard.BlockedURL as e:
                            raise HTTPException(400, str(e))
                    if not model:
                        raise HTTPException(400, "llm_model is required with llm_url")
                    cfg["llm_url"], cfg["llm_model"] = url, model
                    # optional dedicated vision model for receipts —
                    # the chat model is often text-only
                    if fields.present("llm_vision_model"):
                        vm = fields.text("llm_vision_model")
                        if vm:
                            cfg["llm_vision_model"] = vm
                        else:
                            cfg.pop("llm_vision_model", None)
                    # extra_body is arbitrary JSON merged into every request
                    # to this endpoint, so it can carry credentials — GET
                    # reports presence only, like the api_key below. Masking
                    # forces the per-backend twin's contract: "" (or an
                    # omitted key) KEEPS the stored value and an explicit
                    # JSON null clears it, because a masked "" is all a
                    # client has to send back — were that a clear, every save
                    # from a client that didn't retype the JSON would
                    # silently wipe it.
                    # the JSON object's text, secret-bearing like the key:
                    # read without ever echoing it; blank keeps the stored
                    # one, null clears it
                    _eb_in = fields.text("llm_extra_body")
                    if fields.json_type("llm_extra_body") == "null":
                        cfg.pop("llm_extra_body", None)   # explicit null
                    elif _eb_in:
                        eb = _eb_in
                        import json as _json
                        # must parse to an OBJECT: the value is merged
                        # into each request with dict.update, so "42"
                        # or "[1,2]" would pass a loads-only check and then
                        # crash every call using the backend
                        try:
                            _eb = _json.loads(eb)
                        except ValueError:
                            _eb = None
                        if not isinstance(_eb, dict):
                            raise HTTPException(
                                400,
                                "llm_extra_body must be a JSON object")
                        # encrypted at rest like the api_key beside it —
                        # same credential, same envelope (untagged rows
                        # pass through decrypt unchanged)
                        cfg["llm_extra_body"] = crypto.encrypt(conn, eb)
                else:
                    # no endpoint (given or stored): reject stray fields, else
                    # treat as an explicit clear of the whole group
                    if (fields.text("llm_model")
                            or fields.text("llm_api_key")):
                        raise HTTPException(400, "llm_url is required with "
                                            "llm_model / llm_api_key")
                    for k in ("llm_url", "llm_model", "llm_api_key",
                              "llm_extra_body", "llm_vision_model"):
                        cfg.pop(k, None)
                if url and fields.present("llm_api_key"):
                    # same keep-the-stored-value contract as the
                    # per-backend twin and llm_extra_body above: the key is
                    # masked on GET, so "" is all a client that didn't
                    # retype it can send back — were "" a clear, every
                    # round-trip save of the AI card would silently wipe
                    # the credential. Explicit JSON null clears.
                    # write-only; the reader names the field, never the value
                    key = fields.text("llm_api_key")
                    if fields.json_type("llm_api_key") == "null":
                        cfg.pop("llm_api_key", None)
                    else:
                        if key:
                            cfg["llm_api_key"] = crypto.encrypt(conn, key)
            # ---- multi-backend AI (per-task routing) -------------------
            # llm_backends is a full-list REPLACE. An entry that omits
            # api_key keeps its stored (encrypted) key; api_key:"" clears
            # it. The legacy single-endpoint keys fold in as entry id
            # "legacy" the first time the list carries one, inheriting the
            # stored key — nothing is lost crossing over. URLs were all
            # guarded above the lock (_sup_bk).
            _folded = False
            if fields.present("llm_backends"):
                from ..db import crypto as _crypto
                # typed and bounded above the lock; each entry is cleaned
                # below, field by field
                raw_bk = _bk_in or []
                prior_bk = {str(b.get("id")): b
                            for b in (cfg.get("llm_backends") or [])
                            if isinstance(b, dict)}
                _fold_legacy = bool(cfg.get("llm_url")) and "legacy" not in prior_bk
                import uuid as _uuid
                # one tenant-key unwrap for the whole list — per-entry
                # crypto.encrypt would round-trip the DB for every backend
                # while the settings row lock is held
                _enc_key = (_crypto.encryptor(conn)
                            if (_fold_legacy
                                or any(isinstance(e, dict)
                                       and (e.get("api_key")
                                            or e.get("extra_body"))
                                       for e in raw_bk)) else None)
                if _fold_legacy:
                    # the fold-in inherits the stored extra_body alongside
                    # the key — since GET masks both, the client cannot
                    # resend them; omission means "keep". An untagged (plaintext)
                    # value is encrypted as it crosses over, because
                    # from here on nothing ever rewrites it (decrypt passes
                    # untagged values through, so it would keep working —
                    # and keep sitting in the clear in every DB dump).
                    prior_bk["legacy"] = {
                        k: (v if v.startswith(_crypto.PREFIX) else _enc_key(v))
                        for k, v in (("api_key",
                                      str(cfg.get("llm_api_key") or "")),
                                     ("extra_body",
                                      str(cfg.get("llm_extra_body") or "")))
                        if v}
                entries, seen_bk = [], set()
                for e in raw_bk:
                    if not isinstance(e, dict):
                        raise HTTPException(
                            400, "each llm backend must be an object")
                    bid = (str(e.get("id") or "").strip()
                           or _uuid.uuid4().hex[:8])
                    if bid == "bundled" or bid in seen_bk:
                        raise HTTPException(
                            400, "llm_backends: reserved or duplicate id")
                    seen_bk.add(bid)
                    b_url = _keep_masked_llm_url(
                        str(e.get("url") or "").strip(), cfg)
                    b_model = str(e.get("model") or "").strip()
                    if not b_url or not b_model:
                        raise HTTPException(
                            400, "every llm backend needs a url and a model")
                    ent = {"id": bid,
                           "name": (str(e.get("name") or "").strip()[:40]
                                    or "Backend"),
                           "url": b_url, "model": b_model}
                    vm = str(e.get("vision_model") or "").strip()
                    if vm:
                        ent["vision_model"] = vm
                    # extra_body is arbitrary JSON merged into every request
                    # to the backend — it can carry credentials (an
                    # auth-in-body vendor, an org token), so GET masks it
                    # like api_key and this write path mirrors api_key's
                    # keep-the-stored-value contract. One asymmetry, forced
                    # by the masking: clients round-trip the masked ""
                    # from GET, so "" (or an omitted key) KEEPS the stored
                    # value and an explicit JSON null clears it — were ""
                    # a clear, every save from a client that didn't retype
                    # the JSON would silently wipe it.
                    if "extra_body" in e and e.get("extra_body") is None:
                        pass                     # explicit null → cleared
                    elif str(e.get("extra_body") or "").strip():
                        eb = str(e["extra_body"]).strip()
                        import json as _json
                        # an OBJECT, not just JSON — same reason as the
                        # legacy twin above: it is dict.update'd into each
                        # request body at call time
                        try:
                            _eb = _json.loads(eb)
                        except ValueError:
                            _eb = None
                        if not isinstance(_eb, dict):
                            raise HTTPException(
                                400,
                                "llm backend extra_body must be a JSON object")
                        # encrypted at rest like api_key — it can carry a
                        # credential, and the export scrub drops it for the
                        # same reason (untagged legacy rows pass through
                        # decrypt unchanged)
                        ent["extra_body"] = _enc_key(eb)
                    elif prior_bk.get(bid, {}).get("extra_body"):
                        ent["extra_body"] = prior_bk[bid]["extra_body"]
                    if "api_key" in e:
                        k = str(e.get("api_key") or "").strip()
                        if k:
                            ent["api_key"] = _enc_key(k)
                    elif prior_bk.get(bid, {}).get("api_key"):
                        ent["api_key"] = prior_bk[bid]["api_key"]
                    entries.append(ent)
                if entries:
                    cfg["llm_backends"] = entries
                else:
                    cfg.pop("llm_backends", None)
                # the fold-in retires the legacy keys so one endpoint never
                # lives in two places (the resolver would prefer the routed
                # entry anyway; the leftover would only mislead)
                _folded = (_fold_legacy
                           and any(e["id"] == "legacy" for e in entries))
                if any(e["id"] == "legacy" for e in entries):
                    for k in ("llm_url", "llm_model", "llm_api_key",
                              "llm_extra_body", "llm_vision_model"):
                        cfg.pop(k, None)
                # roles pointing at removed entries unroute themselves
                kept = {k: v for k, v in (cfg.get("llm_roles") or {}).items()
                        if v == "bundled" or v in seen_bk}
                if kept:
                    cfg["llm_roles"] = kept
                else:
                    cfg.pop("llm_roles", None)
            if fields.present("llm_roles"):
                from ..engine.llm_categorize import ROLES as _LLM_ROLES
                raw_r = fields.opaque("llm_roles", blank_ok=True,
                                      msg="llm_roles must be an object") or {}
                ids_now = {str(b.get("id"))
                           for b in (cfg.get("llm_backends") or [])
                           if isinstance(b, dict)}
                from ..engine.llm_categorize import (
                    bundled_available as _bundled_available)
                bundled_ok = _bundled_available()
                stored_r = cfg.get("llm_roles") or {}
                roles_out = {}
                for k, v in raw_r.items():
                    if k not in _LLM_ROLES:
                        raise HTTPException(400, f"unknown llm role {k!r}")
                    v = str(v or "").strip()
                    if not v:
                        continue                 # explicit unroute
                    if v == "bundled":
                        if not bundled_ok and stored_r.get(k) == "bundled":
                            continue             # stale route: dropped
                        if not bundled_ok:
                            raise HTTPException(
                                400, "no bundled backend on this instance")
                    elif v not in ids_now:
                        raise HTTPException(
                            400, f"llm_roles[{k}] names an unknown backend")
                    roles_out[k] = v
                if roles_out:
                    cfg["llm_roles"] = roles_out
                else:
                    cfg.pop("llm_roles", None)
            # Nothing is lost crossing over: before the fold, every task
            # without a live route resolved to the legacy endpoint
            # (llm_categorize.resolve_role). The fold retires the keys that
            # resolution read, so each such task is routed to the folded
            # entry explicitly — otherwise the clients' first AI-card save,
            # which sends the roles it read (none), silently moves every
            # task to the operator's backend or to nothing. Only on the save
            # that folds: afterwards an unrouted task means what it says.
            if _folded:
                from ..engine.llm_categorize import ROLES as _LLM_ROLES
                _live = {str(b.get("id")) for b in (cfg.get("llm_backends")
                                                    or [])
                         if isinstance(b, dict)}
                _routes = dict(cfg.get("llm_roles") or {})
                for _r in _LLM_ROLES:
                    if _routes.get(_r) != "bundled" and _routes.get(_r) not in _live:
                        _routes[_r] = "legacy"
                cfg["llm_roles"] = _routes
            # standalone vision-model save. The AI settings card pairs a
            # tenant-picked vision model with the BUNDLED endpoint and
            # sends {llm_vision_model} alone — were the write path only
            # inside the single-endpoint llm_url block above, a
            # bundled-Ollama install could not set a vision model at all.
            # When any of those keys travels with it, the block above
            # already decided its fate (set with the group, or cleared
            # with it). After the llm_backends block on purpose: an
            # explicit value in this save outlives the legacy fold-in's
            # retirement of the key. Empty string clears.
            if (fields.present("llm_vision_model")
                    and not any(fields.present(k) for k in
                                ("llm_url", "llm_model", "llm_api_key"))):
                vm = fields.text("llm_vision_model")
                if vm:
                    cfg["llm_vision_model"] = vm
                else:
                    cfg.pop("llm_vision_model", None)
            # "yes, this endpoint may receive a document with my SSN on it".
            # The tax-document parser refuses a remote model without this and
            # names the setting in the error it raises, so it has to be
            # settable — an instruction pointing at a knob with no write path
            # is a dead end, not a fix. Both clients send it with the AI
            # card's payload (backends + roles) and never with the
            # single-endpoint llm_url group. Unconditional, unlike the vision
            # model above: consent is about WHERE the document goes, so
            # nothing in that group's set-or-clear decides its fate.
            if fields.present("taxdocs_allow_remote_llm"):
                if _yes_word(fields, "taxdocs_allow_remote_llm"):
                    cfg["taxdocs_allow_remote_llm"] = "1"
                else:
                    cfg.pop("taxdocs_allow_remote_llm", None)
            # Pointing mail, the AI endpoint or the daily report somewhere
            # new is a step-up act, like adding a webhook or changing the
            # account email: with a cookie alone, a thief could route the
            # owner's reset link through their own relay and take the
            # account. Checked last, inside the lock, against what the
            # save actually stored, so the refusal rolls every field of
            # this save back and a re-save of unchanged values never asks.
            if _outbound_repointed(conn, _cfg_was, cfg):
                # the shared demonstration login's step-up proves nothing
                # (its password is handed out, its factor waived), so it
                # cannot point the household's mail or data anywhere new
                demoguard.deny_shared_grant(user)
                from .app import _require_elevation
                _require_elevation(
                    user, password=_proof.get("password", ""),
                    totp_code=_proof.get("totp_code", ""),
                    recovery_code=_proof.get("recovery_code", ""))
            # The shared demonstration login is refused the one-off test
            # text and push; switching the schedule's texts or pushes on
            # would make that same send recurring, so it is refused here
            # too. Switching them off stays open — it only takes a send
            # away.
            if demoguard.is_shared_login(user) and \
                    _scheduled_channels_switched_on(_cfg_was, cfg):
                demoguard.deny_shared_grant(user)
            if _smtp_from_missing:
                raise HTTPException(
                    400, "smtp_from: enter the From address mail is sent "
                         "as — the relay username is not an email address, "
                         "so it cannot stand in" if _smtp_user_given else
                         "smtp_from: enter the From address mail is sent as")
        # OUTSIDE the lock and after it commits: sending mail while
        # holding a row lock would hold it for the length of an SMTP
        # conversation, and an invite must only go out once the save it
        # describes is durable.
        # an address added to the list is INVITED, not enrolled.
        # After the save, never before — mailing someone about a change that
        # then 400s on the next field would be a promise the app didn't keep.
        # The invite is also what starts the mail: until it's accepted,
        # `worker._recipients_raw` skips the address entirely.
        # (the saved-a-plan mark rides a budget key's save and is not a
        # setting a person changed by itself)
        _changed_keys = sorted(k for k in set(_cfg_was) | set(cfg)
                               if _cfg_was.get(k) != cfg.get(k)
                               and k != budget.SAVED_BY_USER_KEY)
        if _changed_keys:
            _activity.record(conn, user, "settings", "changed",
                             detail={"keys": _changed_keys})
        # Where the AI tasks send the household's text decides who reads
        # every transaction from now on, and a stolen session that repoints
        # it leaves no trace on any page — so the account hears of it, after
        # the save committed. Only the owner can change these keys (they are
        # dropped from anyone else's body), so the acting account IS the
        # owner. Judged on the resolved routes (the restore judges its
        # merge by the same rule), so a masked echo or a re-posted list
        # sends nothing.
        from ..sync.restore import ai_route_moved
        if ai_route_moved(conn, _cfg_was, cfg):
            from .app import _notify_security_change
            _notify_security_change(user, "ai_route_changed")
        # a display-form entry's consent moves to the bare mailbox the
        # save stored it as, BEFORE the invite diff, so nobody who already
        # said yes is asked again; a joined entry's mailboxes were never
        # asked one by one, so the diff invites them
        if _prev_entries and any(fields.present(k) for k in (
                "email_recipients", "email_recipients_added",
                "email_recipients_removed")):
            from ..notify import recipient_invites as _ri
            _ri.carry_acceptance(user["tenant_id"], _prev_entries)
        added = mailguard.added_recipients(_prev_asked,
                                           cfg.get("email_recipients"))
        if pin_primary_after:
            from ..engine import forecast
            if forecast.auto_pin_primary_checking(conn):
                cfg = budget.load_config(conn)
        view = _settings_view(settings_exposure.readable(conn, cfg))
        if added:
            mailguard.invite_added(user, added)
        reach = _email_reach(conn, user, cfg)
    finally:
        conn.close()
    # built once the save's connection is back in the pool, as on the read
    view["email_recipient_status"] = _status_rows(user, cfg)
    if reach is not None:
        view["email_reach"] = reach
    return settings_exposure.view_for(user.get("role"),
                                      _hide_people(view, user))


@router.post("/settings/recipients/resend",
             dependencies=[Depends(limit("recipient_invite_resend", 10, 3600))])
@documented(response=RecipientResent, body=RecipientResend)
def settings_recipient_resend(user: dict = Depends(_user()),
                              body: Fields = Depends(json_fields)):
    """Send the invite again — the answer to "they never got it".

    Rotates the token (migration 074 keeps one row per address, so the old
    link stops working by construction) and re-sends. Refuses an address
    that isn't on the saved list, so this can't be used as a way to mail
    arbitrary people from someone else's instance."""
    demoguard.deny(user)
    from ..engine import budget
    email = body.text("email", msg="email must be an address")
    if not email:
        raise HTTPException(400, "email is required")
    if demoguard.own_address_only(user, email) != email:
        # the shared demonstration login may re-invite only itself: an
        # answer for any other address would say whether it is on the list
        raise HTTPException(400, "that address is not a saved recipient")
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        # a mailbox inside a stored `Name <a>` / `a;b` entry is on the list
        # (the sender mails it), so it can be re-invited like any other
        if email.lower() not in mailguard.stored_mailboxes(
                cfg.get("email_recipients"))[0]:
            raise HTTPException(400, "that address is not a saved recipient")
        # On hosted, being ON the saved list is not enough — a stored config
        # (a restore, or one saved before the instance was hosted) can hold a
        # free-form external address, and this door would then make the
        # platform mail invite links (naming the inviter's account) to an
        # arbitrary stranger the
        # worker's own membership filter would never actually enrol. Same
        # member-only check the save and restore doors already enforce.
        mailguard.check_recipients(conn, [email])
    finally:
        conn.close()
    # an owner may sit on a list stored before additions were checked, and
    # is still mailed by construction: there is no invitation to resend
    if email.lower() in mailguard.owner_addresses(user["tenant_id"]):
        raise HTTPException(400, "That's you — as the owner you already "
                                 "receive this mail.")
    # The answer says whether mail actually went out. The cooldown and the
    # hourly cap stop a send without any error, and a bare `ok` let both
    # clients tell the owner an invitation was on its way when nothing
    # was. `ok` stays true (the request was valid and nothing failed);
    # `sent` is the truth, and `reason` names why when it is false.
    from ..notify import recipient_invites as _ri
    try:
        res = _ri.resend(user["tenant_id"], email,
                         invited_by=user.get("user_id"),
                         inviter=user.get("email"))
    except Exception:                                    # noqa: BLE001
        import logging
        logging.getLogger("oikonome.api").exception(
            "recipient invite resend failed for %s", email)
        res = None
    if not isinstance(res, dict):
        res = {"sent": False, "reason": "error"}
    out = {"ok": True, "sent": bool(res.get("sent"))}
    if not out["sent"]:
        reason = str(res.get("reason") or "").strip()
        out["reason"] = reason if reason in _RESEND_REASONS else "held"
    out["email_recipient_status"] = _status_rows(user, cfg)
    return _hide_people(out, user)


# The words a not-sent resend may carry. `cooldown` and `cap` are the two
# throttles; the rest say the address's own state stopped it (answered,
# refused, or undeliverable) or the instance cannot send. Anything else
# collapses to `held`, so a client keyed on these words never meets an
# unknown one.
_RESEND_REASONS = frozenset({
    "cooldown", "cap", "held", "accepted", "declined", "complained",
    "blocked", "undelivered", "no_mail", "invalid", "error"})


@router.post("/settings/outbound/resume",
             dependencies=[Depends(limit("outbound-resume", 10, 3600))])
@documented(response=OutboundResumed, body=OutboundResume)
def settings_outbound_resume(user: dict = Depends(_user()),
                             body: Fields = Depends(_json_fields_or_empty)):
    """The owner confirmed the relay, the extra recipients and the AI
    endpoint a credential rotation paused are theirs: the senders use them
    again. Owner only — the pause exists to protect the owner's account.
    Nothing to confirm is not an error.

    Resuming re-opens the same destinations that typing them in fresh
    opens (`_outbound_repointed`), so it is guarded the same way and the
    same way a paused webhook is switched back on: the shared
    demonstration login may not do it, the caller steps up, and the write
    runs under the factor lock after a re-check that the caller is still
    signed in. Without that last part a request that authenticated a
    moment before a reset would wait on the settings row and lift the
    pause the reset had just written, from a session the reset evicted."""
    demoguard.deny(user)
    if user.get("script_token") or user["role"] != "owner":
        raise HTTPException(403, "owner only")
    demoguard.deny_shared_grant(user)
    from .app import _require_elevation, _still_signed_in_under_factor_lock
    # the step-up proof read as it always was: the text of any scalar
    _require_elevation(user, **{
        k: _scalar_text(body, k)
        for k in ("password", "totp_code", "recovery_code")})
    # the banner sends back the fingerprint of the record it showed, a
    # string; left out or null, the pause lifts unconditionally
    version = (None if body.json_type("version") in (None, "null")
               else body.text_or_int("version", default="", strip=False,
                                     msg="version must be a string"))
    from ..notify import outbound_pause as _outbound
    conn = _conn(user)
    try:
        # a fingerprint that has changed since is refused, and the client
        # reloads
        with _still_signed_in_under_factor_lock(user):
            try:
                lifted = _outbound.resume(
                    conn, None if version is None else str(version))
            except _outbound.StaleConfirmation:
                raise HTTPException(
                    409, "what is paused changed since this page loaded — "
                         "reload Settings and check it again")
    finally:
        conn.close()
    return {"ok": True, "resumed": lifted}


@router.post("/settings/smtp-test",
             dependencies=[Depends(limit("smtp-test", 5, 3600))])
@documented(response=SmtpTested)
def settings_smtp_test(user: dict = Depends(_user())):
    """Prove delivery works — a test email to EVERY recipient on the
    list, through the same resolution and fan-out the real senders use.

    Mailing only the signed-in user makes the button actively misleading
    for the thing people use it for: "did adding this person work?" A test
    that never touches the address you just added cannot answer that, and it
    would report success while a second recipient silently received nothing.

    Per-recipient results are returned so a partial failure is visible
    instead of averaging into one green tick.
"""
    demoguard.deny(user)
    from ..jobs.worker import _recipients_and_raw
    from ..engine import budget
    from . import report
    conn = _conn(user)
    try:
        smtp = report.resolve_smtp(conn)
        recipients, raw = _recipients_and_raw(conn, user["tenant_id"])
        _cfg = budget.load_config(conn)
        configured = list(_cfg.get("email_recipients") or [])
    finally:
        conn.close()
    from ..notify import outbound_pause as _outbound
    held = _outbound.paused(_cfg) or {}
    # While a credential rotation holds the Settings relay, resolve_smtp
    # answers with the instance's own relay or with nothing, so a test
    # would either refuse as though the form were empty or prove a relay
    # the form does not show. Say what is actually going on instead.
    if held.get("smtp") and _cfg.get("smtp_host"):
        raise HTTPException(
            400, "your mail relay is paused since your password or second "
                 "factor changed — check it below, then press “These are "
                 "mine — resume” at the top of Settings and test again")
    held_recipients = set(held.get("recipients") or [])
    # the shared demonstration login tests delivery to itself only: the
    # answer names every recipient, and the mail would reach members it is
    # never shown
    recipients = demoguard.own_addresses_only(user, recipients)
    configured = demoguard.own_addresses_only(user, configured)
    # Who is on the list but will NOT be mailed, and why. `_recipients`
    # filters to accepted invites (and, hosted, to members) — correctly —
    # but the button exists to answer "did adding this person work?", so a
    # green tick that says nothing about them is not an answer.
    mailed = {e.lower() for e in recipients}
    delivered_raw = {e.lower() for e in raw}
    # a paused recipient is left out of `raw` too, so it is named before
    # the invitation reason could claim it
    skipped = [
        {"email": e,
         "reason": ("paused since your password, login email or second factor changed "
                    "— confirm on the banner at the top of Settings"
                    if e.strip().lower() in held_recipients
                    else "delivery is failing to this address"
                    if e.lower() in delivered_raw
                    else "they have not accepted their invitation yet")}
        for e in configured if e.lower() not in mailed]
    if not smtp["configured"]:
        raise HTTPException(400, "no SMTP configured — fill the form above "
                                 "(or OIKONOME_SMTP_* in docker/.env)")
    if not recipients:
        raise HTTPException(400, "no recipients configured")
    results = report.send_each(
        "Oikonome test email",
        "It works — this instance can send email.",
        "<p>It works — this instance can send email.</p>",
        recipients, smtp=smtp)
    ok = [r["email"] for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    if failed and not ok:
        _via = ("the host relay" if env_flag("OIKONOME_HOSTED")
                else smtp["host"])
        raise HTTPException(
            400, f"send failed via {_via}: {failed[0]['error']}")
    return {"ok": not failed, "sent_to": ok, "skipped": skipped,
            "failed": [{"email": r["email"], "error": r["error"]}
                       for r in failed],
            # Hosted withholds the operator relay's hostname —
            # the same rule _settings_view already applies
            "via": ("host-managed" if env_flag("OIKONOME_HOSTED")
                    else smtp["host"])}
