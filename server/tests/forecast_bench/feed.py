"""A simulated World served to the production Plaid client.

`SimPlaid(world).transport()` is an `httpx.MockTransport` for the Plaid
endpoints the nightly pull calls: `/accounts/get` (balances as of the
feed's day), `/transactions/sync` (a cursor per served day; a pending row
is removed and comes back posted under a new id with
`pending_transaction_id` set, as Plaid does), `/liabilities/get` (each
card's last statement, minimum, due date and last payment as of the day)
and `/item/get`. Set `feed.day` before each sync; the feed only ever moves
forward, as a real Item does.

The World's account ids are the Plaid account ids, so a forecast's account
ids read back as the World's.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx

PAGE = 250


class SimPlaid:
    def __init__(self, world, *, link: dt.date | None = None):
        self.world = world
        self.day: dt.date = world.start
        # a household that linked late sees no row dated before it
        self.link = link or world.start
        self.calls: dict[str, int] = {}
        self._views: dict[dt.date, dict] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- the record as of a day ----------------------------------------

    def _row(self, t, pending: bool) -> dict:
        merchant = (None if t.kind in ("card_payment", "transfer", "fee",
                                       "interest", "cash")
                    else t.name.title())
        return {
            "transaction_id": t.pending_id if pending else t.id,
            "account_id": t.account,
            "date": (t.date if pending else t.post).isoformat(),
            "authorized_date": t.date.isoformat(),
            "amount": t.amount, "iso_currency_code": "USD",
            "name": t.name, "merchant_name": merchant,
            "original_description": t.name,
            "pending": pending,
            "pending_transaction_id": (None if pending else t.pending_id),
            "payment_channel": "other",
            "personal_finance_category": {"primary": t.primary,
                                          "detailed": t.detailed,
                                          "confidence_level": "HIGH"},
        }

    def view(self, day: dt.date | None) -> dict:
        """{transaction id: row} as the bank shows it at the end of `day`."""
        if day is None:
            return {}
        if day not in self._views:
            out = {}
            for t in self.world.ledger:
                if t.date > day or t.date < self.link:
                    continue
                if t.post <= day:
                    out[t.id] = self._row(t, False)
                else:
                    out[t.pending_id] = self._row(t, True)
            self._views = {k: v for k, v in self._views.items()
                           if k >= day - dt.timedelta(days=40)}
            self._views[day] = out
        return self._views[day]

    def balances(self, day: dt.date) -> list[dict]:
        w = self.world
        out = []
        for a in w.accounts:
            aid = a["id"]
            posted = w.posted[aid].get(day, 0.0)
            if a["type"] == "credit":
                bal = {"current": round(posted, 2),
                       "available": round(10000 - posted, 2),
                       "limit": 10000.0}
            else:
                bal = {"current": round(posted, 2),
                       "available": round(w.available[aid].get(day, 0.0), 2),
                       "limit": None}
            out.append({"account_id": aid, "name": a["name"],
                        "official_name": a["name"], "type": a["type"],
                        "subtype": a["subtype"], "mask": a["mask"],
                        "balances": {**bal, "iso_currency_code": "USD"}})
        return out

    def liabilities(self, day: dt.date) -> list[dict]:
        w = self.world
        out = []
        for c in w.persona.cards:
            cid = c["id"]
            stmts = [s for s in w.statements[cid] if s.close < day]
            pays = [t for t in w.ledger if t.account == cid
                    and t.kind == "card_payment" and t.post <= day]
            last = stmts[-1] if stmts else None
            lp = pays[-1] if pays else None
            # once a due date has passed the issuer names the next one:
            # the next statement's close plus the grace days
            due = None
            if last is not None:
                due = last.due
                if due < day:
                    from .world import _add_months_clamped
                    nxt = _add_months_clamped(last.close, 1, c["close_day"])
                    due = nxt + dt.timedelta(days=c["grace"])
            out.append({
                "account_id": cid,
                "aprs": [{"apr_percentage": round(c["apr"] * 100, 2),
                          "apr_type": "purchase_apr"}],
                "is_overdue": False,
                "last_payment_amount": round(-lp.amount, 2) if lp else None,
                "last_payment_date": lp.post.isoformat() if lp else None,
                "last_statement_balance": last.balance if last else None,
                "last_statement_issue_date": (last.close.isoformat()
                                              if last else None),
                "minimum_payment_amount": last.minimum if last else None,
                "next_payment_due_date": due.isoformat() if due else None,
            })
        return out

    # ---- the endpoints -------------------------------------------------

    def _sync(self, body: dict) -> dict:
        cur = body.get("cursor") or ""
        target = self.day
        if "|" in cur:
            prev_s, target_s, off_s = cur.split("|")
            prev = dt.date.fromisoformat(prev_s) if prev_s else None
            target = dt.date.fromisoformat(target_s)
            off = int(off_s)
        else:
            prev = dt.date.fromisoformat(cur) if cur else None
            off = 0
        if prev is not None and prev > target:
            raise ValueError("the feed only moves forward")
        old, new = self.view(prev), self.view(target)
        added = [new[k] for k in sorted(new) if k not in old]
        removed = ([{"transaction_id": k} for k in sorted(old)
                    if k not in new] if off == 0 else [])
        page = added[off:off + PAGE]
        more = off + PAGE < len(added)
        nxt = (f"{prev.isoformat() if prev else ''}|{target.isoformat()}|"
               f"{off + PAGE}" if more else target.isoformat())
        return {"added": page, "modified": [], "removed": removed,
                "next_cursor": nxt, "has_more": more,
                "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                "request_id": f"sim-{target.isoformat()}-{off}"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] = self.calls.get(path, 0) + 1
        body = json.loads(request.content or b"{}")
        if path == "/accounts/get":
            out = {"accounts": self.balances(self.day),
                   "item": {"item_id": "bench-item"},
                   "request_id": "sim-accounts"}
        elif path == "/transactions/sync":
            out = self._sync(body)
        elif path == "/liabilities/get":
            out = {"accounts": self.balances(self.day),
                   "liabilities": {"credit": self.liabilities(self.day),
                                   "mortgage": [], "student": []},
                   "request_id": "sim-liabilities"}
        elif path == "/item/get":
            out = {"item": {"item_id": "bench-item", "webhook": ""},
                   "status": {"transactions": {
                       "last_successful_update":
                           f"{self.day.isoformat()}T23:00:00Z",
                       "last_failed_update": None}},
                   "request_id": "sim-item"}
        else:
            return httpx.Response(400, text=json.dumps({
                "error_type": "INVALID_REQUEST",
                "error_code": "NOT_SIMULATED",
                "error_message": f"the bench does not serve {path}",
                "request_id": "sim-none"}))
        return httpx.Response(200, text=json.dumps(out))
