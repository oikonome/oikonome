"""Who may read each settings key, and what leaves the instance in an export.

The settings read is one door carrying sixty-odd keys to every member of a
household, viewers included, and the export ZIP carries the stored document
out of the instance. A credential can sit under a key not named like one
(some relays take their API token as the SMTP username), so what a key may
show is declared here, never inferred from where it sits in the code.

So the rule is one table. Every key the settings read can return, and every
key the save takes that the read never echoes, is classified here:

  * ``read`` — the least role that sees the key as stored: ``"viewer"``
    (everyone in the household), ``"owner"``, or ``"never"`` (write-only:
    the read must not carry it at all).
  * ``below`` — what a role under ``read`` gets instead: ``"drop"`` (the key
    is absent) or ``"set"`` (the key is absent and ``<key>_set`` says
    whether a value is stored). Dropped rather than blanked, because both
    clients post the settings document back on save, and a blank reads as
    "cleared".
  * ``url`` — the value is an endpoint URL (or a list/dict carrying them);
    any password in its userinfo or credential-named query value is masked
    for EVERY role, the owner included (the save restores the stored value
    when the masked form comes back unchanged).
  * ``export`` — whether the stored value rides the export ZIP. The deep
    scrub (``sync.restore.scrub_config``) already drops credential-NAMED
    keys; ``export=False`` here covers credentials the name rule cannot
    see.
  * ``sealed`` — the value is stored encrypted under the household's key
    and read decrypted by the roles ``read`` names (``readable``, on the
    stored document before the view is built).
  * ``door`` — the key points somewhere the household's mail or data goes
    (relay, recipients, AI endpoint, the consent to send a tax form to a
    remote model). These are the keys a demo instance must never take,
    whatever else is ever relaxed there.

``test_settings_exposure_table.py`` fails when the read grows a key this
table does not classify, so the next one is decided rather than inherited.
"""

from __future__ import annotations

from typing import NamedTuple


class Exposure(NamedTuple):
    read: str = "viewer"      # least role that sees it: viewer|owner|never
    below: str = "drop"       # what a lesser role gets: drop|set
    url: bool = False         # endpoint URL(s): credentials masked for all
    export: bool = True       # rides the export ZIP (after the deep scrub)
    door: bool = False        # where mail/data is sent: demo never writes it
    sealed: bool = False      # stored encrypted, read decrypted


_ALL = Exposure()
_ROLE_RANK = {"viewer": 0, "member": 1, "owner": 2, "never": 3}

TABLE: dict[str, Exposure] = {
    # ---- the household's money and plan: everyone in the household reads
    # everything (web/permissions.py), so these go to every role as stored
    **{k: _ALL for k in (
        "food_monthly", "other_monthly", "custom_buckets",
        "budgeted_income_monthly", "dynamic_variable_budget",
        "checking_account_id", "birthdate", "ss_estimates",
        "retirement_saving", "income_biweekly_saving",
        "income_biweekly_not_saving", "excluded_accounts",
        "occurrence_caps", "disabled_bills", "dismissed_hints",
        "paycheck_payers", "manual_assets", "theme", "home_web", "home_mobile", "timezone",
        "today_view", "feedback_enabled", "merchant_logos",
        "advisory_fees", "advisory_fee_exempt_masks",
        "investment_basis_override", "history_months", "setup_mode",
        "bill_detection_auto", "biz_wizard_steps", "ret_wizard_steps",
        "income_scenarios", "active_scenario", "savings_goals",
        "outbound_paused", "demo_read_only",
        "ret_wizard_answers")},
    # ---- the household's setup, the owner's to read as well as to write
    # where its mail goes and when, which AI endpoint
    # reads its transactions, which bank-connection keys and billed
    # cross-checks it runs on. Both clients show these to the owner only,
    # and a member or a viewer is told what they need through their own
    # keys (email_list_configured, email_on_list, the *_set booleans).
    # `timezone` stays readable by everyone: every screen dates by it.
    "email_schedule": Exposure(read="owner"),
    # who each cadence reaches (notify/reach), derived on every read
    "email_reach": Exposure(read="owner", export=False),
    "email_send_hour_utc": Exposure(read="owner"),
    "plaid_recurring": Exposure(read="owner"),
    "plaid_enrich_cap": Exposure(read="owner"),
    # the version of each whole-list setting as read, for the save's
    # compare-and-swap; derived from the stored lists, never stored itself
    "list_versions": Exposure(export=False),
    # ---- who gets the mail. The recipient list is the household's own
    # roster of addresses; the mute list and per-address status are cut to
    # the caller's own row for anyone but the owner by the settings door's
    # people filter (api._hide_people), which owns that rule.
    "email_recipients": Exposure(read="owner", door=True),
    "email_muted": Exposure(read="owner"),
    "email_recipient_status": Exposure(export=False),
    # what a non-owner is told instead of the roster: whether a list is
    # configured, and whether it names their own mailbox (derived per
    # caller by api._hide_people, never stored)
    "email_list_configured": Exposure(export=False),
    "email_on_list": Exposure(export=False),
    "email_recipients_added": Exposure(read="never", door=True,
                                       export=False),
    "email_recipients_removed": Exposure(read="never", door=True,
                                         export=False),
    # ---- the mail relay. smtp_user is a credential whatever its name says:
    # Postmark takes the server API token as both the username and the
    # password (notify/delivery reads it as the token). Owner only, the
    # others learn that one is set; and it never rides the export, like the
    # password beside it, and is stored encrypted like it.
    "smtp_host": Exposure(read="owner", door=True),
    "smtp_port": Exposure(read="owner", door=True),
    "smtp_user": Exposure(read="owner", below="set", door=True,
                          export=False, sealed=True),
    "smtp_from": Exposure(read="owner", door=True),
    "smtp_starttls": Exposure(read="owner", door=True),
    "smtp_password": Exposure(read="never", door=True, export=False),
    "smtp_password_set": _ALL,
    "smtp_user_set": _ALL,           # what a non-owner gets for smtp_user
    # ---- the AI endpoint. URLs are masked for every role (a password in
    # the userinfo is a credential like the api key); the key and the extra
    # body are write-only and leave as presence flags.
    "llm_url": Exposure(read="owner", url=True, door=True),
    "llm_model": Exposure(read="owner", door=True),
    "llm_vision_model": Exposure(read="owner", door=True),
    "llm_backends": Exposure(read="owner", url=True, door=True),
    "llm_roles": Exposure(read="owner", door=True),
    # which endpoint and model actually answer each task, the operator's
    # bundled one included: the same setup, read through the resolver
    "llm_bundled": Exposure(read="owner", url=True, export=False),
    "llm_effective": Exposure(read="owner", url=True, export=False),
    "llm_api_key": Exposure(read="never", door=True, export=False),
    "llm_api_key_set": _ALL,
    # echoed as "" with a presence flag beside it (the declared shape)
    "llm_extra_body": Exposure(read="owner", door=True, export=False),
    "llm_extra_body_set": _ALL,
    "taxdocs_allow_remote_llm": Exposure(read="owner", door=True),
    # whether the trained classifier categorizes before the AI endpoint:
    # read only by the owner's AI card
    "categorizer_active": Exposure(read="owner"),
    # ---- the household's own aggregator keys. A client id is an
    # identifier, not a secret, and the Connections card shows which keys
    # are set; the secrets are write-only.
    "plaid_client_id": Exposure(read="owner"),
    "plaid_env": Exposure(read="owner"),
    "plaid_secret": Exposure(read="never", export=False),
    "plaid_secret_set": _ALL,
    "mx_client_id": Exposure(read="owner"),
    "mx_env": Exposure(read="owner"),
    "mx_api_key": Exposure(read="never", export=False),
    "mx_api_key_set": _ALL,
}

