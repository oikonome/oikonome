"""First-boot setup wizard (self-hosted): while NO user exists, /setup
creates the first account and optionally connects an aggregator in the same
step — SimpleFIN (token claimed + first sync), Plaid (BYO developer keys
stored in tenant config, validated at first link attempt), both, or neither
(file imports only) — plus an optional LLM for smart categorization
(llm_url/llm_model/llm_api_key in tenant config, read by the worker's
nightly llm_categorize run; config wins over the operator's OIKONOME_LLM_*
env vars). Once any user exists the wizard is gone (403) — a fresh
install's single-owner bootstrap, not an open signup (hosted signup is
/api/signup; self-hosted operators add users deliberately later).
"""

from __future__ import annotations

import datetime as dt
import os
from ..envnum import env_flag

from ..auth import passwords
from ..db import crypto, tenancy
from ..engine import budget
from ..sync import simplefin

PLAID_ENVS = ("production", "sandbox")

SIMPLEFIN_ITEM = "sfin-main"


class SimplefinFirstPullFailed(Exception):
    """The setup token was claimed and the bridge saved, but the first pull
    did not finish. The connection is live: the next sync retries it, and
    no new token is needed (the old one is spent)."""


def connect_simplefin(conn, setup_token: str, *, days: int = 90,
                      transport=None) -> dict:
    """Claim a SimpleFIN setup token and pull the first `days` of history.

    The setup token is ONE-TIME: the claim spends it and hands back the
    access URL, which exists nowhere else. It is saved before the first
    pull, because that pull is the slow, failure-prone step (a deep window,
    a bridge having a bad minute) — saved only after it, a pull that timed
    out left the person holding a spent token and no connection, with a
    fresh token from the bridge as the only way back. Raises
    SimplefinFirstPullFailed when the claim held and only the pull failed;
    anything else raised means the claim itself did not happen."""
    from ..sync.base import upsert_item
    access = simplefin.claim_setup_token(setup_token, transport=transport)
    upsert_item(conn, SIMPLEFIN_ITEM, "simplefin", "SimpleFIN bridge", access)
    try:
        return simplefin.sync(
            conn, SIMPLEFIN_ITEM, access,
            since=dt.date.today() - dt.timedelta(days=days),
            transport=transport)
    except Exception as e:                                # noqa: BLE001
        raise SimplefinFirstPullFailed(type(e).__name__) from e


SIMPLEFIN_PULL_RETRYING = ("Bank connected, but the first download from "
                           "SimpleFIN did not finish. It retries on the next "
                           "sync — no new token needed.")


