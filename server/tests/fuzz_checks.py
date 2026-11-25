"""What every fuzzed reply is held to beyond "no 5xx"
(specs/invariants-and-fuzzing.md, F2, F4 and F5), and the second
household it is held against.

* F2 — a refusal (4xx) writes nothing: no row of any household table
  changes for it (tests/write_detector.py), apart from the reviewed
  bookkeeping in `write_detector.REFUSAL_MAY_WRITE`.
* F4 — nothing crosses households: the canary household's rows never
  change, whatever the actor sends, and none of its canary words appears
  in any reply to the actor.
* F5 — the pool is clean after every request: no backend is left idle
  inside a transaction or idle holding an advisory lock.

`FuzzCase` is the setup every fuzz module shares. `Canary(app)` is the
second household: an owner of its own, with
accounts, an institution, transactions, a note, a bill, goals and a
budget category named with canary words, under ids of its own that the
fuzz sends from the first household (in paths and in bodies). The ids
carry no canary word, so a door that echoes what it was sent is not a
leak; only a reply that carries the second household's own data is.
"""

from __future__ import annotations

import datetime as dt
import uuid

from oikonome.db import tenancy

from . import body_fuzz, write_detector
from .util import add_bill, add_txn, write_config

# words no fixture, hint or generated value of the first household holds
CANARY_WORDS = ("Quokkavale", "Zephyrine", "Marblewick", "Thistlemoor")


class Canary:
    """The second household. `ids` are the ids the fuzz sends from the
    first household; `digest()` reads every one of its rows."""

    def __init__(self, app):
        q, z, m, t = CANARY_WORDS
        tag = uuid.uuid4().hex[:6]
        self.client, self.email, self.tid = body_fuzz.signup(
            app, f"{t.lower()}")
        self.account, self.card = f"zq-acct-{tag}", f"zq-card-{tag}"
        self.txn, self.item = f"zq-txn-{tag}", f"zq-item-{tag}"
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("INSERT INTO items (id, aggregator, institution_name,"
                         " access_token) VALUES (%s,'test',%s,'tok-z')",
                         (self.item, f"{q} Credit Union"))
            for aid, name, typ, sub in (
                    (self.account, f"{z} Checking", "depository", "checking"),
                    (self.card, f"{m} Rewards", "credit", "credit card")):
                conn.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current,balance_available) VALUES "
                    "(%s,%s,%s,%s,%s,321.09,321.09)",
                    (aid, self.item, name, typ, sub))
            day = dt.date(2026, 7, 1)
            add_txn(conn, day, 61.25, f"{t} Bakery", account=self.card,
                    primary="FOOD_AND_DRINK", txn_id=self.txn)
            add_txn(conn, day, -1800, f"{q} Payroll", account=self.account,
                    primary="INCOME", txn_id=f"zq-pay-{tag}")
            conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                         "VALUES (%s, %s)", (self.txn, f"{z} birthday"))
            self.bill = add_bill(conn, "Zeta Utility", 75.0)
            self.entity = str(conn.execute(
                "INSERT INTO business_entity (name, structure) VALUES "
                "(%s, 'sole_prop') RETURNING id", (f"{q} Studio",)
            ).fetchone()["id"])
            self.receipt = str(conn.execute(
                "INSERT INTO receipts (image, mime) VALUES (%s, 'image/png') "
                "RETURNING id", (b"\x89PNG",)).fetchone()["id"])
            self.hook = str(conn.execute(
                "INSERT INTO webhooks (url, secret) VALUES "
                "('https://hooks.example.invalid/in', 'not-a-secret') "
                "RETURNING id").fetchone()["id"])
            from oikonome.engine import links
            self.group = links.create(conn, [self.account, self.card])
            write_config(conn, savings_goals=[
                {"name": f"{m} fund", "target": 900, "monthly_plan": 50,
                 "tokens": [], "start_balance": 0}],
                custom_buckets=[{"name": f"{t} treats", "parent": "food",
                                 "categories": [], "merchants": [],
                                 "monthly": 40.0}])
        finally:
            conn.close()
        # a viewer of its own, so a role or removal door aimed at one of
        # its people has someone to aim at besides the owner
        self.viewer, self.viewer_email = body_fuzz.join(app, self.client,
                                                        "viewer")
        admin = write_detector._admin()
        try:
            self.users = [str(r["id"]) for r in admin.execute(
                "SELECT id FROM users WHERE tenant_id = %s ORDER BY id",
                (self.tid,)).fetchall()]
        finally:
            admin.close()
        # what the first household sends of this one: by path parameter
        # and by body field
        self.path_ids = {"txn_id": self.txn, "account_id": self.account,
                         "entity_id": self.entity, "rid": self.receipt,
                         "hook_id": self.hook, "group_id": self.group,
                         "item_id": self.item, "tenant_id": str(self.tid),
                         "user_id": self.users[-1],
                         "member_id": self.users[-1]}
        self.body_ids = {**self.path_ids, "id": self.txn,
                         "transaction_id": self.txn, "account": self.account,
                         "bill_id": self.bill, "receipt_id": self.receipt,
                         "entity": self.entity, "txn": self.txn}

    def leaked(self, text: str) -> list[str]:
        return [w for w in CANARY_WORDS if w in text] + [
            e for e in (self.email, self.viewer_email) if e in text]

    def digest(self) -> dict:
        """Every row of every household table this household owns."""
        admin = write_detector._admin()
        try:
            out = {}
            for t in write_detector._tables(admin):
                if t in write_detector.REFUSAL_MAY_WRITE:
                    continue
                rows = admin.execute(
                    f'SELECT * FROM "{t}" WHERE tenant_id::text = %s',
                    (str(self.tid),)).fetchall()
                out[t] = sorted(repr(sorted(r.items())) for r in rows)
            return out
        finally:
            admin.close()

    def close(self):
        self.viewer.close()
        self.client.close()


