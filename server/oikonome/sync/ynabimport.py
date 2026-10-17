"""YNAB register-export importer — tier 1 of the import hub. YNAB's
register CSV carries separate Outflow/Inflow columns (both positive,
currency-formatted); categories are user-defined so they pass through as
raw metadata only (the recurring finder + future categorizer do the rest).

Detection: header contains Date + Payee + Outflow + Inflow.
Sign: Outflow → engine positive (money out); Inflow → negative.
ids: ynab:<sha1|occurrence>; cross-source dedup via the shared sync.dedup
matcher. YNAB's transfer marker ("Transfer : <Account>" payee) and
"Starting Balance" rows map to TRANSFER_* so they never count as spend.
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from ..engine.compat import as_date
from . import flowmap, reimport
from .base import (FileImportIds, Transaction, drop_before_history, mask_file_raw,
                   upsert_transactions)
from .csvimport import bounded_dict_reader, date_order, scrub_identifiers
from .rowcap import capped
from .dedup import Deduper

YNAB_COLUMNS = {"date", "payee", "outflow", "inflow"}


# The register's own columns number about a dozen. Every row becomes a
# dict over the header (twice, with the lower-cased copy) and its raw
# copy, so the row cap alone does not bound the memory: a header of a
# thousand empty columns over the capped row count is a few MB of file
# and gigabytes of dicts, in the process every household shares. A
# header past this is refused before a row is read.
MAX_COLUMNS = 40


def _check_width(fieldnames) -> None:
    if fieldnames is not None and len(fieldnames) > MAX_COLUMNS:
        raise ValueError(
            f"this file has {len(fieldnames):,} columns (max "
            f"{MAX_COLUMNS}) — export the register as it comes, without "
            f"added columns")


def looks_like_ynab(header: list[str]) -> bool:
    return YNAB_COLUMNS <= {h.strip().lower().lstrip('"') for h in header}


def _money(s: str) -> float:
    s = (s or "").strip().replace("$", "").replace(",", "")
    return float(s) if s else 0.0


def _date(s: str, order: str = "mdy") -> dt.date:
    fmts = (("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y") if order == "mdy"
            else ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"))
    for fmt in ("%Y-%m-%d", *fmts, "%Y/%m/%d"):
        try:
            d = dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
        # a year the rest of the app cannot read is refused, never stored
        return as_date(d)
    raise ValueError(f"unrecognized YNAB date: {s!r}")


class _Line(NamedTuple):
    """One register line as the importer reads it."""
    date: dt.date
    amount: float          # the current reading: the column names the side
    signed_as: float       # the earlier reading: out - in, signs kept
    name: str
    payee: str
    raw: dict              # the scrubbed source row
    account: str           # the YNAB account the line names ("" for none)
    stored_account: str | None   # that cell as a stored row's raw keeps it
    account_key: str | None      # the Account header as this file spells it


NO_ACCOUNT_LABEL = "(no account name)"


def label(name: str) -> str:
    """How a YNAB account is named to the person."""
    return name or NO_ACCOUNT_LABEL


def _account_key(row: dict) -> str | None:
    """The register's Account header as this file spells it (a quoted or
    byte-order-marked first column still reads as Account)."""
    for k in row:
        if k is not None and (k.strip().lstrip("\ufeff").strip('"')
                              .strip().lower()) == "account":
            return k
    return None


def parse(text: str) -> list[_Line]:
    """Every line of the register the importer would store, in file
    order. Refuses (ValueError) a date it cannot read."""
    reader = bounded_dict_reader(text)
    _check_width(reader.fieldnames)
    # day-first or month-first is a property of the FILE (see
    # csvimport.date_order) — read the whole column before parsing a row
    # a data row with more cells than the header (a trailing comma, an
    # unquoted comma in a memo) carries the overflow under the key None;
    # it is dropped rather than failing the whole file
    rows = [{k: v for k, v in row.items() if k is not None}
            for row in capped(reader, what="transactions")]
    lows = [{k.strip().lower(): (v or "") for k, v in row.items()}
            for row in rows]
    order = date_order(low.get("date", "") for low in lows)
    key = _account_key(rows[0]) if rows else None
    lines: list[_Line] = []
    for row, low in zip(rows, lows):
        if not low.get("date"):
            continue
        out = _money(low.get("outflow", ""))
        inn = _money(low.get("inflow", ""))
        if out == 0 and inn == 0:
            continue
        # engine sign: positive = out. The column names the direction, so
        # a sign printed inside it (a converter writing inflows negative)
        # is dropped rather than flipping the row.
        amount = abs(out) - abs(inn)
        date = _date(low["date"], order)
        payee = (low.get("payee") or "").strip()
        name = (payee or low.get("memo") or "?").strip()
        # the whole source row lands in transactions.raw, and looks_like_ynab
        # matches on a column SUBSET — a register re-exported through a bank
        # or converter tool can carry an account/routing column into it, and
        # raw rides /export verbatim. Same scrub as the CSV importer; the
        # YNAB "Account" column is a budget-account nickname and is kept.
        raw = scrub_identifiers(row)
        account = (row.get(key) or "").strip() if key else ""
        stored = (mask_file_raw(raw, name, payee or None).get(key)
                  if key else None)
        lines.append(_Line(date, amount, out - inn, name, payee, raw,
                           account, stored, key))
    return lines


def account_names(lines: list[_Line]) -> list[str]:
    """The YNAB accounts the register's lines name, in first-seen order."""
    return list(dict.fromkeys(ln.account for ln in lines))


