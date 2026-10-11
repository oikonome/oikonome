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

from ..engine.compat import as_date
from . import flowmap
from .base import (FileImportIds, Transaction, _descriptor,
                   ensure_split_account, mask_long_digits,
                   upsert_transactions)
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


def _posted_date(t) -> dt.date | None:
    """DTPOSTED as a date, or None — which import_ofx already skips. A
    well-formed year the rest of the app cannot read (0001, 9999) is
    treated like a missing date, so that row is dropped, never stored."""
    posted = getattr(t, "dtposted", None)
    if not posted:
        return None
    try:
        return as_date(posted.date())
    except ValueError:
        return None


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
        window = _statement_window(stmt)
        for t in (stmt.transactions or []):
            out.append({
                "fitid": getattr(t, "fitid", None),
                "date": _posted_date(t),
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
                # the days this statement speaks for (DTSTART..DTEND):
                # a re-import may only restate a stored charge it covers
                "window": window,
            })
    return out


def _statement_window(stmt) -> tuple[dt.date, dt.date] | None:
    """The statement's DTSTART..DTEND as dates, or None when absent or
    unreadable."""
    tl = getattr(stmt, "banktranlist", None)
    start, end = getattr(tl, "dtstart", None), getattr(tl, "dtend", None)
    if not start or not end:
        return None
    try:
        return as_date(start.date()), as_date(end.date())
    except ValueError:
        return None


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


