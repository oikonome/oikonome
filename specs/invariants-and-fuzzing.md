# Invariants, property tests, body fuzzing and a concurrency harness (spec)

The suite proves what its authors thought to check. The tests this spec
describes state a rule once and let a generator look for the case that
breaks it: money invariants over generated households, restore
invariants beyond the round trip, lock and concurrency invariants on
real connections, and hostile request bodies at every door.

## 0. The building blocks

- `tests/test_every_table_round_trips_through_its_lander.py` and
  `tests/roundtrip_builders.py`: a seeded, synthetic household per seed
  (`random.Random`, `OIKONOME_RT_SEEDS` runs more seeds), with scenarios A to F
  (empty, again, onto itself, onto another populated household, the fixed
  point, a missing or oldest manifest) and golden digests.
- `tests/api_conformance.py`: every 2xx JSON reply the suite gets from a
  documented door is checked strictly against its response model.
- `web/schemas/*` `@documented(body=...)`: a body model for each door that
  reads JSON (117 doors). The models describe the doors and are never
  run against live requests. Handlers read bodies through `web.fields.Fields`.
- `tests/lock_order_util.py`: `Side` (one writer on its own connection),
  `Gate` (a pause point patched into a writer), `waits_on_a_lock`,
  `deadlocks_since`. Twelve lock-order tests use it.
- Hand-written invariant tests: `test_money_accuracy_invariants.py`,
  `test_money_engine_invariants.py`, `test_scope_invariants.py`,
  `test_settings_lost_update.py`, `test_partial_reimbursement_cap_race.py`,
  and the settings door matrix in `test_settings_door_reads_through_typed_fields.py`
  (every key against five bad values, by owner and member).
- The documented lock order, beside `erasure.WRITER_LOCKS`: bill claims,
  then storage, pairing, identity, the bill ceiling, the settings row (then
  the member lock),
  then offers, bills and transaction rows. The helpers are
  `bills.hold_bill_claims`, `storage_quota.hold_lock`,
  `reanchor.hold_pairing_lock`, `merchant_identity._lock_identity` and
  `budget.config_txn`. Single-flight doors use session try-locks through
  `tenancy.take_lock` / `tenancy.release_lock`. Request doors wait at most
  `reanchor.REQUEST_LOCK_WAIT_MS` and then answer 409.

Hypothesis (§5) drives the property tests; the seeded round trip is the
other generator they build on.

## 1. Money invariants

Each invariant names the function that decides it, the property, and the
generator. "Cents" means an integer number of cents. A generated amount is
drawn in cents and divided by 100, so the inputs are always amounts a bank
could report. Some invariants also draw sub-cent balances on purpose
(marked), because investment and crypto feeds can carry them.