def multi_account_refusal(names: list[str]) -> str:
    """The refusal a register of several YNAB accounts gets from a door
    that cannot ask where each one goes."""
    shown = ", ".join(label(n) for n in names[:6])
    if len(names) > 6:
        shown += ", …"
    return (f"this YNAB register covers {len(names)} YNAB accounts "
            f"({shown}) — import it on its own to choose where each goes "
            f"(update the app if it does not ask), or export one account "
            f"at a time")


def _earlier_ids_on(conn, lines: list[_Line], pick: str) -> list[set[str]]:
    """Per line, the ids an earlier import of the WHOLE register into
    `pick` gave it: numbered over every line of the file, under both the
    current reading of the line's sign and the earlier one, each with its
    pre-masking twin and whatever id the row stands under after the
    account changed id."""
    out: list[set[str]] = [set() for _ in lines]
    readings = [False]
    if any(round(ln.signed_as * 100) != round(ln.amount * 100)
           for ln in lines):
        readings.append(True)
    for printed in readings:
        ids = FileImportIds("ynab", pick)
        txns = []
        for i, ln in enumerate(lines):
            amount = ln.signed_as if printed else ln.amount
            tid = ids.id_for(ln.date, amount, ln.name)
            out[i].add(tid)
            legacy = ids.legacy_id(tid)
            if legacy:
                out[i].add(legacy)
            txns.append(Transaction(id=tid, account_id=pick, date=ln.date,
                                    amount=amount, name=ln.name))
        for i, t in enumerate(ids.resolve(conn, txns)):
            out[i].add(t.id)
    return out


