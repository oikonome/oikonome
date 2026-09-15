"""OFX/QFX import — banks' native Quicken export format, parsed with
ofxtools and deduped the same way every other importer is.

Same conventions as csvimport:
  * OFX TRNAMT is bank-signed (negative = money out) → flipped to the
    engine convention (positive = out).
  * ids prefer the file's FITID (banks keep it stable) namespaced
    "ofx:<account>:<fitid>"; missing FITID falls back to a content hash
    with an occurrence counter.
  * cross-source dedup via the shared one-to-one, name-aware matcher
    (sync.dedup).
  * TRNTYPE XFER maps to TRANSFER_OUT/IN by sign — bank-declared
    transfers must never count as spend.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re

from ofxtools.Parser import OFXTree

from . import flowmap
from .base import Transaction, ensure_split_account, upsert_transactions
from .rowcap import check_len
from .dedup import WINDOW_DAYS as DEDUP_WINDOW_DAYS  # noqa: F401 (re-export)
from .dedup import Deduper


# ofxtools reads OFX (both the SGML v1 and the XML v2 flavour) with its own
# regex tree builder — it never hands the bytes to an XML parser, so an
# entity bomb inside a v2 file is copied through as literal text rather
# than expanded. That is a property of the library's implementation, not
# of its interface (OFXTree subclasses ElementTree and `parse` accepts a
# parser argument), so we do not lean on it: an OFX statement has no
# business carrying a DTD at all, and refusing one up front costs a bank
# nothing while guaranteeing the answer stays "no" if the parser under us
# ever changes. The whole blob is scanned (one linear pass, no copy) so
# padding the prolog cannot push the declaration past the check.
_DTD_MARKER = re.compile(rb"<!(?:doctype|entity)\b", re.IGNORECASE)


def _refuse_dtd(data: bytes) -> None:
    if _DTD_MARKER.search(data):
        raise ValueError("this OFX file declares an XML DTD (<!DOCTYPE / "
                         "<!ENTITY), which no bank statement needs and "
                         "which is refused as unsafe")


def parse_ofx(data: bytes) -> list[dict]:
    """Raw statement transactions from an OFX/QFX blob (bank + credit-card
    statements both)."""
    _refuse_dtd(data)
    tree = OFXTree()
    tree.parse(io.BytesIO(data))
    ofx = tree.convert()
    out = []
    for stmt in ofx.statements:
        acct = getattr(stmt, "account", None)
        acct_hint = getattr(acct, "acctid", None)
        for t in (stmt.transactions or []):
            out.append({
                "fitid": getattr(t, "fitid", None),
                "date": t.dtposted.date() if getattr(t, "dtposted", None) else None,
                "amount": float(t.trnamt) if getattr(t, "trnamt", None) is not None else None,
                "name": (getattr(t, "name", None) or getattr(t, "memo", None) or "?"),
                "memo": getattr(t, "memo", None),
                "type": getattr(t, "trntype", None),
                "acct": acct_hint,
                # statement-level hints for the multi-statement split:
                # bank statements carry ACCTTYPE; card statements come from
                # <CCSTMTRS> whose CCACCTFROM has no accttype at all
                "accttype": getattr(acct, "accttype", None),
                "cc": type(acct).__name__.startswith("CC"),
            })
    return out


def _stmt_target(conn, rows: list[dict]) -> str:
    """The split account for one statement's rows, keyed by ACCTID suffix
    (stable across re-imports; the full account number never lands in an
    id). Type comes from the statement itself — CCSTMTRS → credit,
    ACCTTYPE SAVINGS → savings, else checking."""
    r0 = rows[0]
    if r0["cc"]:
        label, type_, subtype = "Card", "credit", "credit card"
    elif str(r0["accttype"] or "").upper() == "SAVINGS":
        label, type_, subtype = "Savings", "depository", "savings"
    else:
        label = str(r0["accttype"] or "Account").title()
        type_, subtype = "depository", "checking"
    last4 = str(r0["acct"])[-4:]
    return ensure_split_account(conn, "ofx", f"{label} ...{last4}",
                                key=f"{label}-{last4}",
                                type_=type_, subtype=subtype)


def import_ofx(conn, account_id: str, data: bytes,
               batch_id: str | None = None) -> dict:
    """Parse + upsert one OFX/QFX file into `account_id` — unless the file
    carries several statements: banks export checking + savings + card as
    multiple <STMTRS>/<CCSTMTRS> blocks in one file, and merging them into
    the one selected account corrupts transfers and spend. >1 distinct
    ACCTID → split per statement (the same pattern as the competitor
    split), with per-account counts in the result."""
    rows = check_len([r for r in parse_ofx(data)
                      if r["date"] is not None and r["amount"] is not None],
                     what="transactions")
    by_acct: dict = {}
    for r in rows:
        by_acct.setdefault(r["acct"], []).append(r)
    if len([k for k in by_acct if k]) <= 1:
        return _import_into(conn, account_id, rows, batch_id=batch_id)
    total = {"imported": 0, "rows": 0, "skipped_duplicates": 0,
             "split_accounts": {}}
    for acct in sorted(by_acct, key=str):
        grp = by_acct[acct]
        # rows with no ACCTID at all (rare) stay on the user's pick
        target = _stmt_target(conn, grp) if acct else account_id
        out = _import_into(conn, target, grp, batch_id=batch_id)
        for k in ("imported", "rows", "skipped_duplicates"):
            total[k] += out[k]
        name = conn.execute("SELECT name FROM accounts WHERE id=%s",
                            (target,)).fetchone()
        total["split_accounts"][name["name"] if name else target] = \
            out["imported"]
    total["note"] = (f"file contains {len(by_acct)} statements — split "
                     f"into one account per statement (the selected "
                     f"account was not used)")
    return total


def _import_into(conn, account_id: str, rows: list[dict], *,
                 batch_id: str | None) -> dict:
    deduper = Deduper(conn, account_id, "ofx", [r["date"] for r in rows])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for r in rows:
        amount = -r["amount"]                    # bank sign → engine sign
        name = str(r["name"]).strip()
        if deduper.claim(r["date"], amount, name):
            skipped_dupes += 1
            continue
        if r["fitid"]:
            tid = f"ofx:{account_id}:{r['fitid']}"
        else:
            base = (f"{account_id}|{r['date'].isoformat()}|"
                    f"{round(amount * 100)}|{name.lower()}")
            occurrence[base] = occurrence.get(base, 0) + 1
            tid = "ofx:" + hashlib.sha1(
                f"{base}|{occurrence[base]}".encode()).hexdigest()
        # bank-declared transfers → TRANSFER_* by sign (spend-excluded).
        # OFX PAYMENT/DIRECTDEBIT rows that NAME themselves as a
        # credit-card payment are internal settlements, not spend — else the
        # checking-side payment AND the card's charges both count. Kept tight
        # (looks_like_card_payment) so real bill-pay stays spend.
        ttype = str(r.get("type") or "").upper()
        if ttype == "XFER" or flowmap.looks_like_card_payment(name):
            primary = flowmap.transfer_category(amount)
        else:
            primary = None
        # Data minimization, same rule as the MX sync: the statement's
        # ACCTID is the TRUE account number, carried on each row only as a
        # multi-statement routing hint. transactions.raw is plaintext JSONB
        # that rides /export and the data-portability archive, so only the
        # last 4 may land there (all _stmt_target ever needed anyway).
        raw = {k: (v.isoformat() if isinstance(v, dt.date) else v)
               for k, v in r.items()}
        if raw.get("acct"):
            raw["acct"] = str(raw["acct"])[-4:]
        txns.append(Transaction(
            id=tid, account_id=account_id, date=r["date"], amount=amount,
            name=name, category_primary=primary, raw=raw))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
