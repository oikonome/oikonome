"""Portable, passphrase-sealed export/import of a tenant's whole
*environment config* — the credentials a fresh install needs to become a
working copy: provider API keys (Plaid/MX), the AI backends (every named
endpoint with its key, plus the legacy single endpoint) and which task each
one serves, the SMTP mailer, and every linked bank item WITH its live access
token.

Why a dedicated bundle when the db-dump / CSV export exist? Because both
deliberately drop exactly these values: the CSV export scrubs every config
key containing secret/token/password/client_id and never emits access_token;
the db dump keeps them but encrypted at rest under THIS install's
OIKONOME_MASTER_KEY (db/crypto), so restoring onto a different box leaves
undecryptable ciphertext — banks and integrations silently go dead. This
bundle is master-key-INDEPENDENT: on export we decrypt secrets under the
source key and re-seal the whole payload under a user passphrase (scrypt +
Fernet); on import we re-encrypt every secret under the DESTINATION's master
key. So a household moves to a new machine with
banks still linked and email/LLM already wired — nothing to re-enter.

The file never contains a plaintext secret; only the passphrase unlocks it.
It is NOT a full backup — accounts/transactions/history and the non-secret
config (budgets, buckets) ride the CSV export / db dump. This carries the
credentials those omit, plus the live bank links. After import, one sync
repopulates accounts (aggregator-stable ids, so links/categorizations
reattach).
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import os
from ..envnum import env_flag

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..db import crypto
from ..engine import budget
from ..sync import base as sync_base

FORMAT = 2                      # payload schema version (2: full config)
# scrypt work factors — interactive-login grade (~tens of ms), plenty for a
# file a human types a passphrase for. Stored in the envelope so a future
# tuning still opens old files.
_N, _R, _P = 2 ** 15, 8, 1

# The environment config a fresh install needs. Plaintext keys travel as-is;
# WHITELISTED so a tampered bundle can never inject arbitrary config. Secret
# keys are stored encrypted-at-rest — decrypted on export, re-encrypted under
# the destination master key on import (see _SECRET_KEYS).
_CONFIG_KEYS = (
    "plaid_client_id", "plaid_env",              # Plaid aggregator
    "mx_client_id", "mx_env", "mx_user_guid",    # MX aggregator
    "llm_url", "llm_model", "llm_vision_model",  # legacy single endpoint
    "smtp_host", "smtp_port", "smtp_user", "smtp_from",   # email delivery
)
# llm_extra_body sits here rather than in _CONFIG_KEYS because it can carry a
# credential (an auth-in-body vendor, an org token) and is stored encrypted
# for that reason. Copying the stored value across as plaintext config would
# hand the destination ciphertext it has no key for; decrypt tolerates an
# untagged legacy value, so both shapes travel correctly.
_SECRET_KEYS = (
    "plaid_secret", "mx_api_key", "llm_api_key", "llm_extra_body",
    "smtp_password",
)

# A named AI backend, the shape Settings writes into `llm_backends`. Both
# whitelists are enforced on import so a tampered bundle can neither inject
# unknown fields into the settings document nor smuggle in a backend without
# the endpoint check every other door applies.
_BACKEND_FIELDS = ("id", "name", "url", "model", "vision_model")
_BACKEND_SECRETS = ("api_key", "extra_body")
# same ceiling and same reserved id as the Settings save — the bundle must
# not be the door that walks past either.
_MAX_BACKENDS = 12


# ---- passphrase envelope ---------------------------------------------------


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(
        passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(key)          # Fernet wants urlsafe-b64


def _seal(plaintext: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    token = Fernet(_derive(passphrase, salt, _N, _R, _P)).encrypt(plaintext)
    return json.dumps({
        "oikx": 1, "kdf": "scrypt", "n": _N, "r": _R, "p": _P,
        "salt": base64.b64encode(salt).decode(),
        "blob": token.decode(),
    }, separators=(",", ":")).encode()


def _open(data: bytes, passphrase: str) -> bytes:
    try:
        env = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("not an Oikonome config file")
    if not isinstance(env, dict) or env.get("oikx") != 1:
        raise ValueError("not an Oikonome config file")
    try:
        # the envelope's work factors are ATTACKER-CONTROLLED input —
        # a crafted file with n=2**30 would burn gigabytes of RAM before the
        # passphrase is even checked. Clamp to a generous ceiling (well
        # above anything _seal ever wrote) and refuse the rest as corrupt.
        n, r, p = (int(env.get("n", _N)), int(env.get("r", _R)),
                   int(env.get("p", _P)))
        # The per-dimension bounds are not enough on their own: scrypt's
        # memory cost is ~128*n*r bytes, so n and r each in range still
        # multiply to ~4 GiB at the corners (n=2**20, r=32) — allocated
        # inside derive() BEFORE the passphrase is ever checked. Bound the
        # PRODUCTS against what _seal actually writes (n*r = 2**18, p = 1)
        # with generous tuning headroom, and refuse the rest as corrupt.
        if not (2 ** 10 <= n <= 2 ** 20 and (n & (n - 1)) == 0
                and 1 <= r <= 32 and 1 <= p <= 16
                and n * r <= 2 ** 21          # ≤ ~256 MiB scrypt memory
                and n * r * p <= 2 ** 22):    # bounds CPU time too
            raise ValueError("bad kdf params")
        salt = base64.b64decode(env["salt"])
        key = _derive(passphrase, salt, n, r, p)
        return Fernet(key).decrypt(env["blob"].encode())
    except (KeyError, ValueError, TypeError, AttributeError):
        # AttributeError: a non-string blob/salt (.encode/.b64decode) — a
        # malformed file must be a 400, not a 500
        raise ValueError("corrupted or unrecognized connections file")
    except InvalidToken:
        raise ValueError("wrong passphrase (or the file was tampered with)")


# ---- payload build / apply -------------------------------------------------


def _summary_groups(config: dict, secrets: dict,
                    backends: list | None = None) -> list[str]:
    """Named integrations present in a payload, for a human-readable result."""
    keys = set(config) | set(secrets)
    groups = []
    if keys & {"plaid_client_id", "plaid_secret"}:
        groups.append("plaid")
    if keys & {"mx_client_id", "mx_api_key"}:
        groups.append("mx")
    if keys & {"llm_url", "llm_api_key"} or backends:
        groups.append("llm")
    if any(k.startswith("smtp") for k in keys):
        groups.append("smtp")
    return groups


def _export_backends(conn, cfg: dict) -> list[dict]:
    """The tenant's named AI backends, credentials decrypted for the seal.

    They travel apart from `config` because they are the one integration
    whose secrets live INSIDE a structure rather than in flat keys: the
    whole point of the bundle is that a secret crosses master keys, so each
    entry's api_key/extra_body is decrypted here and re-encrypted under the
    destination's key on import, exactly like the flat secrets.
    """
    out = []
    for b in cfg.get("llm_backends") or []:
        if not isinstance(b, dict) or not b.get("id") or not b.get("url"):
            continue
        ent = {k: b[k] for k in _BACKEND_FIELDS if b.get(k) not in (None, "")}
        for k in _BACKEND_SECRETS:
            if b.get(k):
                ent[k] = crypto.decrypt(conn, b[k])
        out.append(ent)
    return out


def build_payload(conn) -> dict:
    """Plaintext (pre-seal) full-config payload for the current tenant."""
    cfg = budget.load_config(conn)
    config = {k: cfg[k] for k in _CONFIG_KEYS
              if cfg.get(k) not in (None, "")}
    secrets = {k: crypto.decrypt(conn, cfg[k]) for k in _SECRET_KEYS
               if cfg.get(k)}
    backends = _export_backends(conn, cfg)
    roles = {k: str(v) for k, v in (cfg.get("llm_roles") or {}).items()
             if v}
    items = []
    # manual items hold no credential — nothing to migrate, and their tokens
    # are NULL; skip so the bundle is only live aggregator links. A
    # disconnected connection is not a live link: the owner is done with
    # it, and a Plaid item whose release failed keeps its token only so this
    # install's reaper can finish the release. A restored shell has no
    # credential to carry either.
    for r in conn.execute(
            "SELECT id, aggregator, institution_name, access_token, raw "
            "FROM items WHERE aggregator <> 'manual' "
            "AND COALESCE(status, '') NOT IN ('archived', 'restored')"
            ).fetchall():
        items.append({
            "id": r["id"],
            "aggregator": r["aggregator"],
            "institution_name": r["institution_name"],
            "access_token": crypto.decrypt(conn, r["access_token"]),
            "raw": r["raw"] if isinstance(r["raw"], dict) else None,
        })
    # llm_backends/llm_roles are additive keys, deliberately NOT a format
    # bump: an older install still opens this file (it ignores what it does
    # not know) and this reader still opens a bundle written before they
    # existed (they are simply absent).
    return {
        "oikonome_config": FORMAT,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_version": os.environ.get("OIKONOME_VERSION", "unknown"),
        "config": config,
        "secrets": secrets,
        "llm_backends": backends,
        "llm_roles": roles,
        "items": items,
    }


def export_bytes(conn, passphrase: str) -> bytes:
    if not passphrase or len(passphrase) < 8:
        raise ValueError("passphrase must be at least 8 characters")
    payload = build_payload(conn)
    return _seal(json.dumps(payload).encode(), passphrase)


def _check_urls(config: dict) -> None:
    """Same SSRF policy the live Settings save applies. ValueError so the
    import surfaces it as a bad-file message like every other payload fault."""
    from ..web.netguard import BlockedURL, check_host, check_url
    url = config.get("llm_url")
    if url:
        try:
            check_url(str(url), what="the LLM URL in this file")
        except BlockedURL as e:
            raise ValueError(str(e))
    # the bundle whitelist carries smtp_host too — without this,
    # a crafted .oikx walks past the SMTP SSRF guard just as it would the LLM's
    host = config.get("smtp_host")
    if host:
        try:
            check_host(str(host), what="the SMTP host in this file")
        except BlockedURL as e:
            raise ValueError(str(e))


def _clean_backends(raw, *, drop_unreachable: list | None = None) -> list[dict]:
    """Validate an incoming backend list, returning entries with plaintext
    secrets ready to re-encrypt. Every rule the Settings save applies is
    applied here too — field whitelist, entry ceiling, reserved/duplicate
    ids, and the SSRF check on each endpoint. A bundle aims the worker at a
    URL just as a settings save does, so it gets the same guard; anything
    else makes this the door that walks around it.

    `drop_unreachable` splits the two kinds of fault apart. A MALFORMED
    list is a corrupt file and still refuses outright. A well-formed entry
    the guard will not accept — the common case being a LAN or
    container-network host that does not resolve here, e.g. the bundled
    Ollama sidecar at http://ollama:11434 seen from a hosted instance — is
    dropped, its id appended to the caller's list, and the rest of the
    payload proceeds. Callers restoring a whole ledger pass a list, because
    refusing a person's entire financial history over one unreachable AI
    endpoint is not a defensible trade; the Settings save and the .oikx
    import pass nothing and keep refusing, since there the backend IS the
    payload."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("the AI backends in this file are malformed")
    if len(raw) > _MAX_BACKENDS:
        raise ValueError(f"this file carries more than {_MAX_BACKENDS} "
                         "AI backends")
    from ..web.netguard import BlockedURL, check_url
    out, seen = [], set()
    for b in raw:
        if not isinstance(b, dict):
            raise ValueError("the AI backends in this file are malformed")
        bid = str(b.get("id") or "").strip()
        url = str(b.get("url") or "").strip()
        model = str(b.get("model") or "").strip()
        if not bid or not url or not model:
            raise ValueError("every AI backend in this file needs an id, "
                             "a url and a model")
        if bid == "bundled" or bid in seen:
            raise ValueError("this file has a reserved or duplicate AI "
                             "backend id")
        seen.add(bid)
        try:
            check_url(url, what=f"the AI backend {bid!r} in this file")
        except BlockedURL as e:
            if drop_unreachable is None:
                raise ValueError(str(e))
            # dropping is strictly safer than merging: the URL never
            # reaches stored config, so nothing can later fetch it
            drop_unreachable.append(bid)
            continue
        ent = {"id": bid, "url": url, "model": model,
               "name": str(b.get("name") or "").strip()[:40] or "Backend"}
        vm = str(b.get("vision_model") or "").strip()
        if vm:
            ent["vision_model"] = vm
        for k in _BACKEND_SECRETS:
            v = b.get(k)
            if isinstance(v, str) and v.strip():
                ent[k] = v
        out.append(ent)
    return out


