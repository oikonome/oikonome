"""Enrich Costco transactions with what the receipt says was bought,
from the costco_receipts table (rows arrive via the import door; fetching
them is a host-side, opt-in community script). The matching itself is the
shared store matcher — see store_match — with Costco's windows: a
warehouse receipt is dated the day the card was run. Charges no receipt
explains keep their aggregator category: fuel and the membership fee are
already well described, unlike an unexplained Amazon charge."""
from __future__ import annotations

from . import store_match
from .store_match import COSTCO


def run_match(conn) -> dict:
    """Match Costco-merchant transactions against costco_receipts rows."""
    return store_match.run_match(conn, COSTCO)
