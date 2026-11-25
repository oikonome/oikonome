# Restore by per-table landers (spec)

`sync/restore.py` lands every archive member through one declared
**lander** per table (`sync/landers.py`), run in one explicit order, so
each table's idea of "the same row" and "already held" is declared in one
place rather than spelled inline. `_restore_zip` is the door checks, the
settings check, the locks, `ctx.prepare`, `land_all`, `finalize` and
`after_commit`. The round-trip test below holds the landers to the
restore's behaviour.

## The restore, in order

1. **Door** (outside the transaction): zip sizes, archive kind, the cell
   guard, the settings document read and checked (`_check_config`).
2. **Transaction**: the erasure check, `hold_restore_write_locks`, then
   `Ctx.prepare(conn, z, notes, cfg_in)` — the comma renames, the cut
   lineage and the carried-transaction planners, read under the locks and
   before any row lands. The planners only read.
3. **`land_all`**: `ORDER`, a tuple of lander table names and `Step`s (the
   restore state set between landers: the businesses here, the rename and
   bill-name maps, the tax-reserve check, the balance accounts, the alias
   lookup).
4. **`finalize(conn, ctx, out)`**, still in the transaction: the rules an
   older archive needs (035/044), the printed-day note, the settings merge
   (`_settle_settings`: key-wise over the destination's own under the row
   lock, nested secrets carried forward, the cleaners), then
   `rekey_numbered_accounts`, `key_file_import_ids`,
   `account_sources.backfill`, `cut_detected_keys`, `cut_stored_names`.
5. **`after_commit(conn, ctx, out)`**: the AI-route notice, the merge onto
   live accounts, the recipient invites (admin connection), the merchant
   resolve — each its own transaction, a failure costing only itself.

## The lander

```python
@dataclass(frozen=True)
class Lander:
    table: str
    members: tuple[str, ...]          # archive members (bills also reads recurring.csv)
    columns: tuple[str, ...]          # the INSERT list; the SQL is generated from it
    parse: Callable                   # (row, ctx, out) -> values, or Refuse(reason, over, n)
    identity: Columns                 # THE one definition of "the same row"
    presence: Exact | ByKey | Counted | Group
    required, defaults, fixed, casts, computed, returning
    remap, pre, parents, parents_last, queried
    spellings, batch, sequential, allocates_ids
    source, plan, locks, landed, write, after_land
    reads, writes                     # Ctx fields, checked against ORDER
```

**Presence** — how a lander knows a row is already held:

- `Exact()` — `ON CONFLICT (tenant_id, *identity) DO NOTHING`; a catalogue
  test checks the key is a real unique index. `any_key=True`: a clash on
  any unique key is held (a table with several). `replace=Replace(set,
  when, when_params)`: the held row takes the archived one only when the
  condition holds (a person's pin over a bill's, an answer over a pending
  offer, a person's alias or rule over a machine's, a dismissal's mute).
  `Replace` needs a conflict key, so it never combines with `any_key`.
- `ByKey(why, any_key, blank_is_any)` — a tenant-scoped `NOT EXISTS` over
  the identity, NULL-safe, looked for with the values the INSERT writes
  (after `_fit_row`); `blank_is_any` makes a blank cell match any held value
  (a rename without its time). `why` says why no unique index states it.
- `Counted(conflict, why)` — the W-2 rule (`specs/w2-wages.md`): the
  source decides, over all copies, which archived copies are held.
- `Group(why)` — rows travel as one group per identity (a charge's split),
  written whole by the lander's `write`.

**Per row**, `_prepare` asks in this order: required
cells (blank → malformed), maps (`remap`), the **pre-query cell check**
(`_fit_cells`: parent and `queried` cells judged against their columns
before any lander query reads them — a required misfit refuses the row,
an optional one is emptied), the row's own check (`pre`), parents (or
after the parse, `parents_last`), then `parse`. The write judges every
column again (`_fit_row`) as a backstop.

**Hooks**: `source` reads a member whole when a row's fate is decided over
all of them (bill moves, the equity keep-first order, the rename journal,
split groups, counted documents) and counts its own refusals; `plan` sets
up a lander's state where it starts; `locks` are taken first; `landed`
sees each write's per-row results (one per input row, `_fit_row` refusals
included; `out.last_misfits` says which were refused); `write` replaces
the INSERT (split groups, alert mutes) and so takes no `landed` or
`spellings`; `after_land` runs once the member has landed (the wage
re-sum, renumbered ranks, the summary notes).