| # | Deciding function | Property | Generator |
|---|---|---|---|
| M1 | `engine.splits.validate` | Accepts exactly when there are 2 to `MAX_PARTS` parts, every category is distinct and not a flow category, every part is nonzero and has the same sign as the row, and the parts' cents add up to the row's cents. An accepted result sums to the row in cents exactly. It never raises anything but `SplitError`, whatever JSON value it gets. | Row cents in ±[1, `MAX_AMOUNT`·100]. Parts are a random composition of that amount into n parts, then one perturbation: ±1 cent, a sign flip, a duplicate or flow category, float noise (`0.1+0.2`), NaN or ±inf, a 400-digit int, a bool, a string. Pure function. |
| M2 | `splits.set_split` → the ledger, `reporting.compute_cashflow` | Splitting a row into spending categories moves money between categories and changes nothing else. The month's spend, income and saved stay the same, and per-category totals still add up to the month's spend. Clearing the split brings back the earlier figures. | DB: a household from `roundtrip_builders`, a random row, a random valid split from M1's generator. |
| M3 | `compute_cashflow`, `budget.month_status`, `reporting.compute_networth` | An own-transfer pair (TRANSFER_OUT on A, TRANSFER_IN on B, same size, same window) leaves spending, income, saved and the plan's excess unchanged, and moves Net Worth's `current_total` by exactly the two balance changes, which net to 0. A card payment paired with its credit leg counts once, as the card settlement, and never as spend. | DB: a random household, plus a pair of random size on random accounts and dates inside a month. |
| M4 | `compute_cashflow` | For each year: `savings_by_year[y].net == Σ_{known months} (income_m − spend_m)`. The `monthly_by_year[y]` bars sum to that net, within half a cent per bar. A month the feed misses is counted in In and Out but never in Saved. `saved_months_known ≤ saved_months_total`. | DB: 1 to 30 months of synthetic ledger, random gaps in feed coverage, months with no income and real spend. |
| M5 | `web.lenses.cell_totals` / `year_summary` | Year spend is Σ elapsed cells. Year saved is Σ known cells (income − spend). The rate is computed from those two numbers. The twelve cells add up to the figure above them. | Pure over generated cells (status, income or None, spend). DB variant through `year_summary`. |
| M6 | `budget.match_envelopes` | For positive amounts and each envelope: `0 ≤ used ≤ max(0, pool − prior_used)`. `used + overflow == Σ` of the amounts it claimed. Σ `rows` == `used` and Σ `overflow_rows` == `overflow` (within half a cent per row). Every claimed row appears once in full, or as two slices that add up to it. Overflow starts only after the pool is used up, in date order. With `prior_used` (annual), the cap is the year's pool. | Pure: envelopes with random pools, rows with random dates and amounts, a random `prior_used`. |
| M7 | `reporting.account_totals` | `financial_total == compute_networth()["current_total"]`. Σ of every section's `counted_total` == `financial_total` **to the cent**. No loan is in it. A combined household's business accounts are in it exactly when it combines them. | DB: random accounts of every type, including credit, loan, business and shadow twins. **Sub-cent balances** (3 to 4 dp). Parts are shown rounded by largest remainder (§8, D1). |
| M8 | `compute_networth` | `by_institution` and `by_asset_class` each sum to `current_total`, and `full_total == current_total + property_net`. With `owner=`, the owner views together never exceed the whole for disjoint owners. | Same households as M7. |
| M9 | `budget.month_status` → `todayview._plan_rows` | `plan_surplus == income − avg_bills − plan_variable − Σ every savings goal's monthly plan`, and the plan rows add up (income → bills → to spend → saved → excess) on the payload both clients draw. | DB: random bills, envelopes, goals and an income series. |
| M10 | the one deciding function per figure (`specs/counted-figures.md`) | Cross-surface equality: each figure has the same value at every payload path that carries it (Today, Budget, the email context, the lenses). Examples: `income_expected` (paychecks still due), `plan_variable`, `after_cards_bills`, Accounts' `financial_total` and Net Worth's "Financial accounts". | DB: a table of (figure, list of payload paths) and one generated household per seed. |
| M11 | `links.shadow_ids` + every aggregate | Adding a linked shadow twin of an account (a second source of the same real account) changes **no** figure: Net Worth, Accounts, Cash Flow, `month_status`, retirement buckets. | DB: a random household, then a twin of a random account, with rows mirrored. |
| M12 | account exclusion (`excluded_accounts_sql`) | Excluding account X lowers Cash Flow's spend and income by exactly X's own contribution and changes nothing else. Including it again restores the figures. | DB. |
| M13 | partial reimbursements (`NET_AMOUNT_JOINED`, `data.flag_reimbursement`) | A row's net amount is between 0 and its amount. Σ of the matched reimbursements never exceeds the expected cap. | DB: a random flag and random payments in. |
| M14 | `web.fields.money_float`, `typed_money`, `Fields.money/number/int_in` | For every JSON value: either a 400, or a finite number with `|v| ≤ MONEY_MAX`. `format(cents) → parse` round-trips, for both separator conventions. Never any other exception. | Pure: `st.from_type(json)`-style values, plus amounts printed with either separator. |
| M15 | `sync.restore.ret_wizard_answer` (shared by the settings door and the restore) | For every JSON value, returns a value inside `RET_WIZARD_RANGES[key]` or None. **Never raises.** | Pure: all JSON scalars, including ints past float range. |
| M16 | `engine.debt.build` | Under a plan whose payment covers the interest, each debt's balance never rises. Total paid == starting + interest. The month count is finite. No balance is NaN. | Pure or DB: random debts, APRs (including missing ones, per `honest-figures.md`) and payments. |

Two more rules apply to every M row. The property is checked on the value
the payload carries, not on a re-derivation in the test. A failure prints
the seed, or the Hypothesis reproduce blob, and the minimal household.

## 2. Restore invariants beyond the round trip

The round trip already proves landing, idempotence, merging into a
populated household, and the fixed point, per table. Added:

- **R1 Figures survive.** Restore carries inputs, never figures. So for a
  generated household H, every M-figure (at a fixed `today`) on a fresh
  household restored from `export(H)` equals H's. The only figures left out
  are ones that read live connection state (an Item's status).
- **R2 Isolation.** A restore into household A leaves household B
  byte-identical (a per-tenant digest over every RLS table), including when
  B drew ids from the same pools, so the archive's ids collide with B's.
- **R3 Hostile archive cells.** Take each member of a real export, then a
  random row and cell. Mutate the cell with the fuzz vocabulary (§4): wrong
  type, NaN, a 400-digit int, a huge string, deep JSON in a JSON cell, NUL,
  a lone surrogate, a formula prefix. The restore job then finishes (done,
  or a stated refusal) and never stops on an unhandled exception. Every
  archived row is counted exactly once (landed + held + refused == rows),
  and a refused row writes nothing.
- **R4 All or nothing at every step.** Make the k-th lander of `ORDER`
  raise, for every k. The household digest is then unchanged and the job
  reports a failure. `test_a_restore_merge_is_all_or_nothing` holds the
  merge case, and `test_a_restore_failing_at_any_step_changes_nothing`
  every step (§7).
- **R5 Restored settings pass their own door.** Post every key of the
  restored settings document back to `POST /api/settings` as it is. The
  door answers 2xx and the stored document does not change. The door and
  the restore share cleaners (`ret_wizard_answer`, `_CONFIG_SHAPES`). This
  property catches a cleaner the two have drifted apart on. Every settings
  key the app reads or writes is held to a shape on the way in: by
  `_CONFIG_SHAPES`, by one of the cleaners named in
  `restore.HELD_BY_OTHER_CLEANERS`, or by being left behind (policy keys,
  credentials and their companions). A restored value that is not that
  shape is dropped, so the household's own value (or the default) stands,
  with a note; it never lands. A key the app does not know passes as the
  archive carried it, with non-finite numbers read as null.
  `test_a_restored_config_holds_every_known_key_to_its_shape` walks the
  code for the keys, fails on one nothing holds, and restores a hostile
  value under every key before loading Retirement, Net worth and Today.
- **R6 A person's decision is never lowered.** For every lander whose
  presence is `Replace`, a held row that a person decided keeps its
  person-decided columns when the archived row is a machine's.
- **R7 Older archives (long mode).** For a sample of migration numbers,
  an archive whose manifest names that migration satisfies round-trip
  scenarios A and B (F covers only the oldest).
  `tests/archive_eras.py` shapes a generated
  household's export as an install at migration E wrote it, read off the
  migration files: a table a later file creates is left out, a column a
  later file adds is stripped, the bill tables carry their names from
  before 040, a set-aside merchant name is spelled as 136 spelled it (or
  left in `merchant_name` before that), a store's carved split parts are
  dropped before 184, and the manifest names E. Tables schema.sql creates
  with no migration of their own are kept in every era unless their rows
  point into a later table (`BORN_WITH`). A test proves the parser sees
  every column `FLOOR_COLUMNS` dates an archive by.
  `test_an_archive_from_an_older_schema_restores_whole` restores each era's
  archive into an empty household: every row one outcome, nothing refused
  but what the destination rebuilds, every identity the era's columns can
  name present (rows the restore derives on purpose for an archive that
  old, the 035 rules and the pre-097 merchant resolutions, may be added),
  the money invariants, and a second restore that writes nothing and
  changes nothing. Default: one household, two eras (before 040, and 136);
  long mode: every era the archive's shape changed in (61) for every
  seeded household.
- **R8 Restore beside writers.** Handled by the harness (§3): an edit
  behind a restore answers 409 within the bounded wait, and after both
  finish the M-invariants hold.

R1, R2 and R5 reuse `roundtrip_builders` households and its seeds. R3 and
R4 run in the default mode over one seed and a deterministic sample of
cells. The long mode runs every member and cell class.

## 3. Lock and concurrency invariants, and the harness

### Invariants

- **L1 Documented order.** Within one transaction, the household locks
  are requested in non-decreasing rank: bill claims < storage < pairing
  < identity < bill ceiling < settings row < member lock < offers <
  bills < transaction rows. A lock already held may be taken again. The
  bill ceiling ranks after identity because the restore takes it there.
  A shared take of the pairing lock (`pg_advisory_xact_lock_shared`, as
  `data.live_edit_ids` takes it under every edit door) is ranked with the
  pairing lock: it waits on an exclusive holder like any other. The bill
  editor's rename takes the bill ceiling before it moves the settings
  keyed by the bill's name under the settings row, and the demo rebuild
  takes the pairing lock and the bill ceiling before the settings row.
- **L2 No deadlock.** No scenario raises the database's deadlock count
  (`lock_order_util.deadlocks_since`).
- **L3 Serial equivalence.** For a small set of operations run at once on
  one household, the final state (the digest of the rows they touch)
  equals the result of **some** serial order. For n ≤ 4 the harness
  computes every serial order's outcome on clones. No update is lost.
  Two writes to the same field may end in either order. Writes to
  different fields must both land.
- **L4 Busy, never broken.** A request door that loses a race answers 409
  "busy" within `REQUEST_LOCK_WAIT_MS` plus a small margin, never a 5xx.
  A worker or restore that loses waits and then succeeds.
- **L5 Single flight.** Exactly one of N callers runs a single-flight door
  (sync, snapshot, nightly, email, restore) at once. The others report
  that they skipped.
- **L6 Pool hygiene.** After every scenario, and after every fuzzed
  request, no idle pooled backend holds an advisory lock or sits
  `idle in transaction`. This generalizes the `take_lock`/`release_lock`
  rule.
- **L7 Money survives the race.** After any scenario, the M-invariants
  that cover the rows touched still hold. Examples: a split still sums to
  its parent, and a reimbursement cap is never exceeded.

### L1 as a suite-wide hook

`tests/lock_order_hook.py`, installed from `tests/__init__.py` like
`api_conformance`, reads every statement a psycopg cursor runs and
recognises the household locks by the SQL their helpers send (the helpers'
names are bound in many modules at import time, so wrapping the functions
would miss callers; the statements cannot be missed). Per connection it
records the ranks taken in the current transaction, clearing the record
wherever a transaction ends (psycopg sends COMMIT, ROLLBACK and BEGIN
itself, so the hook also wraps `Transaction.__exit__`, `commit` and
`rollback`). A request for a lower rank after a higher one is recorded,
and the test during which it happened fails when it ends, naming both call
sites: raising in place would be swallowed by a best-effort step's
`except Exception`. Try-locks and `FOR UPDATE NOWAIT` never wait and are
not ranked. `test_every_household_lock_helper_has_its_documented_rank`
runs each helper against a recording connection, so a helper whose SQL
changes shape fails there instead of silently leaving the hook blind. Row
locks are out of scope for the hook; the harness checks them through L2.

### Harness: `tests/concurrency.py`, built on `lock_order_util`

- **Actors.** `Side` threads, each on its own `tenancy.tenant_connect`
  connection to one synthetic household, plus an admin observer
  connection. N ≤ 6. The test caps itself under `OIKONOME_POOL_MAX`
  (default 10) and never borrows past it.
- **Start together.** A `threading.Barrier(N)` releases all actors into
  their first statement at once.
- **Interleavings.** Each actor's code gets `Gate` pause points patched
  in at named seams, the lock helpers first and then chosen data
  functions. A schedule is a sequence of (actor, gate) releases. For two
  actors with k gates each, the default mode runs a fixed, small set: both
  orders at each lock seam, plus "A parks holding its locks while B
  arrives". The long mode samples random schedules from a seed
  (`concurrency.random_schedules`, 20 per scenario by default,
  `OIKONOME_CONCURRENCY_SCHEDULES` for more; each actor parks at a random
  seam or none, and the starts and resumptions come in a random order from
  the run's printed `OIKONOME_PROPERTY_SEED`, which every such schedule's
  label names; L5 is held only on a schedule where the others all arrive
  while one caller is parked inside the door). A schedule
  that waits on a lock is detected with `waits_on_a_lock`, so a release
  never sleeps blindly.
- **Oracle.** L2 by `deadlocks_since`. The settle time is paid once per
  scenario batch, not per schedule. L3 by digesting the touched rows and
  comparing them against serial outcomes computed on cloned households
  (`roundtrip_builders` builds clones cheaply). L6 by `pg_locks` and
  `pg_stat_activity` on the observer connection.
- **Real database.** The shard's own test database, as every test uses.
  No mocks of Postgres locking.
- **Scenarios.** Ordered by how often the code paths meet:
  1. two `set_split` on one row, and `set_split` against a settlement of
     its hold;
  2. a merchant-wide category write against a rename, and a bulk
     recategorize against a rename;
  3. two settings saves to different keys, and to the same key (`config_txn`);
  4. a reimbursement flag against pairing and a settlement;
  5. a receipt snap against a file import (storage, then pairing);
  6. a bill approval against detection;
  7. two invite claims with one email;
  8. an erasure against a collector push;
  9. a restore against an edit (L4, R8).

  **Seams.** The pause points are `concurrency.Seam`s rather
  than `Gate`s patched one function at a time: a seam is a household lock
  by its L1 rank, any statement by a pattern (read with its parameters, so
  a lock key counts), or a function before or after it runs, and it is
  read off the statements psycopg runs, the way the L1 hook reads them, so
  a helper bound into a module at import time cannot slip past. Every
  actor starts parked; "together" releases them through one `Barrier`, a
  "parks at" schedule lets one actor run to its seam and then each other
  actor until it is parked, finished or queued behind a lock (read from
  `pg_locks`, never a sleep), and `outlasting` keeps the holder parked
  until the door behind it has answered, which must then be busy (L4's
  bound, not only its order). The same statement hook is the L2 witness:
  a statement killed as a deadlock is recorded where it is raised, so one
  a best-effort step swallows is still charged; `deadlocks_since` backs it
  once per batch (one batch per test class). L3 runs each serial order of
  the actors that took effect (refused and busy ones wrote nothing, which
  the comparison itself holds) on a fresh household built the same way,
  and a scenario whose serial run changes nothing its digest reads, or a
  seam no actor reaches, fails as a blind check. L6 is read once every
  thread the schedule's doors started (the categorize pass after an
  import) has finished, and charges every backend then left stuck, at
  once, with no grace (the serial runs join those threads too, before
  their digest). L7 is
  `concurrency.money_invariants` (a split sums to its live row; no split,
  link or flag on a retired row; no partial links past a charge or a
  deposit) plus each scenario's own. The modules, one per scenario:
  `test_splits_written_at_once_land_whole` (1),
  `test_merchant_category_writes_and_a_rename_at_once_end_as_some_order`
  (2), `test_settings_saved_at_once_lose_no_change` (3),
  `test_a_reimbursement_flag_a_pairing_and_a_settlement_at_once_end_as_some_order`
  (4, all three at once), `test_a_receipt_snap_and_a_file_import_at_once_end_as_some_order`
  (5, an import that parks its upload and one that lands rows),
  `test_a_bill_approval_and_detection_at_once_end_as_some_order` (6, and
  L5 on two detections), `test_two_invite_claims_with_one_email_make_one_member`
  (7), `test_an_erasure_and_a_collector_push_at_once_leave_nothing_behind`
  (8) and `test_a_restore_beside_an_edit_answers_busy_and_keeps_the_figures`
  (9, R8: a note and a split). `test_the_concurrency_harness_sees_what_it_checks`
  proves the oracle sees a lost update, a deadlock, a lock left on a
  backend, a worker giving up busy and a seam nobody reaches.