class _FitidIds:
    """Row ids from the bank's FITID — which is a hint, not a key.

    Some institutions build FITIDs from date+amount, and some number rows
    per download (1, 2, 3 …), so one FITID can name two different charges,
    in one file or across two. Keyed on the FITID alone, the second upsert
    would rewrite the first row's date, amount and name in place and the
    first charge would be gone with nothing said. So the plain
    `ofx:<account>:<fitid>` id belongs to ONE charge — the one already
    stored under it, else the first in this file — and any other charge
    carrying that FITID gets the id suffixed by its own date and amount
    (plus an occurrence number for an identical repeat).

    Minting that suffixed id is only right for a charge the account does
    not already hold, so before it, in order:

    - a FITID whose stored row is a DIFFERENT charge has shown itself to be
      reused, so a row already stored with this line's exact date, amount
      and name (under whatever FITID it came in with) is this line — the
      bank renumbered it between downloads;
    - a FITID with no row under the account's current id may have one
      under an id the account stood under before (a reconnect adoption or
      a comma rename moves the rows but keeps their ids), found by the same
      FITID and the same date, amount and name;
    - a line on a never-seen FITID takes a row with its exact date, amount
      and name stored under a FITID that IS a line of this file but holds
      a different charge there — a new row shifted the bank's numbering,
      pushing this charge onto the next FITID;
    - the stored row under the FITID is the bank restating that charge (a
      posting day shifting, a settled amount), and is updated in place,
      only when it is unmistakably the same economic line: same name and
      sign, the same date or the same amount within a few days, a date
      inside the statement window this file covers, no other free stored
      row or pending line that pairs as well, and no line of the file on
      a FITID whose stored charge is a different one that vanished from
      the file. Each of those is the shape of a bank numbering its rows
      per download, where the FITID names a new charge — and overwriting
      would lose the old one, while declining at worst keeps a twin the
      user can delete.

    Each stored row is taken by at most one line, and a line whose FITID
    the bank keeps stable is never merged into another FITID's row of the
    same content, so two genuinely identical charges stay two rows."""

    RESTATE_DAYS = 3

    def __init__(self, conn, account_id: str, rows: list[dict]):
        self.account_id = account_id
        # a set: every stored row is tested against it, and a list scan
        # made a big re-import stored-rows x file-lines
        bases = {self._base(r["fitid"]) for r in rows if r["fitid"]}
        # base id -> (date, cents, name) of the row stored under it
        self._owner: dict[str, tuple] = {}
        # (date, cents, name) -> ids of the OFX rows on the account
        self._stored: dict[tuple, list[str]] = {}
        # (date, name) -> (cents, id) of the same; a restated amount keeps
        # the day
        self._by_day: dict[tuple, list[tuple]] = {}
        self._windows = self._covered_spans(rows)
        if not bases:
            return
        # Only rows a line could be are loaded: the FITIDs' own rows, and
        # rows dated within a restatement's reach of the file's lines.
        # Every other match is on an exact date the file carries.
        reach = dt.timedelta(days=self.RESTATE_DAYS)
        dates = [r["date"] for r in rows if r["fitid"]]
        for x in conn.execute(
                "SELECT id, date, amount, name FROM transactions "
                "WHERE account_id = %s AND left(id, 4) = 'ofx:' "
                "AND (id = ANY(%s) OR date BETWEEN %s AND %s) "
                "ORDER BY id",
                (account_id, list(bases), min(dates) - reach,
                 max(dates) + reach)).fetchall():
            key = (as_date(x["date"]), round(float(x["amount"]) * 100),
                   self._name_key(x["name"]))
            self._stored.setdefault(key, []).append(x["id"])
            self._by_day.setdefault((key[0], key[2]), []).append(
                (key[1], x["id"]))
            if x["id"] in bases:
                self._owner[x["id"]] = key

    @staticmethod
    def _covered_spans(rows: list[dict]) -> list[tuple]:
        """The day ranges this file speaks for: each statement's declared
        window, and the span of its own lines (a statement without a
        window, or a bank listing a line just outside its own)."""
        spans = {r["window"] for r in rows if r.get("window")}
        dates = [r["date"] for r in rows if r.get("date")]
        if dates:
            spans.add((min(dates), max(dates)))
        return sorted(spans)

    def _covered(self, day) -> bool:
        return any(lo <= day <= hi for lo, hi in self._windows)

    @classmethod
    def _restates(cls, a: tuple, b: tuple) -> bool:
        """Whether (date, cents, name) b can be the bank restating a: same
        name and sign, and the same day or the same amount a few days
        apart."""
        if a[2] != b[2] or (a[1] > 0) - (a[1] < 0) != (b[1] > 0) - (b[1] < 0):
            return False
        return a[0] == b[0] or (
            a[1] == b[1] and abs((a[0] - b[0]).days) <= cls.RESTATE_DAYS)

    def _near(self, key: tuple, exact: dict, by_day: dict) -> set:
        """Everything in the two indexes that `_restates` pairs with key."""
        out = set()
        for d in range(-self.RESTATE_DAYS, self.RESTATE_DAYS + 1):
            out.update(exact.get(
                (key[0] + dt.timedelta(days=d), key[1], key[2]), ()))
        out.update(x for c, x in by_day.get((key[0], key[2]), ())
                   if (c > 0) - (c < 0) == (key[1] > 0) - (key[1] < 0))
        return out

    @staticmethod
    def _name_key(name) -> str:
        # the form the upsert stores a file row's name in
        return (_descriptor(mask_long_digits(str(name or "").strip()))
                or "").lower()

    def _base(self, fitid) -> str:
        return f"ofx:{self.account_id}:{fitid}"

    def _foreign_twin(self, fitid, key, claimed) -> str | None:
        """An unclaimed row with this exact content stored under the same
        FITID but another account id."""
        own = f"ofx:{self.account_id}:"
        for sid in self._stored.get(key, ()):
            if sid in claimed or sid.startswith(own):
                continue
            if sid.split("~", 1)[0].endswith(f":{fitid}"):
                return sid
        return None

    def assign(self, rows: list[tuple]) -> list[str | None]:
        """The id of each (fitid, date, amount, name) row, in order; None
        for a row without a FITID."""
        out: list[str | None] = [None] * len(rows)
        claimed: set[str] = set()
        occ: dict[tuple, int] = {}
        seen: list[tuple] = []
        for fitid, date, amount, name in rows:
            if not fitid:
                seen.append(())
                continue
            base, content = self._base(fitid), (date, round(amount * 100))
            n = occ[(base, content)] = occ.get((base, content), 0) + 1
            seen.append((base, content, n,
                         content + (self._name_key(name),)))
        # 1. the line the FITID's stored row already is
        for i, s in enumerate(seen):
            if s and s[0] in self._owner and s[2] == 1 \
                    and self._owner[s[0]][:2] == s[1]:
                out[i] = s[0]
                claimed.add(s[0])
        # 2. the same line stored under another FITID or account id
        for i, s in enumerate(seen):
            if not s or out[i]:
                continue
            base, content, n, key = s
            own = self._owner.get(base)
            hit = None
            if own is None:
                hit = self._foreign_twin(rows[i][0], key, claimed)
            elif own[:2] != content:
                hit = next((sid for sid in self._stored.get(key, ())
                            if sid not in claimed), None)
            if hit:
                out[i] = hit
                claimed.add(hit)
        # 3. a charge pushed onto a never-seen FITID by a shifted
        # sequence: its stored row sits under a FITID this file reuses for
        # another charge (so the row is in _owner and no line claimed it).
        # A row whose FITID is absent from the file, or still names the
        # same charge, is left alone — those FITIDs are stable, and an
        # identical charge on a new FITID is a new charge. Exact content
        # outranks a restatement, so this runs first.
        for i, s in enumerate(seen):
            if not s or out[i] or s[0] in self._owner:
                continue
            hit = next((sid for sid in self._stored.get(s[3], ())
                        if sid in self._owner and sid not in claimed), None)
            if hit:
                out[i] = hit
                claimed.add(hit)
        # 4. the bank restating the charge its FITID's stored row holds
        for i, tid in enumerate(self._restatements(seen, out, claimed)):
            if tid:
                out[i] = tid
                claimed.add(tid)
        # 5. a charge the account does not hold yet
        for i, s in enumerate(seen):
            if not s or out[i]:
                continue
            base, content, n, key = s
            if base not in self._owner and base not in claimed:
                out[i] = base
                claimed.add(base)
                continue
            tag = hashlib.sha1(
                f"{content[0].isoformat()}|{content[1]}".encode()
            ).hexdigest()[:12]
            tid = f"{base}~{tag}" + (f"#{n}" if n > 1 else "")
            while tid in claimed:
                n += 1
                tid = f"{base}~{tag}#{n}"
            out[i] = tid
            claimed.add(tid)
        return out

    def _restatements(self, seen, out, claimed) -> list[str | None]:
        """The stored row each still-unplaced line restates in place, if
        any; decided against one snapshot so the answer does not depend on
        line order."""
        found: list[str | None] = [None] * len(seen)
        # A line on a FITID whose stored charge is a different one that no
        # line of this file accounts for: the bank reused the FITID for a
        # new charge, so in this file a FITID pairs nothing by itself.
        for s in seen:
            if s and s[0] in self._owner and s[0] not in claimed \
                    and not self._restates(self._owner[s[0]], s[3]):
                return found
        pending = [i for i, s in enumerate(seen) if s and not out[i]]
        exact: dict[tuple, list[int]] = {}
        by_day: dict[tuple, list[tuple]] = {}
        for i in pending:
            key = seen[i][3]
            exact.setdefault(key, []).append(i)
            by_day.setdefault((key[0], key[2]), []).append((key[1], i))
        free = {k: [x for x in v if x not in claimed]
                for k, v in self._stored.items()}
        free_day = {k: [(c, x) for c, x in v if x not in claimed]
                    for k, v in self._by_day.items()}
        for i in pending:
            base, _, _, key = seen[i]
            own = self._owner.get(base)
            if own is None or base in claimed \
                    or not self._restates(own, key) \
                    or not self._covered(own[0]):
                continue
            # the pairing must be the only one either way round: another
            # free stored row as near the line, or another line as near
            # the stored row, and the FITID alone is choosing — which a
            # per-download numbering makes a coin toss
            if self._near(key, free, free_day) == {base} \
                    and self._near(own, exact, by_day) == {i}:
                found[i] = base
        return found


