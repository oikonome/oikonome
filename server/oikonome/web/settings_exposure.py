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
        "manual_assets", "theme", "home_web", "home_mobile", "timezone",
        "today_view", "feedback_enabled", "merchant_logos",
        "plaid_recurring", "plaid_enrich_cap", "email_schedule",
        "email_send_hour_utc", "advisory_fees", "advisory_fee_exempt_masks",
        "investment_basis_override", "history_months", "setup_mode",
        "bill_detection_auto", "biz_wizard_steps", "ret_wizard_steps",
        "income_scenarios", "active_scenario", "savings_goals",
        "categorizer_active", "outbound_paused", "demo_read_only")},
    # ---- who gets the mail. The recipient list is the household's own
    # roster of addresses; the mute list and per-address status are cut to
    # the caller's own row for anyone but the owner by the settings door's
    # people filter (api._hide_people), which owns that rule.
    "email_recipients": Exposure(door=True),
    "email_muted": _ALL,
    "email_recipient_status": Exposure(export=False),
    "email_recipients_added": Exposure(read="never", door=True,
                                       export=False),
    "email_recipients_removed": Exposure(read="never", door=True,
                                         export=False),
    # ---- the mail relay. smtp_user is a credential whatever its name says:
    # Postmark takes the server API token as both the username and the
    # password (notify/delivery reads it as the token). Owner only, the
    # others learn that one is set; and it never rides the export, like the
    # password beside it.
    "smtp_host": Exposure(door=True),
    "smtp_port": Exposure(door=True),
    "smtp_user": Exposure(read="owner", below="set", door=True,
                          export=False),
    "smtp_from": Exposure(door=True),
    "smtp_starttls": Exposure(door=True),
    "smtp_password": Exposure(read="never", door=True, export=False),
    "smtp_password_set": _ALL,
    "smtp_user_set": _ALL,           # what a non-owner gets for smtp_user
    # ---- the AI endpoint. URLs are masked for every role (a password in
    # the userinfo is a credential like the api key); the key and the extra
    # body are write-only and leave as presence flags.
    "llm_url": Exposure(url=True, door=True),
    "llm_model": Exposure(door=True),
    "llm_vision_model": Exposure(door=True),
    "llm_backends": Exposure(url=True, door=True),
    "llm_roles": Exposure(door=True),
    "llm_bundled": Exposure(url=True, export=False),
    "llm_effective": Exposure(url=True, export=False),
    "llm_api_key": Exposure(read="never", door=True, export=False),
    "llm_api_key_set": _ALL,
    # echoed as "" with a presence flag beside it (the declared shape)
    "llm_extra_body": Exposure(door=True, export=False),
    "llm_extra_body_set": _ALL,
    "taxdocs_allow_remote_llm": Exposure(door=True),
    # ---- the household's own aggregator keys. A client id is an
    # identifier, not a secret, and the Connections card shows which keys
    # are set; the secrets are write-only.
    "plaid_client_id": _ALL,
    "plaid_env": _ALL,
    "plaid_secret": Exposure(read="never", export=False),
    "plaid_secret_set": _ALL,
    "mx_client_id": _ALL,
    "mx_env": _ALL,
    "mx_api_key": Exposure(read="never", export=False),
    "mx_api_key_set": _ALL,
}

# The keys a demo instance must never take, even were its blanket
# read-only rule ever relaxed.
DEMO_DOOR_KEYS = frozenset(k for k, e in TABLE.items() if e.door)

# Stored keys that must not leave in an export although no credential-name
# rule would catch them.
NOT_EXPORTED = frozenset(k for k, e in TABLE.items() if not e.export)


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
