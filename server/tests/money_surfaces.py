"""Every surface of one generated household, read the way its readers read
it (specs/invariants-and-fuzzing.md, M9 and M10).

`Surfaces(seed)` signs up a household, builds a money_households
household with a plan into it, and fetches each page's payload through
the API at the fixed TODAY: Today (which the Budget page reads too),
the Month and Year lenses, Cash Flow, Spending, Net Worth, Accounts,
Retirement, Debt and the integrations export; plus the daily email's
context (`report.gather`) and its plain-text render.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from oikonome.db import tenancy

from . import body_fuzz
from .money_households import Household
from .util import TODAY, seed_accounts

DOORS = {
    "today": "/api/today/full",
    "lens_month": "/api/lens/month",
    "lens_year": "/api/lens/year",
    "cashflow": "/api/reports/cashflow",
    "spending": "/api/reports/spending",
    "networth": "/api/reports/networth",
    "accounts": "/api/accounts",
    "retirement": "/api/retirement",
    "debt": "/api/debt-plan",
    "export_summary": "/api/integrations/summary",
    "export_networth": "/api/integrations/networth",
    "export_spending": "/api/integrations/spending",
}

NOON = dt.datetime.combine(TODAY, dt.time(12))


def at_today():
    """The household's clock at noon on TODAY."""
    return mock.patch("oikonome.localtime.now_local", return_value=NOON)


class Surfaces:
    """`payload[name]` for each of `doors` (default: every one of DOORS),
    `status` (budget.month_status), `email` (the email's context)
    `email_text` and `email_html` (its plain and HTML renders); `hh` is the generated household,
    `client` its owner's session (the caller closes it with close()).
    The household's connection is closed once the payloads are read."""

    def __init__(self, seed: int, doors=tuple(DOORS)):
        import oikonome.web.app as appmod
        from oikonome.web import report
        appmod.DEV_MODE = True
        self.seed = seed
        self.client, _email, self.tid = body_fuzz.signup(appmod.app, "surf")
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.hh = Household(seed, plan=True, conn=self.conn)
        self.payload = {}
        with at_today():
            for name in doors:
                path = DOORS[name]
                r = self.client.get(path)
                assert r.status_code == 200, (seed, path, r.text[:300])
                self.payload[name] = r.json()
            from oikonome.engine import budget
            self.status = budget.month_status(self.conn, TODAY)
            self.email = report.gather(self.conn, TODAY)
            _subject, self.email_text, self.email_html = report.build(
                self.email)
        # many households are read at once: none holds a connection
        self.conn.close()

    def close(self):
        self.client.close()
