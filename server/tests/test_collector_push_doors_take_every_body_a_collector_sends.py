"""The collector push doors hand every body a collector sends to its importer
exactly as sent.

A push script calls /api/import/coinbase, /amazon and /costco with a
scoped token, and a script on an older version must keep working against
a newer server: a door stricter than what it sends would stop its data
without anyone noticing until the numbers went stale. The doors read their
bodies through the typed reader, so this pins, with bodies of the shapes
the collectors build (every key they set,
the keys the importer does not read, nulls where a collector leaves a
value out, and the longest list the importer takes), that:

* the importer receives exactly what was sent;
* each push lands, and a repeat lands nothing new;
* the refusals are the ones a collector has always been given — anything
  but a list of rows is "payload needs a non-empty … list", one row past
  the importer's limit is "too many …", both as `bad payload: …`;
* a `kind` that is not one of the two names pins nothing and refuses
  nothing.

Every value here is invented.
"""

import copy
import datetime as dt
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, write_config

DAY = (dt.date.today() - dt.timedelta(days=3)).isoformat()


def _coinbase_payload() -> dict:
    """The shape the Coinbase collector builds: a normalized payload with
    its own bookkeeping keys, an account with nulls it never fills, and
    transactions carrying the exchange's raw record."""
    return {
        "source": "coinbase", "generated_at": DAY + "T04:10:00+00:00",
        "prefix": "coinbase:",
        "items": [{"id": "coinbase-shape", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-shape", "item_id": "coinbase-shape",
                      "name": "Coinbase (shape)", "type": "investment",
                      "subtype": "crypto", "mask": None,
                      "balance_current": 812.4, "balance_available": None,
                      "currency": "USD", "raw": {}}],
        "transactions": [
            {"id": f"coinbase:shape-{i}", "account_id": "coinbase-shape",
             "date": DAY, "amount": amt, "name": f"Coinbase {kind} XYZ",
             "merchant_name": "Coinbase",
             "category_primary": ("TRANSFER_OUT" if amt > 0
                                  else "TRANSFER_IN"),
             "category_detailed": f"COINBASE_{kind.upper()}",
             "pending": 0,
             "raw": {"id": f"shape-{i}", "type": kind,
                     "status": "completed",
                     "amount": {"amount": "0.01", "currency": "XYZ"},
                     "native_amount": {"amount": str(amt),
                                       "currency": "USD"}}}
            for i, (kind, amt) in enumerate((("buy", 50.0),
                                             ("sell", -20.0)))],
        "kind": "api"}


def _amazon_order(i: int) -> dict:
    """One row the order collector builds, keys and all."""
    return {"account": "main", "date": DAY, "amount": -(10.0 + i),
            "payee": "Amazon", "seller": "Example Seller",
            "memo": "a widget", "category": "Shopping",
            "order_number": f"900-0000-{i:04d}", "is_refund": 0,
            "payment_method": "Example card",
            "items": [{"title": "A widget", "price": 10.0 + i,
                       "quantity": 1},
                      {"title": "A gift card", "price": None,
                       "quantity": None}],
            "dedup_key": f"az-shape-{i}"}


def _costco_receipt(i: int, online: bool = False) -> dict:
    """One row the receipt collector builds, in both of its forms."""
    item = {"item_number": f"{1000 + i}", "description": "EXAMPLE ITEM",
            "description2": "", "department": 0 if online else 17,
            "amount": 12.5, "unit": 1, "category": "Shopping"}
    if online:
        item.update(unit_price=12.5, status="Delivered")
    return {"account": "main", "date": DAY, "amount": -12.5,
            "receipt_type": "online" if online else "warehouse",
            "warehouse": "example.com" if online else "EXAMPLE WHSE",
            "barcode": f"online:{i}" if online else f"9990{i:05d}",
            "payment_method": "" if online else "CARD",
            "is_refund": 0, "items": [item], "category": "Shopping",
            "dedup_key": f"cs-shape-{i}-{int(online)}"}