def _clean_roles(raw, backend_ids: set) -> dict:
    """Role→backend routing, kept only where it still points somewhere. A
    role naming a backend this bundle did not bring would resolve to the
    legacy/env layer anyway — dropping it keeps the stored config honest
    about what is actually routed."""
    from ..engine.llm_categorize import ROLES
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items()
            if k in ROLES and v and (v == "bundled" or str(v) in backend_ids)}


def _check_item(it: dict) -> None:
    """A SimpleFIN item's access_token IS a URL the worker will GET every
    sync. simplefin.fetch re-guards it too — this fails the import loudly
    at the door rather than silently every hour after."""
    if (it.get("aggregator") or "").lower() != "simplefin":
        return
    token = it.get("access_token")
    if not token:
        return
    from ..web.netguard import BlockedURL, check_url
    try:
        check_url(str(token), what="a SimpleFIN access URL in this file")
    except BlockedURL as e:
        raise ValueError(str(e))


def _checked_shapes(payload: dict) -> tuple[dict, dict, list]:
    """The payload's config, secrets and items, refused (ValueError, a 400)
    unless each holds the shapes an export writes. Any passphrase seals a
    valid file, so every value here is attacker-shaped: an item id that is
    a list cannot be looked up, and a secret that is a number cannot be
    encrypted — either would otherwise surface as a 500, or be stored and
    break every later read of the setting."""
    config = payload.get("config")
    secrets = payload.get("secrets")
    items = payload.get("items")
    if config is not None and not isinstance(config, dict):
        raise ValueError("this file's config entry is malformed")
    if secrets is not None and not isinstance(secrets, dict):
        raise ValueError("this file's secrets entry is malformed")
    if items is not None and not isinstance(items, list):
        raise ValueError("this file's items entry is malformed")
    config, secrets, items = config or {}, secrets or {}, items or []
    for k, v in config.items():
        if k in _CONFIG_KEYS and v not in (None, "") and (
                isinstance(v, bool) or not isinstance(v, (str, int))):
            raise ValueError(f"this file's {k} setting is malformed")
    for k, v in secrets.items():
        if k in _SECRET_KEYS and v is not None and not isinstance(v, str):
            raise ValueError(f"this file's {k} is malformed")
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in ("id", "aggregator"):
            if it.get(k) and not isinstance(it[k], str):
                raise ValueError(f"a connection's {k} in this file is "
                                 "malformed")
        for k in ("institution_name", "access_token"):
            if it.get(k) is not None and not isinstance(it[k], str):
                raise ValueError(f"a connection's {k} in this file is "
                                 "malformed")
        if it.get("raw") is not None and not isinstance(it["raw"], dict):
            raise ValueError("a connection's details in this file are "
                             "malformed")
    return dict(config), dict(secrets), items