class Checks:
    """`after(where, reply)` → the failures of one request."""

    def __init__(self, canary: Canary):
        self.canary = canary
        self.detector = write_detector.Detector()

    def settle(self):
        """Forget what a household rebuilt between requests wrote."""
        self.detector.delta()

    def after(self, where: str, resp) -> list[str]:
        out = []
        delta = self.detector.delta()
        foreign = {k: n for k, n in delta.items()
                   if k[0] == str(self.canary.tid)}
        if foreign:
            out.append(f"{where} -> {resp.status_code} WROTE THE OTHER "
                       f"HOUSEHOLD: {sorted(foreign)}")
        if 400 <= resp.status_code < 500:
            bad = write_detector.refusal_writes(delta)
            if bad:
                out.append(f"{where} -> {resp.status_code} but wrote "
                           f"{sorted(k[1] for k in bad)}")
        leaked = self.canary.leaked(resp.text)
        if leaked:
            out.append(f"{where} -> {resp.status_code} LEAKED {leaked}")
        stuck = self.detector.stuck_backends()
        if stuck:
            out.append(f"{where} -> pool left {stuck}")
        return out

    def close(self):
        self.detector.close()


class FuzzCase:
    """Mixin for a fuzz module's TestCase: setUpClass installs the write
    detector, builds the other household and the actor's (`ROLE`), and
    proves the checks can see what they look for; tearDownClass removes
    the detector whatever happened."""

    ROLE = "owner"

    @classmethod
    def setUpClass(cls):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        write_detector.install()
        try:
            cls.canary = Canary(appmod.app)
            cls.checks = Checks(cls.canary)
            # the detector counted the other household being built, and
            # its own pages carry its canary words: both checks can see
            # what they look for
            built = {k for k in cls.checks.detector.seen
                     if k[0] == str(cls.canary.tid)}
            assert {"accounts", "transactions"} <= {k[1] for k in built}, built
            own = cls.canary.client.get("/api/accounts").text
            assert cls.canary.leaked(own), "the canary pages show no canary"
            # F5 holds before the first request too: a backend an earlier
            # test left idle in a transaction or holding an advisory lock
            # is that test's leak (the suite has none; the harness's L6
            # holds every scenario to the same rule), named here rather
            # than charged to whichever fuzzed request met it
            assert not cls.checks.detector.stuck_named, (
                "backends stuck before the fuzz began, left by an earlier "
                f"test: {cls.checks.detector.stuck_named}")
            cls.household = body_fuzz.Household(appmod.app, role=cls.ROLE)
            cls.checks.settle()
            cls.untouched = cls.canary.digest()
        except BaseException:
            write_detector.uninstall()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.checks.close()
            cls.canary.close()
        finally:
            write_detector.uninstall()

    def assert_other_household_untouched(self):
        self.assertEqual(self.canary.digest(), self.untouched)


def viewers_own(path: str) -> bool:
    """A write door that is the viewer's own: its account and devices, or a
    read that arrives as a POST."""
    from oikonome.web import permissions
    return (permissions.path_within(path, permissions.SELF_WRITE_OK)
            or permissions.read_only_route(path))


class CrossingCase(FuzzCase):
    """F4 for one role (`ROLE`): every route of the product, sent the
    other household's ids in every path parameter and every body field
    that names a row, and every page with no parameter read. A viewer is
    also refused (403) at each household write door on the way."""

    def test_no_door_reaches_the_other_household(self):
        from oikonome.web.schemas import body_of
        hh = self.household
        routes = [r for r in body_fuzz.product_routes(self.app)
                  if "{" in r.path or body_of(r.endpoint) is not None
                  or "GET" in r.methods]
        self.assertGreater(len(routes), 250)
        writes = {id(r) for r in body_fuzz.write_doors(self.app)}

        def refused(route, resp):
            if (self.ROLE != "viewer" or id(route) not in writes
                    or viewers_own(route.path) or resp.status_code == 403):
                return None
            return "a viewer was not refused"
        hh.path_ids, hh.body_ids = self.canary.path_ids, self.canary.body_ids
        try:
            self.assertEqual(body_fuzz.run_plain(hh, routes, self.checks,
                                                 expect=refused), [])
            self.assertEqual(self._query_ids(hh), [])
        finally:
            hh.path_ids, hh.body_ids = {}, {}
        self.assert_other_household_untouched()

    def _query_ids(self, hh) -> list[str]:
        """Every GET door's query parameter that names a row, sent the
        other household's row of that kind."""
        from oikonome.web import security
        ids = self.canary.body_ids
        failures = []
        for r, _path, query in body_fuzz.text_routes(self.app):
            for name in query:
                n = name.lower()
                value = ids.get(n) or next(
                    (ids[k] for k in ("account", "entity", "txn")
                     if k in n), None) or (ids["txn"] if n.endswith("id")
                                           else None)
                if value is None:
                    continue
                security._limiter._hits.clear()
                resp = hh.client.get(hh.path(r), params={name: value})
                where = f"GET {r.path}?{name}=<other household's>"
                if body_fuzz.server_error(resp):
                    failures.append(f"{where} -> {resp.status_code}")
                failures += self.checks.after(where, resp)
        return failures
