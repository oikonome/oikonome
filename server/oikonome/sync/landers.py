"""One declared lander per restored table, driven by one step.

A restore lands every archive member inside one transaction. Each table
used to be its own loop inside `restore._restore_zip`, so its idea of "the
same row" and "already held" was spelled inline, once per loop. A lander
declares those once: which members it reads, the cells a row cannot do
without, the parents it hangs on, how a row's cells become the values it
writes, and its identity, which is the one definition of "the same row"
the write's presence check, the restore's "present" outcome and the
round-trip test all read. `land_one` runs one lander at the point in the
restore where its loop stood, so the order, the locks and the transaction
are what they were. Design and the remaining tables: specs/restore-landers.md.
"""
from __future__ import annotations

import functools
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping

from ..engine import compliance as _compliance
from ..engine import selfemploy as _selfemploy
from . import restore as R

# Rows written per statement batch. A member is streamed row by row; this
# bounds what one batch holds in memory, not what a restore can carry.
BATCH = 500


@dataclass(frozen=True)
class Refuse:
    """A row this lander will not write, and why (a REFUSALS key). `over`
    names the bound an out-of-range value passed, when it has one, so the
    summary can say which."""
    reason: str
    over: str | None = None
    # how many archived rows the refusal covers (a whole split group)
    n: int = 1


@dataclass(frozen=True)
class Columns:
    """An identity made of column values: two rows are the same row when
    these columns hold the same values."""
    columns: tuple[str, ...]

    def __call__(self, row: Mapping) -> tuple:
        return tuple(row.get(c) for c in self.columns)


def columns(*cols: str) -> Columns:
    return Columns(tuple(cols))


@dataclass(frozen=True)
class Replace:
    """What an archived row does to the row the destination already holds
    under its key: these columns take `set`'s expressions, only `when` the
    condition holds (a person's pin over a bill's, an answer over an offer
    still asked). Otherwise the held row stays and the archived one counts
    as present. `when_params` names values the condition reads beyond the
    written columns, appended to the row's parameters in that order."""
    set: tuple[tuple[str, str], ...]
    when: str
    when_params: tuple[str, ...] = ()

    def clause(self, target: str) -> str:
        sets = ", ".join(f"{c} = {e}" for c, e in self.set)
        return f"ON CONFLICT {target} DO UPDATE SET {sets} WHERE {self.when}"


@dataclass(frozen=True)
class Exact:
    """Held when the destination has a row under the household's own unique
    key on the identity columns: the write is `ON CONFLICT DO NOTHING`, or
    `replace` says when the archived row takes the held one's place.
    `any_key`: a clash with ANY unique key of the table counts as held (a
    table whose identity is one of several keys it keeps)."""
    replace: Replace | None = None
    any_key: bool = False

    def clause(self, lander: "Lander") -> str:
        target = ("" if self.any_key else
                  "(tenant_id, " + ", ".join(lander.identity.columns) + ")")
        if self.replace is not None:
            return self.replace.clause(target)
        return f"ON CONFLICT {target} DO NOTHING".replace("  ", " ")


@dataclass(frozen=True)
class ByKey:
    """Held when a row with the same identity values is already here, for a
    table whose identity no unique index states. NULL matches NULL, so a
    row with a blank identity cell is still found on a second restore.
    `why` says why no unique index can state it (a catalogue test reads
    it, so a table that could have one gets one instead). `any_key`: a
    clash with a unique key the table does keep is held too."""
    why: str = ""
    any_key: bool = False
    # identity column -> its type, for a column whose blank archived value
    # matches any held value (a rename without its time is the same rename
    # at any time: it lands stamped now(), which no later restore can name)
    blank_is_any: Mapping[str, str] = field(default_factory=dict)

    def clause(self, lander: "Lander") -> str:     # pragma: no cover
        raise TypeError("ByKey writes through Lander.sql, not a clause")


@dataclass(frozen=True)
class Counted:
    """Held per copy, not per key: each copy of an identity the household
    held before the restore accounts for one archived copy (the W-2 rule,
    specs/w2-wages.md "Restore"). The lander's source decides it over all
    of a member's rows at once, so the write itself only refuses a
    `conflict` key it lands under; `why` says why no index can state it."""
    conflict: tuple[str, ...]
    why: str = ""

    def clause(self, lander: "Lander") -> str:
        return ("ON CONFLICT (tenant_id, " + ", ".join(self.conflict)
                + ") DO NOTHING")


@dataclass(frozen=True)
class Group:
    """Rows travel as one group per identity (a charge's split): the
    destination keeps its own group, else takes the archived one whole,
    written by the lander's `write`."""
    why: str = ""

    def clause(self, lander: "Lander") -> str:
        return ""


# the household a row is written under, spelled as the tables' own default
TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


class ContextNotReady(RuntimeError):
    """A lander read restore state before the step that sets it ran: the
    restore's order is wrong, not the archive."""


@dataclass
class Ctx:
    """The restore's shared state that landers read. Each field is set by
    the restore at the point it becomes true, never before; a lander names
    the fields it reads in `reads`, and reading one still unset is an
    error that names it."""
    # the restore's connection, for a parse that asks the household
    conn: object = None
    # transaction ids the restore re-keyed, archived id -> the id here
    # (None when the restore runs without the plan)
    carried: dict | None = None
    # The accounts here, each snapshot taken where the restore has always
    # taken it, under its own name so each has one writer: before the
    # ledger, before the links, before the sources, and (`accounts`) before
    # the balances, holdings and liabilities
    ledger_accounts: set | None = None
    link_accounts: set | None = None
    source_accounts: set | None = None
    accounts: set | None = None
    # the accounts that came back with their connection
    adoptable: set | None = None
    # the businesses here once the archive's own have landed
    entities: set | None = None
    # the bills this restore landed under another id or payee
    bill_ids: dict | None = None
    bill_payees: dict | None = None
    # the merchants here, and the archive's ones folded onto live ones
    known_merchants: set | None = None
    # the merchant ids this restore wrote (the summary takes back the ones
    # the post-commit merchant pass prunes)
    landed_merchants: list | None = None
    merchant_map: dict | None = None
    # live machine merchant names cut here (lowered whole -> cut), and the
    # names people gave, lowered
    machine_cut: dict | None = None
    persons_live: set | None = None
    # (a file-import row keys on the cut descriptor, a row keys on it whole)
    alias_where: Callable | None = None
    # (raw, whole canonical) of each machine alias this restore cut
    cut_aliases: set | None = None
    # the pins this restore wrote, and whether the archive marks the ones a
    # link made (None until its first pin row is read)
    restored_pins: list | None = None
    link_marked: bool | None = None
    # the links this restore wrote, (expense_id, reimburse_id), and the
    # ones being written: each charge they name is held to its cost once
    # every link is back
    restored_links: set | None = None
    pending_links: list | None = None
    # the link groups this restore added members to, and the groups of the
    # links being written
    touched_groups: set | None = None
    pending_groups: list | None = None
    # account lineage rows this restore wrote
    lineage: int = 0
    # transaction notes cut to the stored length, told once in the log
    cut_notes: int = 0
    # the businesses this restore wrote, whose tax reserve is judged once
    # their accounts have landed
    landed_entities: list | None = None
    # the businesses this restore gave a type in place of one the Business
    # page does not offer, id -> name, until they land
    retyped_entities: dict | None = None
    # the businesses of the archive that could not be read (skipped, or
    # refused as malformed), and the accounts booked to one of them that
    # are landing as personal
    unread_entities: set | None = None
    personal_ids: set | None = None
    # the merchant names the archive says a person gave
    persons_names: set | None = None
    # the restore's summary sentences
    notes: list | None = None
    # second fronted movements for one charge this restore left out
    fronted_clash: int = 0
    # account ids the archive spells with a comma, and the name each
    # restores under
    renamed: dict | None = None
    # the room left under the active-bill ceiling, the bills that came
    # back archived past it, the due dates left out, and the cap state of
    # the bill being written
    active_room: int = 0
    bills_over_cap: int = 0
    bad_due: int = 0
    last_bill: tuple | None = None
    # whether the archive predates the W-2 figure marks, the years whose
    # income row this restore seeded, and the years a W-2 landed in
    pre_226: bool | None = None
    # whether the archive predates transactions.category_source (098)
    pre_098: bool | None = None
    seeded_years: set | None = None
    w2_years: set | None = None
    # the years an archived income row came for; the archive's stored
    # documents' ids → the ids they landed under (None: here already);
    # whether the archive carries them at all; the years their values wrote
    income_years: set | None = None
    # the years whose income row this restore landed
    landed_income_years: set | None = None
    # the (year, column) pairs the household's own live documents give
    local_cols: set | None = None
    upload_ids: dict | None = None
    upload_kinds: dict | None = None
    # the archived id of the upload being written, until it lands
    upload_pending: int | None = None
    archive_has_uploads: bool | None = None
    upload_years: set | None = None
    # the archive being restored, for a parse that reads another member
    z: object = None
    # the restore's tally, for a hook that counts what a write repaired
    out: object = None
    # the archive's settings document, checked at the door
    cfg_in: object = None
    # the day the restore runs, and the household's alerts by identity
    # (every row of one alert, under either spelling)
    today: object = None
    held_alerts: dict | None = None
    # what the settings merge decided, told after the commit: whether an AI
    # task's text now goes somewhere new, the credentials not carried onto
    # a moved address, and the daily email's recipients the archive brought
    route_moved: bool = False
    left_behind: list = field(default_factory=list)
    archived_recipients: list = field(default_factory=list)
    # the accounts this restore created, under the ids a re-key gave them
    settled_accounts: set | None = None
    # the push door each csv-named connection was fed by, read on demand
    push_fed: dict | None = None
    # the live merchants by aggregator entity, the archive's merchants
    # folded onto them, and the entity of the merchant being written
    live_entities: dict | None = None
    folded: list | None = None
    last_merchant: tuple | None = None
    # the connections here, the accounts this restore created, and the
    # hand-made accounts whose id still spells a full number
    known_items: set | None = None
    landed_accounts: set | None = None
    numbered_ids: list | None = None
    # the accounts a stand-in note may name, and the card accounts a bare
    # "payment" reads as a settlement on
    noted_accounts: set | None = None
    cards: set | None = None
    # the rows written carrying an override, the OFX rows written, the
    # person's categories for rows already here, and, beside each row of
    # the write in progress, (lands at 0 for want of an amount, carries an
    # override)
    overridden: list | None = None
    landed_ofx: list | None = None
    user_ids: list | None = None
    user_cats: list | None = None
    txn_pending: list | None = None

    def prepare(self, conn, z, *, notes: list, cfg_in) -> None:
        """Read the archive against what the household held when the
        restore began, before any row lands: the account ids it renames,
        the lineage cut ids follow, and which archived transaction lands
        under which id here (`carried`). The exact map first: the pairing
        after it reads the archive through it, so a row it places is never
        offered as a twin of another."""
        self.conn, self.z, self.notes, self.cfg_in = conn, z, notes, cfg_in
        planned = R._COMMA_RENAMES.get()
        if planned is not None:
            planned.update(R._plan_comma_renames(conn, z))
        moved = R._MOVED_ACCOUNTS.get()
        if moved is not None:
            R._resolve_cut_lineage(conn, z, moved)
        carried = R._MOVED_TXNS.get()
        if carried is not None:
            carried.update(R._plan_keyed_txns(conn, z))
            carried.update(R._plan_foreign_keyed_txns(conn, z, carried))
            carried.update(R._plan_moved_txns(conn, z))
            carried.update(R._plan_plain_by_content(conn, z, carried))
        self.carried = carried

    def _ready(self, name: str):
        v = getattr(self, name)
        if v is None:
            raise ContextNotReady(f"restore: ctx.{name} read before it was set")
        return v

    @staticmethod
    def business_id(v) -> str:
        """A business id as it is stored: uuids are kept lower case."""
        return (v or "").strip().lower()

    def entity(self, v):
        """The business id `v` names, as stored, or None when it is not
        here."""
        v = self.business_id(v)
        return v if v in self._ready("entities") else None

    def remap(self, kind: str, v):
        if kind == "account":
            return R._account_id(v)
        if kind == "business":
            return self.business_id(v)
        if kind == "bill":
            # a bill this restore landed under its cut id
            return self._ready("bill_ids").get(v, v or None)
        # a lander naming a kind no branch answers is a defect, never a
        # cell: not a ValueError, which the driver reads as the row's
        raise LookupError(f"no such restore map: {kind}")

    def holds(self, conn, kind: str, v) -> bool:
        if kind == "transaction":
            return R._txn_exists(conn, v)
        if kind == "account":
            return v in self._ready("accounts")
        if kind == "link_account":
            return v in self._ready("link_accounts")
        if kind == "source_account":
            return v in self._ready("source_accounts")
        if kind == "adoptable":
            return v in self._ready("adoptable")
        if kind == "business":
            return self.entity(v) is not None
        if kind == "receipt":
            # a value no receipt id can be is no receipt here, never a
            # query the database refuses
            try:
                v = str(uuid.UUID(str(v)))
            except ValueError:
                return False
            return conn.execute("SELECT 1 FROM receipts WHERE id = %s",
                                (v,)).fetchone() is not None
        raise LookupError(f"no such restore parent: {kind}")


# Restore steps outside any lander that set a Ctx field, by the field. The
# order test finds each one's assignment in `_restore_zip` and checks it
# comes before every lander that reads the field.
# The fields Ctx.prepare sets, before the restore's transaction opens.
PREPARED = ("conn", "z", "notes", "cfg_in", "carried")


@dataclass(frozen=True)
class Lander:
    table: str
    members: tuple[str, ...]
    columns: tuple[str, ...]
    parse: Callable
    identity: Columns
    presence: Exact | ByKey | Counted | Group = Exact()
    # cells a row cannot do without: a blank one refuses the row as
    # malformed before anything else is asked of it
    required: tuple[str, ...] = ()
    # column -> the expression a blank cell takes instead (a timestamp the
    # restore stamps itself)
    defaults: Mapping[str, str] = field(default_factory=dict)
    # column -> the expression it is always written as (no parameter)
    fixed: Mapping[str, str] = field(default_factory=dict)
    # column -> (an expression with placeholders, the values feeding them
    # in order): a value the database decides as it writes the row (the
    # one household member a business may have)
    computed: Mapping[str, tuple[str, tuple[str, ...]]] = field(
        default_factory=dict)
    # (conn, z, ctx, out) -> the archived rows in the order they land,
    # where a member is read whole first: its order is not the member's own
    # (one fronted movement per charge is kept by a rule), or a row's fate
    # is decided over all of them (bill moves, counted documents). Rows it
    # refuses on the way it counts itself.
    source: Callable | None = None
    # (conn, ctx, out, lander, values) -> writes parsed rows a single
    # INSERT cannot (a split group replaced whole, an alert muted under
    # every spelling it is held by), counting what it wrote
    write: Callable | None = None
    # raw cells a lander's own queries read before the write, judged
    # against their columns first (parent cells always are)
    queried: tuple[str, ...] = ()
    # a taken id moves to the next free one (MAX+1), which only the row
    # before it written can make right: one row per write
    allocates_ids: bool = False
    # a row's parse or landed hook reads what the row before it wrote (the
    # live merchant of an entity, a quota, a cap counted in rows landed):
    # one row per write
    sequential: bool = False
    # column -> the type its parameter is cast to
    casts: Mapping[str, str] = field(default_factory=dict)
    remap: Mapping[str, str] = field(default_factory=dict)
    # A row's own check, after its required cells and maps and before its
    # parents: Refuse, or None to go on. It sees every row in order.
    pre: Callable | None = None
    # column -> (parent kind, "orphaned" | "orphaned_if_set" | "null")
    parents: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    # the parents are asked after the cells' own values, where the restore
    # has always refused a bad value before a missing parent
    parents_last: bool = False
    # A row is several spellings of one archived row (an alias under its
    # cut and its whole key): parse returns a list of values, landed and
    # counted as one.
    spellings: bool = False
    # Rows per write. 1 where a row's check reads what an earlier row of
    # the same table wrote (a quota reserved per new receipt, a lineage
    # cap counted in rows landed).
    batch: int = BATCH
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    locks: tuple[Callable, ...] = ()
    plan: Callable | None = None
    # the column the write hands back per row, where it is not the first
    # identity column (an alert's id, which later rows of the alert mute)
    returning: str | None = None
    # (ctx, results): the RETURNING value of each row of a write, in order,
    # None for a row not written
    landed: Callable | None = None
    after_land: tuple[Callable, ...] = ()
    # (ctx, row): a row skipped, or refused as malformed, so a later
    # lander can say what it did with the rows that pointed at it
    on_unread: Callable | None = None

    def __post_init__(self):
        if (isinstance(self.presence, Exact) and self.presence.any_key
                and self.presence.replace is not None):
            # a replace names the key it replaces under; "any key" has none
            raise ValueError(f"{self.table}: Replace needs a conflict key")
        if self.write is not None and (self.landed is not None
                                       or self.spellings):
            # a custom write counts its own rows; nothing comes back per row
            raise ValueError(f"{self.table}: a custom write has no per-row "
                             "results to hand on")
        if (self.allocates_ids or self.sequential) and self.batch != 1:
            raise ValueError(f"{self.table}: an id taken as MAX+1 needs the "
                             "row before it written")
        if isinstance(self.presence, ByKey) and self.computed:
            raise ValueError(f"{self.table}: ByKey looks for written cells")
        judged = {*self.queried, *self.parents}
        for col, (expr, keys) in self.computed.items():
            if col not in self.columns:
                raise ValueError(f"{self.table}: computed {col} is not written")
            if col in {*self.fixed, *self.defaults, *self.casts}:
                raise ValueError(f"{self.table}: {col} is computed and "
                                 "declared another way too")
            if expr.count("%s") != len(keys):
                raise ValueError(f"{self.table}: computed {col} has "
                                 f"{expr.count('%s')} placeholders for "
                                 f"{len(keys)} values")
            # The write cannot judge a value inside an expression, so every
            # value the expression reads besides the column's own is one
            # the driver judges before any query (a parent or `queried`
            # cell, see _fit_cells).
            unjudged = [k for k in keys if k != col and k not in judged]
            if unjudged:
                raise ValueError(f"{self.table}: computed {col} reads "
                                 f"{unjudged}, which nothing judges first")

    def _value(self, c: str) -> str:
        if c in self.fixed:
            return self.fixed[c]
        if c in self.computed:
            return self.computed[c][0]
        p = f"%s::{self.casts[c]}" if c in self.casts else "%s"
        return f"COALESCE({p}, {self.defaults[c]})" if c in self.defaults \
            else p

    @functools.cached_property
    def sql(self) -> str:
        cols = ", ".join(self.columns)
        vals = ", ".join(self._value(c) for c in self.columns)
        back = f"RETURNING {self.returning or self.identity.columns[0]}"
        if isinstance(self.presence, ByKey):
            # the household is named here as well as by row security, so
            # a connection that bypasses it still only finds this one's rows
            any_ = self.presence.blank_is_any
            match = " AND ".join([f"tenant_id = {TENANT}"] + [
                f"(%s::{any_[c]} IS NULL OR {c} = %s)" if c in any_
                else f"{c} IS NOT DISTINCT FROM %s"
                for c in self.identity.columns])
            clash = " ON CONFLICT DO NOTHING" if self.presence.any_key else ""
            return (f"INSERT INTO {self.table} ({cols}) SELECT {vals}"
                    f" WHERE NOT EXISTS (SELECT 1 FROM {self.table}"
                    f" WHERE {match}){clash} {back}")
        return (f"INSERT INTO {self.table} ({cols}) VALUES ({vals}) "
                f"{self.presence.clause(self)} {back}")

    def key_tail(self, key: Mapping) -> tuple:
        """A ByKey row's lookup parameters from its identity values."""
        any_ = self.presence.blank_is_any
        out = ()
        for c in self.identity.columns:
            out += (key[c], key[c]) if c in any_ else (key[c],)
        return out

    def params(self, values: Mapping) -> tuple:
        row = ()
        for c in self.columns:
            if c in self.computed:
                row += tuple(values[k] for k in self.computed[c][1])
            elif c not in self.fixed:
                row += (values[c],)
        if isinstance(self.presence, ByKey):
            row += self.key_tail(dict(zip(self.identity.columns,
                                          self.identity(values))))
        elif (isinstance(self.presence, Exact)
              and self.presence.replace is not None):
            row += tuple(values[k] for k in self.presence.replace.when_params)
        return row