def _bundle_value(config: dict, k: str):
    v = config.get(k)
    return v if k in _CONFIG_KEYS and v not in (None, "") else None


def _live_items(conn, aggregator: str) -> list[str]:
    tok = "AND access_token IS NOT NULL " if aggregator == "plaid" else ""
    return [r["institution_name"] or r["id"] for r in conn.execute(
        "SELECT id, institution_name FROM items WHERE aggregator=%s "
        + tok + "AND COALESCE(status,'ok') <> 'archived' ORDER BY id",
        (aggregator,)).fetchall()]


def _refuse_stranding_key_swaps(conn, config: dict) -> set:
    """Refuse (ValueError) a bundle whose Plaid or MX client or environment
    differs from the one this household's live connections were made
    under, the check the dedicated key doors make: a Plaid access token and
    an MX user only work with the client that made them, so swapping the
    keys under them leaves those connections syncing nothing, and still
    billing, with nothing left to release them by. Also keeps a live MX
    user's guid from being replaced by the bundle's, and drops a guid the
    new MX client cannot reach: the settings keys to drop are returned,
    and `config` loses a guid it must not write."""
    cur = budget.load_config(conn)
    inc_id = _bundle_value(config, "plaid_client_id")
    inc_env = _bundle_value(config, "plaid_env")
    if inc_id or inc_env:
        had_id = (cur.get("plaid_client_id")
                  or os.environ.get("OIKONOME_PLAID_CLIENT_ID"))
        default_env = os.environ.get("OIKONOME_PLAID_ENV", "production")
        had_env = ((cur.get("plaid_env") if cur.get("plaid_client_id")
                    else None) or default_env)
        new_id = inc_id or had_id
        # a stored environment only counts beside a stored client id
        new_env = ((inc_env or cur.get("plaid_env"))
                   if (inc_id or cur.get("plaid_client_id")) else None
                   ) or default_env
        if (new_id, new_env) != (had_id, had_env):
            live = _live_items(conn, "plaid")
            if live:
                raise ValueError(
                    "this bundle carries different Plaid keys — disconnect "
                    f"these Plaid connections first: {', '.join(live)}")
    guid = cur.get("mx_user_guid")
    if guid:
        mx_id = _bundle_value(config, "mx_client_id")
        mx_env = _bundle_value(config, "mx_env")
        moving = ((mx_id is not None and mx_id != cur.get("mx_client_id"))
                  or (mx_env is not None
                      and mx_env != (cur.get("mx_env") or "production")))
        live = _live_items(conn, "mx")
        if moving and live:
            raise ValueError(
                "this bundle carries different MX keys — disconnect these "
                f"MX connections first: {', '.join(live)}")
        if live:
            # the user holding live members stays; the bundle's is another
            # install's, reachable from here by nothing
            config.pop("mx_user_guid", None)
        elif moving and not _bundle_value(config, "mx_user_guid"):
            # the new client cannot see the old user; the next use mints one
            return {"mx_user_guid"}
    return set()