def _earlier_lines_on(conn, lines: list[_Line], pick: str,
                      names) -> list[tuple[int, str]]:
    """(file line, row id) for each live row on `pick` an earlier import
    of the whole register put there for a YNAB account in `names`.

    A row counts only when BOTH hold: its id is exactly an id that import
    gave one of this account's lines, and its own raw records that line's
    Account. Nothing but `pick` is looked at, and nothing is matched by
    content alone — a row that merely looks like a card line is not one."""
    names = set(names)
    mine = [i for i, ln in enumerate(lines) if ln.account in names]
    if not mine or not pick:
        return []
    cands = _earlier_ids_on(conn, lines, pick)
    want: dict[str, list[int]] = {}
    for i in mine:
        for c in cands[i]:
            want.setdefault(c, []).append(i)
    key = lines[0].account_key
    if not want or key is None:
        return []
    hits: list[tuple[int, str]] = []
    for r in conn.execute(
            "SELECT id, raw->>%s AS acct FROM transactions"
            " WHERE account_id = %s AND removed = 0 AND id = ANY(%s)",
            (key, pick, sorted(want))).fetchall():
        for i in want[r["id"]]:
            if r["acct"] is not None and r["acct"] == lines[i].stored_account:
                hits.append((i, r["id"]))
                break
    return hits


def earlier_on_pick(conn, lines: list[_Line], pick: str,
                    names) -> dict[str, list[str]]:
    """The live rows on `pick` an earlier import of the whole register put
    there, per YNAB account in `names` (see _earlier_lines_on)."""
    hits: dict[str, list[str]] = {}
    for i, rid in _earlier_lines_on(conn, lines, pick, names):
        hits.setdefault(lines[i].account, []).append(rid)
    return {n: list(dict.fromkeys(v)) for n, v in hits.items()}


def _foreign_left(conn, lines: list[_Line], pick: str, names,
                  proven: set[str]) -> int:
    """Rows on `pick` that record one of these YNAB accounts but carry no
    id this register gives — another export's, which nothing proves came
    from this file. Left where they are and counted."""
    key = lines[0].account_key if lines else None
    values = sorted({ln.stored_account for ln in lines
                     if ln.account in set(names) and ln.stored_account})
    if key is None or not values:
        return 0
    return int(conn.execute(
        "SELECT count(*) AS n FROM transactions"
        " WHERE account_id = %s AND removed = 0 AND left(id, 5) = 'ynab:'"
        "   AND raw->>%s = ANY(%s) AND NOT id = ANY(%s)",
        (pick, key, values, sorted(proven))).fetchone()["n"])


def summary(conn, lines: list[_Line], pick: str) -> list[dict]:
    """The register's YNAB accounts for the person to place: rows, the
    dates they span, and how many rows an earlier whole-register import
    left on the picked account."""
    names = account_names(lines)
    stored = earlier_on_pick(conn, lines, pick, names)
    out = []
    for n in names:
        dates = [ln.date for ln in lines if ln.account == n]
        out.append({"name": n, "label": label(n), "rows": len(dates),
                    "first_date": min(dates).isoformat(),
                    "last_date": max(dates).isoformat(),
                    "stored_on_pick": len(stored.get(n, []))})
    return out


