"""The household's OUTBOUND settings pause when the owner's credential
rotates, until the owner confirms them.

A hijacked owner session can repoint where the household's data goes
without leaving a session behind: the mail relay saved in Settings (every
later reset link goes through it), extra daily-mail recipients, the AI
endpoint that reads transaction text. Every other foothold a hijacker
plants — sessions, passkeys, tokens, webhooks, the SMS number — is evicted
or paused by a credential rotation; these three were not, so a password
reset kept mailing through the intruder's relay.

So an OWNER's rotation (password change, email change, TOTP turned on
or off, the first passkey enrolled — the posture changes that also evict
the other sessions) and recovery (an emailed reset, a cleared set of
factors) record what was set under `outbound_paused` in tenant config,
and the senders stand down on those channels until the owner presses
Resume in Settings: the relay falls back to the operator's env relay
(or to a logged link), the listed recipients are left off every send,
the AI endpoint falls back to the operator's. Nothing is deleted — the
owner's own settings are one click from working again, and the banner
says what was paused and when, so a relay or an address they do not
recognise is caught at that moment. Pausing until the owner confirms
beats logging the reset: a log line stops nothing.

An owner's recovery or credential rotation also removes the members that
owner's recent invites created (auth/reset). They are named on the same
record, so the banner that asks the owner to confirm the settings also says
who was removed and that the ones the owner recognises can be invited again.
Their addresses come off every mail list at the same moment, so pressing
Resume never mails them.

Stored shape, written by this module only:
  {"at": iso, "reason": "rotation"|"recovery",
   "smtp": bool, "recipients": [addresses], "llm": bool,
   "aggregator": bool, "aggregator_clients": {"plaid"|"mx": client_id},
   "removed_members": [{"email", "role", "joined": iso}]}
A missing or unreadable value reads as not paused — the value is ours,
and a channel the record cannot name cannot be paused by name.

`aggregator` is the bank-link hold. The household's own Plaid or MX client
decides who receives the bank logins the owner types into Link or the MX
widget, so a hijacker who saved their own client there would keep
receiving every new bank login after the owner recovers. The hold refuses
only the doors that hand logins out (a new link, a re-auth, the MX
widget: see `links_held`); it never changes which credentials resolve,
because an access token or MX user works only with the client that made
it — standing the keys down would fail every sync of the banks already
linked and mint new ones on the wrong account. Syncs keep running: they
read with a token already held and send the client nothing new.

The hold names the clients it was armed over (`aggregator_clients`), and
holds a provider only while that same client is stored: keys the owner
saves after the record was armed (a step-up door, under the factor lock)
are not the keys the record named — a different client by its id, the
same client saved again by its later `<provider>_keys_saved_at` stamp. A
record from before the clients were named holds whichever client is
stored. Clearing the keys lifts that provider's hold by itself, and the
clear doors drop the provider from the record (`drop_stale_hold`), and the
record with it once it names nothing; the Settings view names the held clients
(`aggregator_keys`, derived at read time, ids only) so `version` moves
when they change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

log = logging.getLogger("oikonome.outbound")

KEY = "outbound_paused"
ROTATION = "rotation"
RECOVERY = "recovery"


PROVIDERS = ("plaid", "mx")
_LABEL = {"plaid": "Plaid", "mx": "MX"}


def _held(raw, cfg: dict, provider: str) -> bool:
    """Whether the raw record holds new links for `provider` under `cfg`."""
    from ..envnum import env_flag
    if not isinstance(raw, dict) or not raw.get("aggregator"):
        return False
    if env_flag("OIKONOME_HOSTED"):
        return False                     # hosted never stores these keys
    cid = str((cfg or {}).get(f"{provider}_client_id") or "")
    if not cid:
        return False                     # the keys are gone: nothing to hold
    if _saved_after(cfg.get(f"{provider}_keys_saved_at"), raw.get("at")):
        return False                     # saved since: not the keys it named
    named = raw.get("aggregator_clients")
    if isinstance(named, dict):
        return str(named.get(provider) or "") == cid
    return True                          # a record from before the names


def _saved_after(saved_at, armed_at) -> bool:
    """Whether keys stamped `saved_at` were saved after a record armed at
    `armed_at`. Every door that stores keys stamps them, under the factor
    lock the arming runs under, so a later stamp means the owner saved them
    after the rotation: the same client id saved again after a clear (or
    after a swap away and back) is the owner's, not the client the record
    named. A missing or unreadable stamp proves nothing either way."""
    try:
        saved = dt.datetime.fromisoformat(str(saved_at))
        armed = dt.datetime.fromisoformat(str(armed_at))
        return saved > armed
    except (TypeError, ValueError):
        return False


def links_held(cfg: dict, provider: str) -> bool:
    """True while new bank links through `provider` ("plaid" or "mx") are
    on hold: an armed record names the household's own client for it, that
    client is still the one stored, and this is not a hosted instance.
    The one predicate every link door reads."""
    return _held((cfg or {}).get(KEY), cfg or {}, provider)


def hold_message(cfg: dict, provider: str) -> str:
    """The sentence a held door answers with."""
    raw = (cfg or {}).get(KEY)
    why = ("your account was recovered"
           if isinstance(raw, dict) and raw.get("reason") == RECOVERY
           else "your password, login email or second factor changed")
    label = _LABEL.get(provider, provider)
    return (f"New bank links are on hold since {why}: confirm the {label} "
            f"keys saved in Settings are yours (Settings, top banner), or "
            f"disconnect their banks and remove the keys.")


def refuse_if_held(cfg: dict, provider: str) -> None:
    """Raise ValueError with the hold's sentence when `provider` is held.
    Every link door maps ValueError to a 400 or a shown message already."""
    if links_held(cfg, provider):
        raise ValueError(hold_message(cfg, provider))


def paused(cfg: dict) -> dict | None:
    """The validated record, or None when nothing is paused."""
    raw = (cfg or {}).get(KEY)
    if not isinstance(raw, dict):
        return None
    recips = raw.get("recipients")
    recips = ([str(a).strip().lower() for a in recips if str(a).strip()]
              if isinstance(recips, list) else [])
    out = {"at": str(raw.get("at") or ""),
           "reason": RECOVERY if raw.get("reason") == RECOVERY else ROTATION,
           "smtp": bool(raw.get("smtp")), "recipients": recips,
           "llm": bool(raw.get("llm")),
           "aggregator": False}
    # the held clients, named from the live config: client ids are not
    # secrets (Settings shows them to the owner), and nothing else of the
    # keys is ever copied here
    held = [p for p in PROVIDERS if _held(raw, cfg, p)]
    if held:
        out["aggregator"] = True
        out["aggregator_keys"] = [
            {"provider": p, "client_id": str(cfg.get(f"{p}_client_id")),
             "env": str(cfg.get(f"{p}_env") or "production"),
             "saved_at": (str(cfg.get(f"{p}_keys_saved_at"))
                          if cfg.get(f"{p}_keys_saved_at") else None)}
            for p in held]
    removed = _removed_members(raw.get("removed_members"))
    if removed:
        out["removed_members"] = removed
    if not (out["smtp"] or out["recipients"] or out["llm"]
            or out["aggregator"] or removed):
        return None
    out["version"] = _version(out)
    return out


def drop_stale_hold(cfg: dict) -> None:
    """Settle the record against the keys stored now, in place: a provider
    whose named client is no longer the one stored (cleared, or swapped
    for another) comes off `aggregator_clients`, the hold goes when no
    client is left to name, and a record that then names nothing is
    removed. Called by the doors that clear keys, inside their settings
    transaction, and before anything merges into the record.

    Reading such a record already shows nothing held, but the raw record
    is what a later write merges into: left in place, a rotation that
    removed a member would show the old record's date and reason, and the
    client it named would be held again the moment the owner saved that
    same client id. A record from before the clients were named is
    pinned to the clients it holds now, so clearing one provider never
    leaves the other provider's next client held by a record that never
    saw it."""
    raw = cfg.get(KEY)
    if not isinstance(raw, dict):
        return
    raw = dict(raw)
    named = ({p: str(cfg.get(f"{p}_client_id")) for p in PROVIDERS
              if _held(raw, cfg, p)} if raw.get("aggregator") else {})
    if named:
        raw["aggregator_clients"] = named
    else:
        raw["aggregator"] = False
        raw.pop("aggregator_clients", None)
    cfg[KEY] = raw
    if paused(cfg) is None:
        cfg.pop(KEY, None)


def _version(record: dict) -> str:
    """A fingerprint of everything the banner shows. A restore widening
    the record, or a second rotation or recovery adding to it, keeps or
    rewrites `at` as it pleases, but always changes this — so Resume can
    tell that the record the owner confirmed is no longer the one stored."""
    body = {k: v for k, v in record.items() if k != "version"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()
                          ).hexdigest()[:20]


class StaleConfirmation(Exception):
    """Resume named a record that has changed since the owner saw it."""


def _removed_members(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        if isinstance(m, dict) and str(m.get("email") or "").strip():
            out.append({"email": str(m["email"]).strip().lower(),
                        "role": str(m.get("role") or ""),
                        "joined": str(m.get("joined") or "")})
    return out


def _what_is_set(cfg: dict, owner_email: str) -> dict:
    """What a rotation has to pause: the tenant relay (self-host only —
    hosted mail is always the operator's), every configured recipient
    other than the owner's own address, and a tenant AI endpoint (a
    legacy URL or a routed named backend)."""
    from ..envnum import env_flag
    from ..web import mailguard
    smtp = bool(cfg.get("smtp_host")) and not env_flag("OIKONOME_HOSTED")
    boxes, _ = mailguard.split_mailboxes(
        cfg.get("email_recipients") if isinstance(cfg.get("email_recipients"), list)
        else [cfg.get("email_recipients")] if cfg.get("email_recipients") else [])
    me = (owner_email or "").strip().lower()
    recipients = sorted({b.lower() for b in boxes if b.lower() != me})
    routed = any(str(v) and str(v) != "bundled"
                 for v in (cfg.get("llm_roles") or {}).values()
                 if isinstance(cfg.get("llm_roles"), dict))
    llm = bool(cfg.get("llm_url")) or routed
    # The household's own Plaid or MX client receives the bank logins of
    # every new link, so a rotation holds new links on it (the bank-link
    # hold, see the module docstring). The keys keep serving existing
    # connections: switching a live client out from under its linked Items
    # would strand them or mint new ones on the wrong account. Hosted never
    # stores these.
    aggregator = (bool(cfg.get("plaid_client_id") or cfg.get("mx_client_id"))
                  and not env_flag("OIKONOME_HOSTED"))
    return {"smtp": smtp, "recipients": recipients, "llm": llm,
            "aggregator": aggregator}


def pause_for_rotation(conn, user_id, reason: str = ROTATION) -> dict | None:
    """Record the pause for the OWNER's household; a member's rotation
    does nothing (only an owner can set these). Returns what was paused,
    or None when nothing was set. Runs on a control-plane or admin
    connection like the SMS pause: the tenant is pinned for this
    transaction only and the row is named by tenant id too."""
    u = conn.execute("SELECT tenant_id, email FROM users "
                     "WHERE id=%s AND role='owner'", (user_id,)).fetchone()
    if not u:
        return None
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                     (str(u["tenant_id"]),))
        row = conn.execute("SELECT config FROM tenant_settings "
                           "WHERE tenant_id=%s FOR UPDATE",
                           (u["tenant_id"],)).fetchone()
        cfg = row["config"] if row else None
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = None
        if not isinstance(cfg, dict):
            return None
        what = _what_is_set(cfg, u["email"] or "")
        # the clients the hold is armed over: whatever is stored now (a
        # client saved later by the owner is not one the record names)
        clients = ({p: str(cfg[f"{p}_client_id"]) for p in PROVIDERS
                    if cfg.get(f"{p}_client_id")}
                   if what["aggregator"] else {})
        prior = paused(cfg)
        if prior:
            # a second rotation widens what is paused, never narrows it
            what = {"smtp": what["smtp"] or prior["smtp"],
                    "recipients": sorted(set(what["recipients"])
                                         | set(prior["recipients"])),
                    "llm": what["llm"] or prior["llm"],
                    "aggregator": what["aggregator"] or prior["aggregator"]}
            if prior.get("removed_members"):
                # the owner has not yet seen who an earlier recovery removed
                what["removed_members"] = prior["removed_members"]
        if what["aggregator"]:
            what["aggregator_clients"] = clients
        if not (what["smtp"] or what["recipients"] or what["llm"]
                or what["aggregator"]):
            return None
        record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "reason": RECOVERY if reason == RECOVERY else ROTATION,
                  **what}
        conn.execute(
            "UPDATE tenant_settings SET config = jsonb_set(config, %s, %s) "
            "WHERE tenant_id=%s",
            (["outbound_paused"], json.dumps(record), u["tenant_id"]))
    log.info("outbound settings paused for tenant %s (%s): smtp=%s "
             "recipients=%d llm=%s bank-link hold=%s", u["tenant_id"],
             record["reason"], record["smtp"], len(record["recipients"]),
             record["llm"], record["aggregator"])
    return record


def _without_mailbox(lst, mine: str) -> tuple[list, bool]:
    """A stored mail list minus one mailbox, and whether it named it. A
    legacy joined entry (`Name <a>`, `a;b`) is the mailboxes it names, as
    the sender splits it, and keeps its other mailboxes."""
    from ..web import mailguard
    lst = lst or []
    if isinstance(lst, str):
        lst = [lst]
    kept, hit = [], False
    for m in lst:
        boxes, bad = mailguard.split_mailboxes([m])
        if not any(b.strip().lower() == mine for b in boxes):
            kept.append(m)
            continue
        hit = True
        kept.extend(b for b in boxes if b.strip().lower() != mine)
        kept.extend(bad)
    return kept, hit


def note_removed_members(conn, owner_id, members: list[dict],
                         reason: str = RECOVERY) -> None:
    """Members an owner's recovery (or credential rotation, `reason`) is
    removing: off every mail list, off the paused recipients, and named on
    the record for the banner. `reason` labels a record this call creates;
    one already armed keeps its own, so a rotation that removed a member
    is never shown as a recovery and a recovery is never relabelled. Inside
    the caller's transaction, on the control-plane or admin connection the
    recovery runs on, and BEFORE their users rows are deleted — the
    settings row is taken ahead of any users row, the order every
    account-security door keeps.

    The extra-recipient list keeps its meaning when it empties: a
    configured list is "the owner plus these", an absent one is everyone
    verified in the household, so an emptied list becomes the owners' own
    addresses rather than widening (the roster removal does the same)."""
    if not members:
        return
    from ..web import api as _api
    from . import email_prefs
    u = conn.execute("SELECT tenant_id FROM users WHERE id=%s "
                     "AND role='owner'", (owner_id,)).fetchone()
    if not u:
        return
    tid = u["tenant_id"]
    gone = {str(m["email"]).strip().lower() for m in members}
    owners = {str(r["email"]).strip().lower() for r in conn.execute(
        "SELECT email FROM users WHERE tenant_id=%s AND role='owner'",
        (tid,)).fetchall()} - gone
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                     (str(tid),))
        # a household that never saved a setting still gets the banner
        conn.execute("INSERT INTO tenant_settings (tenant_id, config) "
                     "VALUES (%s, '{}'::jsonb) "
                     "ON CONFLICT (tenant_id) DO NOTHING", (tid,))
        row = conn.execute("SELECT config FROM tenant_settings "
                           "WHERE tenant_id=%s FOR UPDATE", (tid,)).fetchone()
        cfg = row["config"] if row else None
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = None
        if not isinstance(cfg, dict):
            return
        for mine in sorted(gone):
            kept, hit = _without_mailbox(cfg.get("email_recipients"), mine)
            if hit:
                kept = kept or sorted(owners)
                # with no owner to fall back on, an emptied list would
                # widen, so the entry stays (the pause below still holds it)
                if kept:
                    cfg["email_recipients"] = kept
            if "email_muted" in cfg:
                cfg["email_muted"], _ = _without_mailbox(
                    cfg.get("email_muted"), mine)
            email_prefs.without(cfg, mine)
            # a settings page loaded before this cannot save them back on
            _api.remember_removed_address(cfg, mine)
        # a record that names nothing any more (its keys were cleared) is
        # not one to merge into: this change gets its own date and reason
        drop_stale_hold(cfg)
        rec = cfg.get(KEY) if isinstance(cfg.get(KEY), dict) else {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": RECOVERY if reason == RECOVERY else ROTATION,
            "smtp": False, "recipients": [], "llm": False}
        rec = {**rec, "recipients": [
            a for a in (rec.get("recipients") or [])
            if str(a).strip().lower() not in gone]}
        rec["removed_members"] = _removed_members(
            rec.get("removed_members")) + [
            {"email": str(m["email"]).strip().lower(),
             "role": str(m.get("role") or ""),
             "joined": str(m.get("joined") or "")} for m in members]
        cfg[KEY] = rec
        conn.execute("UPDATE tenant_settings SET config = %s "
                     "WHERE tenant_id=%s", (json.dumps(cfg), tid))


def resume(conn, version: str | None = None) -> bool:
    """The owner confirmed the settings are theirs: lift the pause.
    Tenant-scoped connection; returns whether anything was paused. The
    door calling this owes a step-up and runs it under the factor lock
    (web/api.settings_outbound_resume): lifting the pause re-opens the
    same destinations typing them in fresh would.

    `version` is the fingerprint of the record the owner's banner showed.
    When it is given and the stored record has changed since — a restore
    widened it, a later recovery added to it — nothing is lifted and
    StaleConfirmation is raised, so a destination the owner never saw is
    never confirmed by a click on an older banner."""
    from ..engine import budget
    with budget.config_txn(conn) as cfg:
        if KEY not in cfg:
            return False
        if version is not None:
            now = paused(cfg)
            if (now or {}).get("version") != str(version):
                raise StaleConfirmation()
        cfg.pop(KEY, None)
    return True