def instance_has_users(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def bootstrap(email: str, password: str, simplefin_token: str = "",
              plaid_client_id: str = "", plaid_secret: str = "",
              plaid_env: str = "production", llm_url: str = "",
              llm_model: str = "", llm_api_key: str = "",
              transport=None) -> dict:
    """Create tenant + first user; claim + sync SimpleFIN when a setup token
    is supplied; store BYO Plaid keys in tenant config when supplied (no
    validation here — the first link attempt validates, same as the
    Accounts-page flow); store optional smart-categorization LLM settings
    in tenant config (llm_url + llm_model required together, key optional
    and encrypted at rest — local servers need none). Returns {tenant_id,
    user_id, sync, sync_error, plaid_saved, llm_saved}. Raises ValueError
    on bad input BEFORE any user is created; the ROUTE enforces the
    no-users gate."""
    email = email.strip().lower()
    from .mailguard import valid_address
    if not valid_address(email):
        raise ValueError("enter a valid email address")
    err = passwords.password_error(password)
    if err:
        raise ValueError(err)
    plaid_client_id = plaid_client_id.strip()
    plaid_secret = plaid_secret.strip()
    if bool(plaid_client_id) != bool(plaid_secret):
        raise ValueError("enter both the Plaid client_id and secret, "
                         "or leave both blank")
    llm_url = llm_url.strip()
    llm_model = llm_model.strip()
    llm_api_key = llm_api_key.strip()
    if bool(llm_url) != bool(llm_model):
        raise ValueError("enter both the LLM endpoint URL and model name, "
                         "or leave both blank")
    if llm_api_key and not llm_url:
        raise ValueError("an LLM API key needs an endpoint URL and model "
                         "name to go with it")
    if llm_url:
        from .netguard import BlockedURL, check_url
        try:
            check_url(llm_url, what="the LLM endpoint")
        except BlockedURL as e:
            raise ValueError(str(e))
    admin = tenancy.admin_connect()
    try:
        # The route's instance_has_users() check and this insert are a
        # TOCTOU window — two parallel /setup posts (or /setup racing
        # /api/signup) during first boot could each pass the check and mint a
        # second owner/tenant. Serialize the claim: one transaction-scoped
        # advisory lock, re-check inside it, so exactly one racer creates the
        # first account and the rest get a clear 403 from the route's retry.
        with admin.transaction():
            admin.execute("SELECT pg_advisory_xact_lock(hashtext('oikonome:setup-claim'))")
            # re-check under the lock closes the TOCTOU. DEV_MODE exempt: the
            # test suite bootstraps many tenants against one control plane
            # (which is the hosted shape); prod self-host never sets it.
            if not env_flag("OIKONOME_HOSTED") \
                    and os.environ.get("OIKONOME_DEV") != "1" \
                    and instance_has_users(admin):
                raise ValueError("this instance is already set up")
            tid = tenancy.create_tenant(admin, email)
            row = admin.execute(
                """INSERT INTO users (tenant_id, email, password_hash, verified_at)
                   VALUES (%s,%s,%s,now()) RETURNING id""",
                (tid, email, passwords.hash_password(password))).fetchone()
    finally:
        admin.close()
    sync_result = None
    sync_error = None
    if simplefin_token.strip():
        # The user row already exists at this point, so a bad token must NOT
        # raise — that would end the one-shot wizard with no account usable.
        # Finish setup and surface the error instead; the Accounts page
        # offers a retry form.
        conn = tenancy.tenant_connect(tid)
        try:
            sync_result = connect_simplefin(conn, simplefin_token,
                                            transport=transport)
        except SimplefinFirstPullFailed:
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "setup simplefin first pull failed")
            sync_error = SIMPLEFIN_PULL_RETRYING
        except Exception:  # noqa: BLE001 — wizard must complete
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "setup simplefin connect failed")
            sync_error = ("Bank connection failed. Your account was created "
                          "— retry the token from the Accounts page.")
        finally:
            conn.close()
    plaid_saved = False
    if plaid_client_id and plaid_secret:
        # Stored exactly where the Accounts-page link flow reads them
        # (sync/plaid.credentials: tenant config keys), AFTER the SimpleFIN
        # sync so first-run budget seeding sees any synced history. Never
        # log the values — they are credentials.
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = plaid_client_id
                # encrypt at rest (same as llm_api_key below and the Settings
                # path); sync/plaid.credentials decrypts on read
                cfg["plaid_secret"] = crypto.encrypt(conn, plaid_secret)
                cfg["plaid_env"] = (plaid_env if plaid_env in PLAID_ENVS
                                    else "production")
        finally:
            conn.close()
        plaid_saved = True
    llm_saved = False
    if llm_url:
        # Read by the worker's nightly llm_categorize run via
        # budget.load_config; tenant config wins over the operator's
        # OIKONOME_LLM_* env. The key is a credential: encrypted at rest
        # (db/crypto, same as aggregator tokens) and never logged.
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["llm_url"] = llm_url
                cfg["llm_model"] = llm_model
                if llm_api_key:
                    cfg["llm_api_key"] = crypto.encrypt(conn, llm_api_key)
        finally:
            conn.close()
        llm_saved = True
    return {"tenant_id": tid, "user_id": str(row["id"]), "sync": sync_result,
            "sync_error": sync_error, "plaid_saved": plaid_saved,
            "llm_saved": llm_saved}