def import_ynab(conn, account_id: str, text: str,
                batch_id: str | None = None,
                accounts: dict | None = None,
                remove_earlier: dict | None = None) -> dict:
    """Import a YNAB register into `account_id`, the account the person
    picked.

    A register of one YNAB account lands there. A register of several
    needs `accounts`: every YNAB account it names mapped to an existing
    account id, or to None to leave it out — and is refused without one,
    because putting a card's lines on checking counts them twice.
    `remove_earlier` names the YNAB accounts whose rows an earlier
    whole-register import left on `account_id` are to be removed."""
    lines = parse(text)
    names = account_names(lines)
    if accounts is None:
        if len(names) > 1:
            raise ValueError(multi_account_refusal(names))
        result = _import_mapped(conn, lines, [account_id] * len(lines),
                                batch_id)
        result.pop("_ids", None)
        result.pop("_landed", None)
        return result
    missing = [n for n in names if n not in accounts]
    if missing:
        raise ValueError("choose where these YNAB accounts go: "
                         + ", ".join(label(n) for n in missing))
    dest_of = [accounts[ln.account] or None for ln in lines]
    if not any(dest_of):
        raise ValueError("every YNAB account in the file is skipped — "
                         "nothing to import")
    # a placed register writes to several accounts and may remove rows:
    # the pairing lock before the first row any of that touches, so this
    # import keeps the one order every writer keeps (pairing lock, then
    # rows) — a correction or a removal would otherwise ask for it after
    # the first account's rows were written
    from .reanchor import hold_pairing_lock
    hold_pairing_lock(conn)
    out = {"removed_earlier": 0, "kept_annotated": 0, "foreign_left": 0}
    # the rows an earlier whole-register import left on the pick for the
    # accounts now going elsewhere, read before this import writes a row
    away = [n for n in names if accounts[n] != account_id]
    asked = [n for n in away if (remove_earlier or {}).get(n) is True]
    doomed: list[str] = []
    moved: set[int] = set()
    if away and account_id:
        hits = _earlier_lines_on(conn, lines, account_id, away)
        proven = {rid for _, rid in hits}
        out["foreign_left"] = _foreign_left(conn, lines, account_id, away,
                                            proven)
        asked_set = set(asked)
        doomed = list(dict.fromkeys(
            rid for i, rid in hits if lines[i].account in asked_set))
        # the lines whose earlier row is about to leave the pick: each is
        # the household's own row changing account, so it lands at its
        # destination whatever the history window says — left out there,
        # the removal below would delete it from the ledger altogether
        moved = {i for i, _ in hits if lines[i].account in asked_set
                 and dest_of[i]}
    # the import first: a refusal it raises (a dedup window too large)
    # then leaves the ledger as it was, removals included
    result = _import_mapped(conn, lines, dest_of, batch_id, moved=moved)
    if doomed:
        from . import batches
        gone = batches.retire_rows(conn, [i for i in doomed
                                          if i not in result["_ids"]])
        out["removed_earlier"] = gone["removed"]
        out["kept_annotated"] = gone["kept_annotated"]
        out["kept_by_feed"] = gone["kept_by_feed"]
        out["kept_archived"] = gone["kept_archived"]
    # rows each YNAB account put in the ledger (its lines a feed already
    # had are counted under skipped_duplicates instead)
    per: dict[str, int] = {}
    for i in result.pop("_landed"):
        n = label(lines[i].account)
        per[n] = per.get(n, 0) + 1
    result.pop("_ids", None)
    result.update(out)
    result["split_accounts"] = per
    notes = []
    if out["removed_earlier"]:
        n = out["removed_earlier"]
        notes.append(
            f"Removed {n} row{'s' if n != 1 else ''} an earlier import of "
            f"this register had put on the picked account. Undoing this "
            f"import does not bring {'them' if n != 1 else 'it'} back.")
    held = out.get("kept_by_feed", 0) + out.get("kept_archived", 0)
    if held:
        notes.append(
            f"Kept {held} of those row{'s' if held != 1 else ''}: a bank "
            f"feed relies on {'them' if held != 1 else 'it'}, or "
            f"{'they belong' if held != 1 else 'it belongs'} to an archived "
            f"business.")
    if notes:
        result["note"] = " ".join(notes)
    return result