class CollectorPushDoorsTakeEveryCollectorBody(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"collect-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()
        minted = cls.client.post("/api/tokens", json={
            "name": "collectors", "password": "correct-horse-battery"}).json()
        cls.bare = TestClient(appmod.app)
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _push(self, door, body):
        return self.bare.post(f"/api/import/{door}", headers=self.hdr,
                              json=body)

    def _handed(self, door, target, body):
        """What the door hands its importer for `body`."""
        seen = []

        def capture(conn, rows):
            seen.append(copy.deepcopy(rows))
            return {"ok": True, "pushed": 0}
        with mock.patch(target, side_effect=capture):
            r = self._push(door, body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(seen), 1)
        return seen[0]

    # ---- the importer sees what was sent ----------------------------

    def test_the_coinbase_importer_receives_the_whole_payload(self):
        sent = _coinbase_payload()
        sent["a_key_a_newer_collector_adds"] = {"nested": [1, None]}
        got = self._handed("coinbase",
                           "oikonome.sync.coinbase_push.import_payload", sent)
        self.assertEqual(got, sent)

    def test_the_order_importer_receives_every_row_as_sent(self):
        rows = [_amazon_order(i) for i in range(3)]
        got = self._handed(
            "amazon", "oikonome.sync.amazon_orders.import_orders",
            {"orders": rows, "kind": "scrape", "source": "amazon",
             "days": 30})
        self.assertEqual(got, rows)

    def test_the_receipt_importer_receives_every_row_as_sent(self):
        rows = [_costco_receipt(0), _costco_receipt(1, online=True)]
        got = self._handed(
            "costco", "oikonome.sync.costco_receipts.import_receipts",
            {"receipts": rows, "kind": "scrape", "source": "costco"})
        self.assertEqual(got, rows)

    def test_the_longest_list_each_importer_takes_reaches_it(self):
        from oikonome.sync import amazon_orders, costco_receipts
        for door, target, key, n, row in (
                ("amazon", "oikonome.sync.amazon_orders.import_orders",
                 "orders", amazon_orders.MAX_ORDERS, _amazon_order(0)),
                ("costco", "oikonome.sync.costco_receipts.import_receipts",
                 "receipts", costco_receipts.MAX_RECEIPTS,
                 _costco_receipt(0))):
            with self.subTest(door):
                rows = [row] + [0] * (n - 1)
                got = self._handed(door, target, {key: rows,
                                                  "kind": "scrape"})
                self.assertEqual(len(got), n)
                self.assertEqual(got[0], row)

    # ---- the pushes land ---------------------------------------------

    def test_each_collector_body_lands_and_a_repeat_lands_nothing(self):
        bodies = (
            ("coinbase", _coinbase_payload()),
            ("amazon", {"orders": [_amazon_order(i) for i in range(2)],
                        "kind": "scrape"}),
            ("costco", {"receipts": [_costco_receipt(0),
                                     _costco_receipt(1, online=True)],
                        "kind": "scrape"}))
        for door, body in bodies:
            with self.subTest(door):
                first = self._push(door, body)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json()["pushed"], 2)
                again = self._push(door, body)
                self.assertEqual(again.status_code, 200, again.text)
                if door != "coinbase":
                    self.assertEqual(again.json()["new"], 0)
        conn = tenancy.tenant_connect(self.tid)
        try:
            n = {t: conn.execute(f"SELECT count(*) AS n FROM {t} "
                                 f"WHERE {w}").fetchone()["n"]
                 for t, w in (
                     ("transactions", "id LIKE 'coinbase:shape-%'"),
                     ("amazon_orders", "dedup_key LIKE 'az-shape-%'"),
                     ("costco_receipts", "dedup_key LIKE 'cs-shape-%'"))}
            kinds = {r["source"]: r["kind"] for r in conn.execute(
                "SELECT source, kind FROM script_heartbeats").fetchall()}
        finally:
            conn.close()
        self.assertEqual(n, {"transactions": 2, "amazon_orders": 2,
                             "costco_receipts": 2})
        self.assertIn("scrape", kinds.values())

    # ---- the refusals a collector has always had ----------------------

    def test_a_body_without_a_list_of_rows_keeps_its_old_refusal(self):
        for door, key in (("amazon", "orders"), ("costco", "receipts")):
            for body in ({}, {key: None}, {key: []}, {key: {"a": 1}},
                         {key: "rows"}, {key: 7}):
                with self.subTest(door=door, body=body):
                    r = self._push(door, body)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertEqual(
                        r.json()["detail"],
                        f"bad payload: payload needs a non-empty {key} list")

    def test_one_row_past_the_limit_keeps_its_old_refusal(self):
        from oikonome.sync import amazon_orders, costco_receipts
        for door, key, n in (("amazon", "orders", amazon_orders.MAX_ORDERS),
                             ("costco", "receipts",
                              costco_receipts.MAX_RECEIPTS)):
            with self.subTest(door):
                r = self._push(door, {key: [0] * (n + 1)})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"],
                                 f"bad payload: too many {key} (max {n})")

    def test_a_kind_of_another_type_pins_nothing_and_is_not_refused(self):
        for kind in (7, ["api"], {"a": 1}, None, "API", True):
            with self.subTest(kind=kind):
                r = self._push("amazon", {"orders": [_amazon_order(0)],
                                          "kind": kind})
                self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