**`sequential=True`** (with `batch=1`): a row's parse or hook reads what
the row before it wrote — the live merchant of an entity, a storage
reserve, a cap counted in rows landed, a MAX+1 id (`allocates_ids`).
Construction refuses a larger batch. Transactions keep their batch of
1000 through pipelined `executemany`.

## Payback past a charge

A partial reimbursement link nets the money it records off its charge
(`budget.NET_AMOUNT`) and off its deposit's income (`reporting.INCOME_NET`).
The link door never lets the links on a charge repay more than it cost
(`web/data.py` `_charge_room`), nor the links on a deposit claim more than
it brought in (`_deposit_left`: its partial links AND the charges fully
paired to it), but the reimbursements lander lands archived rows as they
are, so an archive (an older build's, or a hand-edited one) could bring
back a $60 charge carrying $110 of links (its spend floored at $0, the
deposit's $50 that was never repaid gone from income), or a $100 deposit
partial-linked for $100 to each of two charges (both charges' spend at $0,
$100 of payback that never came in).

**The one function that decides:** `web.data.trim_partial_links`, run by
the reimbursements lander's last `after_land` step (`_trim_links`), inside
the restore's transaction and under its pairing lock, so it sees every link
and is all or nothing with the rest. It holds both caps at once:

- the charges looked at are those a link this restore wrote names, and the
  deposits likewise (the links of a write that actually landed: a link is
  noted only once all its cells have parsed);
- on a charge, what came back is `budget.REIMB_RECEIVED` (partial links
  plus full pairs, a link on a deposit no longer on the ledger counting
  nothing); on a deposit, what it claims is `_DEPOSIT_LEFT`'s (partial
  links plus the charges fully paired to it, a link to a charge no longer
  on the ledger counting nothing);
- every partial link counted on such a charge or deposit is weighed in one
  pass: the links the household already held first, then the ones this
  restore wrote, each set oldest first (`created_at`, then the ids). An
  archive's links are always older than any the person made since the
  export, so oldest first across both would cut a link the person moved
  to another charge or deposit to reinstate the stale one; a restored link
  takes only the room the household's own links leave
  (`web.data.trim_restored_links`). Each keeps what it
  carried, or less where its charge's or its deposit's room has run out,
  and a link left with nothing is removed. On a charge or a deposit alone
  that is cutting the excess newest first, the order the live re-check
  (`_shrink_partials`) cuts in. Charge-side then deposit-side, one after
  the other, would also leave both caps held, but a link the charge side
  removed for room an older link's deposit then took back away would come
  back on the next restore: one pass oldest first is the only order a
  second restore leaves as it is.
- A full pair is never touched, and a link to a row the feed withdrew
  neither counts nor is cut. A charge or deposit restored at 0 because the
  archive had no amount (`amount_repaired`) is left alone: its links wait
  for the amount the person puts back, as `repair_pairs` leaves them.
  `trim_partial_links_to_charges` is the same pass with no deposit named.

**What readers see:** the charge nets to what it cost less what came back,
never below it; the deposit's income is what it brought in less what its
links claim, never below 0; the cut part of a link is spend again on its
charge and income again on its deposit, through `NET_AMOUNT` and
`INCOME_NET` (a TRANSFER_IN stamp on a deposit its partial links no longer
use up already reads as income net of its links). In the $60 / $110 case:
$60 linked, the charge's spend $0, the deposit's $50 income. In the $100
deposit / two $100 charges case: the older link keeps $100, the newer is
removed, so the second charge counts $100 of spend again.

**What the restore reports:** an archived link cut is a repair of its
written row (`payback_trimmed`), told in the summary; one cut to nothing
was not written after all and is counted `over_cap`. A link the household
already held that a restored one pushed past its charge or its deposit is
cut too (by age, like the rest), and told in a note (it is no archived
row, so no counter of the archive's).

**An older archive, a repeated restore:** the rule reads only
`reimbursements` and `transactions`, which every supported archive carries
in this shape, so every era is held to it the same way. A second restore
of the same archive writes nothing and cuts nothing: every link it carries
is already present, so nothing is named. The one exception is a link the
first restore removed: it lands again, and the pass gives it again exactly
the nothing it had (the links older than it keep what they kept, so its
room is what it was), so it is removed again, the household unchanged and
the link counted `over_cap` again.

## Ctx

`Ctx` replaces the threaded locals. Each field has **one writer** —
`Ctx.prepare` (`PREPARED`), a `Step`, or a lander's `writes` — and is set
where it becomes true; a test walks `ORDER` and checks every field a
lander or step reads is written by an earlier one, and that no field has
two writers (each accounts snapshot has its own name). Reading a field
still unset raises `ContextNotReady`, naming it.

## The round-trip property test

`tests/test_every_table_round_trips_through_its_lander.py`. One synthetic
builder per lander, seeded `random` (8 fixed seeds; more by env var; a
failure prints its seed): twins, NULL in every nullable column (read from
`information_schema`), unicode and formula-leading text, colliding ids,
money edges. Scenarios:

- A: export → empty household: identity multisets equal, every archived
  row has exactly one outcome.
- B/C: the same archive again, or into its own household: nothing written.
- D: into a different populated household: nothing lost, multiplicity
  max(source, destination) per identity (per owner for documents), no
  duplicate under any unique key, W-2 sums equal the stored companions.
- E: export of A restores to A (fixed point).
- F: a manifest-less / oldest-supported archive still satisfies A and B.

**Coverage guard**: every tenant RLS table (minus the export's skip list)
has a lander and a builder, or a stated reason in `RECORDS_ONLY`. A
migration that adds a table fails here until it does.

## Rules every lander holds

- compliance_obligation lands with the door's checks (URL scheme,
  recurrence, fee bounds).
- Bounded NUMERIC columns are parsed against their bounds: a value that
  would overflow refuses its row, never the whole restore.
- liabilities, holdings, crypto_holdings land only for accounts the
  archive carries (a parent check; no FK states it).
- The no-JS restore door runs in its own transaction, so its "after
  commit" steps run after the commit.
- merchant_renames presence lets a blank `at` match any held value
  (`blank_is_any`), so a re-restore appends no duplicate.
- income_documents builds its identity one way (jsonb and json.dumps
  disagree on NaN), so neither document of a non-finite pair is lost.
- An unreadable REQUIRED date refuses that row (`malformed`, listed in
  the summary) and the rest lands; an unreadable OPTIONAL date keeps the
  row without it. No table refuses the whole archive over a date.
- Text where a number belongs follows the same rule: `_f` raises
  `_NotANumber`, and `_prepare` (and the `source` hooks that read
  numbers) refuse that row as `malformed`. A non-finite number still
  refuses the archive (`_f`).
- A NUL in a key of a JSON cell (`{"a\u0000": 1}`) makes the value
  malformed, since the key is never rewritten: `_j` reads it as an empty
  cell, so it costs only that value, and the summary names the file and
  column.
- A row with a category and no `category_source` is given one (098's
  derivation) only for an archive older than 098 (`ctx.pre_098`, from
  `archive_schema`); a newer one keeps the NULL it carried.
