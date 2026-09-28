"""Enrich Amazon transactions with real item categories from the
amazon_orders table (rows arrive via import/restore; fetching them is a
host-side, opt-in community script). The matching itself is the shared
store matcher — see store_match — with Amazon's windows: the order date
may precede card posting by up to a week, and every Amazon charge is
opaque, so the ones no order explains are stamped "Amazon - Unmatched"
to keep gaps visible."""
from __future__ import annotations

from . import store_match
from .store_match import AMAZON

DATE_WINDOW_BEFORE = AMAZON.window_before
DATE_WINDOW_AFTER = AMAZON.window_after
AMAZON_NAME_RE = AMAZON.name_re


def run_match(conn) -> dict:
    """Match Amazon-merchant transactions against amazon_orders rows."""
    return store_match.run_match(conn, AMAZON)
