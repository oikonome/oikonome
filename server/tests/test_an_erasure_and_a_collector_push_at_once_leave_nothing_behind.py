"""An account erasure and a collector push at once leave nothing of the
household behind (specs/invariants-and-fuzzing.md, section 3, eighth
scenario).

A push was authenticated before it reached its write; an erasure flips
the household's status and then wipes its rows. Whichever goes first,
the end is the same as one after the other: the push lands and is then
erased, or it is refused because the household is going. A push that
landed after the wipe would leave rows under a tenant id nothing ever
sweeps again.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from oikonome import erasure
from oikonome.db import tenancy
from oikonome.sync import coinbase_push
from oikonome.web import adminconsole, api, api_imports  # noqa: F401
from oikonome.web.fields import Fields

from .concurrency import ScenarioCase, Scenario, Seam
from .restore_invariants import tenant_digest

DAY = dt.date(2026, 3, 2).isoformat()
RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}

WRITERS = Seam("the erasure's push writer lock",
               sql=r"\bpg_advisory_lock\(hashtext.* -- oikonome:push:")
PUSH = Seam("the push's shared writer lock",
            sql=r"pg_try_advisory_xact_lock_shared")


def _payload(txn_id: str) -> dict:
    return {
        "items": [{"id": "coinbase-erase", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-erase", "item_id": "coinbase-erase",
                      "name": "Coinbase (erase)", "type": "investment",
                      "subtype": "crypto", "balance_current": 500.0}],
        "transactions": [{"id": txn_id, "account_id": "coinbase-erase",
                          "date": DAY, "amount": 100.0,
                          "name": "MAPLE EXCHANGE"}]}


def _setup(hh):
    coinbase_push.import_payload(hh.conn, _payload("coinbase:er-one"))


def _push(hh, ctx):
    return api_imports.coinbase_import_api(
        user=hh.user, body=Fields(_payload("coinbase:er-two")))


def _erase(hh, ctx):
    admin = tenancy.admin_connect()
    try:
        return adminconsole._purge_tenant(admin, hh.tid, "owner@example.dev",
                                          ip="test")
    finally:
        admin.close()


def _digest(hh, ctx):
    rows = {t: len(r) for t, r in tenant_digest(hh.tid).items()}
    admin = tenancy.admin_connect()
    try:
        tenant = bool(admin.execute("SELECT 1 FROM tenants WHERE id = %s",
                                    (hh.tid,)).fetchone())
    finally:
        admin.close()
    return tenant, tuple(sorted(rows.items()))


class AnErasureBesideAPush(ScenarioCase, unittest.TestCase):
    def test_an_erasure_and_a_collector_push_leave_nothing_behind(self):
        self.check(Scenario(
            "an erasure beside a collector push",
            setup=_setup, actors={"erase": _erase, "push": _push},
            seams=(WRITERS, PUSH), digest=_digest,
            doors=frozenset({"push"}),
            invariants=lambda hh, ctx: (
                [] if _digest(hh, ctx) == (False, ())
                else [f"left behind: {_digest(hh, ctx)}"]),
            patches=lambda: mock.patch.object(
                erasure, "release_external", return_value=dict(RELEASED))))


if __name__ == "__main__":
    unittest.main()