def _import_into(conn, account_id: str, rows: list[dict], *,
                 batch_id: str | None) -> dict:
    deduper = Deduper(conn, account_id, "ofx", [r["date"] for r in rows])
    txns: list[Transaction] = []
    ids = FileImportIds("ofx", account_id)
    fitids = _FitidIds(conn, account_id, rows)
    skipped_dupes = 0
    kept = []
    for r in rows:
        amount = -r["amount"]                    # bank sign → engine sign
        name = str(r["name"]).strip()
        if deduper.claim(r["date"], amount, name):
            skipped_dupes += 1
            continue
        kept.append((r, amount, name))
    # FITID ids are decided for the whole file at once: which stored row a
    # line is depends on which rows the other lines already are
    by_fitid = fitids.assign([(r["fitid"], r["date"], amount, name)
                              for r, amount, name in kept])
    for (r, amount, name), tid in zip(kept, by_fitid):
        if tid is None:
            tid = ids.id_for(r["date"], amount, name)
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
               for k, v in r.items() if k != "window"}
        if raw.get("acct"):
            raw["acct"] = str(raw["acct"])[-4:]
        # NAME/MEMO can carry the same number in free text ("ONLINE
        # TRANSFER TO CHK 1234567890123"); the stored name is masked, and
        # raw must not keep the copy the name lost
        for k in ("name", "memo"):
            raw[k] = mask_long_digits(raw.get(k))
        txns.append(Transaction(
            id=tid, account_id=account_id, date=r["date"], amount=amount,
            name=name, category_primary=primary, raw=raw))
    txns = ids.resolve(conn, txns)
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
