"""Every figure a household reads survives a restore of its export
(specs/invariants-and-fuzzing.md, R1; the figures are those of
specs/counted-figures.md, read where readers read them).

A restore carries inputs, never figures. So for a generated household
with a plan (tests/money_households.py, one per seed of tests/props.py),
settled as the nightly passes leave a household (merchants resolved, the
checking anchor picked, the categorize pass and the pair repair run),
every page payload of tests/money_surfaces.py (Today, the Month and Year
lenses, Cash Flow, Spending, Net Worth, Accounts, Retirement, Debt, the
integrations export) and the daily email's context read the same, at the
fixed TODAY:

* on a new signed-up household restored from its export, and
* on the household itself after its own export is restored into it,

both through the restore job's own path (`restore_job.restore_now`,
which also runs the pair repair and the categorize pass).

Every value of every payload is compared, figures and text alike, except
what is not the household's data:

* a connection's or account's `status`: live connection state (a restored
  connection is a shell until it is reconnected);
* `generated_at` and `_built_at`: the clock;
* `merchant_id`: the id a merchant row has in this household; the name
  shown for it is compared;
* on the new household only, a ledger row's `category_source`,
  `category_why` and `flow_narrow`: a row the archive carries without a
  category source is labelled by the restore's rule for archives written
  before the column (sync/restore.py, `upgrade_archived_txn`). They say
  why a row has its category; the category and every figure over it are
  compared.
"""

from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import forecast, llm_categorize, merchant_identity
from oikonome.sync import export, restore_job
from oikonome.web import report

from . import body_fuzz, props
from .money_households import Household
from .money_surfaces import DOORS, at_today
from .util import TODAY, seed_accounts

NOT_DATA = {"status", "generated_at", "_built_at", "merchant_id"}
LABELS_OF_AN_UNSOURCED_ROW = {"category_source", "category_why",
                              "flow_narrow"}


class _Now:
    """The post-restore categorize pass runs in a thread; here it runs
    before the pages are read, so they read what it wrote."""

    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


def _leaves(v, path="", out=None) -> dict:
    out = {} if out is None else out
    if isinstance(v, dict):
        for k, x in v.items():
            _leaves(x, f"{path}.{k}", out)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _leaves(x, f"{path}[{i}]", out)
    else:
        out[path] = v
    return out


def _last_key(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def read(client, conn) -> dict:
    """Every payload and the email's context, as plain JSON leaves."""
    got = {}
    for name, path in DOORS.items():
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        got[name] = r.json()
    got["email"] = report.gather(conn, TODAY)
    return _leaves(json.loads(json.dumps(got, default=str)))


def differences(before: dict, after: dict, skip: set) -> dict:
    """The leaves that differ; a key held as null reads as one left out
    (the restore's settings cleaners spell an unset field either way)."""
    return {k: (before.get(k, "<absent>"), after.get(k, "<absent>"))
            for k in sorted(set(before) | set(after))
            if _last_key(k) not in skip
            and before.get(k) != after.get(k)}


def settle(conn):
    """What the nightly passes leave behind on a household."""
    from oikonome.web.data import repair_pairs
    merchant_identity.resolve(conn)
    forecast.auto_pin_primary_checking(conn)
    llm_categorize.categorize_new(conn)
    repair_pairs(conn)


class ARestoreKeepsEveryFigure(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()

    @classmethod
    def tearDownClass(cls):
        print(f"\n[timing] {cls.__name__}: "
              f"{time.monotonic() - cls.started:.1f}s", flush=True)

    def test_figures_read_the_same_after_a_restore(self):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        for seed in props.seeds():
            with self.subTest(seed=seed), at_today():
                client, _e, tid = body_fuzz.signup(appmod.app, "r1")
                conn = tenancy.tenant_connect(tid)
                try:
                    seed_accounts(conn)
                    Household(seed, plan=True, conn=conn)
                    settle(conn)
                    before = read(client, conn)
                    data = export.build_zip(conn)
                finally:
                    conn.close()
                self.assertGreater(len(before), 1000)

                other, _e2, other_tid = body_fuzz.signup(appmod.app, "r1b")
                conn = tenancy.tenant_connect(other_tid)
                try:
                    with mock.patch("threading.Thread", _Now):
                        restore_job.restore_now(conn, data,
                                                tenant_id=other_tid)
                    after = read(other, conn)
                finally:
                    conn.close()
                    other.close()
                self.assertEqual(
                    differences(before, after,
                                NOT_DATA | LABELS_OF_AN_UNSOURCED_ROW), {},
                    f"seed {seed}: restored into a new household")

                conn = tenancy.tenant_connect(tid)
                try:
                    with mock.patch("threading.Thread", _Now):
                        restore_job.restore_now(conn, data, tenant_id=tid)
                    again = read(client, conn)
                finally:
                    conn.close()
                    client.close()
                self.assertEqual(differences(before, again, NOT_DATA), {},
                                 f"seed {seed}: restored into itself")


if __name__ == "__main__":
    unittest.main()