# The keys a demo instance must never take, even were its blanket
# read-only rule ever relaxed.
DEMO_DOOR_KEYS = frozenset(k for k, e in TABLE.items() if e.door)

# Stored keys that must not leave in an export although no credential-name
# rule would catch them.
NOT_EXPORTED = frozenset(k for k, e in TABLE.items() if not e.export)

# Stored encrypted, shown decrypted to the roles that may read them.
SEALED = frozenset(k for k, e in TABLE.items()
                   if e.sealed and e.read != "never")


def readable(conn, cfg: dict) -> dict:
    """A copy of the stored settings document with every SEALED key
    decrypted (`conn` is the household's), for building the settings view.
    A value that will not decrypt (the key it was sealed under is gone) is
    left out rather than failing the whole read; a client posting the
    document back then leaves the stored value as it is."""
    from ..db import crypto
    out = dict(cfg)
    for k in SEALED:
        if out.get(k):
            try:
                out[k] = crypto.decrypt(conn, out[k])
            except Exception:                            # noqa: BLE001
                out.pop(k)
    return out


def _rank(role) -> int:
    # an unknown role reads as a viewer: least privilege, as permissions.py
    return _ROLE_RANK.get(role if role in ("owner", "member") else "viewer")


def _mask_urls(v):
    from ..engine.llm_categorize import redact_url
    if isinstance(v, str):
        return redact_url(v)
    if isinstance(v, list):
        return [_mask_urls(x) for x in v]
    if isinstance(v, dict):
        return {k: (redact_url(x) if k == "url" and isinstance(x, str)
                    else _mask_urls(x) if isinstance(x, (dict, list)) else x)
                for k, x in v.items()}
    return v


def view_for(role: str, view: dict) -> dict:
    """The settings view (api._settings_view's output) as `role` may see
    it. Unclassified keys are withheld from everyone but the owner: a key
    nobody decided about is not a key a viewer gets by default."""
    have = _rank(role)
    out: dict = {}
    for k, v in view.items():
        e = TABLE.get(k)
        if e is None:
            if have >= _ROLE_RANK["owner"]:
                out[k] = v
            continue
        if e.read == "never":
            continue
        if have < _ROLE_RANK[e.read]:
            if e.below == "set":
                out[f"{k}_set"] = bool(v)
            continue
        out[k] = _mask_urls(v) if e.url else v
    return out


def export_view(cfg: dict) -> dict:
    """The stored settings document as the export ZIP carries it: the deep
    credential scrub, then the credentials no key-name rule can see."""
    from ..sync.restore import scrub_config
    out = scrub_config(cfg)
    for k in NOT_EXPORTED:
        out.pop(k, None)
    return out