def apply_payload(conn, payload: dict) -> dict:
    """Merge a decrypted payload into the current tenant, re-encrypting every
    secret under THIS install's master key. Idempotent (items upsert by id)."""
    if not isinstance(payload, dict) or \
            payload.get("oikonome_config") != FORMAT:
        raise ValueError("unsupported config file version")
    config, secrets, items_raw = _checked_shapes(payload)
    # On hosted, tenants NEVER bring their own aggregator credentials (the
    # platform runs Plaid/MX under its own keys). A bundle must not become a
    # side door around that — strip any BYO aggregator keys before applying,
    # the same policy the dedicated key-save endpoints enforce.
    # `items` is attacker-shaped JSON (any passphrase seals a valid file):
    # a non-list value here — a string iterates per-character, a dict per
    # key — would surface as an AttributeError 500 instead of the clean
    # 400 every other malformed field gets. Same doctrine as
    # _clean_backends' isinstance check.
    items_in = [it for it in items_raw
                if isinstance(it, dict)
                and it.get("id") and it.get("aggregator")]
    if env_flag("OIKONOME_HOSTED"):
        _BYO = {"plaid_client_id", "plaid_secret", "plaid_env",
                "mx_client_id", "mx_api_key", "mx_env", "mx_user_guid"}
        for k in _BYO:
            config.pop(k, None)
            secrets.pop(k, None)
        # The ITEMS are BYO credentials too — a SimpleFIN
        # item's access_token IS the bearer URL to bank data, and hosted
        # items share the platform's Plaid client. The dedicated link doors
        # all refuse BYO on hosted (_no_byo_on_hosted); a bundle must not be
        # the side door around them, so items are stripped like the keys.
        items_in = []
    # WHITELIST both sides — a bundle can only set known config/secret keys,
    # never inject arbitrary tenant config.
    # The whitelist covers key NAMES, not VALUES. `llm_url` saved through
    # Settings is netguarded, and the same field arriving in a bundle must be
    # too — otherwise an imported file aims the worker at an internal address
    # and the guard is bypassed by a different door. Same policy, both doors.
    _check_urls(config)
    backends = _clean_backends(payload.get("llm_backends"))
    roles = _clean_roles(payload.get("llm_roles"),
                         {b["id"] for b in backends})
    # Everything from the cap count to the last item upsert runs under the
    # link lock, in one transaction, as the dedicated key doors do: a bank
    # link in flight either lands before the live-connection checks below
    # or sees its keys replaced and hands its Item back, and a concurrent
    # link cannot pass the institution cap beside this bundle. The caller
    # holds the tenant sync lock, so no MX user or member is minted under
    # the old keys meanwhile either.
    from . import plaid as plaid_mod
    with conn.transaction():
        plaid_mod.link_lock(conn)
        drop = _refuse_stranding_key_swaps(conn, config)
        # The institution cap has to be enforced here as well as at the Plaid
        # doors — otherwise a bundle of
        # N hand-written items sails straight past it. Refuse the whole
        # bundle before the settings row lock; an uncapped instance skips
        # this.
        if items_in:
            existing = {r["id"] for r in
                        conn.execute("SELECT id FROM items").fetchall()}
            fresh = [it for it in items_in if it["id"] not in existing]
            cap = sync_base.institution_cap(conn)
            if cap is not None and fresh:
                n = sync_base.institutions_held(conn)
                if n + len(fresh) > cap:
                    raise ValueError(
                        f"this bundle would connect {len(fresh)} more "
                        f"institution(s), passing this instance's cap of "
                        f"{cap} (currently {n}) — disconnect some first")
        # Under the row lock: this merges into the WHOLE settings document,
        # so an unlocked read-modify-write here discards whatever a
        # concurrent Settings save wrote. The shape validation stays above
        # both locks — a bundle that is going to be refused for its content
        # should never take either.
        with budget.config_txn(conn) as cfg:
            # what was set before the bundle landed, so a credential
            # rotation's armed pause can be widened to whatever it brings in
            before = copy.deepcopy(cfg)
            for k, v in config.items():
                if k in _CONFIG_KEYS and v not in (None, ""):
                    cfg[k] = v
            for k in drop:
                cfg.pop(k, None)
            for k, v in secrets.items():
                if k in _SECRET_KEYS and v is not None:
                    cfg[k] = crypto.encrypt(conn, v)
            # keys the bundle brought in were saved now, as far as the
            # bank-link hold's banner is concerned (server-written only)
            for prov, fields in (
                    ("plaid", ("plaid_client_id", "plaid_secret",
                               "plaid_env")),
                    ("mx", ("mx_client_id", "mx_api_key", "mx_env"))):
                if not cfg.get(f"{prov}_client_id"):
                    cfg.pop(f"{prov}_keys_saved_at", None)
                elif any(cfg.get(k) != before.get(k) for k in fields):
                    cfg[f"{prov}_keys_saved_at"] = dt.datetime.now(
                        dt.timezone.utc).isoformat()
            if backends:
                # a bundle brings a whole AI setup, so its list replaces this
                # tenant's by id — same-id entries are the same backend arriving
                # with its credential, and entries only this install has are
                # kept, since the bundle says nothing about them
                enc = crypto.encryptor(conn)
                prior = {b["id"]: b for b in (cfg.get("llm_backends") or [])
                         if isinstance(b, dict) and b.get("id")}
                for ent in backends:
                    for k in _BACKEND_SECRETS:
                        if ent.get(k):
                            ent[k] = enc(ent[k])
                    prior[ent["id"]] = ent
                if len(prior) > _MAX_BACKENDS:
                    # the merged total is the only count that can exceed the
                    # ceiling, and it is knowable only under the lock — so this
                    # one check lives here and aborts the transaction
                    raise ValueError(
                        f"importing these AI backends would pass the limit of "
                        f"{_MAX_BACKENDS} — remove some first")
                cfg["llm_backends"] = list(prior.values())
            if roles:
                cfg["llm_roles"] = {**(cfg.get("llm_roles") or {}), **roles}
            # a relay or AI endpoint imported while a rotation's pause is armed
            # stands down with the rest until the owner presses Resume, rather
            # than sending at once
            from .restore import widen_armed_pause
            widen_armed_pause(before, cfg, conn)

        # a connection the owner disconnected here stays disconnected: a bundle
        # written before the disconnect (or by another install) must not hand
        # it back its credential or switch it on again
        archived = {r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE status = 'archived'").fetchall()}
        items_in = [it for it in items_in if it["id"] not in archived]
        n_items = 0
        for it in items_in:
            _check_item(it)
            sync_base.upsert_item(
                conn, it["id"], it["aggregator"],
                it.get("institution_name") or it["aggregator"],
                it.get("access_token"), it.get("raw"))
            n_items += 1
        return {"config": _summary_groups(config, secrets, backends),
                "items": n_items}


def import_bytes(conn, data: bytes, passphrase: str) -> dict:
    payload = json.loads(_open(data, passphrase))
    return apply_payload(conn, payload)
