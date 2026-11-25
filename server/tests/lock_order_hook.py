"""Every test checks the household lock order it exercises
(specs/invariants-and-fuzzing.md, L1).

The locks a writer WAITS on inside its transaction are taken in one order,
written down beside `erasure.WRITER_LOCKS`. Two writers taking a pair the
other way round can each hold what the other wants, and Postgres then kills
one of them — but only when they happen to meet, which a test of one writer
never shows. So the order is checked on every statement instead: installed
from tests/__init__.py, the hook reads each statement a psycopg cursor runs,
recognises the household locks by their SQL, and records per connection the
ranks taken in the current transaction. Asking for a lower rank after a
higher one (a rank already held may be taken again: the locks are
re-entrant) is a violation, and the test during which it happened fails
when it ends, naming both call sites.

Failing at the end of the test, not by raising in place: a best-effort step
wrapped in `except Exception` would swallow an exception raised inside it,
and the test would pass over the very inversion it ran.

Row locks are out of scope here; the two-connection tests
(tests/lock_order_util.py) catch those through the deadlock count. Try-locks
never wait and so cannot close a cycle; they are not ranked.
"""

from __future__ import annotations

import threading
import traceback
import unittest
import weakref

import psycopg
from psycopg.pq import TransactionStatus

from oikonome.engine.merchant_identity import _IDENTITY_LOCK_SPACE

# The documented order (erasure.WRITER_LOCKS' comment), lowest first.
RANKS = {
    0: "the household bill-claim lock (bills.hold_bill_claims)",
    1: "the file-storage lock (storage_quota.hold_lock)",
    2: "the reimbursement-pairing lock (reanchor.hold_pairing_lock)",
    3: "the merchant-identity lock (merchant_identity._lock_identity)",
    4: "the bill-ceiling lock (bills.lock_active_bills)",
    # a stored tax document's locks (engine/taxdocs.py, _lock_years then
    # lock_document_ids): a restore takes them after the bill-ceiling lock
    # when its income lands, and merges the settings row after; numbered
    # between so the ranks below keep the numbers their seams use
    4.5: "a tax-document year lock (taxdocs._lock_years)",
    4.6: "the tax-document ids lock (taxdocs.lock_document_ids)",
    5: "the settings row (budget.config_txn)",
    6: "the household-member lock (oikonome:tenant-members)",
}


def rank_of(query, params) -> int | None:
    """The rank of the household lock `query` waits on, or None."""
    sql = query if isinstance(query, str) else (
        query.decode() if isinstance(query, bytes) else str(query))
    low = sql.lower()
    # the shared form waits on an exclusive holder just as the plain one
    # does (an edit's live_edit_ids takes the pairing lock so), so it is
    # ranked the same
    if "pg_advisory_xact_lock(" in low \
            or "pg_advisory_xact_lock_shared(" in low:
        text = low + " " + " ".join(str(p) for p in (
            params.values() if isinstance(params, dict) else params or ()))
        if "oikonome:bill-claims:" in text:
            return 0
        if "'bills-active'" in low:
            return 4
        if "'income-documents'" in low:
            return 4.5
        if "'income-document-ids'" in low:
            return 4.6
        if "'oikonome:storage:'" in low:
            return 1
        if "'reimbursement-link'" in low:
            return 2
        if "oikonome:tenant-members:" in text:
            return 6
        if params and not isinstance(params, dict) \
                and params[0] == _IDENTITY_LOCK_SPACE:
            return 3
        return None
    if "tenant_settings" in low and "for update" in low \
            and "nowait" not in low and "skip locked" not in low:
        return 5
    return None


class _Txn:
    __slots__ = ("held",)

    def __init__(self):
        self.held: dict[int, str] = {}       # rank -> where it was taken


_txns: "weakref.WeakKeyDictionary[psycopg.Connection, _Txn]" = (
    weakref.WeakKeyDictionary())
_violations: list[str] = []
_guard = threading.Lock()
_installed = False


def _site() -> str:
    """Where the lock was asked for: the helper that took it and the two
    frames that called it, outside psycopg, the pool wrapper and this hook."""
    frames = []
    for fr in reversed(traceback.extract_stack()[:-3]):
        f = fr.filename
        if "/psycopg" in f or f.endswith(("lock_order_hook.py",
                                          "db/tenancy.py")):
            continue
        frames.append(f"{f.rsplit('/server/', 1)[-1]}:{fr.lineno} {fr.name}")
        if len(frames) == 3:
            break
    return " <- ".join(frames) or "?"


def _observe(cur, query, params) -> None:
    rank = rank_of(query, params)
    conn = cur.connection
    with _guard:
        txn = _txns.get(conn)
        if txn is None or conn.info.transaction_status == TransactionStatus.IDLE:
            txn = _txns[conn] = _Txn()
        if rank is None or rank in txn.held:
            return
        above = [r for r in txn.held if r > rank]
        site = _site()
        if above:
            r = max(above)
            _violations.append(
                f"{RANKS[rank]} asked for at {site} while {RANKS[r]} was "
                f"already held (taken at {txn.held[r]})")
        txn.held[rank] = site


def take_violations() -> list[str]:
    with _guard:
        out = list(dict.fromkeys(_violations))
        _violations.clear()
    return out


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    orig_execute = psycopg.Cursor.execute

    def execute(self, query, params=None, *a, **kw):
        try:
            _observe(self, query, params)
        except Exception:                                # noqa: BLE001
            pass                    # the recorder never breaks a statement
        return orig_execute(self, query, params, *a, **kw)

    psycopg.Cursor.execute = execute

    # A transaction ends without a statement this hook sees (psycopg sends
    # COMMIT and ROLLBACK itself), and the next may BEGIN the same way; so
    # the record is cleared wherever a transaction can end.
    def _ended(conn):
        try:
            if conn.info.transaction_status == TransactionStatus.IDLE:
                with _guard:
                    _txns.pop(conn, None)
        except Exception:                                # noqa: BLE001
            pass

    orig_exit = psycopg.Transaction.__exit__

    def tx_exit(self, *exc):
        try:
            return orig_exit(self, *exc)
        finally:
            _ended(self.connection)

    psycopg.Transaction.__exit__ = tx_exit
    for name in ("commit", "rollback"):
        orig = getattr(psycopg.Connection, name)

        def ending(self, *a, _orig=orig, **kw):
            try:
                return _orig(self, *a, **kw)
            finally:
                _ended(self)
        setattr(psycopg.Connection, name, ending)

    orig_run = unittest.TestCase.run

    def run(self, result=None):
        take_violations()              # anything from before this test
        out = orig_run(self, result)
        found = take_violations()
        if found and result is not None:
            try:
                raise AssertionError(
                    "household locks taken out of the documented order "
                    "(erasure.WRITER_LOCKS):\n  " + "\n  ".join(found))
            except AssertionError as e:
                import sys
                result.addFailure(self, sys.exc_info())
                del e
        return out

    unittest.TestCase.run = run