def _import_mapped(conn, lines: list[_Line], dest_of: list,
                   batch_id: str | None,
                   moved: set[int] | frozenset = frozenset()) -> dict:
    """Write each line to its destination (`dest_of[i]`, None to leave it
    out).

    Each destination numbers its ids over EVERY line of the file in file
    order, keeping only its own: a line's id then depends on the file and
    where it lands, never on where the other accounts went, and an
    account that lands where a whole-register import once put the file
    gets exactly the ids it got then. The dedup runs against each
    destination's own rows, so a card line meets the card's feed.

    `moved` names the lines whose earlier row on another account is being
    removed by this import: they are not new to the household, so the
    history window does not leave them out."""
    txns_all: list[Transaction] = []
    landed: list[int] = []
    skipped_dupes = corrected = skipped_older = 0
    for dest in dict.fromkeys(d for d in dest_of if d):
        mine = [d == dest for d in dest_of]
        deduper = Deduper(conn, dest, "ynab",
                          [ln.date for ln, m in zip(lines, mine) if m])
        ids = FileImportIds("ynab", dest)
        # the ids the rows had while a sign printed inside a direction
        # column still flipped the row, numbered the same way; only a row
        # whose amount that rule changed has a candidate (see
        # reimport.adopt_rule_change)
        old_ids = FileImportIds("ynab", dest)
        rule_changed = any(round(ln.signed_as * 100) != round(ln.amount * 100)
                           for ln, m in zip(lines, mine) if m)
        txns: list[Transaction] = []
        line_of: list[int] = []     # the file line each of txns came from
        old_of: list[list[tuple[str, float]]] = []
        absorbed = []   # lines the feed now holds whose earlier row must go
        for i, (ln, m) in enumerate(zip(lines, mine)):
            if m and deduper.claim(ln.date, ln.amount, ln.name):
                skipped_dupes += 1
                if rule_changed and round(ln.signed_as * 100) != round(ln.amount * 100):
                    # the earlier import landed (and numbered) this row
                    oid = old_ids.id_for(ln.date, ln.signed_as, ln.name)
                    absorbed.append((ln.date, ln.amount, (ln.name, ln.payee),
                        [(x, ln.signed_as) for x in (oid, old_ids.legacy_id(oid)) if x],
                        lambda row, raw=dict(ln.raw), n=ln.name, m=ln.payee:
                            reimport.same_line_raw(row["raw"], raw, n, m)))
                continue
            tid = ids.id_for(ln.date, ln.amount, ln.name)
            oid = (old_ids.id_for(ln.date, ln.signed_as, ln.name)
                   if rule_changed else None)
            if not m:
                continue
            line_of.append(i)
            old_of.append(
                [(x, ln.signed_as) for x in (oid, old_ids.legacy_id(oid))
                 if x]
                if oid and round(ln.signed_as * 100) != round(ln.amount * 100)
                else [])
            # YNAB's transfer marker + starting-balance rows are flows, not
            # spend/income: outflow → TRANSFER_OUT, inflow → TRANSFER_IN
            pl = ln.payee.lower()
            primary = (flowmap.transfer_category(ln.amount)
                       if pl.startswith("transfer : ")
                       or pl == "starting balance" else None)
            txns.append(Transaction(
                id=tid, account_id=dest, date=ln.date, amount=ln.amount,
                name=ln.name, merchant_name=ln.payee or None,
                category_primary=primary, raw=dict(ln.raw)))
        txns = ids.resolve(conn, txns)
        corrected += reimport.adopt_rule_change(
            conn, dest, txns, old_of, same_line=reimport.same_line_raw)
        corrected += reimport.retire_absorbed(conn, dest, "ynab", absorbed,
                                              taken={t.id for t in txns})
        # the household's history window (Settings → how far back), applied
        # after the ids are numbered over every line, so a line it leaves
        # out does not shift the numbering of the lines that land, and after
        # the re-import bookkeeping, so a row an earlier import stored under
        # the old sign rule is still moved and then corrected by the write
        inside, older = drop_before_history(
            conn, [t for i, t in zip(line_of, txns) if i not in moved])
        skipped_older += older
        keep = {id(t) for t in inside} | {
            id(t) for i, t in zip(line_of, txns) if i in moved}
        landed.extend(i for i, t in zip(line_of, txns) if id(t) in keep)
        txns = [t for t in txns if id(t) in keep]
        if batch_id:
            from . import batches
            batches.tag(conn, txns, batch_id)
        txns_all.extend(txns)
    added = upsert_transactions(conn, txns_all)
    return {"imported": added, "rows": len(txns_all),
            "skipped_duplicates": skipped_dupes, "corrected": corrected,
            "skipped_older": skipped_older,
            "_ids": {t.id for t in txns_all}, "_landed": landed}
