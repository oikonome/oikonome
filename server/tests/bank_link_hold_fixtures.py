"""Shared fixtures for the bank-link hold tests: a household on its own
Plaid and MX keys, real-shaped (client ids, encrypted secrets), and the
record a recovery leaves behind."""

from __future__ import annotations

import datetime as dt

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.notify import outbound_pause as op

PLAID_CLIENT = "5f2c9e1ab03d4c0012a7f3e1"
MX_CLIENT = "c8b1f6a2-4d7e-4f0a-9b3c-2e5d8a1f6c40"
PLAID_SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f"
MX_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0aa55"


def save_household_keys(conn, plaid=True, mx=True) -> None:
    with budget.config_txn(conn) as cfg:
        if plaid:
            cfg["plaid_client_id"] = PLAID_CLIENT
            cfg["plaid_secret"] = crypto.encrypt(conn, PLAID_SECRET)
            cfg["plaid_env"] = "production"
        if mx:
            cfg["mx_client_id"] = MX_CLIENT
            cfg["mx_api_key"] = crypto.encrypt(conn, MX_KEY)
            cfg["mx_env"] = "production"


def arm_hold(conn, **extra) -> None:
    """The record a recovery writes on a household whose own keys are
    saved: nothing else was set, so the hold is all it carries."""
    with budget.config_txn(conn) as cfg:
        clients = {p: cfg[f"{p}_client_id"] for p in ("plaid", "mx")
                   if cfg.get(f"{p}_client_id")}
        cfg[op.KEY] = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": op.RECOVERY, "smtp": False, "recipients": [],
            "llm": False, "aggregator": True,
            "aggregator_clients": clients, **extra}