def _refused(out, L: Lander, got: Refuse) -> None:
    if got.over is not None:
        out.out_of_range(L.table, got.over)
    else:
        out.refuse(L.table, got.reason, got.n)


def _fit_cells(conn, out, L: Lander, r: dict):
    """The row with the cells a lander's own queries read (its parents and
    `queried`) judged against their columns before anything asks the
    household about them: one its column cannot hold would end the restore
    at the query. Other cells are judged as written, after the parse. A required cell that does not
    fit refuses the row as malformed; an optional one is left empty. The
    write judges every column again (`_fit_row`)."""
    cols = R._table_columns(conn, L.table)
    for c in dict.fromkeys((*L.queried, *L.parents)):
        v = r.get(c)
        if c not in cols or v is None or v == "" or R._takes(cols[c], v):
            continue
        label = f"a {c} that is not a valid {R._type_name(cols[c])}"
        if not cols[c]["nullable"] and c not in L.defaults:
            out._misfit(L.table, label)
            return None
        out.emptied(L.table, label)
        r = {**r, c: None}
    return r


def _parents(conn, ctx: Ctx, out, L: Lander, r: dict):
    for col, (kind, missing) in L.parents.items():
        v = r.get(col)
        if missing == "orphaned_if_set" and not v:
            continue
        if not ctx.holds(conn, kind, v):
            if missing != "null":
                out.refuse(L.table, "orphaned")
                return None
            r = {**r, col: None}
    return r


def _prepare(conn, ctx: Ctx, out, L: Lander, r: dict):
    """The values one archived row writes, or None when it was refused
    (and counted so). The checks run in the order the restore has always
    asked them: a missing cell, the row's own check, a missing parent,
    then the cells' own values (or those, then the parent)."""
    for c in L.required:
        if not r.get(c):
            out.refuse(L.table, "malformed")
            return None
    if L.remap:
        r = dict(r)
        for col, kind in L.remap.items():
            r[col] = ctx.remap(kind, r.get(col))
    r = _fit_cells(conn, out, L, r)
    if r is None:
        return None
    try:
        if L.pre is not None:
            got = L.pre(r, ctx)
            if isinstance(got, Refuse):
                _refused(out, L, got)
                return None
        if not L.parents_last:
            r = _parents(conn, ctx, out, L, r)
            if r is None:
                return None
        got = L.parse(r, ctx, out)
    except R._AmountOutOfRange as e:
        # a cell the row's own pass did not judge: that row, never the
        # archive (a restore judges each archived row on its own)
        _refused(out, L, Refuse("out_of_range", over=e.over or None))
        return None
    except (R._NotANumber, R._Unreadable):
        out.refuse(L.table, "malformed")
        return None
    if isinstance(got, Refuse):
        _refused(out, L, got)
        return None
    if L.parents_last and _parents(conn, ctx, out, L, r) is None:
        return None
    if L.write is not None:
        return got
    if L.spellings:
        return [_fitted_key(conn, L, L.params(v)) for v in got]
    return _fitted_key(conn, L, L.params(got))


def _fitted_key(conn, L: Lander, row: tuple) -> tuple:
    """A ByKey row's identity, looked for as the INSERT will write it: an
    optional identity cell the write leaves empty is looked for empty."""
    if not isinstance(L.presence, ByKey):
        return row
    fitted, _, why = R._fit_row(conn, L.sql, row)
    if why is not None:
        return row                  # refused at the write, as it would be
    written = [c for c in L.columns if c not in L.fixed]
    # the written cells stay as they are, so the write still counts
    # what it leaves empty
    return row[:len(written)] + L.key_tail(
        {c: fitted[written.index(c)] for c in L.identity.columns})


# the only id the summary names a skipped row by: one that says nothing.
# Others can spell what the household wrote (a bill's id is a slug of its
# payee, a hand-made account's can carry its number), so such a row is
# named by its place.
_UUID_ID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

# A table with at least this many rows, more than half of them failing
# the same unanticipated way, is not a few odd cells: the code or the
# file is wrong, and the archive is refused rather than restored without
# that table.
_SKIP_FLOOR = 5


def _row_ident(r, line: int) -> str:
    """How the summary names a skipped row: its id when that is a uuid,
    else its line among the file's rows. Never a cell's text."""
    if isinstance(r, dict):
        line = r.get("_line", line)
        v = r.get("id")
        if v is not None and _UUID_ID.fullmatch(str(v)):
            return f"id {v}"
    return f"row {line}"


def _row_count(r) -> int:
    """The archived rows one landed row stands for (a split's parts)."""
    parts = r.get("_parts") if isinstance(r, dict) else None
    return len(parts) if isinstance(parts, list) else 1


def land_one(conn, z, ctx: Ctx, out, L: Lander) -> None:
    """Land every archived row of one lander's members. Rows are judged
    one by one as they stream, and written in batches through
    `out.land`, which counts each written, present or misfit."""
    ctx.out = out
    # taken first, where the restore has always taken them: before the
    # lander reads what the household holds
    for lock in L.locks:
        lock(conn)
    if L.plan is not None:
        L.plan(conn, z, ctx)
    batch: list = []

    def write(rows, as_one=False):
        if L.write is not None:
            L.write(conn, ctx, out, L, rows)
            return
        got = out.land(conn, L.table, L.sql, rows, as_one=as_one)
        if L.landed is not None:
            L.landed(ctx, got)

    def flush():
        if batch:
            write(batch)
            batch.clear()

    rows = (((n, r) for n, r in enumerate(L.source(conn, z, ctx, out), 1))
            if L.source is not None else
            ((n, r) for member in L.members
             for n, r in enumerate(R._rows(z, member), 1)))
    read = skipped = 0
    for line, r in rows:
        read += 1
        malformed = (out.refused.get((L.table, "malformed"), 0)
                     if L.on_unread is not None else 0)
        out.begin_row()
        try:
            values = _prepare(conn, ctx, out, L, r)
        except R._ArchiveRefused:
            raise
        except ValueError:
            # a cell no check of the lander anticipated (Python's own
            # parser meeting it): that row, skipped and named, never the
            # archive. It is not written, so what hangs on it is left out
            # as for any refused row, and what the row's reading already
            # counted is taken back, so it is told once. Anything else (a
            # database error, a TypeError, a lookup or attribute the code
            # got wrong) ends the restore.
            out.skip(L.table, _row_ident(r, line), _row_count(r))
            skipped += 1
            if L.on_unread is not None:
                L.on_unread(ctx, r)
            continue
        finally:
            out.end_row()
        if values is None:
            if (L.on_unread is not None and malformed
                    < out.refused.get((L.table, "malformed"), 0)):
                L.on_unread(ctx, r)
            continue
        if L.spellings:
            write(values, as_one=True)
            continue
        batch.append(values)
        if len(batch) >= L.batch:
            flush()
    flush()
    if read >= _SKIP_FLOOR and skipped * 2 > read:
        raise R._ArchiveRefused(
            f"the backup's {L.table} could not be read: {skipped} of its "
            f"{read} rows failed the same unexpected way, so nothing was "
            "restored")
    for hook in L.after_land:
        hook(conn, ctx, out)


# ---- the leaf tables ----------------------------------------------------

def _liability(r, ctx, out):
    return {"account_id": r["account_id"], "as_of": R._ts(r.get("as_of")),
            "raw": R.jsonb(R.scrub_liability_raw(
                R.clean_liability_raw(R._j(r.get("raw")))))}


def _holding(r, ctx, out):
    try:
        quantity, price, value = (R._money(r.get("quantity")),
                                  R._money(r.get("price")),
                                  R._money(r.get("value")))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    return {"account_id": r["account_id"], "symbol": r["symbol"],
            "name": r.get("name") or None, "quantity": quantity,
            "price": price, "value": value, "as_of": R._ts(r.get("as_of")),
            # the identifiers a Plaid object carries never land in raw,
            # which leaves the instance verbatim through every export
            "raw": R.jsonb({k: v for k, v in
                            R._raw_dict(R._j(r.get("raw"))).items()
                            if k not in R._PLAID_RAW_IDS})}


def _crypto_holding(r, ctx, out):
    try:
        quantity, native = (R._money(r.get("quantity")),
                            R._money(r.get("native_usd")))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    return {"account_id": r["account_id"], "currency": r["currency"],
            "quantity": quantity, "native_usd": native,
            "as_of": R._ts(r.get("as_of")),
            "raw": R.jsonb(R._raw_dict(R._j(r.get("raw"))))}


def _networth_month(r, ctx, out):
    try:
        total = R._money(r["total"])
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    return {"month": r["month"], "total": total,
            "source": r.get("source") or None}