## 4. Body fuzzing

### Inventory

Every `APIRoute` under `/api` whose endpoint carries `body_of(...)`. A
completeness test asserts that every route depending on
`json_fields`/`json_fields_or_empty` documents a body, or is named in an
exclusion list that says why. A coverage count (doors × mutation classes
reached) is written as `api_conformance` writes its count.

### Valid bodies

The generator reads each model's `model_json_schema()` and builds its own
small Hypothesis strategy: Literal → `sampled_from`, `X | None`, lists,
nested `Shape` refs, str, int, float, bool. The body models are flat and
simple, so no JSON-Schema library is needed (§5). A per-door hints table
maps a field to values that need household state, drawn from the fixture
household: ids of existing transactions, accounts and bills, category
keys, ISO dates. Without hints, most doors would refuse a random body at
its first field check, and the deep fields would never be reached.

### Hostile mutations

Apply one mutation to a valid body, at one path:

- **Types:** each JSON type in place of the expected one. Top level as a
  list, a string or null.
- **Numbers:** NaN, ±Infinity (as the bare tokens Python's JSON reader
  accepts), 1e308, −0.0, 5e-324, a 400-digit int (past float range), a
  4,400-digit int (past Python's int-parse limit), `0.1+0.2`.
- **Strings:** empty, whitespace only, 10 kB, 200 kB (inside the body
  limit), NUL, a lone surrogate, a right-to-left override, combining marks,
  emoji, a formula prefix, SQL LIKE and regex metacharacters
  (`%_\`, `(.*){999}`), one very long token.
- **Containers:** nesting 50, 900 and 5,000 deep, a list of 10k items, an
  object with 10k keys.
- **Keys:** each required key missing, each key null, an extra key, a
  duplicate key in the raw JSON.
- **Encoding:** a wrong content type, invalid UTF-8, a BOM.

### Roles and households

Owner (elevated), owner (not elevated), member, viewer, signed out. Two
households: A is the actor. B is seeded with canary strings and its own
ids, which A sends in paths and bodies.

### Properties, checked on every request

- **F1** Status < 500. A 503 is allowed only with a detail from a short,
  reviewed list (for example "the assistant needs a local LLM
  configured", or a starved pool).
- **F2** A 4xx writes nothing. The test database gets a row-level AFTER
  INSERT/UPDATE/DELETE trigger on every tenant table. The trigger counts
  writes per (tenant, table) in an unlogged side table. The fuzz module
  installs it in `setUpClass` and drops it in `tearDownClass`, inside a
  `finally`. A rolled-back write leaves no count, which matches the rule:
  no write commits. An explicit allowlist names the tables that a refusal
  legitimately writes (failed-auth counters, lockouts, session
  `last_seen`, activity of a refused step-up), each with its reason. In
  long mode, a per-tenant digest cross-checks the trigger.
- **F3** A 2xx reply conforms. `api_conformance` already checks this, at
  no extra cost.
- **F4** No cross-household access. As A, with B's ids, the reply is
  4xx, or 2xx touching nothing of B: B's digest is unchanged, and no B
  canary appears in any reply to A. Doors that use `admin_connect` (they
  bypass RLS: mostly `app.py`, `adminconsole.py`, one in `api_settings.py`)
  run under every role, B's ids included.
- **F5** Pool hygiene after every request, the same check as the
  concurrency harness's pool hygiene.

### Deterministic default

Every door × every mutation class, once. The path that a class hits is
chosen by a seed derived from the door's path, so the run is the same
every time and covers every door-and-class pair. The long mode adds
Hypothesis-random mutations, several paths per body, and a random role.

## 5. Dependencies (test-only, pinned)

- `hypothesis==6.170.14` (released before the two-week age floor; requires
  `sortedcontainers>=2.1,<3`).
- `sortedcontainers==2.4.0`.

These are test-only and never part of the image. They resolve against
the server's pins into their own hashed `server/requirements-test.txt`
(from `server/requirements-test.in`), which `make lock` generates and
`make sync` installs. `test_the_suite_runs_the_locked_dependency_versions`
holds that file to the same rules: every pin exact and hashed, the venv
at the pins, and the Dockerfile never naming it. A missing Hypothesis is
an import error, not a silent skip. A skipped property test is a test
nobody runs.

Not taken: `hypothesis-jsonschema` (adds `jsonschema`, `referencing` and
the compiled `rpds-py` to cover models a 60-line strategy covers) and
`schemathesis` (a second test runner and HTTP layer, where `TestClient`
already is one).

## 6. Runtime budget

The sharded suite deals test modules to shards by file **size**, not by
time. A small, slow module can therefore land on an already heavy shard.
Rules:

- No module takes more than ~25 s on its own. The fuzz work is split
  by router into at least three modules, and the money properties into
  pure and database-backed modules.
- **Default (every run):** Hypothesis profile `default` with
  `derandomize=True`, `database=None`, `deadline=None`, and
  `max_examples=40` for pure properties. Seeded households use
  `roundtrip_builders` with 3 fixed seeds. Fuzzing covers each door ×
  value class once, every text field × every text class, plus the
  whole-body classes: about 3,000 body requests and 320 GET text requests,
  in three modules of 15–23 s each (about 55 s in one process; the large
  text bodies dominate). Concurrency uses the fixed schedules only, with one deadlock
  settle per batch. Target: **under 120 s of added CPU in total, about
  30 s of added wall time across four shards.**
- **Measured (one process):** owner fuzz 15, 21 and 23 s;
  member fuzz 19 and 8 s; viewer 18 s; cross-household 17, 16 and 16 s;
  the database-backed money properties 1 to 6 s each. Each fuzz module
  installs the write detector and builds the canary household once
  (about 2 s).
- **Measured (concurrency):** ten concurrency modules, 8 to 10 s each
  alone and the restore's 14 s, about half of it the one deadlock settle
  per module; mostly waiting, little CPU.
- **Measured (the cash forecast benchmark, specs/forecast-benchmark.md):**
  `test_forecast_bench_world` (the simulator, pure) under 1 s;
  `test_forecast_benchmark_a` 10 s (three personas, four days, and one
  of them run twice for determinism) and `_b` 7 s (three personas). Its
  long module (every persona, three seeds, both bill modes, weekly) is
  about 24 min of CPU in one process.
- **Long (by hand):** `OIKONOME_PROPERTY=long` selects the `long` profile
  (`max_examples=2000`, random seed, printed reproduce blobs) and
  `OIKONOME_PROPERTY_SEEDS=<n>` seeded households. This follows the
  precedent of `OIKONOME_RT_SEEDS`. It also turns on random interleavings
  and every archive cell class. Run it with `make test-long`, which is
  never part of `make test`.
- Every failure prints what reproduces it: a seed, or the
  `@reproduce_failure` blob.

## 7. Where each part is held

1. **Foundation and pure money properties.** The pins, the Hypothesis
   profiles and `tests/props.py` (profile and seed switches); M1, M6,
   M14 and M15 as pure properties.
2. **Fuzz F1.** The inventory and its completeness test, the generator
   from models plus hints, the mutation vocabulary, and no 5xx.
3. **The L1 lock-order hook.** Suite-wide, from `tests/__init__.py`. The
   rank table is checked against the documented order.
4. **Database-backed money properties.** M2 to M5, M7 to M13 and M16 on
   `roundtrip_builders` households, the cross-surface equality table,
   and R5. The cross-surface table lives in
   `tests/test_a_figure_reads_the_same_on_every_surface.py` and reads
   every page's payload through the API (`tests/money_surfaces.py`); the
   figures that differ by design (the Year lens's standing excess, Cash
   Flow's payment-day dating, the Today bills row's month schedule) are
   named in its note and kept out of it. The generated households carry
   a plan (bills, an envelope, budgets, a typed income or a paycheck
   series, goals in both modes), and their brokerage is its own login:
   with every account on one login no month's Saved would be measured,
   and a Saved property would hold vacuously.
5. **Fuzz F2, F4 and F5.** The detector is `tests/write_detector.py`
   (every table with a `tenant_id` column, the control-plane ones
   included; its reviewed allowlist is empty, since no refusal the fuzz
   reaches writes a household table), the per-reply checks and the
   canary household are `tests/fuzz_checks.py`. The three owner modules
   hold every reply to F2, F4 and F5; two member modules run every value
   and whole-body class at every door (the large text bodies stay with
   the owner's run: the role decides who may write, not how a body is
   read); the viewer module holds every write door to 403 (except the
   viewer's own account doors and the reads that arrive as a POST, which
   get the hostile classes instead); and three cross-household modules,
   one per role, send the canary household's ids to every route in every
   path parameter and id field, read every page, and digest the canary
   household at the end.
6. **Restore R1 to R4 and R6.** `test_a_restore_keeps_every_figure`
   (every value of every money surface and the email's context, on a new
   household and on the household itself, through
   `restore_job.restore_now`; what is not the household's data is named
   in its note), `test_a_restore_touches_only_its_own_household` (A's
   archive into B through the job: A and a signed-up C byte-identical and
   never written, by digest and by the write counter, twice; C's owner,
   an accepted recipient in A's archive, is not enrolled in B's mail),
   `test_a_hostile_archive_cell_never_breaks_the_restore` (20 cell shapes
   and 4 member shapes in every member of one export: done, or a stated
   refusal that wrote nothing, with every row counted once; single cases
   are pinned in `test_a_hostile_restored_cell_costs_only_its_row`),
   `test_a_household_restored_from_a_hostile_archive_still_reads` (every
   landing shape at once: every GET page below 500 and in its documented
   shape, the email, and the household's own export restores), and
   `test_a_restore_failing_at_any_step_changes_nothing` (a failure after
   every lander and step of `ORDER`, the locks, `Ctx.prepare`, each part
   of `finalize` and `FINALIZE`: the job reports it, the household is
   byte-identical, nothing after the commit ran, the sync lock is free).
   The repair after a restore (`AFTER_COMMIT`, then `repair_pairs` and the
   categorize pass in `restore_now`) is outside the all-or-nothing rule by
   design and says so there. R6's cases are held one table at a time by
   the restore tests (the rejected offer, the dismissed alert, the linked
   household's decisions) rather than by one property over every
   `Replace` lander.
7. **The concurrency harness.** `tests/concurrency.py`, L2 to L7, the
   nine scenarios and R8 (section 3). About 5 s of each scenario module
   is the one deadlock settle; the schedules themselves take 0.2 to
   1.1 s, and the restore's outlasting schedule waits out the door's 5 s
   bound.
8. **Long mode.** `make test-long` (`scripts/test-long.py`) runs every
   test module that imports a generator or harness with
   `OIKONOME_PROPERTY=long`, `OIKONOME_PROPERTY_SEEDS=12`,
   `OIKONOME_RT_SEEDS=24` and one printed `OIKONOME_PROPERTY_SEED`;
   `JOBS=n` deals them over n test databases, `ONLY=` picks modules,
   `SEED=` replays. `props.run_seed()` is the one seed for long mode's
   own random choices: the concurrency schedules and the fuzz's fields
   (each value class at up to three fields of a door, drawn from the seed
   and named in every failure). R7 is
   `test_an_archive_from_an_older_schema_restores_whole` (section 2): 3 s
   alone by default, about 3 minutes in long mode (61 eras x 12
   households). A whole long run (`JOBS=6`) takes about 22 minutes of
   wall time, bounded by the hostile-archive module's every-column walk.
   `DEVELOPMENT.md` → "Property tests" explains running it.

## 8. Product rules

- **D1 Parts add up to their total.** Wherever a page shows a total and
  the parts beneath it (the Accounts sections, Net Worth by institution
  and by asset class), the parts as shown add up exactly to the total as
  shown. The parts are rounded by largest remainder, so a part may move by
  a cent and the total never does. (The Year lens gets the same result
  the other way round: its total is the sum of its cells.) M7 and M8
  assert the shown parts, not the unrounded ones.
- **D2 The later save wins.** When two members save the same field
  within seconds, the later save wins and the earlier person is not told.
  The concurrency harness's serial-equivalence check holds only that a change nobody else touched is never
  lost.
- **D3 Corrections apply from now on.** A fix to how a figure is computed
  changes current and future months. A closed month keeps the figure it
  was frozen with. No test recomputes a frozen month to assert against
  the corrected rule.
- **D5 A withdrawn deposit repays nothing, past months too.** A deposit
  the bank feed withdraws stops counting as a payback, partial link or
  full pair; the charge is spend again until something else repays it,
  and a deposit reported again never doubles its links. Closed months are
  recomputed for this case: the one exception to D3.
- **D6 A chain adds up as printed.** A line of terms that produce a
  figure ("in → bills → to spend → saved → excess") is held to D1: the
  figure is rounded once and the terms by largest remainder to it, so as
  printed they produce it.

## 9. Edge cases the tests pin

Each is a rule a property or fuzz test above reaches, also held by a
regression test of its own.

- **A claim age past float range is refused, not a 500.**
  `sync.restore.ret_wizard_answer` makes its whole-number check
  (`float(v).is_integer()`) inside the guard that catches
  `OverflowError`. So `{"ret_wizard_answers": {"ssage": <a 400-digit
  integer>}}` is a 400 at `POST /api/settings`, and in a restored archive
  (`_ret_answers`, applied through `_CONFIG_SHAPES`) it drops that one
  answer and the rest of the restore lands. M15 and a door-and-archive
  regression test hold it.
- **The merchant-wide category doors cap the payee.**
  `POST /api/bills/merchant-category/preview` and
  `POST /api/bills/merchant-category` cap `payee` at the longest
  descriptor the ledger stores (`sync.base.MAX_DESCRIPTOR`), so the
  longest real name still previews and a longer one is a 400. Uncapped,
  the payee becomes a token match in `web.data._token_family` that
  Postgres refuses as "regular expression is too complex".
- **A push nested thousands of levels deep is a 400.** Python's JSON
  reader takes a 5,000-level document; the NUL-stripping walk every
  collector push runs (`engine.compat.strip_nul`) refuses one that deep
  with ValueError, which `/api/import/coinbase`, `/api/import/amazon` and
  `/api/import/costco` answer as a 400 "bad payload".
- **An advisory-fee platform is read literally.** The advisory-fee
  estimate escapes each typed platform fragment before using it in a
  LIKE pattern, so a fragment of `%` or `_` matches only itself, not
  every investment institution.
- **Parts add up to their headline (D1).** `reporting.account_totals` and
  `compute_networth` round each section, institution and asset class by
  largest remainder (`reporting.shown_parts`) to the headline, which
  never moves, so balances carrying fractions of a cent (investment and
  some bank feeds) never leave the parts shown a cent off it.
- **The settings door takes back its own read of the retirement
  answers.** `GET /api/settings` shows the answers with the mark that
  says which a current client saved (`answers_v2`, kept inside the
  object so a restore carries it). `POST /api/settings` skips that mark
  in a body: `ret_answers_v` alone decides what a save marks, so posting
  the object back as read leaves the stored answers unchanged.
- **A deposit reported again takes only the room its charge has left
  (D5).** A partial link on a deposit the bank feed withdrew claims
  nothing, so the charge's room opens and a second deposit can repay it.
  When the feed reports the first deposit again under its id (the ingest
  upserts set `removed` back to 0), its links take only the room the
  charge has left, so no charge carries more links than it cost
  (specs/counted-figures.md, "A withdrawn deposit repays nothing"). A
  full pair on a deposit the feed withdraws becomes the partial link
  recording what it repaid, at once, and the charge is spend again;
  closed months are mended by the same repair. M13's walk withdraws any
  deposit and reports withdrawn ones again.
- **The plan line adds up as printed (D6).** The terms of "income in →
  bills → to spend → saved → excess" are rounded by largest remainder to
  the printed excess, once, on the server (`budget.plan_line`); the
  email, and Today and Budget on both clients, print those terms
  (specs/counted-figures.md, "The plan line adds up as printed"). M9
  asserts the printed line.
- **A refused notification test writes nothing (F2).**
  `POST /api/notify/test` refuses a channel it does not know, a text test
  with no verified or consenting number, and a push test with no browser
  or device anywhere before it builds the day's verdict, which writes the
  household's alert log.
- **A household that never synced reads its sync status in shape (F3).**
  `GET /api/jobs/sync/status` answers `{"state": "none", "progress": {}}`
  with the `started_at` and `updated_at` the documented reply requires,
  both null.
- **Text where a number belongs costs only its row (R3).** `restore._f`
  raises `_NotANumber` for a cell that does not read as a number (a
  charge's amount, a 0/1 flag, a bill's amount, a split, a tax document's
  figure or any other number column), and `landers._prepare` and the
  reading hooks of bills, splits and tax documents count that row as
  malformed; the rest lands, as every other unreadable cell (dates,
  bounded numbers) already costs only its row. A non-finite number still
  refuses the archive, as `_f` states.
- **A connection's `raw` nested past the jsonb walk lands empty.**
  `_raw_dict` reads such a document as malformed, so the connection lands
  with an empty `raw` instead of failing the column's object check.
- **Half a surrogate pair in a JSON cell reads as U+FFFD.** JSON may
  spell `"\ud800"` alone; Python reads it and PostgreSQL refuses it in
  jsonb, so `restore._j` reads it as U+FFFD.
- **A member that is not UTF-8 is refused in words.** It is a bad file
  named in words, never the codec's message; so is a settings document
  nested past the walk.
- **Every row that lands is one a page can read.** A pending bill offer
  with no kind or amount is refused as malformed, since the detector
  always writes both and `GET /api/bills` documents both as present (the
  generated clients read them so). An answered offer still lands for its
  answer (the Bills screen asks only about pending ones). A receipt with
  a status the app never writes lands as waiting to be read, inside the
  documented set the Receipts page reads. The round-trip builders never
  leave an offer's kind and amount NULL, as no writer does.
- **An unnamed institution is "Other", apart from one named "Other".**
  `by_institution` (and its ring and the integrations copy) lists a
  connection with no institution name as "Other", grouped by the name
  and whether one was given (`_live_accounts.inst_unnamed`), so it stays
  apart from an institution literally named "Other"; the web donut and
  the phone's breakdown pair the ring's rows by position, not by label,
  and the Accounts page counts the two as two institutions
  (`test_an_unnamed_institution_is_other_apart_from_one_named_other`). A
  label only: no total moves.
- **A NUL in a JSON cell's key costs only that value.**
  `compat.strip_nul` refuses such a key by design (a key check must never
  see a rewritten key), and every jsonb write runs it. So `restore._j`
  reads a JSON cell whose document holds a NUL in a key as an empty cell
  (a `raw` lands as an empty document, the settings document as no
  settings, the household's own kept), the rest of the archive lands,
  and the summary names the file and column (`_rows` notes which cell
  spelled the escape). The key is never rewritten. A file that is no
  export (a settings document nested past the walk, a member that is not
  UTF-8) is still refused whole
  (`test_a_nul_in_a_json_key_costs_only_that_value`).
- **A restore keeps an empty category source.** The derivation in
  `upgrade_archived_txn` is 098's, for rows stored before the
  `category_source` column existed, so it runs only for an archive older
  than 098, by the archive's own age (`archive_upgrades.archive_schema`:
  its manifest, or the columns its headers show), as 035, 044 and 226 are
  read. A newer archive's row lands with exactly the source it carried,
  NULL included. The category and every figure are unchanged either way
  (`test_a_restore_keeps_an_empty_category_source_from_an_archive_that_had_the_column`).
- **Tests leave no work behind (L6, F5).** Every test releases the
  session locks it takes, and the suite joins the threads a test's doors
  started at the end of every test (`tests/thread_hygiene.py`,
  `util.join_product_threads`; a test that waits on its own uses
  `util.outlive_no_threads`), proven by
  `test_a_test_outlives_the_threads_its_doors_start`.
  `OIKONOME_THREAD_SURVEY` lists, per test, any thread the product
  started that is still running. The joins add about 150 s of waiting to
  a whole run (about 40 s a shard). So a fuzz module joins any thread
  started outside a test (a class's setup) and then refuses to begin
  beside any stuck backend, excusing none, and L6 joins the schedule's
  own threads and then charges any stuck backend at once.
- **An archive from before the bills rename keeps its offers.** An
  install before 040 kept the offers, and the household's answers to
  them, in `recurring_proposals`, so its export carries them as
  `recurring_proposals.csv`. The offers lander reads both names, as the
  bills lander reads the old `recurring.csv`: a rejected offer is the
  only thing keeping the detector from asking again. R7's era before 040
  holds it.
- **A restore never brings back more payback than a charge cost, nor
  claims more of a deposit than it brought in.** The reimbursements
  lander would otherwise take each archived link's amount as it is, and
  M13's cap holds only for links a door wrote. The restore cuts the
  partial links on a charge to what it cost, full pairs counted as
  `budget.REIMB_RECEIVED` counts them, newest first, and the cut part of
  each deposit is income again (`restore-landers.md`, "Payback past a
  charge"; `test_a_restore_never_brings_back_more_payback_than_its_charge`).
  It cuts the partial links on a deposit to its amount the same way, full
  pairs on it counted as `_deposit_left` counts them, newest first, and
  the uncovered part of each charge is spend again
  (`test_a_restore_never_claims_more_of_a_deposit_than_it_brought_in`).
  Both limits are held by one pass, `web.data.trim_partial_links`, over
  the charges and deposits the restore's links name, oldest link kept
  first: on one side alone that is the newest cut first, and it is the
  only order in which a second restore changes nothing. (Charge side then
  deposit side, run one after the other, removes a link for room an older
  link's deposit then gives back, and the next restore brings it back: a
  $100 charge linked for $100 from a $60 deposit and, later, for $50 from
  a $50 one ends at $60 + $0 after one restore and $60 + $40 after the
  second; the one pass gives $60 + $40 at once.) R7 holds every restored
  charge to its cost and every restored deposit to its amount, whatever
  the source carried.
