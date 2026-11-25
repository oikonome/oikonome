"""A reconnect keeps the forecast's grading history (specs/forecast-grading.md,
"An account that moves"). A reconnect moves the checking account to the id
the fresh link minted, and a restore-merge folds a restored account into the
live one it duplicates; the kept nights name the checking account, so they
move with it. Otherwise every pair straddling the move is skipped as "your
checking account changed" when it is the same account, and the pairs before
it read the ledger of an account that no longer exists — no card payments,
no transfers. A night left naming an account that is gone (one kept before
the move carried nights along) is skipped, never graded against an empty
ledger. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast, forecast_score as fs
from oikonome.engine.compat import jsonb
from oikonome.sync import adopt

from .util import TODAY, add_txn, make_db

H = fs.HORIZONS
NIGHTS = 120


def _plant(conn, acct="chk"):
    """NIGHTS kept nights on `acct` that predicted each balance to within
    a few dollars, a card payment out of `acct` every fifth day, and the
    usual walk assuming a little less than that."""
    first = TODAY - dt.timedelta(days=NIGHTS)
    bal = {first + dt.timedelta(days=k): 4000.0 - 6 * k
           for k in range(NIGHTS + 61)}
    for k in range(NIGHTS):
        d = first + dt.timedelta(days=k)
        at = [round((bal[d + dt.timedelta(days=h)] + (k % 7) - 3) * 100)
              for h in H]
        out = [h * 900 for h in H]
        walks = {s: {"at": at, "cards_out": out}
                 for s in ("usual", "now", "due")}
        conn.execute(
            "INSERT INTO forecast_nights (as_of, model, checking_id, "
            "start_checking, fresh, walks, pace, inputs) VALUES "
            "(%s, 'w-test', %s, %s, true, %s, %s, %s)",
            (d, acct, bal[d], jsonb(walks),
             jsonb({"daily": 6.0,
                    "shares": {"checking": 0.75, "card": 0.25}}),
             jsonb({"cards": [], "card_debt": 0, "events": [],
                    "one_offs": []})))
        if k % 5 == 0:
            add_txn(conn, d, 41.0, "WILLOW CARD PAYMENT", account=acct,
                    primary="LOAN_PAYMENTS",
                    detailed=fs.CARD_PAYMENT)


def _score(conn):
    forecast.forget_learned()
    return fs.compute_score(conn, TODAY, cfg={})


class AReconnectCarriesTheNights(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _plant(self.conn)
        self.before = _score(self.conn)
        # the grading this test protects is a real one
        self.assertGreater(self.before["n"], 0)
        self.assertIsNotNone(self.before["card_paying"])
        self.assertEqual(self.before["skips"]["account_changed"], 0)

    def _checking_ids(self):
        return {r["checking_id"] for r in self.conn.execute(
            "SELECT DISTINCT checking_id FROM forecast_nights")}

    def test_a_reconnect_grades_the_same_as_without_it(self):
        self.conn.execute("INSERT INTO items (id, aggregator) "
                          "VALUES ('it-new', 'test')")
        self.assertTrue(adopt._rewrite_account_id(
            self.conn, "chk", "chk-new", "it-new"))
        self.assertEqual(self._checking_ids(), {"chk-new"})
        self.assertEqual(_score(self.conn), self.before)

    def test_a_restore_merge_grades_the_same_as_without_it(self):
        c = self.conn
        c.execute("INSERT INTO accounts (id, item_id, name, type, subtype, "
                  "balance_current) VALUES ('chk-live', 'it1', "
                  "'Test Checking', 'depository', 'checking', 5000)")
        with c.transaction():
            adopt._move_account_children(c, "chk", "chk-live")
            c.execute("DELETE FROM accounts WHERE id = 'chk'")
        self.assertEqual(self._checking_ids(), {"chk-live"})
        self.assertEqual(_score(c), self.before)

    def _shares(self):
        return [r["s"] for r in self.conn.execute(
            "SELECT pace->'shares' AS s FROM forecast_nights")]

    def test_a_moved_card_keeps_its_share_of_the_pace(self):
        self.conn.execute("INSERT INTO items (id, aggregator) "
                          "VALUES ('it-new', 'test')")
        self.assertTrue(adopt._rewrite_account_id(
            self.conn, "card", "card-new", "it-new"))
        self.assertEqual(
            {tuple(sorted(s.items())) for s in self._shares()},
            {(("card-new", 0.25), ("checking", 0.75))})

    def test_a_merged_card_adds_its_share_to_the_live_ones(self):
        c = self.conn
        c.execute("INSERT INTO accounts (id, item_id, name, type, subtype, "
                  "balance_current) VALUES ('card-live', 'it1', "
                  "'Test Card', 'credit', 'credit card', 250)")
        c.execute("UPDATE forecast_nights SET pace = jsonb_set(pace, "
                  "'{shares,card-live}', '0.5')")
        adopt._move_account_children(c, "card", "card-live")
        adopt._move_account_children(c, "card", "card-live")
        self.assertEqual(
            {tuple(sorted((k, float(v)) for k, v in s.items()))
             for s in self._shares()},
            {(("card-live", 0.75), ("checking", 0.75))})

    def test_carrying_the_nights_twice_changes_nothing(self):
        c = self.conn
        c.execute("INSERT INTO accounts (id, item_id, name, type, subtype, "
                  "balance_current) VALUES ('chk-live', 'it1', "
                  "'Test Checking', 'depository', 'checking', 5000)")
        adopt._move_account_children(c, "chk", "chk-live")
        adopt._move_account_children(c, "chk", "chk-live")
        self.assertEqual(self._checking_ids(), {"chk-live"})


class ANightOnAnAccountThatIsGoneIsSkipped(unittest.TestCase):
    """Nights kept before a move carried them along name an account the
    household no longer has. Their ledger reads as empty — no card
    payment, no transfer — which is not a real record of $0, so those
    pairs are not graded."""

    def test_the_pairs_are_skipped_not_read_as_an_empty_ledger(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _plant(conn, acct="chk")
        live = _score(conn)
        conn.execute("UPDATE forecast_nights SET checking_id = 'gone-acct'")
        got = _score(conn)
        self.assertEqual(got["n"], 0)
        self.assertIsNone(got["card_paying"])
        self.assertIsNone(got["band"])
        self.assertTrue(all(v is None for v in got["quantiles"].values()))
        self.assertGreaterEqual(got["skips"]["account_changed"], live["n"])

    def test_the_miss_of_such_a_night_is_not_explained(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _plant(conn, acct="chk")
        day = TODAY - dt.timedelta(days=3)
        as_of = day - dt.timedelta(days=7)
        self.assertIsNotNone(fs.miss(conn, day, 7, TODAY))
        conn.execute("UPDATE forecast_nights SET checking_id = 'gone-acct' "
                     "WHERE as_of = %s", (as_of,))
        conn.execute("UPDATE forecast_nights SET checking_id = 'gone-acct' "
                     "WHERE as_of = %s", (day,))
        self.assertIsNone(fs.miss(conn, day, 7, TODAY))


class ARecordedNightNamesOneAccount(unittest.TestCase):
    """The reconnect carries a night's `checking_id` and the card keys of
    `pace.shares` (adopt._carry_forecast_nights). Nothing else in a kept
    night may hold an account id, or the move would leave it behind: the
    walks are per scenario and the inputs name a card by its name."""

    def test_no_account_id_inside_the_walks_or_the_inputs(self):
        from .test_the_forecast_keeps_what_it_said import _household
        conn = _household(cards=3, bills=3)
        self.addCleanup(conn.close)
        # the fixture card's id is "card", which is also a key of the
        # inputs' card entries: an id no key can be
        self.assertTrue(adopt._rewrite_account_id(conn, "card",
                                                  "acct-willow-1", "it1"))
        forecast.forget_learned()
        self.assertTrue(fs.record(conn, TODAY))
        ids = {r["id"] for r in conn.execute("SELECT id FROM accounts")}
        row = conn.execute("SELECT walks, pace, inputs, checking_id "
                           "FROM forecast_nights").fetchone()
        self.assertIn(row["checking_id"], ids)
        # the night did name the cards, by their names
        self.assertTrue(row["inputs"]["cards"])

        def strings(v):
            if isinstance(v, str):
                yield v
            elif isinstance(v, dict):
                for k, x in v.items():
                    yield k
                    yield from strings(x)
            elif isinstance(v, list):
                for x in v:
                    yield from strings(x)
        shares = row["pace"]["shares"]
        self.assertLessEqual(set(shares) - {"checking"}, ids)
        rest = {**row["pace"], "shares": list(shares.values())}
        for col, v in (("walks", row["walks"]), ("pace", rest),
                       ("inputs", row["inputs"])):
            held = set(strings(v)) & ids
            self.assertEqual(held, set(), col)


def _respell(data: bytes, ids: dict[str, str], extra_accounts=()) -> bytes:
    """The archive with every account id in `ids` spelled the other way —
    in the account rows, every account_id cell, the nights' checking_id
    and the card keys of their pace shares — plus `extra_accounts` (rows
    copied from the first account with a new id and name)."""
    import csv
    import io
    import json
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            body = zin.read(info)
            if info.filename.endswith(".csv"):
                rows = list(csv.DictReader(io.StringIO(body.decode())))
                if rows:
                    base = info.filename.rsplit("/", 1)[-1]
                    for r in rows:
                        for col in ("account_id", "checking_id") + (
                                ("id",) if base == "accounts.csv" else ()):
                            if r.get(col) in ids:
                                r[col] = ids[r[col]]
                        if base == "forecast_nights.csv" and r.get("pace"):
                            pace = json.loads(r["pace"])
                            pace["shares"] = {ids.get(k, k): v for k, v
                                              in pace["shares"].items()}
                            r["pace"] = json.dumps(pace)
                    if base == "accounts.csv":
                        for aid in extra_accounts:
                            rows.append(dict(rows[0], id=aid,
                                             name=f"Spare {aid}",
                                             display_name=""))
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, list(rows[0]),
                                       lineterminator="\n")
                    w.writeheader()
                    w.writerows(rows)
                    body = buf.getvalue().encode()
            zout.writestr(info, body)
    return out.getvalue()


class ARestoredNightNamesTheAccountItsLedgerLandsOn(unittest.TestCase):
    """An archive's account ids are respelled on the way in (a comma
    becomes '-', or the taken-name form when '-' belongs to another
    account). A restored night names its checking account, and its pace
    names its cards, under the same spelling the account and its ledger
    land under — or its grading reads an account that is not there."""

    def test_a_comma_id_whose_plain_name_is_taken(self):
        import uuid
        from oikonome.db import tenancy
        from oikonome.sync import export, restore
        from .test_the_forecast_keeps_what_it_said import _household
        from .util import _ensure_db
        src = _household(cards=2)
        try:
            forecast.forget_learned()
            self.assertTrue(fs.record(src, TODAY))
            data = export.build_zip(src)
        finally:
            src.close()
        data = _respell(data, {"chk": "chk,a", "card": "card,b"},
                        extra_accounts=("chk-a",))
        _ensure_db()
        admin = tenancy.admin_connect()
        tid = tenancy.create_tenant(admin, f"fn-{uuid.uuid4().hex[:8]}")
        admin.close()
        dest = tenancy.tenant_connect(tid)
        self.addCleanup(dest.close)
        restore.restore_outcomes(dest, data)
        chk = adopt.account_id_when_taken("chk,a")
        self.assertEqual({r["account_id"] for r in dest.execute(
            "SELECT DISTINCT account_id FROM transactions "
            "WHERE name = 'OAKFIELD PAYROLL'")}, {chk})
        night = dest.execute("SELECT checking_id, pace FROM forecast_nights"
                             ).fetchone()
        self.assertEqual(night["checking_id"], chk)
        ids = {r["id"] for r in dest.execute("SELECT id FROM accounts")}
        self.assertIn(chk, ids)
        self.assertIn("card-b", night["pace"]["shares"])
        self.assertLessEqual(set(night["pace"]["shares"]) - {"checking"},
                             ids)


class ANightRecordedDuringTheMoveLandsOnTheNewId(unittest.TestCase):
    """The nightly step (worker) and a reconnect (web) are separate
    processes. A night recorded after the move carried the nights but
    before it committed would otherwise land on the old id, which nothing
    moves again, and its pairs would never be graded. The recorder holds
    the accounts it names against the move, then finds the old one gone
    and records under the new id."""

    def test_the_move_commits_between_the_build_and_the_insert(self):
        import threading
        from .test_reconnect_onto_restored import (_AfterTheCopy,
                                                   _second_connection)
        from .test_the_forecast_keeps_what_it_said import _household
        conn = _household(cards=2)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO items (id, aggregator) "
                     "VALUES ('it-new', 'test')")
        c2 = _second_connection(self, conn)
        c2.execute("SET statement_timeout = '20s'")
        forecast.forget_learned()
        done = []

        def the_nightly_step():
            t = threading.Thread(
                target=lambda: done.append(fs.record(c2, TODAY)))
            t.start()
            t.join(1.5)
            self.thread = t

        proxy = _AfterTheCopy(conn, the_nightly_step,
                              after="DELETE FROM accounts")
        self.assertTrue(adopt._rewrite_account_id(proxy, "chk", "chk-new",
                                                  "it-new"))
        self.thread.join(20)
        self.assertEqual(done, [True])
        self.assertEqual([r["checking_id"] for r in conn.execute(
            "SELECT checking_id FROM forecast_nights")], ["chk-new"])


if __name__ == "__main__":
    unittest.main()