def _networth_day(r, ctx, out):
    by_class = R._j(r.get("by_class"))
    try:
        total = R._money(r.get("total"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    try:
        day = R._req_date(r["date"])
    except R._Unreadable:
        return Refuse("malformed")
    return {"date": day, "total": total,
            "by_class": R.jsonb(by_class) if by_class is not None else None}


def _forecast_nights_in(conn, z, ctx, out):
    """The archive's forecast nights, each with the household's day."""
    from .. import localtime
    # the zone read plainly: load_config takes the settings row's lock,
    # which no lander may hold before the locks later landers take
    zone = conn.execute("SELECT config->>'timezone' AS z FROM tenant_settings"
                        ).fetchone()
    today = localtime.now_local({"timezone": zone and zone["z"]}).date()
    for r in R._rows(z, "forecast_nights.csv"):
        yield {**r, "_today": today}


def _forecast_walks_ok(walks) -> bool:
    """`walks` in the shape forecast_score.compact writes: per scenario
    ("usual" always), balances in cents at each horizon (`at`, None where
    the walk did not reach), the low point and its day, and optional
    per-horizon card outflows."""
    from ..engine.forecast_score import HORIZONS

    def cents(v):
        return (isinstance(v, int) and not isinstance(v, bool)
                and abs(v) <= R.MAX_AMOUNT * 100)

    def offset(v):
        return v is None or (isinstance(v, int) and not isinstance(v, bool)
                             and abs(v) <= 2 * 366)
    if not isinstance(walks, dict) or not isinstance(walks.get("usual"), dict):
        return False
    for w in walks.values():
        if not isinstance(w, dict):
            return False
        at = w.get("at")
        if not (isinstance(at, list) and len(at) == len(HORIZONS)
                and all(v is None or cents(v) for v in at)):
            return False
        if "min" in w and not cents(w["min"]):
            return False
        if not (offset(w.get("min_day")) and offset(w.get("neg_day"))):
            return False
        co = w.get("cards_out")
        if co is not None and not (isinstance(co, list)
                                   and len(co) == len(HORIZONS)
                                   and all(cents(v) for v in co)):
            return False
        if "after" in w and not cents(w["after"]):
            return False
    return True


# a night's input events: far more than any 60-day walk has
MAX_FORECAST_EVENTS = 2000


def _forecast_events_ok(events) -> bool:
    """`inputs.events` as forecast_score.compact writes it: a list of
    [day offset, cents, label, kind, card id or null]."""
    def whole(v, bound):
        return isinstance(v, int) and not isinstance(v, bool) and abs(v) <= bound

    def text(v):
        return isinstance(v, str) and len(v) <= 1000   # a payee, a kind, an id
    return (isinstance(events, list) and len(events) <= MAX_FORECAST_EVENTS
            and all(isinstance(e, list) and len(e) == 5
                    and whole(e[0], 2 * 366)
                    and whole(e[1], R.MAX_AMOUNT * 100)
                    and text(e[2]) and text(e[3])
                    and (e[4] is None or text(e[4]))
                    for e in events))


def _forecast_shares_in(shares: dict, ctx) -> dict:
    """A night's pace shares keyed by the ids its cards restore under —
    the remap `checking_id` gets ("checking" is a bucket, not an id). Two
    archived ids that restore as one account add their shares."""
    out: dict = {}
    for k, v in shares.items():
        k = k if k == "checking" else ctx.remap("account", k)
        if (k in out and isinstance(out[k], (int, float))
                and isinstance(v, (int, float))):
            v = out[k] + v
        out[k] = v
    return out


def _forecast_night(r, ctx, out):
    """A night the cash forecast kept (specs/forecast-grading.md). It
    lands `restored`, so this instance never grades its own nights against
    the source's, and without its score (graded again when read). Only a
    night this instance could have kept: before the household's today (the
    first row of a day stands, so a restored today or later would stand in
    for the night the instance is about to keep), its walks in the
    record's shape. A night past the keep still lands; the next night's
    trim takes it, as it would have on the source."""
    try:
        day = R._req_date(r["as_of"])
    except R._Unreadable:
        return Refuse("malformed")
    if day >= r["_today"]:
        # the instance keeps its own night for today: harmless, never damage
        return Refuse("rebuilt")
    try:
        start = R._money(r.get("start_checking"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    walks = R._j(r.get("walks"))
    if not _forecast_walks_ok(walks) or start is None:
        return Refuse("malformed")
    pace = R._j(r.get("pace"))
    if isinstance(pace, dict) and isinstance(pace.get("shares"), dict):
        pace = {**pace, "shares": _forecast_shares_in(pace["shares"], ctx)}
    inputs = R._j(r.get("inputs"))
    if isinstance(inputs, dict) and "events" in inputs and not (
            _forecast_events_ok(inputs["events"])):
        # the drill reads these; an unreadable list lands as none (the
        # night and its walks still stand)
        inputs = {k: v for k, v in inputs.items() if k != "events"}
    return {"as_of": day, "recorded_at": R._ts(r.get("recorded_at")),
            "model": (r.get("model") or "").strip() or "unknown",
            "checking_id": r["checking_id"], "start_checking": start,
            "fresh": R._b(r.get("fresh")), "walks": R.jsonb(walks),
            "pace": R.jsonb(pace if isinstance(pace, dict) else {}),
            "inputs": (R.jsonb(inputs) if isinstance(inputs, dict)
                       else None)}


def _store_day(r):
    """A store order's amount and day, or the refusal of the row: an
    amount past any real sum first, then a day that does not read."""
    try:
        R._money(r.get("amount"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    try:
        return R._req_date(r["date"])
    except R._Unreadable:
        return Refuse("malformed")


def _amazon_order(r, ctx, out):
    day = _store_day(r)
    if isinstance(day, Refuse):
        return day
    return {"dedup_key": r["dedup_key"], "account": r.get("account") or "",
            "date": day, "amount": R._money(r.get("amount")) or 0.0,
            "payee": r.get("payee") or "Amazon",
            "seller": r.get("seller") or "", "memo": r.get("memo") or "",
            "category": r.get("category") or "",
            "category_source": r.get("category_source") or "rules",
            "order_number": r.get("order_number") or "",
            "is_refund": R._i(r.get("is_refund"), 0),
            "payment_method": r.get("payment_method") or "",
            "items_json": R.jsonb(R._json_list(R._j(r.get("items_json")))),
            "inserted_at": R._ts(r.get("inserted_at"))}


def _costco_receipt(r, ctx, out):
    day = _store_day(r)
    if isinstance(day, Refuse):
        return day
    return {"dedup_key": r["dedup_key"], "account": r.get("account") or "main",
            "date": day, "amount": R._money(r.get("amount")) or 0.0,
            "receipt_type": r.get("receipt_type") or "warehouse",
            "warehouse": r.get("warehouse") or "",
            "category": r.get("category") or "",
            "category_source": r.get("category_source") or "rules",
            "summary": r.get("summary") or "",
            "is_refund": R._i(r.get("is_refund"), 0),
            "payment_method": r.get("payment_method") or "",
            "items_json": R.jsonb(R._json_list(R._j(r.get("items_json")))),
            "inserted_at": R._ts(r.get("inserted_at"))}


def _amazon_summary(r, ctx, out):
    return {"dedup_key": r["dedup_key"], "summary": r["summary"],
            "created_at": R._ts(r.get("created_at"))}


def _import_batch(r, ctx, out):
    # `account_id` is the account the file was imported into, for display,
    # and is not a pointer anything follows: it is renamed with the
    # accounts but never checked against them
    return {"id": r["id"], "source": r["source"],
            "account_id": r.get("account_id") or None,
            "filename": R.card_number.cut_as_slug_reads(
                R._descriptor(r.get("filename"))) or None,
            "row_count": R._i(r.get("row_count"), 0) or 0,
            "created_at": R._ts(r.get("created_at"))}


def _business_flag(r, ctx, out):
    return {"txn_id": r["txn_id"], "flagged_at": R._ts(r.get("flagged_at"))}


def _reimburse_flag(r, ctx, out):
    try:
        expected = R._money(r.get("expected"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    # when it was flagged is what the waiting list shows
    return {"txn_id": r["txn_id"], "partial": R._i(r.get("partial"), 0) or 0,
            "expected": expected, "flagged_at": R._ts(r.get("flagged_at"))}


def _transaction_note(r, ctx, out):
    from ..web.data import NOTE_MAX
    note = r.get("note")
    if not note:
        # clearing a note deletes its row: no door stores a blank one
        return Refuse("malformed")
    # A note is cut at the length the API stores when it is written: a
    # longer one is a value no edit could have produced, and the next edit
    # of it would silently cut it anyway. Cut, not refused — the rest of
    # the note, and the archive, are still the household's.
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX]
        ctx.cut_notes += 1
    return {"txn_id": r["txn_id"], "note": note}


def _tell_cut_notes(conn, ctx, out):
    if ctx.cut_notes:
        from ..web.data import NOTE_MAX
        R.log.warning("restore: %d transaction note(s) longer than %d "
                      "characters were cut to that length",
                      ctx.cut_notes, NOTE_MAX)


def _business_txn_class(r, ctx, out):
    from ..engine.books import BUCKETS
    # The bucket is an enum the app validates on write but the DB does not
    # constrain, so a hand-edited ZIP is the one way a value outside the
    # set can exist at all — and this column is exported to CSV, where
    # arbitrary text is a formula-injection carrier. Refuse the row rather
    # than plant it.
    bucket = (r.get("bucket") or "").strip()
    if bucket not in BUCKETS:
        return Refuse("malformed")
    return {"txn_id": r["txn_id"], "bucket": bucket,
            "sched_c_line": r.get("sched_c_line") or None,
            "note": r.get("note") or None}


def _compliance_obligation(r, ctx, out):
    # The Business page's own judgement of an obligation, since a fee past
    # its column would roll back the whole restore. The link is optional:
    # one the page would refuse (not http(s), so it would run as script for
    # whoever opens the page) is left out and the obligation lands.
    url = r.get("url")
    try:
        _compliance._safe_url(url)
    except ValueError:
        out.emptied("compliance_obligation", "a link that is not http(s)")
        url = None
    # read as every figure of the archive is: a non-finite fee refuses the
    # archive, text where the fee belongs refuses the row
    try:
        fee = R._f(r.get("fee"))
    except R._NotANumber:
        return Refuse("malformed")
    try:
        title, recurrence, fee, url = _compliance.check_obligation(
            title=r.get("title"),
            recurrence=r.get("recurrence") or "yearly",
            fee=fee, url=url)
        due = R._req_date(r["due_date"])
    except _compliance.FeeTooLarge:
        return Refuse("out_of_range",
                      over=f"a fee over {_compliance.FEE_MAX:,.2f}")
    except ValueError:
        return Refuse("malformed")
    return {"id": r["id"], "entity_id": r["entity_id"], "title": title,
            "due_date": due, "recurrence": recurrence, "fee": fee,
            "url": url, "note": r.get("note") or None,
            "created_at": R._ts(r.get("created_at"))}


def _mileage(r, ctx, out):
    try:
        day = R._req_date(r["date"])
    except R._Unreadable:
        return Refuse("malformed")
    try:
        miles = R._numeric(r.get("miles"), "miles", 10, 1)
    except R._AmountOutOfRange as e:
        return Refuse("out_of_range", over=e.over)
    except R._NotANumber:
        return Refuse("malformed")
    try:
        _selfemploy.check_trip(miles)   # the page's own rules
    except ValueError:
        return Refuse("malformed")
    return {"id": r["id"], "entity_id": r["entity_id"], "date": day,
            "miles": miles, "purpose": r.get("purpose") or None,
            "note": r.get("note") or None,
            "created_at": R._ts(r.get("created_at"))}


_NOW = {"created_at": "now()"}
# a row of a transaction's own: one for a transaction that is not here
# (an archive from before those tables had foreign keys can carry one) is
# left out and counted, never allowed to abort the restore
_ON_TXN = {"txn_id": ("transaction", "orphaned")}
# A balance row is its account's: one for an account the archive did not
# carry, or this restore left out, has nothing to hang on
_ON_ACCOUNT = dict(remap={"account_id": "account"},
                   parents={"account_id": ("account", "orphaned")},
                   reads=("accounts",))
# written in the form it is checked in, so an id the archive spelled in
# upper case or padded lands as the business it names
_ON_BUSINESS = dict(remap={"entity_id": "business"},
                    parents={"entity_id": ("business", "orphaned")},
                    reads=("entities",))

LANDERS: dict[str, Lander] = {L.table: L for L in (
    Lander("liabilities", ("liabilities.csv",),
           ("account_id", "as_of", "raw"), _liability,
           columns("account_id"), required=("account_id",),
           defaults={"as_of": "now()"}, **_ON_ACCOUNT),
    Lander("holdings", ("holdings.csv",),
           ("account_id", "symbol", "name", "quantity", "price", "value",
            "as_of", "raw"), _holding,
           columns("account_id", "symbol"),
           required=("account_id", "symbol"), **_ON_ACCOUNT),
    Lander("crypto_holdings", ("crypto_holdings.csv",),
           ("account_id", "currency", "quantity", "native_usd", "as_of",
            "raw"), _crypto_holding,
           columns("account_id", "currency"),
           required=("account_id", "currency"), **_ON_ACCOUNT),
    Lander("networth_recorded", ("networth_recorded.csv",),
           ("month", "total", "source"), _networth_month, columns("month"),
           required=("month", "total")),
    Lander("networth_snapshot", ("networth_snapshot.csv",),
           ("date", "total", "by_class"), _networth_day, columns("date"),
           required=("date",)),
    # what the cash forecast said each night: a night this instance kept
    # itself wins (Exact: ON CONFLICT DO NOTHING)
    Lander("forecast_nights", ("forecast_nights.csv",),
           ("as_of", "recorded_at", "model", "checking_id",
            "start_checking", "fresh", "walks", "pace", "inputs",
            "restored"), _forecast_night, columns("as_of"),
           source=_forecast_nights_in,
           required=("as_of", "checking_id", "start_checking", "walks"),
           defaults={"recorded_at": "now()"}, fixed={"restored": "true"},
           remap={"checking_id": "account"}),
    Lander("amazon_orders", ("amazon_orders.csv",),
           ("dedup_key", "account", "date", "amount", "payee", "seller",
            "memo", "category", "category_source", "order_number",
            "is_refund", "payment_method", "items_json", "inserted_at"),
           _amazon_order, columns("dedup_key"),
           required=("dedup_key", "date")),
    Lander("costco_receipts", ("costco_receipts.csv",),
           ("dedup_key", "account", "date", "amount", "receipt_type",
            "warehouse", "category", "category_source", "summary",
            "is_refund", "payment_method", "items_json", "inserted_at"),
           _costco_receipt, columns("dedup_key"),
           required=("dedup_key", "date")),
    Lander("amazon_summaries", ("amazon_summaries.csv",),
           ("dedup_key", "summary", "created_at"), _amazon_summary,
           columns("dedup_key"), required=("dedup_key", "summary"),
           defaults=_NOW),
    # The import batches the restored ledger rows are tagged with. Batch
    # ownership rides in each transaction's `raw._batches`, so without
    # this table the tags point at batches that are not here: Recent
    # imports lists nothing, and the one-click undo for a file the person
    # imported — the only way back out of a bad import, since those rows
    # come from no feed — is gone for good, while the rows themselves keep
    # counting in every total.
    Lander("import_batches", ("import_batches.csv",),
           ("id", "source", "account_id", "filename", "row_count",
            "created_at"), _import_batch, columns("id"),
           required=("id", "source"), defaults=_NOW,
           remap={"account_id": "account"}),
    Lander("business_flags", ("business_flags.csv",),
           ("txn_id", "flagged_at"), _business_flag, columns("txn_id"),
           defaults={"flagged_at": "now()"}, parents=_ON_TXN),
    Lander("reimburse_flags", ("reimburse_flags.csv",),
           ("txn_id", "partial", "expected", "flagged_at"), _reimburse_flag,
           columns("txn_id"), defaults={"flagged_at": "now()"},
           parents=_ON_TXN),
    Lander("transaction_notes", ("transaction_notes.csv",),
           ("txn_id", "note"), _transaction_note, columns("txn_id"),
           parents=_ON_TXN, writes=("cut_notes",),
           after_land=(_tell_cut_notes,)),
    Lander("business_txn_class", ("business_txn_class.csv",),
           ("txn_id", "bucket", "sched_c_line", "note"), _business_txn_class,
           columns("txn_id"), parents=_ON_TXN),
    Lander("compliance_obligation", ("compliance_obligation.csv",),
           ("id", "entity_id", "title", "due_date", "recurrence", "fee",
            "url", "note", "created_at"), _compliance_obligation,
           columns("id"), required=("id", "entity_id", "due_date", "title"),
           defaults=_NOW, **_ON_BUSINESS),
    Lander("mileage_log", ("mileage_log.csv",),
           ("id", "entity_id", "date", "miles", "purpose", "note",
            "created_at"), _mileage, columns("id"),
           required=("id", "entity_id", "date", "miles"), defaults=_NOW,
           **_ON_BUSINESS),
)}


# ---- a transaction's children and the tables that replace on a condition

def _start_pins(conn, z, ctx):
    ctx.restored_pins, ctx.link_marked = [], None


def _pin_check(r, ctx):
    if ctx.link_marked is None:
        ctx.link_marked = "link_made" in r
    if r.get("category") is None:
        return Refuse("malformed")
    return None


def _pin(r, ctx, out):
    # link_prior's empty string (the person's "no category" pin the link
    # replaced) and NULL (no pin replaced) are one empty cell in a CSV; an
    # archive that says which it was carries link_prior_set beside it.
    # Without it an empty cell means none, as it always has.
    prior = r.get("link_prior") or None
    if prior is None and R._b(r.get("link_prior_set")):
        prior = ""
    # bill_id (a bill's transaction category, not a person's correction)
    # travels too, else a restore turns every bill-stamped row into a hand
    # edit the bill can't revert. So does the link mark and the pin it
    # replaced: without them an unlink after a restore would leave a
    # link-made transfer behind, or lose the person's category the link had
    # set aside.
    return {"transaction_id": r["transaction_id"], "category": r["category"],
            "bill_id": r.get("bill_id"), "set_at": R._ts(r.get("set_at")),
            "link_made": R._b(r.get("link_made")), "link_prior": prior}


def _pins_landed(ctx, got):
    ctx.restored_pins.extend(g for g in got if g is not None)


def _classify_pins(conn, ctx, out):
    # an archive written before overrides carried their kind: classify them
    # from the pins just restored and the store prefixes, exactly as
    # migration 127 did for live ledgers
    from ..engine import categories as _cats
    _cats.backfill_override_source(conn)
    # an archive written before the feed's withdrawals were stamped:
    # recognise them by the rule migration 157 applied to live ledgers
    from .base import stamp_unstamped_feed_withdrawals
    stamp_unstamped_feed_withdrawals(conn)


def _reimbursement(r, ctx, out):
    try:
        amount = R._money(r.get("amount"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    row = {"expense_id": r["expense_id"], "reimburse_id": r["reimburse_id"],
           "partial": R._i(r.get("partial"), 0) or 0, "amount": amount,
           "created_at": R._ts(r.get("created_at"))}
    # noted once every cell has parsed: a cell refused after the note
    # would leave it without a written row, and _reimbursements_landed
    # pairs the notes with the write's results in order
    ctx.pending_links.append((r["expense_id"], r["reimburse_id"]))
    return row


def _start_reimbursements(conn, z, ctx):
    ctx.restored_links, ctx.pending_links = set(), []


def _reimbursements_landed(ctx, got):
    ctx.restored_links.update(k for k, w in zip(ctx.pending_links, got)
                              if w is not None)
    ctx.pending_links.clear()


def _trim_links(conn, ctx, out):
    # Partial links that, with the full pairs, repay a charge past what it
    # cost or claim a deposit past what it brought in are cut back to both
    # limits, the newest first: the limits the link door keeps, so a
    # restored household reads as one linked by hand would
    # (specs/restore-landers.md, "Payback past a charge"). Only the charges
    # and deposits a link this restore wrote names. An archived link cut is
    # a repair of its written row; one cut to nothing was not written after
    # all, and is left out past the limit. A link the household already
    # held is weighed before every restored one, so it is cut only where
    # the held links alone are past a limit; it is no archived row, so
    # that is told in a note.
    from ..web.data import trim_restored_links
    cut = trim_restored_links(conn, ctx.restored_links)
    held = 0
    for e, r, kept in cut:
        if (e, r) not in ctx.restored_links:
            held += 1
        elif kept:
            out.repair("reimbursements", "payback_trimmed")
        else:
            out.written["reimbursements"] -= 1
            out.refuse("reimbursements", "over_cap")
    if held:
        ctx._ready("notes").append(
            f"{held} partial reimbursement{'s' if held != 1 else ''} this "
            "household already held, together with the restored ones, "
            "repaid a charge past what it cost or claimed a deposit past "
            "what it brought in, and "
            f"{'were' if held != 1 else 'was'} cut back to it")


def _classify_link_pins(conn, ctx, out):
    # An archive written before pins carried the link mark is classified
    # once the pairs are back, over the pins this restore wrote only
    if ctx.link_marked is False and ctx.restored_pins:
        from ..web.data import classify_link_made_pins
        classify_link_made_pins(conn, ctx.restored_pins)


def _match(r, ctx, out):
    return {"transaction_id": r["transaction_id"],
            "dedup_key": r.get("dedup_key") or None,
            "matched_at": R._ts(r.get("matched_at"))}


def _accounts_now(conn) -> set:
    return {r["id"] for r in conn.execute(
        "SELECT id FROM accounts").fetchall()}


def _start_links(conn, z, ctx):
    ctx.touched_groups, ctx.pending_groups = set(), []
    ctx.link_accounts = _accounts_now(conn)


def _start_sources(conn, z, ctx):
    ctx.source_accounts = _accounts_now(conn)


def _link(r, ctx, out):
    row = {"group_id": r["group_id"], "account_id": r["account_id"],
           "home_rank": R._i(r.get("home_rank"), 0)}
    # noted once every cell has parsed, as _reimbursement does
    ctx.pending_groups.append(str(r["group_id"]))
    return row


def _links_landed(ctx, got):
    # only a group a link was written into has ranks to make unique
    ctx.touched_groups.update(g for g, w in zip(ctx.pending_groups, got)
                              if w is not None)
    ctx.pending_groups.clear()


def _renumber_links(conn, ctx, out):
    if ctx.touched_groups:
        R._renumber_home_ranks(conn, sorted(ctx.touched_groups))


def _start_lineage(conn, z, ctx):
    ctx.lineage = 0
    ctx.adoptable = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE item_id IS NOT NULL").fetchall()}


def _old_id(r) -> str:
    return str(r.get("old_account_id") or "").strip()


def _lineage_check(r, ctx):
    old_id = _old_id(r)
    if not r.get("account_id") or not old_id or old_id == r["account_id"]:
        return Refuse("malformed")
    if ctx.lineage >= R._MAX_ADOPTION_LINEAGE:
        return Refuse("over_cap")
    return None


def _adoption(r, ctx, out):
    item = ctx.conn.execute("SELECT item_id FROM accounts WHERE id = %s",
                            (r["account_id"],)).fetchone()
    return {"account_id": r["account_id"],
            "old_account_id": _old_id(r)[:R._MAX_ACCOUNT_ID],
            "item_id": item["item_id"],
            "adopted_at": R._ts(r.get("adopted_at"))}


def _lineage_landed(ctx, got):
    ctx.lineage += sum(g is not None for g in got)


def _source_cells(r):
    return (str(r.get("door") or "").strip(),
            str(r.get("source_hmac") or "").strip())


def _source_check(r, ctx):
    door, digest = _source_cells(r)
    if not (door and digest and r.get("account_id")) or len(door) > 32 \
            or len(digest) > 80:
        return Refuse("malformed")
    return None


def _source(r, ctx, out):
    door, digest = _source_cells(r)
    # a stored-name row is a claim only while it is the digest of the cut
    # name its account stores; any other is pruned (account_sources.
    # backfill), so it is refused, never counted
    if digest.startswith("c:") and not R._account_sources.c_row_holds(
            ctx.conn, digest, r["account_id"]):
        return Refuse("malformed")
    return {"door": door, "source_hmac": digest,
            "account_id": r["account_id"],
            "created_at": R._ts(r.get("created_at"))}


def _receipt(r, ctx, out):
    import base64

    from ..engine import receipts as _receipts
    from ..engine import storage_quota as _sq
    # A ZIP cell must satisfy exactly what a live upload must (receipts.
    # add): a mime on the allowlist and the same size cap. The stored mime
    # is what the image door serves the bytes back as, so a crafted mime is
    # a same-origin content type of the attacker's choosing, and an
    # unbounded cell is a member-cell-sized allocation per row. Oversize or
    # off-list rows are skipped like corrupt ones — the rest still restore.
    mime = str(r.get("mime") or "image/jpeg").strip().lower()
    if mime not in _receipts.ALLOWED_MIMES:
        return Refuse("malformed")
    if len(r["image"]) > _receipts.MAX_IMAGE * 4 // 3 + 4:
        return Refuse("over_cap")             # cheaper than decoding first
    try:
        img = base64.b64decode(r["image"], validate=True)
    except Exception:                                   # noqa: BLE001
        return Refuse("malformed")            # a corrupt cell, not an abort
    if not img:
        return Refuse("malformed")
    if len(img) > _receipts.MAX_IMAGE:
        return Refuse("over_cap")
    kind = r.get("kind") or "receipt"
    if kind not in _receipts.KINDS:
        kind = "receipt"
    # a receipt with no transaction is one WAITING for its charge
    # (engine/receipt_match) and restores as waiting; only a receipt can
    txn_id = r.get("txn_id") or None
    if txn_id is None and kind != "receipt":
        return Refuse("malformed")
    method = r.get("match_method") or None
    if method not in (None, "auto", "manual"):
        method = None
    # a waiting receipt keeps 'manual' only as the mark of a hand match
    # whose charge went (the matcher leaves it to the person); a waiting
    # 'auto' is not a state anything writes
    if txn_id is None and method != "manual":
        method = None
    parsed = (R._receipt_parsed(R._j(r.get("parsed")))
              if r.get("parsed") else None)
    unmatched = R._j(r.get("unmatched_txn_ids"))
    unmatched = ([str(x)[:200] for x in unmatched[:200]
                  if isinstance(x, (str, int))]
                 if isinstance(unmatched, list) else [])
    opt, opt_mime = R._receipt_optimized(r)
    # the household's file storage is held to the same ceiling a snap is;
    # a receipt already here costs nothing new
    if not ctx.conn.execute("SELECT 1 FROM receipts WHERE id = %s",
                            (r["id"],)).fetchone():
        try:
            _sq.reserve(ctx.conn, len(img) + len(opt or b""))
        except _sq.StorageQuotaExceeded:
            return Refuse("over_cap")
    return {"id": r["id"], "txn_id": txn_id, "image": img, "mime": mime,
            # a status the app never writes reads as a receipt still
            # waiting to be read, as the Receipts screen lists them
            "kind": kind, "status": (r.get("status") if r.get("status") in
                                     _RECEIPT_STATUSES else "uploaded"),
            "parsed": R.jsonb(parsed) if parsed is not None else None,
            "error": r.get("error") or None,
            "created_at": R._receipt_ts(r.get("created_at")),
            "parsed_at": R._receipt_ts(r.get("parsed_at")),
            "matched_at": (R._receipt_ts(r.get("matched_at"))
                           if txn_id and method else None),
            "match_method": method, "unmatched_txn_ids": unmatched,
            "image_optimized": opt, "optimized_mime": opt_mime,
            # which slip it is (specs/receipts.md, "A receipt is one
            # slip"): ids are kept, so a label and a person's "two
            # purchases" land as written; every receipt lands undecided,
            # and the restore decides it once committed
            # (restore.after_commit)
            "slip_id": _uuid_cell(r.get("slip_id")),
            "slip_source": (r.get("slip_source")
                            if r.get("slip_source") in _SLIP_SOURCES
                            else None),
            # always decided again once the restore has committed: the
            # stored labels and a person's answers are kept, and deciding
            # only adds the joins of the same file (an archive copy landing
            # beside a local copy of it) while honouring every answer
            "slip_decided_at": None,
            "not_same_slip": _uuid_list(r.get("not_same_slip"))}


_SLIP_SOURCES = ("image", "person")


def _uuid_cell(v) -> str | None:
    import uuid as _uuid
    try:
        return str(_uuid.UUID(str(v))) if v else None
    except (ValueError, TypeError, AttributeError):
        return None


def _uuid_list(v) -> list[str]:
    """A list cell of receipt ids (an archive writes a uuid[] as JSON or as
    a Postgres array literal); anything that is not an id is dropped."""
    if not v:
        return []
    got = R._j(v) if isinstance(v, str) and v.lstrip().startswith("[") else v
    if isinstance(got, str):
        got = [x.strip().strip('"') for x in got.strip("{}").split(",")]
    if not isinstance(got, list):
        return []
    out = [u for u in (_uuid_cell(x) for x in got[:200]) if u]
    return list(dict.fromkeys(out))


_RECEIPT_STATUSES = ("uploaded", "parsing", "parsed", "failed")


def _receipt_item(r, ctx, out):
    try:
        amount = R._money(r.get("amount"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    if amount is None:
        # a line item is a priced line: reading a receipt drops any line
        # without a finite amount, and every client renders the amount as a
        # number — so the archive may not bring in a shape the app itself
        # never stores
        return Refuse("malformed")
    return {"receipt_id": r["receipt_id"], "line": R._i(r.get("line"), 0),
            "description": r.get("description") or "",
            "qty": R._f(r.get("qty")), "amount": amount,
            "tag": r.get("tag") or ""}


def _activity(r, ctx, out):
    import json
    import uuid
    at = R._ts(r.get("at"))
    if at is None:
        return Refuse("malformed")
    # the whole restore is one transaction, so a malformed id must be
    # caught here rather than left to the driver
    try:
        rid = str(uuid.UUID(str(r["id"])))
    except ValueError:
        return Refuse("malformed")
    detail = R._j(r.get("detail"))
    if not isinstance(detail, dict) or len(json.dumps(
            detail, default=str).encode()) > R.ACTIVITY_DETAIL_MAX:
        detail = None
    # an entry names a row by id with no key to follow: it follows the row
    # to the id it lands under here, as every child does (a re-id'd row's
    # old id beside the entry's sentence would give away what the re-id hid)
    carried = ctx.carried
    target = r.get("target")
    if carried and target:
        target = carried.get(target, target)
    if (carried and detail is not None
            and isinstance(detail.get("ids"), list)):
        detail["ids"] = [carried.get(x, x) if isinstance(x, str) else x
                         for x in detail["ids"]]
    # The actor's user id is dropped (people are numbered per instance)
    # and the actor text kept, which is what the page shows anyway. The
    # label and sentence are cut, then clipped, as activity.record writes
    # them: they quote a payee or a row as it read then.
    return {"id": rid, "at": at, "actor": str(r["actor"])[:120],
            "kind": str(r["kind"])[:40], "action": str(r["action"])[:40],
            "target": (str(target)[:R.ACTIVITY_TARGET_MAX]
                       if target else None),
            "label": R.card_number.cut(str(r.get("label") or ""))[:120],
            "summary": R.card_number.cut(str(r["summary"]))[:300],
            "detail": R.jsonb(detail) if detail is not None else None}


def _bill_offer(r, ctx, out):
    try:
        amount = R._money(r.get("amount"))
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    # a status the app never writes is an offer nobody has answered yet
    status = (r.get("status") if r.get("status") in
              ("pending", "approved", "rejected", "auto", "superseded")
              else "pending")
    if status == "pending" and (amount is None or not r.get("kind")):
        # the Bills screen asks about every pending offer and reads its
        # kind and amount, which the detector always files: one without
        # is no question. An answered one lands as it is, for its answer
        return Refuse("malformed")
    payee, evidence = R._offer_cut(
        ctx.conn, r.get("kind") or None, R._descriptor(r.get("payee")),
        # a cell carrying a list or a number becomes no evidence, a value
        # of the wrong type inside it is dropped: never a crash on the
        # Bills screen
        R._offer_evidence_typed(R._raw_dict(R._j(r.get("evidence")))),
        ctx._ready("bill_payees"))
    # the cadence a bill may take, as a restored bill's own is brought in
    # (restore.clean_bill_raw): never a cycle past twenty years, which the
    # lapse rule and an approve would roll centuries out
    frequency = r.get("frequency") or None
    interval = max(1, min(240, R._i(r.get("interval"), 1)))
    while interval > 1 and R._bills.cadence_too_long(frequency, interval):
        interval -= 1
    return {"id": r["id"], "kind": r.get("kind") or None,
            "bill_type": r.get("bill_type") or "occurrence",
            "payee": payee, "amount": amount,
            "frequency": frequency, "interval": interval,
            "next_due": R._bills.stored_date(r.get("next_due")),
            "evidence": R.jsonb(evidence),
            "llm_tag": r.get("llm_tag") or None,
            # a hand-edited status the app never writes would sit in the
            # table invisible to every query that reads it (see above)
            "status": status,
            "created_at": R._ts(r.get("created_at")),
            "decided_at": R._ts(r.get("decided_at"))}


def _mapped(r, k, ctx) -> bool:
    return str(r.get(k) or "").strip().lower() in ctx.merchant_map


def _merge_offer(r, ctx, out):
    known, mapped = ctx._ready("known_merchants"), ctx._ready("merchant_map")
    frm = R._known_merchant(r.get("from_merchant_id"), known, mapped)
    into = R._known_merchant(r.get("into_merchant_id"), known, mapped)
    if not frm or not into:
        return Refuse("orphaned")
    folded = any(_mapped(r, k, ctx)
                 for k in ("from_merchant_id", "into_merchant_id"))
    if frm == into:
        # both sides are one merchant here: the restore folded them onto
        # the same live one, or the file names a pair of one
        return Refuse("merged" if folded else "malformed")
    # the proposer keys a pair by its two ids, so a decision on a side the
    # restore mapped onto a live merchant is re-keyed onto the live pair,
    # where the next proposal run will look for it
    from ..engine.merchant_merge import MAX_SIGNAL, _sig
    ev = R._j(r.get("evidence"))
    if isinstance(ev, dict):
        # a reason longer than the proposer writes is not one it wrote;
        # every Merchants and Today load reads these
        for k in ("signals", "supports"):
            if isinstance(ev.get(k), list):
                ev[k] = [x for x in ev[k]
                         if not isinstance(x, str) or len(x) <= MAX_SIGNAL]
    return {"id": _sig(frm, into) if folded else r["id"],
            "from_merchant_id": frm, "into_merchant_id": into,
            "evidence": R.jsonb(ev if isinstance(ev, dict) else {}),
            # a hand-edited status the app never writes would sit in the
            # table unreadable by every query; anything unrecognised is
            # simply an undecided offer
            "status": (r.get("status") if r.get("status") in
                       ("pending", "approved", "rejected", "stale")
                       else "pending"),
            "created_at": R._ts(r.get("created_at")),
            "decided_at": R._ts(r.get("decided_at"))}


def _start_aliases(conn, z, ctx):
    ctx.cut_aliases = set()


def _alias_keys(ctx, key) -> list:
    """The keys an alias or rule on `key` is written under: the cut key
    where file rows now carry it, the whole one kept only while a row
    still keys on it."""
    filed, kept = ctx._ready("alias_where")(key)
    return ([R.card_number.cut(key)] if filed else []) + (
        [key] if kept or not filed else [])


def _alias(r, ctx, out):
    raw = R._descriptor(r["raw_merchant"])
    canonical = R._descriptor(r["canonical"])
    # a machine's alias names its merchant as the resolver does, cut; a
    # person's says what they typed
    if (r.get("method") or "manual") != "manual":
        cut_canonical = R.card_number.cut(canonical)
        if cut_canonical != canonical:
            ctx.cut_aliases.add((r["raw_merchant"], canonical))
        canonical = cut_canonical
    # A NULL method is a frozen mapping: the layer1 recompute only touches
    # method='layer1' rows and the preservation logic only recognises
    # 'llm'/'manual', so a row with no method is invisible to both forever.
    # A hand-edited or pre-method CSV defaults to 'manual' — the
    # preservation-safe choice, since manual rows are never recomputed
    # over. An alias whose merchant is not in the archive resolves by its
    # canonical string on the next pass, as an alias with no merchant does.
    return [{"raw_merchant": key, "canonical": canonical,
             "method": r.get("method") or "manual",
             "as_of": R._ts(r.get("as_of")),
             "merchant_id": R._known_merchant(
                 r.get("merchant_id"), ctx._ready("known_merchants"),
                 ctx._ready("merchant_map")),
             "raw_method": r.get("method") or None}
            for key in _alias_keys(ctx, raw)]


def _rule_keys(ctx, key) -> list:
    """A rule follows the cut as the alias does. A rule keyed on a machine
    merchant's whole name, with no alias of that key, is read through the
    merchant's name and follows it to the cut name; a person's merchant
    keeps its rule."""
    machine_cut = ctx._ready("machine_cut")
    persons_live = ctx._ready("persons_live")
    out_keys = []
    for k in _alias_keys(ctx, key):
        follow = machine_cut.get(k.lower())
        if (follow is not None and k.lower() not in persons_live
                and not ctx.conn.execute(
                    "SELECT 1 FROM merchant_canonical"
                    " WHERE raw_merchant = %s", (k,)).fetchone()):
            k = follow
        if k not in out_keys:
            out_keys.append(k)
    return out_keys


def _rule(r, ctx, out):
    return [{"merchant": key, "category_primary": r["category_primary"],
             "source": r.get("source") or "llm",
             "classified_at": R._ts(r.get("classified_at")),
             "disabled": R._b(r.get("disabled"))}
            for key in _rule_keys(ctx, R._descriptor(r["merchant"]))]


_ON_TXN_ID = {"transaction_id": ("transaction", "orphaned")}

LANDERS.update({L.table: L for L in (
    # A pin a BILL wrote on the destination gives way to the person's own
    # pin for the same row from the archive; a person's pin already here,
    # or a link's, stays. An archive written before pins carried the link
    # mark is classified after the reimbursements, once the pairs are back.
    Lander("manual_categories", ("manual_categories.csv",),
           ("transaction_id", "category", "bill_id", "set_at", "link_made",
            "link_prior"), _pin, columns("transaction_id"),
           presence=Exact(replace=Replace(
               set=(("category", "EXCLUDED.category"), ("bill_id", "NULL"),
                    ("set_at", "EXCLUDED.set_at"), ("link_made", "FALSE"),
                    ("link_prior", "EXCLUDED.link_prior")),
               when="manual_categories.bill_id IS NOT NULL"
                    " AND NOT manual_categories.link_made"
                    " AND EXCLUDED.bill_id IS NULL"
                    " AND NOT EXCLUDED.link_made")),
           defaults={"set_at": "now()"}, remap={"bill_id": "bill"},
           pre=_pin_check, parents=_ON_TXN_ID, plan=_start_pins,
           landed=_pins_landed, after_land=(_classify_pins,),
           reads=("bill_ids",), writes=("restored_pins", "link_marked")),
    # created_at is what an archive without the link mark is classified by.
    # Without it the pair gets clock_timestamp(), later than the now() any
    # undated pin was given: the tie breaks toward a pin looking hand-set,
    # so an unlink may leave a link-made transfer behind but never clears
    # one a person chose.
    Lander("reimbursements", ("reimbursements.csv",),
           ("expense_id", "reimburse_id", "partial", "amount", "created_at"),
           _reimbursement, columns("expense_id", "reimburse_id"),
           defaults={"created_at": "clock_timestamp()"},
           parents={"expense_id": ("transaction", "orphaned"),
                    "reimburse_id": ("transaction", "orphaned")},
           plan=_start_reimbursements, landed=_reimbursements_landed,
           after_land=(_classify_link_pins, _trim_links),
           reads=("restored_pins", "link_marked", "notes"),
           writes=("restored_links", "pending_links")),
    Lander("amazon_matches", ("amazon_matches.csv",),
           ("transaction_id", "dedup_key", "matched_at"), _match,
           columns("transaction_id"), defaults={"matched_at": "now()"},
           parents=_ON_TXN_ID),
    Lander("costco_matches", ("costco_matches.csv",),
           ("transaction_id", "dedup_key", "matched_at"), _match,
           columns("transaction_id"), defaults={"matched_at": "now()"},
           parents=_ON_TXN_ID),
    # account_links IS the shadow set — without it a round trip un-shadows
    # every duplicate source (Plaid + SimpleFIN of the same real account)
    # and every money aggregate double-counts. Guarded on the account
    # existing so a partial archive cannot plant dangling links, and the
    # ranks of every group it adds to are made unique after.
    Lander("account_links", ("account_links.csv",),
           ("group_id", "account_id", "home_rank"), _link,
           columns("account_id"), required=("group_id", "account_id"),
           remap={"account_id": "account"},
           parents={"account_id": ("link_account", "orphaned")},
           plan=_start_links, landed=_links_landed,
           after_land=(_renumber_links,),
           writes=("link_accounts", "touched_groups", "pending_groups")),
    # Which ids each account stood under before an adoption moved it. A
    # file row keeps the id it was hashed under, so a statement re-imported
    # after this restore finds its earlier rows only through this lineage;
    # without it every pre-feed line of the statement lands twice. Only the
    # lineage is loaded: each row arrives reconciled with no cutoff, so the
    # one-shot reconnect guard it once drove stays off here. Bounded in
    # rows written, so written one at a time, and only for an account the
    # archive restored with its connection.
    Lander("account_adoptions", ("account_adoptions.csv",),
           ("account_id", "old_account_id", "item_id", "adopted_at",
            "reconciled_at"), _adoption,
           columns("account_id", "old_account_id"),
           presence=ByKey(
               why="its unique key is the account and the moment it was "
                   "adopted; the same old id adopted twice is one lineage "
                   "fact, which no index states",
               any_key=True),
           defaults={"adopted_at": "now()"},
           fixed={"reconciled_at": "now()"},
           remap={"account_id": "account"}, pre=_lineage_check,
           parents={"account_id": ("adoptable", "orphaned")},
           batch=1, sequential=True, plan=_start_lineage,
           landed=_lineage_landed,
           reads=("conn",), writes=("adoptable", "lineage")),
    # Which account each source name fills (sync/account_sources). Keyed
    # rows are HMACs under the secret of the household that wrote them,
    # which never travels: restored into the same household they match
    # again; restored elsewhere they match nothing, and its sources find
    # their accounts by the stored-name rows. Only for an account the
    # archive restored; never re-pointed onto another.
    Lander("account_sources", ("account_sources.csv",),
           ("door", "source_hmac", "account_id", "created_at"), _source,
           columns("door", "source_hmac", "account_id"),
           presence=Exact(any_key=True), defaults={"created_at": "now()"},
           remap={"account_id": "account"}, pre=_source_check,
           parents={"account_id": ("source_account", "orphaned")},
           plan=_start_sources, reads=("conn",),
           writes=("source_accounts",)),
    # Receipt images (base64 in the CSV), the Schedule-C expense-report
    # input. One that names a transaction the archive does not carry is
    # skipped, never turned into a waiting receipt the household did not
    # have. Written one at a time: a new receipt reserves its bytes against
    # the household's storage, and a second row of the same id must find
    # the first one already here.
    Lander("receipts", ("receipts.csv",),
           ("id", "txn_id", "image", "mime", "kind", "status", "parsed",
            "error", "created_at", "parsed_at", "matched_at", "match_method",
            "unmatched_txn_ids", "image_optimized", "optimized_mime",
            "slip_id", "slip_source", "slip_decided_at", "not_same_slip"),
           _receipt, columns("id"), required=("id", "image"),
           defaults={"created_at": "now()"},
           parents={"txn_id": ("transaction", "orphaned_if_set")},
           # the parse asks whether this receipt is here already
           queried=("id",), batch=1, sequential=True, reads=("conn",)),
    Lander("receipt_items", ("receipt_items.csv",),
           ("receipt_id", "line", "description", "qty", "amount", "tag"),
           _receipt_item, columns("receipt_id", "line"),
           required=("receipt_id",),
           parents={"receipt_id": ("receipt", "orphaned")},
           parents_last=True),
    # the activity log travels whole: it is the household's own record of
    # who changed what, and nothing can regenerate it; a row already here
    # (same id) is left alone, so restoring the same archive twice does not
    # double the log
    Lander("activity_log", ("activity_log.csv",),
           ("id", "at", "actor_user_id", "actor", "kind", "action", "target",
            "label", "summary", "detail"), _activity, columns("id"),
           required=("id", "actor", "kind", "action", "summary"),
           casts={"id": "uuid"}, fixed={"actor_user_id": "NULL"},
           reads=("carried",)),
    # The decisions on recurring-bill offers. A rejected candidate is
    # re-derived under the same id every night, so its answered row is the
    # only thing keeping the offer from coming back. The destination's
    # nightly may already have offered the same candidate under the same
    # derived id: an offer it is still asking about takes the household's
    # archived answer. A proposal names its bill by payee, following a bill
    # this restore moved. Archives written before the bills rename carry
    # the same rows as recurring_proposals.csv.
    Lander("bill_proposals", ("bill_proposals.csv", "recurring_proposals.csv"),
           ("id", "kind", "bill_type", "payee", "amount", "frequency",
            "interval", "next_due", "evidence", "llm_tag", "status",
            "created_at", "decided_at"), _bill_offer, columns("id"),
           presence=Exact(replace=Replace(
               set=(("status", "EXCLUDED.status"),
                    ("decided_at", "COALESCE(EXCLUDED.decided_at, now())")),
               when="bill_proposals.status = 'pending'"
                    " AND EXCLUDED.status IN ('approved', 'rejected')")),
           required=("id", "payee"), defaults={"created_at": "now()"},
           reads=("conn", "bill_payees")),
    # The merge offers and, above all, the decisions on them: a rejection
    # is the household saying "these two are different businesses", and
    # the only thing keeping the pair from being offered again. Both sides
    # must be merchants here (the columns are NOT NULL, so there is no
    # "resolve it later" form). A pending offer here takes the archive's
    # answer: a pending offer is the machine asking, a decision the reply.
    Lander("merchant_merge_proposals", ("merchant_merge_proposals.csv",),
           ("id", "from_merchant_id", "into_merchant_id", "evidence",
            "status", "created_at", "decided_at"), _merge_offer,
           columns("id"),
           presence=Exact(replace=Replace(
               set=(("status", "EXCLUDED.status"),
                    ("decided_at", "COALESCE(EXCLUDED.decided_at, now())")),
               when="merchant_merge_proposals.status = 'pending'"
                    " AND EXCLUDED.status <> 'pending'")),
           required=("id",),
           defaults={"evidence": "'{}'::jsonb", "created_at": "now()"},
           reads=("known_merchants", "merchant_map")),
    # A person's alias (method 'manual') replaces the destination's machine
    # alias for the same descriptor; anything else already here stays, and
    # the alias as stored, blank method and all, is present, never
    # rewritten. The key is a bank descriptor, cut as the ledger strings it
    # has to equal are; one archive row counts once, however many keys it
    # took.
    Lander("merchant_canonical", ("merchant_canonical.csv",),
           ("raw_merchant", "canonical", "method", "as_of", "merchant_id"),
           _alias, columns("raw_merchant"),
           presence=Exact(replace=Replace(
               set=(("canonical", "EXCLUDED.canonical"),
                    ("method", "EXCLUDED.method"),
                    ("as_of", "EXCLUDED.as_of"),
                    ("merchant_id", "EXCLUDED.merchant_id")),
               when="EXCLUDED.method = 'manual'"
                    " AND merchant_canonical.method"
                    " IS DISTINCT FROM 'manual'"
                    " AND NOT (merchant_canonical.method"
                    " IS NOT DISTINCT FROM %s::text"
                    " AND merchant_canonical.canonical"
                    " = EXCLUDED.canonical)",
               when_params=("raw_method",))),
           required=("raw_merchant", "canonical"),
           defaults={"as_of": "now()"}, spellings=True,
           plan=_start_aliases,
           reads=("alias_where", "known_merchants", "merchant_map"),
           writes=("cut_aliases",)),
    # A person's rule beats the destination's guess: a household that
    # linked a bank before restoring already holds seed and model rules for
    # the merchants its first sync saw, and the archive's rule for the same
    # merchant, when a PERSON set it, replaces the machine's. Two rules of
    # the same kind leave the destination's in place.
    Lander("merchant_categories", ("merchant_categories.csv",),
           ("merchant", "category_primary", "source", "classified_at",
            "disabled"), _rule, columns("merchant"),
           presence=Exact(replace=Replace(
               set=(("category_primary", "EXCLUDED.category_primary"),
                    ("source", "EXCLUDED.source"),
                    ("classified_at", "EXCLUDED.classified_at"),
                    ("disabled", "EXCLUDED.disabled")),
               when="EXCLUDED.source = 'user'"
                    " AND merchant_categories.source <> 'user'")),
           required=("merchant", "category_primary"),
           defaults={"classified_at": "now()"}, spellings=True,
           reads=("conn", "alias_where", "machine_cut", "persons_live")),
)})


# ---- the business tables -----------------------------------------------

def _start_entities(conn, z, ctx):
    ctx.landed_entities = []
    ctx.retyped_entities = {}
    ctx.unread_entities = set()


def _entity_unread(ctx, r):
    ctx.unread_entities.add(Ctx.business_id(r.get("id")))


# A business of a type the Business page does not offer (an older spelling,
# a blank cell) lands as this one rather than being left out with every
# account and movement booked to it; the summary names each such business
# so the owner can set the real type.
_UNKNOWN_STRUCTURE = "sole_prop"


_ENTITY_FIELDS = (("state", "a state"),
                  ("registered_agent", "a registered agent"),
                  ("fiscal_year_end", "a fiscal year end"),
                  ("income_tax_rate", "a tax rate"),
                  ("home_office_sqft", "a home office size"))


def _fiscal_day(v):
    """A fiscal year end written as a day in another spelling ('12/31',
    '6-30': a business saved before the page checked the field) in the
    page's own MM-DD; anything else as it was, for the page to judge."""
    m = (re.fullmatch(r"\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*", v)
         if isinstance(v, str) and not R._too_long(v) else None)
    return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else v


def _entity_owner(v) -> str:
    """Whose business an archived row says it is: one of the three the
    page sets, or 'unknown' — an archive written before the column has
    none, and an unknown value nets no W-2 wages (the safe reading)."""
    from ..engine import entities
    return v if v in entities.OWNERS else "unknown"


def _entity_row(r, ctx, out):
    from ..engine import entities
    # The page's own judgement of each field (entities.check_field). The
    # name is required; every other field is optional, and one the page
    # would refuse is left out so the business lands — or every account,
    # member and movement booked to it would go too.
    try:
        entities.check_field("name", r.get("name"))
    except ValueError:
        return Refuse("malformed")
    fields = {}
    for k, what in _ENTITY_FIELDS:
        v = r.get(k)
        if k == "fiscal_year_end":
            v = _fiscal_day(v)
        try:
            checked = entities.check_field(k, v or None)
        except ValueError:
            out.emptied("business_entity",
                        f"{what} the business page refuses")
            fields[k] = None
            continue
        # text is kept as the archive wrote it; numbers as read
        fields[k] = (checked if k in ("income_tax_rate", "home_office_sqft")
                     else (v or None))
    owner = _entity_owner(r.get("owner"))
    if r.get("owner") and owner != r.get("owner"):
        out.emptied("business_entity",
                    "whose business it is, in a value the page does not set")
    status = r.get("status") or "active"
    if status not in ("active", "archived"):
        out.emptied("business_entity",
                    "a status the business page does not set")
        status = "active"
    structure = r.get("structure")
    try:
        entities.check_field("structure", structure)
    except ValueError:
        structure = _UNKNOWN_STRUCTURE
        ctx.retyped_entities[str(r["id"])] = r.get("name")
    return {"id": r["id"], "name": r.get("name"),
            "structure": structure, "state": fields["state"],
            "formation_date": R._opt_date(r.get("formation_date")),
            "business_start_date": R._opt_date(r.get("business_start_date")),
            "ein_last4": r.get("ein_last4") or None,
            "registered_agent": fields["registered_agent"],
            "fiscal_year_end": fields["fiscal_year_end"], "status": status,
            "home_office_sqft": fields["home_office_sqft"],
            "income_tax_rate": fields["income_tax_rate"],
            "filing_status": r.get("filing_status") or None,
            # whose W-2 wages close its wage base (an older archive: not
            # said)
            "owner": owner,
            # without it a round trip forgets which account the tax money
            # sits in
            "tax_reserve_account_id": R._account_id(
                r.get("tax_reserve_account_id")) or None,
            # an archived entity restores as archived on the same date, not
            # freshly closed. One archived before the date was kept was
            # last touched by the archiving itself, so that is its date, as
            # migration 089 dated the stored ones
            "archived_at": R._ts(r.get("archived_at")) or (
                R._ts(r.get("updated_at"))
                if (r.get("status") or "") == "archived" else None),
            # when the business was set up is its history, not this
            # instance's; an archive without it restores as now
            "created_at": R._ts(r.get("created_at"))}


def _entities_landed(ctx, got):
    ctx.landed_entities.extend(str(g) for g in got if g is not None)
    # said only of a business this restore wrote: one already here kept
    # its own type
    for g in got:
        name = (ctx.retyped_entities.pop(str(g), None)
                if g is not None else None)
        if name is not None:
            ctx._ready("notes").append(
                f"{name}: its type was not recognised and was set to Sole "
                f"proprietor — check it on the Business page")


def _member(r, ctx, out):
    from ..engine import entities
    # optional: the member lands without it, and the movements recorded
    # for them with it
    try:
        share = entities._member_pct(r.get("ownership_pct"))
    except ValueError:
        out.emptied("entity_membership", "an ownership share outside 0-100")
        share = None
    household = R._b(r.get("is_household"))
    # A mark is taken under the same lock the Settings door marks under:
    # the NOT EXISTS in the write alone cannot see a mark another
    # transaction has not committed, and the unique index would then
    # refuse this insert and abort the whole restore.
    if household:
        entities._hold_household_mark(ctx.conn, r["entity_id"])
    return {"id": r["id"], "entity_id": r["entity_id"],
            "member_name": r.get("member_name"), "ownership_pct": share,
            "is_manager": R._b(r.get("is_manager")),
            "created_at": R._ts(r.get("created_at")),
            "is_household": household}


_FRONTED = ("contribution", "reimbursement")


def _movements_in_order(conn, z, ctx, out):
    """The archive's movements, the one to keep of two fronted movements
    for a charge first: the movement for the business the cost is booked
    to now (the transaction's own assignment, else its account's), and
    the earliest recorded only when neither matches — as the upgrade keeps
    it, so the result does not depend on the archive's row order or on
    whether this install has the index yet."""
    ctx.fronted_clash = 0
    movements = list(R._rows(z, "equity_movement.csv"))
    booked = {r["id"]: str(r["ent"]) if r["ent"] else None
              for r in conn.execute(
                  "SELECT t.id, COALESCE(t.entity_id, a.entity_id) AS ent"
                  "  FROM transactions t"
                  "  LEFT JOIN accounts a ON a.id = t.account_id"
                  " WHERE t.id = ANY(%s)",
                  ([m["txn_id"] for m in movements
                    if m.get("txn_id")],)).fetchall()}

    def keep_first(r):
        ent = (r.get("entity_id") or "").strip().lower() or None
        return (booked.get(r.get("txn_id")) != ent,
                str(r.get("created_at") or ""), str(r.get("id") or ""))
    return sorted(movements, key=keep_first)


def _movement(r, ctx, out):
    from ..engine import entities, equity
    try:
        m_date = R._req_date(r["date"])
    except R._Unreadable:
        return Refuse("malformed")
    try:
        amount = R._numeric(r.get("amount"), "an amount", 14, 2)
    except R._AmountOutOfRange as e:
        return Refuse("out_of_range", over=e.over)
    except R._NotANumber:
        return Refuse("malformed")
    # the member is optional: one that is no id is left out and the
    # movement lands; the page's own rules judge the rest
    member = r.get("member_id") or None
    if member is not None and not entities._valid_uuid(member):
        out.emptied("equity_movement", "a member that is no id")
        member = None
    try:
        equity.check_movement(kind=r.get("kind"), amount=amount,
                              member_id=member)
    except ValueError:
        return Refuse("malformed")
    txn = r.get("txn_id")
    if (txn and r.get("kind") in _FRONTED and ctx.conn.execute(
            "SELECT 1 FROM equity_movement WHERE txn_id = %s"
            " AND kind = ANY(%s) AND id::text <> %s",
            (txn, list(_FRONTED), str(r["id"]))).fetchone()):
        # the charge's one fronted movement is already here
        ctx.fronted_clash += 1
        return Refuse("present")
    return {"id": r["id"], "entity_id": r["entity_id"],
            "kind": r.get("kind"), "amount": amount, "date": m_date,
            "member_id": member, "txn_id": txn,
            "form": r.get("form") or None, "note": r.get("note") or None,
            # it decides which of two fronted movements is the earlier one,
            # here and on every upgrade
            "created_at": R._ts(r.get("created_at"))}


def _tell_fronted_clash(conn, ctx, out):
    n = ctx.fronted_clash
    if n:
        ctx._ready("notes").append(
            f"{n} business movement{'s' if n != 1 else ''} left out: each "
            "was a second contribution or reimbursement for a transaction "
            "that already has one (the one for the business the charge is "
            "booked to is kept, else the earliest)")


def _vendor(r, ctx, out):
    # The mark follows its merchant (a rename must not drop a contractor
    # off the year-end list). A pointer to a merchant the archive does not
    # carry is dropped; the name still finds it.
    merchant_id = R._known_merchant(r.get("merchant_id"),
                                    ctx._ready("known_merchants"),
                                    ctx._ready("merchant_map"))
    name = R._mark_name(ctx.conn, r["entity_id"], r.get("merchant"),
                        merchant_id, ctx._ready("machine_cut"),
                        ctx._ready("persons_names"))
    if merchant_id is None:
        merchant_id = R._mark_merchant(ctx.conn, r["entity_id"], name)
    return {"id": r["id"], "entity_id": r["entity_id"], "merchant": name,
            "reportable": R._b(r.get("reportable")),
            "tin_last4": r.get("tin_last4") or None,
            "note": r.get("note") or None, "merchant_id": merchant_id,
            # what undoing a merge puts back
            "merged_from": R.jsonb([n for n in R._json_list(
                R._j(r.get("merged_from"))) if isinstance(n, dict)]),
            "created_at": R._ts(r.get("created_at"))}


LANDERS.update({L.table: L for L in (
    # Before accounts and transactions: their entity_id points at it. The
    # encrypted EIN never leaves in an export (only a gated reveal shows
    # it), so it restores empty; the last four carry for display.
    Lander("business_entity", ("business_entity.csv",),
           ("id", "name", "structure", "state", "formation_date",
            "business_start_date", "ein_last4", "registered_agent",
            "fiscal_year_end", "status", "home_office_sqft",
            "income_tax_rate", "filing_status", "tax_reserve_account_id",
            "archived_at", "created_at", "owner"), _entity_row, columns("id"),
           required=("id",), defaults={"created_at": "now()"},
           plan=_start_entities, landed=_entities_landed,
           on_unread=_entity_unread,
           reads=("notes",),
           writes=("landed_entities", "retyped_entities",
                   "unread_entities")),
    # The household mark comes back with the member: without it the
    # estimate silently widens to every member's tax and the capital
    # account loses the household's share. A business has at most one
    # household member (the database enforces it), so a mark arrives only
    # while its business has none — the destination's own mark, or the
    # archive's first, is the one kept.
    Lander("entity_membership", ("entity_membership.csv",),
           ("id", "entity_id", "member_name", "ownership_pct", "is_manager",
            "created_at", "is_household"), _member, columns("id"),
           required=("id", "entity_id"), defaults={"created_at": "now()"},
           computed={"is_household": (
               "%s AND NOT EXISTS (SELECT 1 FROM entity_membership"
               " WHERE entity_id = %s AND is_household)",
               ("is_household", "entity_id"))},
           **{**_ON_BUSINESS, "reads": ("conn", "entities")}),
    # One fronted movement (a contribution or a reimbursement) per charge:
    # the database refuses a second on an install with the index, and the
    # engine everywhere. An archive from before the rule can carry two; the
    # second is present, and said so in the summary. Any unique key held
    # counts as present, the per-charge-and-kind one too. A movement whose
    # charge is gone keeps itself without it, as the foreign key does.
    # Written one at a time: the second of a pair must find the first.
    Lander("equity_movement", ("equity_movement.csv",),
           ("id", "entity_id", "kind", "amount", "date", "member_id",
            "txn_id", "form", "note", "created_at"), _movement,
           columns("id"), presence=Exact(any_key=True),
           required=("id", "entity_id", "date", "kind", "amount"),
           defaults={"created_at": "now()"}, source=_movements_in_order,
           remap=_ON_BUSINESS["remap"],
           parents={**_ON_BUSINESS["parents"],
                    "txn_id": ("transaction", "null")},
           batch=1, sequential=True, after_land=(_tell_fronted_clash,),
           reads=("conn", "entities", "notes"), writes=("fronted_clash",)),
    # A destination that already marked this vendor under another id holds
    # it (the table is unique on the business and merchant too). Written
    # one at a time: a mark's name and merchant are chosen against the
    # marks already here.
    Lander("vendor_1099", ("vendor_1099.csv",),
           ("id", "entity_id", "merchant", "reportable", "tin_last4", "note",
            "merchant_id", "merged_from", "created_at"), _vendor,
           columns("id"), presence=Exact(any_key=True),
           required=("id", "entity_id"), defaults={"created_at": "now()"},
           batch=1, sequential=True, **{**_ON_BUSINESS, "reads": (
               "conn", "entities", "known_merchants", "merchant_map",
               "machine_cut", "persons_names")}),
)})


# ---- planned and counted tables -----------------------------------------

def _lock_active_bills(conn):
    from ..engine import bills
    # a restore is held to the same ceiling on active bills as a save
    bills.lock_active_bills(conn)


def _bills_in_plan(conn, z, ctx, out):
    """The archive's bills with where each lands. A detected bill is named
    by the ledger, and its id is the slug of its payee: one whose payee
    spells a card number lands under the cut name and that name's id, as
    the bill pass names it now. Where each lands is decided for all of
    them at once, before any is written (_plan_bill_moves), so the order
    the archive lists them in never changes the answer."""
    from ..engine import bills
    ctx.active_room = bills.MAX_ACTIVE_BILLS - bills.active_bill_count(conn)
    ctx.bills_over_cap = ctx.bad_due = 0
    # bills.csv since the rename; older backups say recurring.csv
    member = ("bills.csv" if "bills.csv" in z.namelist()
              else "recurring.csv")
    rows = []
    for r in R._rows(z, member):
        if not r.get("id"):
            out.refuse("bills", "malformed")
            continue
        try:
            amount = R._money(r.get("amount"))
            monthly = R._money(r.get("monthly_amount"))
        except R._AmountOutOfRange:
            out.refuse("bills", "out_of_range")
            continue
        except R._NotANumber:
            out.refuse("bills", "malformed")
            continue
        raw = R.clean_bill_raw(R._j(r.get("raw")))
        # an archive made before the mean-year rate carries the old 52-week
        # figure; restate it as migration 183 did for stored rows
        monthly = bills.restated_monthly(amount, monthly, raw)
        # A due date past the years the product reads (a bill rolled on a
        # centuries-long cycle by an older install) would abort the whole
        # archive here, and every nightly pass after it; the bill is kept
        # without its due date, and the note says so.
        due_on = None
        if r.get("due_on"):
            try:
                due_on = R.as_date(str(r["due_on"])[:10])
            except (TypeError, ValueError, OverflowError):
                due_on = None
            if due_on is None:
                raw.pop("dueOn", None)
                ctx.bad_due += 1
        # the plan anchors on raw dueOn: a row whose stored copy was
        # dropped as unreadable takes it back from the column
        if due_on is not None and not raw.get("dueOn"):
            raw["dueOn"] = due_on.isoformat()
        rows.append((r, amount, monthly, raw, due_on))
    plan = R._plan_bill_moves(
        conn, [(r["id"], R._descriptor(r.get("payee")),
                raw.get("source") == "manual")
               for r, _a, _m, raw, _d in rows])
    for (r, amount, monthly, raw, due_on), moved in zip(rows, plan):
        yield {**r, "_bill": (amount, monthly, raw, due_on, moved)}


def _bill(r, ctx, out):
    amount, monthly, raw, due_on, (bill_id, payee, held) = r["_bill"]
    archived_id, archived_payee = r["id"], R._descriptor(r.get("payee"))
    # what names a bill by its archived id or payee (pins, offers, two
    # settings) follows these onto the one it landed as
    if held is not None:
        ctx.bill_ids[archived_id] = held[0]
        ctx.bill_payees[archived_payee] = held[1]
        return Refuse("present")
    # the uid names the bill through a rename, which rewrites its id and
    # payee: a bill here holding the archived uid IS this row, renamed
    # since the archive was made, and the row is counted present
    uid = _archived_uid(r.get("uid"))
    same = uid and ctx.conn.execute(
        "SELECT id, payee FROM bills WHERE uid = %s::uuid", (uid,)).fetchone()
    if same:
        ctx.bill_ids[archived_id] = same["id"]
        ctx.bill_payees[archived_payee] = same["payee"]
        return Refuse("present")
    try:
        active = R._i(r.get("active"), 1)
        completed = R._i(r.get("is_completed"), 0)
    except R._NotANumber:
        return Refuse("malformed")
    if (bill_id, payee) != (archived_id, archived_payee):
        ctx.bill_ids[archived_id] = bill_id
        ctx.bill_payees[archived_payee] = payee
    # Bills past the ceiling still come back, archived — the household
    # loses nothing and can bring them back one by one — and the note
    # says how many.
    capped = bool(active and ctx.active_room <= 0)
    if capped:
        active = 0
    ctx.last_bill = (active, capped)
    return {"id": bill_id, "uid": _archived_uid(r.get("uid")),
            # the day it was set up as archived; an archive from before
            # it was kept, or one that does not read as a day, lands none
            # (not known) — never the day of the restore
            "created_on": _archived_day(r.get("created_on")),
            "type": r.get("type"), "payee": payee,
            "amount": amount, "frequency": r.get("frequency") or None,
            "monthly_amount": monthly, "due_on": due_on,
            "category": r.get("category") or None,
            "merchant": r.get("merchant") or None,
            "is_completed": completed,
            "active": active, "raw": R.jsonb(raw)}


def _archived_day(v) -> str | None:
    """An archived day (ISO text) when it reads as one, else None."""
    import datetime as _dt
    try:
        return (_dt.date.fromisoformat(str(v)[:10]).isoformat()
                if v not in (None, "") else None)
    except (ValueError, TypeError):
        return None


def _archived_uid(v) -> str | None:
    """A bill's archived uid when it reads as one; None (the column's
    default draws one) otherwise, or for an archive from before bills had
    one."""
    try:
        return str(uuid.UUID(str(v))) if v not in (None, "") else None
    except (ValueError, TypeError, AttributeError):
        return None


def _bill_landed(ctx, got):
    active, capped = ctx.last_bill
    wrote = sum(g is not None for g in got)
    if active:
        ctx.active_room -= wrote
    if capped:
        ctx.bills_over_cap += wrote


def _tell_bills(conn, ctx, out):
    from ..engine import bills
    notes = ctx._ready("notes")
    n = ctx.bills_over_cap
    if n:
        notes.append(f"{n} active bill{'s' if n != 1 else ''} past the "
                     f"limit of {bills.MAX_ACTIVE_BILLS} came back archived")
    n = ctx.bad_due
    if n:
        notes.append(f"{n} bill{'s' if n != 1 else ''} carried a due date "
                     "outside the supported years and came back without "
                     "one")


def _budget_month(r, ctx, out):
    from ..engine.budget import _snapshot_budgets_numeric
    y, m = R._i(r.get("year")), R._i(r.get("month"))
    # non-finite budgets read as null BEFORE the numeric check, so that
    # check refuses the month rather than the write aborting
    cfg = R._finite_only(R._j(r.get("config")))
    if not y or not m or not (1 <= m <= 12) or not (1 <= y <= 9998) \
            or not isinstance(cfg, dict):
        return Refuse("malformed")
    # the budgets must be NUMBERS — the live save path numbers every write,
    # and this archive is the one door that bypasses it. A stored "x" would
    # fail the history endpoint and the month/year lenses with no UI to
    # remove the row.
    if not _snapshot_budgets_numeric(cfg):
        return Refuse("malformed")
    # a frozen month is read back as a config like any other — the week
    # lens hands its excluded list straight to the spend query
    R.clean_excluded_accounts(cfg)
    R._rename_account_keys(cfg, ctx._ready("renamed"))
    # the frozen bill schedule travels too; shape-checked here and row by
    # row on read (snapshot_bill_rows), so a crafted blob degrades to "no
    # frozen schedule", never a crash
    snap_bills = R._finite_only(R._j(r.get("bills")))
    if not (isinstance(snap_bills, dict)
            and isinstance(snap_bills.get("rows"), list)):
        snap_bills = None
    else:
        # each frozen row's raw config gets the pass a live bill gets: the
        # lenses expand these rows the same way, and a recurrence that is a
        # string or an interval that is text would fail that month and its
        # year. A row whose payee or amount is malformed is dropped.
        snap_bills = {**snap_bills, "rows": [
            {**b, "raw": R.clean_bill_raw(b.get("raw"))}
            for b in snap_bills["rows"]
            if isinstance(b, dict)
            and isinstance(b.get("payee"), str)
            and isinstance(b.get("amount"), (int, float))
            and not isinstance(b.get("amount"), bool)]}
    return {"year": y, "month": m, "config": R.jsonb(R.scrub_config(cfg)),
            "bills": R.jsonb(snap_bills) if snap_bills is not None else None,
            "captured_at": R._ts(r.get("captured_at")),
            "source": (r.get("source") if r.get("source")
                       in ("close", "backfill") else "close")}


def _journal(conn, z, ctx, out):
    """The archive's renames in the archive's id order. The journal's ids
    come from ONE sequence every household shares, and an archive's ids
    are whatever the file says. Kept, and the sequence advanced past them,
    one archive could spend the shared counter to the edge of its range
    and every household's next rename would fail. Nothing points at a
    journal row by id; the id only orders the rows. So each row gets a
    fresh id here, inserted in that order, and the sequence moves only as
    far as these inserts take it."""
    def order(r):
        try:
            return (0, int(R._f(r.get("id")) or 0))
        except R._ArchiveRefused:
            raise
        except (TypeError, ValueError, OverflowError):
            return (1, 0)

    def journalled(r) -> bool:
        if r.get("raw_merchant") and r.get("to_canonical"):
            return True
        out.refuse("merchant_renames", "malformed")
        return False

    # A journal row's result is cut where it is what a machine's alias (cut
    # above) still reads; then a `from` no journal row names as somebody's
    # result is a machine's, and cut too. A person's stays.
    journal = sorted((r for r in R._rows(z, "merchant_renames.csv")
                      if journalled(r)), key=order)
    cut_aliases = ctx._ready("cut_aliases")
    for r in journal:
        # a result this household journals whole (its own alias for the
        # descriptor is a person's) is the rename it already holds: cut,
        # it would read as a new one and be journalled twice
        if ((r["raw_merchant"], r["to_canonical"]) in cut_aliases
                and not conn.execute(
                    "SELECT 1 FROM merchant_renames WHERE raw_merchant = %s"
                    " AND to_canonical = %s",
                    (r["raw_merchant"], r["to_canonical"])).fetchone()):
            r["to_canonical"] = R.card_number.cut(r["to_canonical"])
    results = {r["to_canonical"] for r in journal}
    for r in journal:
        frm = r.get("from_canonical") or None
        if (R.card_number.holds_number(frm) and frm not in results
                and not conn.execute(
                    "SELECT 1 FROM merchant_renames"
                    " WHERE to_canonical = %s", (frm,)).fetchone()):
            r["from_canonical"] = R.card_number.cut(frm)
    return journal


def _rename(r, ctx, out):
    # the journal row follows its alias: onto the masked key when
    # file-import rows carry it and nothing keys on the full one
    raw = r["raw_merchant"]
    filed, kept = ctx._ready("alias_where")(raw)
    key = R.card_number.cut(raw) if filed and not kept else raw
    # what the rename did to the category rules it moved, so undoing it on
    # this instance can put them back; anything but the object the rename
    # writes is no note at all
    rules = R._j(r.get("rules"))
    return {"raw_merchant": key,
            "from_canonical": r.get("from_canonical") or None,
            "to_canonical": r["to_canonical"], "at": R._ts(r.get("at")),
            "rules": R.jsonb(rules) if isinstance(rules, dict) else None}


def _split_groups(conn, z, ctx, out):
    """The archive's split parts, one group per charge in the order the
    charges first appear. A part whose row is absent (a pre-FK archive)
    is skipped like any other annotation."""
    groups: dict[str, list] = {}
    # each group's first line in the file, which names it if it is skipped
    lines: dict[str, int] = {}
    for at, r in enumerate(R._rows(z, "transaction_splits.csv"), 1):
        if not R._txn_exists(conn, r.get("txn_id")):
            out.refuse("transaction_splits", "orphaned")
            continue
        try:
            amount, line = R._f(r.get("amount")), R._i(r.get("line"), 0)
        except R._NotANumber:
            amount = None
        if not r.get("category") or amount is None:
            out.refuse("transaction_splits", "malformed")
            continue
        lines.setdefault(r["txn_id"], at)
        groups.setdefault(r["txn_id"], []).append(
            (line or 0, r["category"], amount,
             (r.get("source") or "user").strip() or "user"))
    for txn_id, parts in groups.items():
        parts.sort(key=lambda p: p[0])
        yield {"txn_id": txn_id, "_parts": parts, "_line": lines[txn_id]}


def _split(g, ctx, out):
    from ..engine import splits
    txn_id, parts = g["txn_id"], g["_parts"]
    n = len(parts)
    # a store's parts are regenerated by its matcher from the restored
    # records; only a person's are worth carrying
    if any(src != "user" for *_, src in parts):
        return Refuse("rebuilt", n=n)
    # only a PERSON's split here outranks the archive's: a store matcher's
    # parts are the receipt's reading, recomputed on every match run, and
    # a person's split outranks those wherever it comes from — so they
    # give way to the archive's hand split
    conn = ctx.conn
    if conn.execute("SELECT 1 FROM transaction_splits WHERE txn_id = %s"
                    " AND source = 'user' LIMIT 1", (txn_id,)).fetchone():
        return Refuse("present", n=n)
    row = conn.execute("SELECT amount FROM transactions WHERE id = %s",
                       (txn_id,)).fetchone()
    # the door's own rule: a split on a row the spend rollups do not read
    # is counted by nothing and dropped by the next sync's stale sweep, so
    # it is skipped rather than reported restored
    if not row or not splits.splittable(conn, txn_id):
        return Refuse("malformed", n=n)
    try:
        clean = splits.validate(row["amount"], [
            {"category": c, "amount": a} for _, c, a, _src in parts])
    except splits.SplitError:
        return Refuse("malformed", n=n)
    return {"txn_id": txn_id, "lines": clean}


def _write_split(conn, ctx, out, L, groups):
    for g in groups:
        # Every part judged against its columns before anything is deleted:
        # one the database would refuse refuses the split here, and the
        # store's parts stay, rather than ending the restore after them.
        lines = [(g["txn_id"], line, p["category"], p["amount"])
                 for line, p in enumerate(g["lines"], 1)]
        why = next((w for _, _, w in (R._fit_row(conn, L.sql, row)
                                      for row in lines) if w), None)
        if why is not None:
            out._misfit("transaction_splits", why)
            if len(lines) > 1:
                out.refuse("transaction_splits", "malformed", len(lines) - 1)
            continue
        # the store's parts give way; a person's would have kept the row
        conn.execute("DELETE FROM transaction_splits WHERE txn_id = %s"
                     " AND source <> 'user'", (g["txn_id"],))
        for row in lines:
            conn.execute(L.sql, row)
        # validate returns one line per part, or raises
        out.wrote("transaction_splits", len(g["lines"]))


def _income_years(conn, z, ctx):
    from ..engine import taxdocs
    # The year locks of every year this restore writes income for, taken
    # before the first income row is written: a live W-2 store holds its
    # year's lock while it writes the year's row, so a restore that wrote
    # rows first and asked for the lock after could deadlock with it. Only
    # the archive's own years (a W-2 can hold no other), read ahead, so a
    # restore holds a handful of locks, not two hundred; a person's W-2
    # edit behind this restore answers busy.
    years: set[int] = set()
    for name in ("income_annual.csv", "income_documents.csv",
                 "income_upload_values.csv"):
        for r in R._rows(z, name):
            # parsed as the rows below are (`_i` takes "2024.0"), so no row
            # can land in a year whose lock was not taken
            try:
                y = R._i(r.get("year"))
            except R._ArchiveRefused:
                # a non-finite number refuses the archive here as anywhere
                raise
            except Exception:                          # noqa: BLE001
                # the row's own pass refuses the cell as it always has
                continue
            if y in taxdocs.W2_YEARS:
                years.add(y)
    taxdocs._lock_years(conn, years, bounded=False)
    # every year is worked out again once the uploads have landed; first,
    # what the household holds that no upload of its own explains is kept
    # as its own upload, so nothing the archive brings sets it back, and
    # the (year, column) pairs its documents give are noted: an archived
    # figure from before the list never replaces one of them
    ctx.income_years = set(years)
    ctx.landed_income_years = set()
    # and the household's document ids, held to the end (after the year
    # locks, as every writer takes them): a live store of a year this
    # restore does not hold allocates from the same MAX+1, and adopting
    # below asks for them as a person's request does
    taxdocs.lock_document_ids(conn)
    taxdocs.adopt_unexplained(conn, sorted(years))
    ctx.local_cols = {(r["year"], c) for r in conn.execute(
        """SELECT v.year, v.fields FROM income_upload_values v
             JOIN income_uploads u ON u.id = v.upload_id
            WHERE u.removed_at IS NULL""").fetchall()
        if isinstance(r["fields"], dict) for c in r["fields"]}
    # An archive older than migration 226 carries no record of what the
    # W-2s wrote: as the migration does, a figure on a row only the W-2
    # path made (source exactly 'w2') is seeded as theirs, and the years
    # this restore landed such a row in are reconciled with their W-2s once
    # those land (taxdocs.reconcile_unmarked).
    ctx.seeded_years = set()
    ctx.pre_226 = R._upgrades.older_than(R._upgrades.archive_schema(z),
                                         R._W2_FIGURE_MARKS)



def _income_year(r, ctx, out):
    f = R._f
    try:
        # read as the year locks were (which skipped what they could not
        # read): a year that is no number refuses its row
        year = R._i(r["year"])
    except R._NotANumber:
        return Refuse("malformed")
    mark = f(r.get("primary_wages_w2"))
    if ctx.pre_226:
        mark = None
        if r.get("source") == "w2" and f(r.get("primary_wages")) is not None:
            mark = f(r.get("primary_wages"))
    return {"year": year, "total_income": f(r.get("total_income")),
            "agi": f(r.get("agi")),
            "taxable_income": f(r.get("taxable_income")),
            "tax_paid": f(r.get("tax_paid")), "wages": f(r.get("wages")),
            "primary_wages": f(r.get("primary_wages")),
            "investment_income": f(r.get("investment_income")),
            "capital_gain": f(r.get("capital_gain")),
            "spouse_wages": f(r.get("spouse_wages")),
            "ss_earnings": f(r.get("ss_earnings")),
            "medicare_earnings": f(r.get("medicare_earnings")),
            "filing_status": r.get("filing_status") or None,
            "joint": R._i(r.get("joint"), 0),
            "source": r.get("source") or None, "note": r.get("note") or None,
            # what the W-2s last wrote into each wage figure
            # (specs/w2-wages.md)
            "primary_wages_w2": mark,
            "spouse_wages_w2": f(r.get("spouse_wages_w2"))}


def _income_years_landed(ctx, got):
    # the years whose income row this restore wrote: only their figures
    # are the archive's to adopt after landing
    ctx.landed_income_years.update(g for g in got if g is not None)
    if ctx.pre_226:
        ctx.seeded_years.update(g for g in got if g is not None)


def _documents(conn, z, ctx, out):
    """The archive's documents, each with whether it is already here and
    whether it looks like a W-2 the household holds. Read whole first (a
    household holds a few dozen), because whether one is already here is
    decided per identity over all of its copies, not row by row."""
    from ..engine import taxdocs
    ctx.w2_years = set()
    docs: list[dict] = []
    for line, r in enumerate(R._rows(z, "income_documents.csv"), 1):
        if r.get("id") in (None, "") or r.get("year") in (None, ""):
            out.refuse("income_documents", "malformed")
            continue
        try:
            # read as the year locks were (which skipped what they could
            # not read): a cell that is no number refuses its row
            year, doc_id = R._i(r["year"]), R._i(r["id"])
        except R._NotANumber:
            out.refuse("income_documents", "malformed")
            continue
        amounts = R._j(r.get("amounts"))
        # `ein` is DELIBERATELY not restored. Taxdocs persists only
        # ein_last4, but an older archive may still carry the full W-2 EIN
        # in this CSV, and reading it back would store it again. The last
        # four are restored instead, which is what the live write path
        # stores (preferring the column it writes, falling back to the last
        # four OF a legacy plaintext value).
        form = r.get("form") or ""
        if form == "W-2" and year not in taxdocs.W2_YEARS:
            # no W-2 can be stored for it, and its year lock was not taken
            out.refuse("income_documents", "malformed")
            continue
        # A document's id is only a number within its household (each one
        # counts 1, 2, 3...), so an archive's ids collide with the
        # documents a populated household already has, and keying on them
        # would drop the archive's different documents as present. Present
        # means the same document; one that is not lands under the next
        # free id, as the live write path allocates it.
        try:
            ident, key = R._income_document_identity(r, year, form, amounts)
        except R._NotANumber:
            out.refuse("income_documents", "malformed")
            continue
        docs.append({"_line": line,
                     "r": r, "ident": ident, "key": key, "id": doc_id,
                     "said": r.get("owner") or None, "present": False})

    # "Present" is counted, not looked up: an archive may hold the same
    # document twice (two spouses' identical W-2s, or one person's two kept
    # as a second W-2), and a lookup would find the first copy this very
    # restore landed and drop the second. Each copy the household held
    # BEFORE the restore accounts for one archive copy — first the copy
    # under the same person, then any — so two spouses' identical W-2s
    # never both land under one of them. A copy named since the archive was
    # written is still present.
    held: dict[tuple, list] = {}
    for d in docs:
        if d["key"] not in held:
            held[d["key"]] = [row["owner"] for row in conn.execute(
                """SELECT owner FROM income_documents
                    WHERE year = %s AND form = %s
                      AND payer IS NOT DISTINCT FROM %s
                      AND ein_last4 IS NOT DISTINCT FROM %s
                      AND primary_amount IS NOT DISTINCT FROM %s
                      AND amounts IS NOT DISTINCT FROM %s::jsonb
                      AND notes IS NOT DISTINCT FROM %s""",
                d["ident"]).fetchall()]
    # how many exact copies of each identity the household held: those are
    # twins, answered by the count below, and a lookalike is a stored W-2
    # BEYOND them
    n_held = {k: len(owners) for k, owners in held.items()}
    for same_person in (True, False):
        for d in docs:
            owners = held[d["key"]]
            if d["present"] or not owners:
                continue
            if same_person:
                if d["said"] not in owners:
                    continue
                owners.remove(d["said"])
            else:
                owners.pop()
            d["present"] = True
    # An archived W-2 that is not here but LOOKS like a W-2 the household
    # already holds (same box 1, no EIN saying another employer) is the
    # question the upload asks — the same form re-read or replaced since
    # the export, or a second W-2 — and a restore cannot ask it. It lands
    # as nobody's (listed, counted for nobody until named) rather than
    # summed in a second time. Checked against the household as it was,
    # before anything below lands.
    for d in docs:
        ident = d["ident"]
        d["lookalike"] = (
            not d["present"] and ident[1] == "W-2"
            and len(taxdocs.w2_matches(conn, ident[0], ident[2], ident[3],
                                       ident[4])) > n_held[d["key"]])
    return docs


def _document(d, ctx, out):
    from ..engine import taxdocs
    conn = ctx.conn
    r, ident = d["r"], d["ident"]
    year, form = ident[0], ident[1]
    if d["present"]:
        # the same document is already here, under whatever id
        return Refuse("present")
    seeded = ctx._ready("seeded_years")
    # Whose it is. A build older than migration 226 never asked (every W-2
    # it stored is the primary earner's), so its archive's word is not
    # evidence: in a year this restore made, the W-2s land as archived and
    # are reconciled with the year's figure after, as migration 227 does;
    # in a year the household already had, they land by the rule an older
    # app's upload follows (taxdocs.unsaid_owner) and are never assumed.
    if form == "W-2" and d["lookalike"]:
        owner = "unassigned"
    elif form == "W-2" and ctx.pre_226 and year not in seeded:
        owner = taxdocs.unsaid_owner(conn, year)
    elif form == "W-2" and "owner" not in r:
        owner = ("primary" if year in seeded
                 else taxdocs.unsaid_owner(conn, year))
    else:
        owner = r.get("owner") or "primary"
        if owner not in ("primary", "spouse", "unassigned"):
            owner = "unassigned"
    doc_id = d["id"]
    # a taken id moves to the next free one, under the document-id lock
    if conn.execute("SELECT 1 FROM income_documents WHERE id = %s",
                    (doc_id,)).fetchone():
        doc_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id "
                              "FROM income_documents").fetchone()["id"]
    if form == "W-2" and year is not None:
        ctx.w2_years.add(year)
    # the upload it came in, under the id that upload landed with; one the
    # archive does not carry (or that was here already) is left unnamed and
    # is given an upload of its own after (_resolve_income)
    try:
        old_upload = R._i(r.get("upload_id"))
    except R._NotANumber:
        old_upload = None
    if old_upload is not None and old_upload in ctx.upload_ids \
            and ctx.upload_ids[old_upload] is None:
        # its upload is here already: its forms are the household's as it
        # left them — held by a removal, or stored — never landed again
        return Refuse("present")
    upload_id = ctx.upload_ids.get(old_upload) if old_upload else None
    return {"id": doc_id, "year": ident[0], "form": ident[1],
            "owner": owner, "payer": ident[2], "ein_last4": ident[3],
            "primary_amount": ident[4], "amounts": ident[5],
            "notes": ident[6], "upload_id": upload_id,
            # an archive older than migration 233 carries no mark: unmarked
            "payer_named_by_person": R._b(r.get("payer_named_by_person"))}


def _uploads(conn, z, ctx, out):
    """The archive's stored documents (income_uploads). An upload is the
    same one by its `uid` and nothing else: two can share their kind, file
    name and moment (every upload the migration made does). One already
    here, removed or not, is not landed again; a new one lands under its
    own id, or the next free one when that is taken (`_upload`)."""
    from ..engine import taxdocs
    ctx.upload_ids = {}
    ctx.upload_kinds = {}
    ctx.upload_pending = None
    ctx.archive_has_uploads = "income_uploads.csv" in z.namelist()
    rows = []
    for line, r in enumerate(R._rows(z, "income_uploads.csv"), 1):
        try:
            old = R._i(r.get("id"))
        except R._NotANumber:
            out.refuse("income_uploads", "malformed")
            continue
        at = _not_ahead(R._ts(r.get("stored_at")))
        uid = R._uuid_or_none(r.get("uid"))
        gone_cell = (r.get("removed_at") or "").strip()
        gone = _not_ahead(R._ts(gone_cell)) if gone_cell else None
        if (old is None or at is None or uid is None
                or r.get("kind") not in taxdocs.UPLOAD_KINDS
                or (gone_cell and gone is None)):
            out.refuse("income_uploads", "malformed")
            continue
        held = conn.execute("SELECT id FROM income_uploads WHERE uid = %s",
                            (uid,)).fetchone()
        rows.append({"_line": line,
                     "r": r, "old": old, "at": at, "uid": uid,
                     "removed_at": gone,
                     "held": held["id"] if held else None})
    return rows


# the earliest moment a stored document may carry (as a receipt's): year 1
# cannot be shown in any zone west of UTC
_UPLOAD_YEAR_MIN = 1900


def _not_ahead(ts):
    """An archived moment no later than now: a document dated ahead (a
    clock that ran fast, an edited archive) would outrank every document
    stored after it, and the nightly adoption would date a new one past it
    every night. One before 1900 is no moment (None: the row is refused)."""
    import datetime as _dt
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    if ts.year < _UPLOAD_YEAR_MIN:
        return None
    return min(ts, _dt.datetime.now(_dt.timezone.utc))


def _held_forms(cell) -> list | None:
    """A removed upload's held forms as the put-back will write them: only
    readable forms, the EIN cut to its last four (as the documents lander
    cuts it), so a restore never stores what the live door would not."""
    from ..engine import taxdocs
    held = R._j(cell)
    if not isinstance(held, list):
        return None
    forms = [f for f in map(taxdocs._held_form, held[:taxdocs.MAX_W2_ROWS
                                                     * 40]) if f]
    return forms or None


def _upload(d, ctx, out):
    conn = ctx.conn
    ctx.upload_kinds[d["old"]] = d["r"]["kind"]
    if d["held"] is not None:
        # here already (removed or not, as the household left it): its
        # values and forms are the household's, and are not landed again
        ctx.upload_ids[d["old"]] = None
        return Refuse("present")
    from ..engine import taxdocs
    new = d["old"]
    if conn.execute("SELECT 1 FROM income_uploads WHERE id = %s",
                    (new,)).fetchone():
        # the live door's allocator, under its lock (held already: the
        # restore took it with the year locks, `_income_years`)
        new = taxdocs._new_upload_id(conn)
    # mapped once the write says it landed (_upload_landed): a row that
    # does not land names no id its values and forms could follow
    ctx.upload_pending = d["old"]
    r = d["r"]
    held = _held_forms(r.get("held_documents"))
    return {"id": new, "uid": d["uid"], "kind": r["kind"],
            # the file name as the live door keeps it: cut, bounded, no path
            "filename": taxdocs._clean_filename(r.get("filename")),
            "stored_at": d["at"], "removed_at": d["removed_at"],
            "held_documents": R.jsonb(held) if held else None}


def _upload_landed(ctx, got):
    old, ctx.upload_pending = ctx.upload_pending, None
    if old is not None:
        ctx.upload_ids[old] = got[0] if got else None


def _start_upload_values(conn, z, ctx):
    ctx.upload_years = set()


def _upload_value(r, ctx, out):
    try:
        old, year = R._i(r.get("upload_id")), R._i(r.get("year"))
    except R._NotANumber:
        return Refuse("malformed")
    from ..engine import taxdocs
    fields = R._j(r.get("fields"))
    # a year no tax year can be (1e308 reads as a number) is no row of a
    # document: its figures could never be worked out for it
    if (old is None or year not in taxdocs.W2_YEARS
            or not isinstance(fields, dict)):
        return Refuse("malformed")
    if old not in ctx.upload_ids:
        return Refuse("orphaned")
    new = ctx.upload_ids[old]
    if new is None:
        return Refuse("present")
    kept = {k: float(v) for k, v in fields.items()
            if k in taxdocs.ANNUAL_FIELDS and taxdocs._is_num(v)
            and taxdocs._money(v)}
    if str(ctx.upload_kinds.get(old, "")).startswith("legacy_"):
        # an archived figure from before the list never replaces one the
        # household's own documents give the same year
        kept = {k: v for k, v in kept.items()
                if (year, k) not in ctx.local_cols}
    if not kept:
        # a row that gives no figure the household lacks is no year of
        # the document: it is not landed empty (taxdocs._years_by)
        return Refuse("present")
    ctx.upload_years.add(year)
    return {"upload_id": new, "year": year, "fields": R.jsonb(kept)}


def _resolve_income(conn, ctx, out):
    """After the income tables land: what they landed that no upload
    explains is given uploads — an archive from before the stored list as
    migration 231 gave a household its own (taxdocs.backfill_household,
    when the household holds no upload), every other unexplained figure or
    form by taxdocs.adopt_unexplained — and every year the archive brought
    income for is worked out again (taxdocs.resolve_year): a no-op for a
    household restored from its own export. The year locks were taken by
    the income plan, which adopted the household's own unexplained figures
    before anything landed."""
    from ..engine import taxdocs
    years = sorted(ctx.upload_years | ctx.income_years)
    # held since the plan; asked again so adopting never waits bounded
    taxdocs.lock_document_ids(conn)
    if not ctx.archive_has_uploads:
        taxdocs.backfill_household(conn)
    # forms the archive brought without an upload are given one; figures
    # only where the archive wrote the year's row: a year the household
    # held kept its own row, which the plan adopted before anything landed
    taxdocs._adopt_forms(conn, years)
    taxdocs.adopt_unexplained(conn, sorted(ctx.landed_income_years))
    for year in years:
        taxdocs.resolve_year(conn, year)


def _resum_w2_years(conn, ctx, out):
    from ..engine import taxdocs
    # Each year a W-2 may have landed in is summed once: a no-op for a
    # household restored from its own export, the right figure for one
    # merged into a populated household. The locks were taken before.
    for year in sorted(ctx.w2_years):
        if year in ctx.seeded_years:
            taxdocs.reconcile_unmarked(conn, year)
        taxdocs.resum(conn, year)


LANDERS.update({L.table: L for L in (
    Lander("bills", ("bills.csv", "recurring.csv"),
           ("id", "uid", "type", "payee", "amount", "frequency",
            "monthly_amount", "due_on", "category", "merchant",
            "is_completed", "active", "synced_at", "raw", "created_on"),
           _bill,
           columns("id"),
           fixed={"synced_at": "now()"}, locks=(_lock_active_bills,),
           # the archived uid, unless another bill here holds it: _bill
           # counts a row whose uid a bill here holds as that bill, so
           # this is only the archive naming one uid twice; the column's
           # default then draws a new one
           computed={"uid": (
               "COALESCE((SELECT %s::uuid WHERE NOT EXISTS (SELECT 1 FROM"
               f" bills WHERE tenant_id = {TENANT} AND uid = %s::uuid)),"
               " gen_random_uuid())", ("uid", "uid"))},
           source=_bills_in_plan, batch=1, sequential=True,
           landed=_bill_landed,
           after_land=(_tell_bills,),
           reads=("bill_ids", "bill_payees", "notes"),
           writes=("active_room", "bills_over_cap", "bad_due",
                   "last_bill")),
    # Per-month frozen budgets: without them every closed month on the
    # destination falls back to the no-verdict net view. Scrubbed on the
    # way in like the live settings blob — the snapshot only feeds
    # month_status math, so a crafted archive must not smuggle credential
    # or policy keys through this second config door. A month already
    # frozen here stays frozen.
    Lander("budget_snapshots", ("budget_snapshots.csv",),
           ("year", "month", "config", "bills", "captured_at", "source"),
           _budget_month, columns("year", "month"),
           defaults={"captured_at": "now()"}, reads=("renamed",)),
    # A row already in the journal (the same archive restored twice) is
    # recognised by what it says, not by id.
    Lander("merchant_renames", ("merchant_renames.csv",),
           ("raw_merchant", "from_canonical", "to_canonical", "at", "rules"),
           _rename, columns("raw_merchant", "from_canonical", "to_canonical",
                            "at"),
           presence=ByKey(
               why="the journal keeps every rename, and one without its "
                   "time is the same rename at any time: no index says that",
               blank_is_any={"at": "timestamptz"}),
           defaults={"at": "now()"}, source=_journal,
           reads=("cut_aliases", "alias_where")),
    # A hand split is one statement about one charge — these parts, adding
    # up to it — so it restores per transaction, whole or not at all.
    # Merging an archive's lines into a split the row already has would
    # produce parts from both versions summing past the charge, and the
    # sync's stale-split pass would then delete every part, the current
    # split included. So a row that already has a person's split keeps it;
    # otherwise the archive's parts must pass the same validation a
    # person's edit does against the restored amount, or none are written.
    Lander("transaction_splits", ("transaction_splits.csv",),
           ("txn_id", "line", "category", "amount", "source"), _split,
           columns("txn_id"),
           presence=Group(why="a split is its charge's parts together"),
           fixed={"source": "'user'"}, source=_split_groups,
           write=_write_split, reads=("conn",)),
    Lander("income_annual", ("income_annual.csv",),
           ("year", "total_income", "agi", "taxable_income", "tax_paid",
            "wages", "primary_wages", "investment_income", "capital_gain",
            "spouse_wages", "ss_earnings", "medicare_earnings",
            "filing_status", "joint", "source", "note", "primary_wages_w2",
            "spouse_wages_w2"), _income_year, columns("year"),
           required=("year",), plan=_income_years,
           landed=_income_years_landed,
           writes=("pre_226", "seeded_years", "income_years",
                   "local_cols", "landed_income_years")),
    # the stored documents and what each wrote (specs/tax-documents.md,
    # "Stored documents"): who stored or removed one is this instance's
    # person, never carried
    Lander("income_uploads", ("income_uploads.csv",),
           ("id", "uid", "kind", "filename", "stored_at", "removed_at",
            "held_documents"), _upload, columns("uid"),
           casts={"uid": "uuid"},
           fixed={"stored_by": "NULL", "removed_by": "NULL"},
           source=_uploads, batch=1, sequential=True, allocates_ids=True,
           returning="id", landed=_upload_landed,
           reads=("conn",), writes=("upload_ids", "upload_kinds",
                                    "upload_pending",
                                    "archive_has_uploads")),
    Lander("income_upload_values", ("income_upload_values.csv",),
           ("upload_id", "year", "fields"), _upload_value,
           columns("upload_id", "year"), required=("upload_id", "year"),
           plan=_start_upload_values,
           reads=("upload_ids", "upload_kinds", "local_cols"),
           writes=("upload_years",)),
    Lander("income_documents", ("income_documents.csv",),
           ("id", "year", "form", "owner", "payer", "ein_last4",
            "primary_amount", "amounts", "notes", "upload_id",
            "payer_named_by_person"), _document,
           columns("year", "form", "payer", "ein_last4", "primary_amount",
                   "amounts", "notes"),
           presence=Counted(
               conflict=("id",),
               why="twins are real: one copy is present per copy the "
                   "household held, the same person's first"),
           source=_documents, batch=1, sequential=True, allocates_ids=True,
           after_land=(_resum_w2_years,),
           reads=("conn", "pre_226", "seeded_years", "upload_ids"),
           writes=("w2_years",)),
)})


# ---- the spine: connections, merchants, accounts, transactions ----------

def _item(r, ctx, out):
    aggregator = r.get("aggregator") or "manual"
    if aggregator == "csv":
        if ctx.push_fed is None:
            ctx.push_fed = R._push_fed_items(ctx._ready("z"))
        # a push door's connection an archive from before the doors stamped
        # their own name still calls 'csv' (migration 022)
        aggregator = ctx.push_fed.get(str(r["id"]), aggregator)
    archived = ((r.get("status") or "") == "archived"
                or aggregator != (r.get("aggregator") or "manual"))
    return {"id": r["id"], "aggregator": aggregator,
            "institution_id": r.get("institution_id") or None,
            "institution_name": r.get("institution_name") or None,
            # a sync-less shell until the aggregator is reconnected — except
            # archived items (push-fed plan-CSV/coinbase, archived simplefin
            # children) which stay archived: 'restored' would re-enter them
            # into connection-freshness checks and the hourly pull loop
            # they were deliberately kept out of
            "status": "archived" if archived else "restored",
            "raw": R.jsonb(R._raw_dict(R._j(r.get("raw")))),
            "logo": r.get("logo") or None,
            "brand_color": r.get("brand_color") or None,
            "url": r.get("url") or None,
            # Why and when a disconnected bank went. The reason is what
            # refuses a reconnect that would bring its accounts in twice (a
            # newer link adopted them) or bring back one the owner deleted,
            # and what lets a new bridge token revive only the banks that
            # went down with the bridge; the date is where its catch-up
            # pull starts. Only a disconnected row carries them: a live
            # connection restores as a shell, and a stray reason on it
            # would mean nothing. resync_from stays behind on purpose — it
            # is this instance's own pull bookkeeping.
            "archived_reason": (r.get("archived_reason") or None)
            if archived else None,
            "archived_at": R._ts(r.get("archived_at")) if archived else None,
            # when the bank was linked is the household's history, not this
            # instance's; an archive without it restores as now
            "linked_at": R._ts(r.get("linked_at"))}


def _start_merchants(conn, z, ctx):
    ctx.live_entities = {
        r["plaid_entity_id"]: r["id"] for r in conn.execute(
            "SELECT id::text AS id, plaid_entity_id FROM merchants "
            "WHERE plaid_entity_id IS NOT NULL").fetchall()}
    ctx.merchant_map, ctx.folded = {}, []
    ctx.persons_names = R._persons_names(conn, z)
    ctx.machine_cut, ctx.persons_live = {}, set()
    ctx.landed_merchants = []


def _merchant(r, ctx, out):
    # A machine's merchant name keeps only the last four of a card number,
    # as the resolver writes it; a name a person gave (on the merchant, or
    # as a manual alias's canonical) stays as they typed it. `machine_cut`
    # maps each live machine name cut here, lowered, to its cut spelling:
    # the rules and 1099 marks keyed on the whole name follow it.
    name = R._descriptor(r.get("name"))
    source = r.get("name_source") or "layer1"
    live_row = not r.get("merged_into")
    if (source == "manual"
            or (isinstance(name, str)
                and name.lower() in ctx.persons_names)):
        if live_row and isinstance(name, str):
            ctx.persons_live.add(name.lower())
    else:
        cut = R.card_number.cut(name)
        if live_row and cut != name:
            ctx.machine_cut[name.lower()] = cut
        name = cut
    aid = str(r["id"]).strip().lower()
    entity = r.get("plaid_entity_id") or None
    live = ctx.live_entities.get(entity) if entity else None
    if live is not None and live != aid:
        ctx.merchant_map[aid] = live
        ctx.folded.append((live, r))
        return Refuse("merged")
    ctx.last_merchant = (entity, aid)
    return {"id": r["id"], "plaid_entity_id": entity,
            # the same clamp the ledger's descriptors get: this name is
            # matched word by word against them
            "name": name, "name_source": source,
            "kind": r.get("kind") or "merchant",
            "logo_url": r.get("logo_url") or None,
            "website": r.get("website") or None,
            "phone": r.get("phone") or None, "mcc": r.get("mcc") or None,
            "parent_id": r.get("parent_id") or None,
            "merged_into": r.get("merged_into") or None,
            "created_at": R._ts(r.get("created_at"))}


def _merchant_landed(ctx, got):
    entity, aid = ctx.last_merchant
    if got[0] is not None:
        ctx.landed_merchants.append(aid)
    if got[0] is not None and entity:
        ctx.live_entities[entity] = aid


def _settle_merchants(conn, ctx, out):
    if ctx.merchant_map:
        R._fold_mapped_merchants(conn, ctx.merchant_map, ctx.folded)
    # An archive can point at merchants it does not carry — a partial
    # export, a hand-edited CSV, a ZIP written before merchants existed
    # for some of its rows. Every such pointer is a lie the rest of the
    # restore would faithfully copy in, and the nightly identity pass
    # cannot run over a dangling merchant pointer. Normalise the graph
    # against what is ACTUALLY in the table now, and keep the set of real
    # ids to screen the transaction and alias rows after.
    ctx.known_merchants = R._normalise_merchant_graph(conn)


def _start_accounts(conn, z, ctx):
    ctx.known_items = {r["id"] for r in conn.execute(
        "SELECT id FROM items").fetchall()}
    ctx.landed_accounts, ctx.numbered_ids = set(), []
    ctx.personal_ids = set()


def _account(r, ctx, out):
    names = R._account_sources
    aid = R._account_id(r["id"])
    if aid != r["id"]:
        ctx._ready("renamed")[r["id"]] = aid
    name = r.get("name")
    # a display name is typed by a person whatever the account's source,
    # so a card number typed into one is cut on every account, as the
    # rename door and `oikonome migrate` cut it
    display = names.stored_account_name(r.get("display_name") or None)
    official = r.get("official_name") or None
    if r.get("item_id") == "manual":
        # A folder import named the account after its file, and an archive
        # written before the import cut the number from that name brings
        # the full account number back with it — typed in groups too, in
        # any of its names. `oikonome migrate` makes the same cut to stored
        # names (base.mask_manual_account_names).
        name, display, official = (names.stored_account_name(v)
                                   for v in (name, display, official))
        # a hand-made id spelling a full number is re-keyed once the rows
        # have landed; any other keeps it, and is named
        restored = str(aid or "")
        if (R.card_number.LONG_RUN_RE.search(restored)
                and not R._sync_base.NUMBERED_MANUAL_ID_RE.search(restored)):
            ctx.numbered_ids.append(name or "an account")
    return {"id": aid,
            "item_id": (r.get("item_id")
                        if r.get("item_id") in ctx.known_items else None),
            "name": name, "display_name": display, "official_name": official,
            "type": r.get("type"), "subtype": r.get("subtype") or None,
            "mask": r.get("mask") or None,
            # _f, not truthiness — a non-string 0 must restore as 0, never
            # NULL
            "balance_current": R._balance(r.get("balance_current")),
            "balance_available": R._balance(r.get("balance_available")),
            "currency": r.get("currency") or None,
            "entity_id": _account_entity(r, aid, ctx),
            # the account's default owner label ("yours/mine/ours"); an
            # archive without the column restores as unattributed
            "owner": r.get("owner") or None,
            "raw": R.jsonb(R.scrub_account_raw(R._raw_dict(R._j(
                r.get("raw"))))),
            # user_removed_at (the soft-hide): without it an export→restore
            # round trip UN-hides every account the user hid — back in the
            # lists, back in the money aggregates, with no indication
            # anything changed
            "user_removed_at": R._ts(r.get("user_removed_at")),
            # type_user_set stops sync from overwriting a type the USER
            # corrected; landed FALSE, the restored account looks right
            # until the next aggregator sync silently reclassifies it.
            # balance_limit rides along: it is what credit-utilisation reads.
            "type_user_set": R._b(r.get("type_user_set")),
            "balance_limit": R._balance(r.get("balance_limit"))}


def _account_entity(r, aid, ctx):
    """The business an account is booked to here. One whose business the
    archive carried but could not be read lands as personal, and is noted
    so the summary can say so."""
    cell = r.get("entity_id")
    ent = ctx.entity(cell)
    if (ent is None and cell
            and Ctx.business_id(cell) in (ctx.unread_entities or ())):
        ctx.personal_ids.add(aid)
    return ent


def _accounts_landed(ctx, got):
    ctx.landed_accounts.update(g for g in got if g is not None)


def _settle_accounts(conn, ctx, out):
    personal = len(ctx.personal_ids & ctx.landed_accounts)
    if personal:
        ctx._ready("notes").append(
            f"{R._plural(personal, 'account', 'accounts')} of a business "
            "that could not be read "
            f"{'was' if personal == 1 else 'were'} restored as personal")
    ids = ctx.numbered_ids
    if ids:
        # the id is permanent (history, adoptions and settings all name
        # it), so it is not re-keyed; the note names each account by its
        # masked name, never by the id that holds the number
        ctx._ready("notes").append(
            f"{R._plural(len(ids), 'account', 'accounts')} created from a "
            "file named after its full account number still "
            f"carr{'ies' if len(ids) == 1 else 'y'} that number in an "
            "internal id that travels in exports: " + ", ".join(ids)
            + ". The importing guide says how to replace one")
    # restored accounts may carry entity ids; the connection's entity
    # scope was stamped at checkout, before they existed
    from ..db import tenancy
    tenancy.set_entity_scope(conn)


def _start_ledger(conn, z, ctx):
    # the accounts a restored stand-in note may name: those here now,
    # under any id an adoption here moved them from
    ctx.noted_accounts = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts").fetchall()} | {
        r["old_account_id"] for r in conn.execute(
            "SELECT old_account_id FROM account_adoptions").fetchall()}
    ctx.ledger_accounts = _accounts_now(conn)
    # the context that lets a bare "payment" read as a card settlement
    ctx.cards = R._sync_base.credit_account_ids(conn)
    ctx.overridden, ctx.landed_ofx, ctx.txn_pending = [], [], []
    ctx.user_ids, ctx.user_cats = [], []
    # whether the archive predates the column saying who decided a row's
    # category: only then is an empty source derived as 098 derived it
    ctx.pre_098 = R._upgrades.older_than(R._upgrades.archive_schema(z),
                                         R._CATEGORY_SOURCE_COLUMN)


def _ledger_account_check(r, ctx):
    aid = R._account_id(r.get("account_id"))
    if aid and aid not in ctx.ledger_accounts:
        return Refuse("orphaned")
    return None


def _txn_raw(r, ctx, amount_missing=False):
    base = R._sync_base
    raw = R.scrub_transaction_raw(R._raw_dict(R._j(r.get("raw"))))
    if amount_missing:
        raw["_amount_missing"] = True
    # an archive written before file rows' raw was masked still carries
    # the full number in the cell the name was cut from
    if base.is_file_import_id(r.get("id")):
        raw = base.mask_file_raw(raw, r.get("name"), r.get("merchant_name"))
    # the row an earlier guard noted this one was carried from is named by
    # id with no key to follow: it follows that row to the id it lands
    # under here (see base._MENTIONS_SQL)
    was = raw.get("_carried_from")
    carried = ctx.carried
    if carried and isinstance(was, str) and was in carried:
        raw["_carried_from"] = carried[was]
    return base.stood_in_for_restore(raw, R._account_id, ctx.noted_accounts)


def _ledger_row(r, ctx, out):
    try:
        values = _txn_values(r, ctx)
    except R._AmountOutOfRange:
        return Refuse("out_of_range")
    except R._Unreadable:
        return Refuse("malformed")
    # beside the row: whether it lands at 0 for want of an amount, and
    # whether it carries an override the rules are derived over
    user = ((r["id"], R._descriptor(r.get("category_override")) or None)
            if r.get("override_source") == "user" else None)
    ctx.txn_pending.append((R._f(r.get("amount")) is None,
                            bool(values["category_override"]), user))
    return values


def _txn_values(r, ctx):
    d, f = R._descriptor, R._f
    # every live door writes a number here, and every ledger reader turns
    # it into cents — the duplicate matchers first, so a blank row would
    # fail every later import onto its account. Leaving the row out would
    # drop its receipt, note and split as orphans, so it lands at 0 and its
    # raw says the archive had no amount.
    amount = R._money(r.get("amount"))  # 0 is a real amount
    amount_missing = amount is None
    m_name, m_aside = R._merchant_name_cells(r)
    raw = _txn_raw(r, ctx, amount_missing)
    merchant_id = R._known_merchant(r.get("merchant_id"),
                                    ctx.known_merchants, ctx.merchant_map)
    # the migrations that rewrote stored rows, applied to this one
    # (upgrade_archived_txn): an archive written before any of them lands
    # in the shape they left the household's own rows in
    c = R.upgrade_archived_txn({
        "id": r["id"],
        "name": d(r.get("name")),
        "merchant_name": m_name,
        "merchant_name_set_aside": m_aside,
        "merchant_outlet": d(r.get("merchant_outlet")) or None,
        "amount": 0.0 if amount_missing else amount,
        "removed": R._i(r.get("removed"), 0),
        "category_primary": r.get("category_primary") or None,
        "category_detailed": r.get("category_detailed") or None,
        # The aggregator's OWN verdict and its confidence. These are what
        # llm_categorize's low-confidence gate reads, so dropping them
        # would silently change which rows the model revisits. The
        # confidence is a TEXT tier ('VERY_HIGH'/'HIGH'/'LOW'/…), never a
        # number — float-coercing it would raise on any Plaid row and, the
        # restore being one transaction, abort all of it
        "category_plaid": r.get("category_plaid") or None,
        "category_plaid_detailed": r.get("category_plaid_detailed") or None,
        "category_plaid_confidence":
            r.get("category_plaid_confidence") or None,
        "category_override": d(r.get("category_override")) or None,
        # migration 098: the aggregator's facts as columns
        "category_source": r.get("category_source") or None,
        "check_number": d(r.get("check_number")) or None,
        "payment_channel": r.get("payment_channel") or None,
        "payment_processor": r.get("payment_processor") or None,
        "location_city": d(r.get("location_city")) or None,
        "location_region": r.get("location_region") or None,
        "location_address": d(r.get("location_address")) or None,
        "location_postal": r.get("location_postal") or None,
        "location_lat": (f(r.get("location_lat"))
                         if r.get("location_lat") not in (None, "")
                         else None),
        "location_lon": (f(r.get("location_lon"))
                         if r.get("location_lon") not in (None, "")
                         else None),
        "location_store": r.get("location_store") or None,
        "mcc": r.get("mcc") or None,
        "authorized_date": R._opt_date(r.get("authorized_date")),
    }, raw, on_card=R._account_id(r.get("account_id")) in ctx.cards,
        has_merchant=merchant_id is not None,
        before_category_source=ctx.pre_098)
    t_name, m_name, m_aside, outlet = (
        c["name"], c["merchant_name"], c["merchant_name_set_aside"],
        c["merchant_outlet"])
    # a file import's names keep only the last four of any long digit run,
    # as the upsert writes them — an archive written before that rule would
    # otherwise bring a full account or card number back into the most
    # travelled field of the row
    if R._sync_base.is_file_import_id(r["id"]):
        t_name, m_name, m_aside, outlet = (
            R.card_number.cut(v) for v in (t_name, m_name, m_aside, outlet))
    return {"id": r["id"],
            "account_id": R._account_id(r.get("account_id")) or None,
            "date": R._req_date(r["date"]),
            "authorized_date": c["authorized_date"], "amount": c["amount"],
            "name": t_name, "merchant_name": m_name,
            "category_primary": c["category_primary"],
            "category_detailed": c["category_detailed"],
            "category_plaid": c["category_plaid"],
            "category_plaid_detailed": c["category_plaid_detailed"],
            "category_plaid_confidence": c["category_plaid_confidence"],
            "category_override": c["category_override"],
            "pending": R._i(r.get("pending"), 0),
            "pending_transaction_id": r.get("pending_transaction_id") or None,
            "payment_channel": c["payment_channel"], "removed": c["removed"],
            "entity_id": ctx.entity(r.get("entity_id")),  # per-txn override
            # per-transaction owner exception (effective owner is
            # COALESCE(owner_override, account.owner)) and the
            # statement-derived outlet brand (payee display/search, the
            # merchant rename screen)
            "owner_override": r.get("owner_override") or None,
            "merchant_outlet": outlet,
            # the aggregator's name for this one charge, which the resolver
            # set aside in favour of the merchant the bank line means. NULL
            # when the archive never had one — dropping it would re-key the
            # row on a name the ledger had already judged a misreading.
            "merchant_name_set_aside": m_aside,
            # the merchant ROW — but only when the archive actually carried
            # it. A pointer at a merchant that is not here is worse than no
            # pointer: identity reads crash on it and the ledger shows
            # nothing at all.
            "merchant_id": merchant_id,
            "raw": R.jsonb(raw),
            "category_source": c["category_source"],
            "check_number": c["check_number"],
            "payment_processor": c["payment_processor"],
            "location_city": c["location_city"],
            "location_region": c["location_region"],
            "location_address": c["location_address"],
            "location_postal": c["location_postal"],
            "location_lat": c["location_lat"],
            "location_lon": c["location_lon"],
            "location_store": c["location_store"], "mcc": c["mcc"],
            "override_source": r.get("override_source") or None,
            # a hold the person retired stays retired through the next sync
            "retired_at": R._ts(r.get("retired_at")),
            "removed_at": _archived_removed_at(r, raw, c["removed"])}


def _archived_removed_at(r, raw, removed):
    """When an archived retired row left the ledger, by migration 230's
    rule: the archive's own removed_at, else the feed's withdrawal stamp,
    else the moment someone decided it (retired_at), else the time of this
    restore — a row whose retirement time is unknown waits one fresh
    reconcile window, since its twin may arrive with the next sync. A live
    row has none (specs/receipts.md, "When a row left the ledger")."""
    if not removed:
        return None
    from ..engine.budget import FEED_REMOVED_KEY
    feed = raw.get(FEED_REMOVED_KEY) if isinstance(raw, dict) else None
    for v in (r.get("removed_at"), feed, r.get("retired_at")):
        if isinstance(v, str) or v is None:
            t = R._ts(v)
            if t is not None:
                return t
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _ledger_landed(ctx, got):
    pending, ctx.txn_pending = ctx.txn_pending, []
    # a person's category goes onto a row the destination already holds,
    # from a row the write let through: not one its columns refused
    for user, why in zip((u for *_r, u in pending), ctx.out.last_misfits):
        if user is not None and why is None:
            ctx.user_ids.append(user[0])
            ctx.user_cats.append(user[1])
    # a repaired row counts only when ITS insert wrote it: one the
    # destination already held keeps whatever amount it has there, and of
    # an id the archive carries twice only the copy that landed says
    # whether the amount was missing
    ctx.out.repair("transactions", "amount_repaired", sum(
        1 for g, (fixed, _o, _u) in zip(got, pending)
        if g is not None and fixed))
    # the rows this restore wrote carrying a category override, which the
    # rules derived from overrides are derived over
    ctx.overridden.extend(g for g, (_f, over, _u) in zip(got, pending)
                          if g is not None and over)
    ctx.landed_ofx.extend(g for g in got
                          if isinstance(g, str) and g.startswith("ofx:"))


def _settle_ledger(conn, ctx, out):
    from . import feed_dates
    if ctx.landed_ofx and R._dated_by_the_utc_day(
            R._upgrades.archive_schema(ctx.z)):
        # A build older than printed-day dating dated every OFX row by its
        # UTC day, and only the archive's age can say so: its batch times
        # are the source's, and this install's own dating change says
        # nothing about them. So the rows say it themselves — a later
        # import then never gives them the printed-day note and can still
        # move each onto the day its line printed. A row that already
        # carries a note keeps it.
        conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "%s::text, %s::text) WHERE id = ANY(%s::text[]) "
            "AND jsonb_typeof(raw) = 'object' AND (raw ->> %s) IS NULL",
            (feed_dates.PRINTED_DAY_KEY, feed_dates.UTC_DAY_VALUE,
             ctx.landed_ofx, feed_dates.PRINTED_DAY_KEY))
    # A PERSON's category on a row the destination already holds under the
    # same id (a statement imported on both sides hashes to the same ids)
    # replaces a bill's, a store's or no override there; the insert skips
    # the row whole, so these are applied after it. Only the id and the
    # category are held, a few strings per correction.
    if ctx.user_ids:
        conn.execute(
            """UPDATE transactions t
                  SET category_override = v.cat, override_source = 'user'
                 FROM unnest(%s::text[], %s::text[]) AS v(id, cat)
                WHERE t.id = v.id
                  AND t.override_source IS DISTINCT FROM 'user'""",
            (ctx.user_ids, ctx.user_cats))
    ctx.user_ids = ctx.user_cats = None


_LEDGER_COLUMNS = (
    "id", "account_id", "date", "authorized_date", "amount", "name",
    "merchant_name", "category_primary", "category_detailed",
    "category_plaid", "category_plaid_detailed", "category_plaid_confidence",
    "category_override", "pending", "pending_transaction_id",
    "payment_channel", "removed", "entity_id", "owner_override",
    "merchant_outlet", "merchant_name_set_aside", "merchant_id", "raw",
    "category_source", "check_number", "payment_processor", "location_city",
    "location_region", "location_address", "location_postal", "location_lat",
    "location_lon", "location_store", "mcc", "override_source", "retired_at",
    "removed_at")

LANDERS.update({L.table: L for L in (
    Lander("items", ("items.csv",),
           ("id", "aggregator", "institution_id", "institution_name",
            "status", "raw", "logo", "brand_color", "url", "archived_reason",
            "archived_at", "linked_at"), _item, columns("id"),
           required=("id",), defaults={"linked_at": "now()"},
           reads=("z",), writes=("push_fed",)),
    # The merchant ROWS come before the transactions and aliases that point
    # at them; an archive with no merchants file restores the rest, and the
    # nightly resolve rebuilds identity from the raw records. Any unique
    # key held counts: merchants also has a partial unique index on
    # plaid_entity_id.
    #
    # A restore MERGES into the household, and one that linked its banks
    # before restoring already holds a merchant for every entity the
    # aggregator named, under an id of its own. The archive's row for the
    # same entity is the same merchant: it is not inserted, its id is
    # mapped onto the live one, and everything later in the archive that
    # names it (transactions, aliases, 1099 marks, merge pointers, merge
    # decisions) follows the map. Written one at a time: a merchant that
    # lands becomes the live one for its entity, for the rows after it.
    Lander("merchants", ("merchants.csv",),
           ("id", "plaid_entity_id", "name", "name_source", "kind",
            "logo_url", "website", "phone", "mcc", "parent_id",
            "merged_into", "created_at"), _merchant, columns("id"),
           presence=Exact(any_key=True), required=("id", "name"),
           defaults={"created_at": "now()"}, batch=1, sequential=True,
           plan=_start_merchants, landed=_merchant_landed,
           after_land=(_settle_merchants,),
           writes=("live_entities", "merchant_map", "folded",
                   "persons_names", "machine_cut", "persons_live",
                   "last_merchant", "known_merchants",
                   "landed_merchants")),
    Lander("accounts", ("accounts.csv",),
           ("id", "item_id", "name", "display_name", "official_name",
            "type", "subtype", "mask", "balance_current",
            "balance_available", "currency", "entity_id", "owner", "raw",
            "user_removed_at", "type_user_set", "balance_limit"), _account,
           columns("id"), required=("id",), plan=_start_accounts,
           landed=_accounts_landed, after_land=(_settle_accounts,),
           reads=("entities", "renamed", "notes", "unread_entities"),
           writes=("known_items", "landed_accounts", "numbered_ids",
                   "personal_ids")),
    # Transactions are BATCHED. Every other member is bounded by what
    # people do — accounts, bills, notes — but this one is the ledger: a
    # restore can be tens of thousands of rows, and a per-row execute()
    # pays a full client/server round trip for each. psycopg pipelines
    # executemany, so the same INSERT travels in chunks instead. Memory is
    # unchanged: the member still streams, and only one chunk is held. Every
    # column the export writes is written: the export is SELECT *, so a
    # column left out is silently lost on a round trip.
    Lander("transactions", ("transactions.csv",), _LEDGER_COLUMNS,
           _ledger_row, columns("id"), required=("id", "date"),
           pre=_ledger_account_check, batch=1000, plan=_start_ledger,
           landed=_ledger_landed, after_land=(_settle_ledger,),
           reads=("carried", "known_merchants", "merchant_map", "entities",
                  "z"),
           writes=("ledger_accounts", "noted_accounts", "cards",
                   "overridden", "pre_098",
                   "landed_ofx", "user_ids", "user_cats", "txn_pending")),
)})


# ---- the alert log ------------------------------------------------------

# Every held row of an alert takes the mute in one statement: one result
# row naming every row muted, or none when each was muted already.
_MUTE_HELD = """WITH muted AS (
                    UPDATE alerts_log SET dismissed = 1
                     WHERE id = ANY(%s) AND dismissed = 0
                       AND mute_changed_at IS NULL
                    RETURNING id)
                SELECT array_agg(id) AS ids FROM muted
                HAVING count(*) > 0"""


def _start_alerts(conn, z, ctx):
    import datetime
    from collections import defaultdict
    ctx.today = datetime.date.today()
    # a row an older build stored with a card number still whole is the
    # same alert as its cut form, so one alert can be held under both
    # spellings, and the dismissal mutes each of them
    ctx.held_alerts = defaultdict(list)
    for a in conn.execute(
            "SELECT id, kind, message FROM alerts_log").fetchall():
        ctx.held_alerts[R._alert_identity(a["kind"], a["message"])].append(
            a["id"])


def _alert(r, ctx, out):
    if not R._b(r.get("dismissed")):
        # the next Today build raises it again if it still holds
        return Refuse("rebuilt")
    kind, sev, msg = (str(r.get(k) or "").strip()
                      for k in ("kind", "severity", "message"))
    # Its dates are held to today, or a crafted future `last_seen` would
    # make every snapshot read as stale and freeze the row.
    try:
        first = min(R.as_date(r.get("first_seen")), ctx.today)
        last = min(R.as_date(r.get("last_seen")), ctx.today)
    except (TypeError, ValueError):
        first = last = None
    if (not kind or not sev or not msg or first is None
            or len(kind) > 60 or len(sev) > 20
            or len(msg) > R.ALERT_MESSAGE_MAX):
        return Refuse("malformed")
    ident = R._alert_identity(kind, msg)
    return {"_ident": ident, "kind": kind, "severity": sev,
            "message": ident[1], "first_seen": min(first, last),
            "last_seen": last, "active": 1 if R._b(r.get("active")) else 0}


def _write_alert(conn, ctx, out, L, alerts):
    for a in alerts:
        held = ctx.held_alerts.get(a["_ident"])
        if held:
            # held here, under either spelling: every such row takes the
            # mute, and the alert counts once
            out.land(conn, "alerts_log", _MUTE_HELD, [(list(held),)])
            continue
        # a restore is a merge: the same alert raised here since keeps its
        # own dates and state, and only takes the person's mute
        landed = out.land(conn, "alerts_log", L.sql, [L.params(a)])
        if landed and landed[0] is not None:
            ctx.held_alerts[a["_ident"]].append(landed[0])


LANDERS["alerts_log"] = Lander(
    # The alert log is this instance's operating state and the next Today
    # build rebuilds it, so it stays behind, except for what in it is the
    # household's own: the alerts the person dismissed. Dropped, every
    # muted alert lights up again here. A dismissed row lands as it was
    # (still active or cleared); over a row this household holds it only
    # sets the mute, and never one the person un-muted here
    # (mute_changed_at set), and with no `seen_at`: it is no view of the
    # world, so the next snapshot decides it as usual (a still-active alert
    # keeps the mute, a recurrence after a clearance is news). Written one
    # at a time: an alert that lands is held, for the rows after it.
    "alerts_log", ("alerts_log.csv",),
    ("kind", "severity", "message", "first_seen", "last_seen", "active",
     "dismissed"), _alert, columns("kind", "message"),
    presence=Exact(replace=Replace(
        set=(("dismissed", "1"),),
        when="alerts_log.dismissed = 0"
             " AND alerts_log.mute_changed_at IS NULL")),
    fixed={"dismissed": "1"}, returning="id", plan=_start_alerts,
    write=_write_alert, batch=1, sequential=True,
    writes=("today", "held_alerts"))


# ---- the order the restore lands in -------------------------------------

@dataclass(frozen=True)
class Step:
    """Restore state set between landers, where it becomes true."""
    name: str
    run: Callable                   # (conn, ctx, out)
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()


def _known_entities(conn, ctx, out):
    # An archive written table by table, each at its own moment, can hold
    # a row whose parent it missed: an account opened after the
    # connections were read, a business deleted after the ledger was. The
    # export now reads one snapshot, but archives from before that exist,
    # and one dangling pointer would roll back the whole restore. So
    # parents are checked against what is here: a pointer to a missing
    # business or connection is dropped (what deleting that parent does to
    # it anyway), and a row that cannot exist without its missing parent
    # is skipped.
    ctx.entities = {str(r["id"]) for r in conn.execute(
        "SELECT id FROM business_entity").fetchall()}


def _start_renames(conn, ctx, out):
    # ids the archive spells with a comma, and the name each restores
    # under — the settings keys naming them are rewritten to match
    ctx.renamed = {}


def _start_bill_names(conn, ctx, out):
    # what names a bill by its archived id or payee (pins, offers, two
    # settings) follows these onto the one it landed as
    ctx.bill_ids, ctx.bill_payees = {}, {}


def _judge_tax_reserves(conn, ctx, out):
    # A business's tax reserve is one of ITS accounts, as the page
    # requires; judged once the accounts have landed. One that names any
    # other account (a personal one, one the archive did not carry) is
    # left out, and the set-aside card measures all of the business's cash
    # and says so.
    if not ctx.landed_entities:
        return
    for _ in conn.execute(
            """UPDATE business_entity e
                  SET tax_reserve_account_id = NULL
                WHERE e.id = ANY(%s::uuid[])
                  AND e.tax_reserve_account_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM accounts a
                       WHERE a.id = e.tax_reserve_account_id
                         AND a.entity_id = e.id)
            RETURNING e.id""", (ctx.landed_entities,)).fetchall():
        out.emptied("business_entity", "a tax reserve that is not one of "
                    "the business's own accounts")


def _balance_accounts(conn, ctx, out):
    # a balance row is its account's: the accounts here once the ledger
    # and its links have landed
    ctx.accounts = _accounts_now(conn)


def _alias_lookup(conn, ctx, out):
    """`ctx.alias_where(raw)`: (a file-import row keys on the cut `raw`,
    some row still keys on `raw` itself), both False-True for a short raw.

    An alias is keyed on the row's identity string, which for a file import
    is masked (here and by the live upsert). Its key is cut the same way
    whenever a restored file-import row now carries the masked string, so
    the alias goes on joining its rows and the full number does not survive
    in it; the full spelling is kept only while some row still keys on it
    (an aggregator row keeps its name). Migration 153 applies the same rule
    to stored aliases."""
    from ..engine.merchant_sql import RAW_KEY
    seen: dict[str, tuple[bool, bool]] = {}

    def where(raw):
        masked = R.card_number.cut(raw)
        if masked == raw:
            return False, True
        if raw not in seen:
            hit = conn.execute(
                f"""SELECT
                      EXISTS (SELECT 1 FROM transactions t
                               WHERE split_part(t.id, ':', 1) = ANY(%s)
                                 AND {RAW_KEY} = %s) AS filed,
                      EXISTS (SELECT 1 FROM transactions t
                               WHERE {RAW_KEY} = %s) AS kept""",
                ([p.rstrip(":") for p in
                  R._sync_base.FILE_IMPORT_ID_PREFIXES], masked, raw)
            ).fetchone()
            seen[raw] = (bool(hit["filed"]), bool(hit["kept"]))
        return seen[raw]
    ctx.alias_where = where


ORDER: tuple = (
    "items", "merchants", "business_entity",
    Step("entities", _known_entities, writes=("entities",)),
    Step("renames", _start_renames, writes=("renamed",)),
    "accounts", "transactions",
    Step("bill_names", _start_bill_names,
         writes=("bill_ids", "bill_payees")),
    "bills", "bill_proposals", "import_batches", "manual_categories",
    "reimbursements", "reimburse_flags", "account_links",
    "account_adoptions", "account_sources", "receipts", "receipt_items",
    "business_flags",
    # the business tables: entities and transactions are both here now
    Step("tax_reserves", _judge_tax_reserves, reads=("landed_entities",)),
    "entity_membership", "equity_movement", "business_txn_class",
    "compliance_obligation", "mileage_log", "vendor_1099",
    "transaction_notes", "transaction_splits", "activity_log",
    "alerts_log",
    # history and reference tables
    Step("balance_accounts", _balance_accounts, writes=("accounts",)),
    "liabilities", "holdings", "crypto_holdings", "networth_recorded",
    "networth_snapshot", "forecast_nights", "budget_snapshots",
    "income_annual",
    "income_uploads", "income_upload_values", "income_documents",
    Step("income_resolve", _resolve_income,
         reads=("archive_has_uploads", "upload_years", "income_years",
                "landed_income_years")),
    Step("alias_keys", _alias_lookup, writes=("alias_where",)),
    "merchant_canonical", "merchant_renames", "merchant_categories",
    "merchant_merge_proposals", "amazon_orders", "amazon_matches",
    "costco_receipts", "costco_matches", "amazon_summaries",
)


def run_step(step: Step, conn, ctx: Ctx, out):
    return step.run(conn, ctx, out)


def land_all(conn, z, ctx: Ctx, out) -> None:
    """Every lander, and the steps between them, in ORDER."""
    for entry in ORDER:
        if isinstance(entry, Step):
            run_step(entry, conn, ctx, out)
        else:
            land_one(conn, z, ctx, out, LANDERS[entry])


# The restore's last steps in its transaction, and its steps once it has
# committed (sync/restore.py), declared as the ORDER's steps are so the
# order test covers what they read and write. `cfg_in` is read as it is:
# an archive with no settings document has none.
FINALIZE = Step(
    "finalize", lambda conn, ctx, out: R.finalize(conn, ctx, out),
    reads=("z", "cfg_in", "notes", "renamed", "bill_payees", "overridden",
           "landed_ofx", "landed_accounts"),
    writes=("route_moved", "left_behind", "archived_recipients",
            "settled_accounts"))
AFTER_COMMIT = Step(
    "after_commit", lambda conn, ctx, out: R.after_commit(conn, ctx, out),
    reads=("z", "notes", "route_moved", "settled_accounts",
           "archived_recipients", "left_behind", "landed_merchants"))
