# Splitting the JSON API into one module per area

`server/oikonome/web/api.py` held the whole SPA/mobile JSON API: 202 routes
and their helpers in one module of about fourteen thousand lines. It is split
into one module per area of the product. The split is **moves only**: no
route, method, parameter, dependency, response setting, middleware, auth or
role gate changes, and no two routes that could answer the same request
change order. What changes is which file a handler lives in.

## 1. The areas

"Route positions" are indexes into the API router's route list as it stood
before the split (`api_routes` in `tests/fixtures/api_route_table.json`), so
the table cannot drift with line numbers. "Size" counts the routes plus the
helpers only that area reaches: S under three hundred lines, M up to a
thousand, L up to two thousand, XL beyond.

| Module | Area | Routes | Size | Route positions (old order) |
|---|---|---|---|---|
| `api_today.py` | Today, lenses, budget snapshots, budget suggestions, activity, alerts, calendar | 12 | M | 0-5, 85, 92-96 |
| `api_ledger.py` | transactions (browse, export, category, split, bulk, note, owner), owners, categories, merchants, rules | 23 | L | 6-22, 51, 60-64 |
| `api_accounts.py` | accounts list/edit, provider keys (MX, Plaid), connection export/import, account links, holdings, sync jobs, disconnect/reconnect, hide/remove | 30 | L | 23-37, 72-79, 185-191 |
| `api_reimburse.py` | reimbursement pairing | 7 | S | 38-44 |
| `api_bills.py` | bills, detection, proposals, history, merchant-category | 14 | L | 45-50, 52-59 |
| `api_assistant.py` | the assistant chat, report, history, threads | 7 | S | 65-71 |
| `api_settings.py` | settings read/save, recipients, outbound resume, SMTP test | 5 | XL | 80-84 |
| `api_retirement.py` | retirement projection, debt payoff plan | 3 | S | 86-88 |
| `api_continuity.py` | the continuity packet's fields and email | 3 | S | 89-91 |
| `api_receipts.py` | receipts: upload, match, parse, items, report | 13 | M | 97-109 |
| `api_access.py` | script tokens, consented support access | 6 | S | 110-112, 199-201 |
| `api_imports.py` | import hub, bulk import jobs, restore progress, mapped/YNAB import, tax documents, batches/rollback, onboarding, history coverage, enrichment, collector pushes (Coinbase, Amazon, Costco, plan activity) | 27 | L | 113-132, 178-184 |
| `api_business.py` | business flags, entities, members, equity, books, P&L, tax pack, compliance, mileage, vendors, account/transaction entity | 45 | L | 133-177 |
| `api_notify.py` | notification channels (SMS, web push, test), send-now email | 7 | M | 192-198 |

About four hundred lines of helpers are reached by more than one area
(section 2), and a few dozen by no route at all (kept for other modules that
import them, such as `pages.py`).

## 2. Shared helpers: `web/api_common.py`

A helper more than one area reaches goes to `api_common.py`. A helper one
area reaches goes with that area. **Area modules import from `api_common`
and from the rest of the package, never from `api.py` or from each other**,
so there is no import cycle and no area depends on another. Reaching into
`app.py` happens only INSIDE a handler (a call-time import), never while the
module is being imported (section 3, "Any API module can be imported
first").

A helper is in `api_common` only once a SECOND module needs it.
`api_common` holds:

- the router itself (`router`, below) and the request basics: `_user`,
  `_conn`, `_today`, `_today_for`, `_ser`, `_scalar_text`, `_literal_true`,
  `_ck_ym`, `_ck_year`, `_BODY_ROWS_MAX`;
- the permission checks: `_owner_only`, `_may_edit`, `_step_up_proof`;
- the CSV writers: `_csv_safe`, `_csv_piece_safe`, `_SafeCsvWriter`,
  `_csv_writer`, `_filename_safe`, `_same_site_download` (ledger, receipts,
  business);
- names shared by ledger and bills (and sometimes accounts or today):
  `_category_why`, `_SOURCE_WORDS`, `_category_long_ok`, `_category_name`,
  `_new_category_or_400`, `_stamp_recurring`, `_recurring_stamper`,
  `MERCHANT_NAME_MAX`, `_merchant_name_or_400`, `_label_field`,
  `_budgets_set`, `_setup_mode_of`.

The typed body reader (`json_fields`, `json_fields_or_empty`, `Fields`, the
money readers) already lives in `web/fields.py`, and the lock-wait bound
(`bounded_lock_wait`) in `sync/reanchor.py`; areas import them from there.

**Old names stay importable.** `pages.py`, `notify/outbound_pause.py` and many
tests read helpers and handlers as `api.<name>`. Every moved name something
outside its new module reads that way is imported back into `api.py`
(`from .api_<area> import name  # noqa: F401`), as `api.py` already does for
the money readers that moved to `fields.py`.

## 3. Mounting: one router, imported in order

All API modules register on ONE `APIRouter(prefix="/api")`, defined in
`api_common.py`. `api.py` keeps `router` as that same object, so `app.py`
still mounts `api.router` exactly once, at the same place among the app's
routers.

Each area module's routes register when it is imported. `api.py` imports an
area module at the point in the file where its routes used to be, so the
router's route list (the order Starlette tries patterns in) stays exactly as
it was. `api.py` is a docstring, a list of
imports in route order, and the compatibility re-exports.

### Any API module can be imported first

Route order must not depend on which module a process happened to import
first (a script, a REPL, one test module run alone), and no API module may
need the app to exist in order to be imported. Two things make that so:

- **The signed-in-user dependency lives in `web/session_user.py`**, which
  imports nothing but the framework. `current_user` there is the ONE
  dependency object; the checks it runs stay in `app.py`
  (`_resolve_current_user`) and are looked up when a request arrives, not
  when a module is imported. `app.py` re-exports the same object as
  `app.current_user`, so a route in `app.py` and a route in an area module
  share one dependency (FastAPI resolves it once per request) and one
  override key. `api_common._user()` returns it without touching `app.py`.
- **`api.py` declares the order** (`AREAS`) and sorts the router's routes
  into it after importing the areas: areas in `AREAS` order, and inside an
  area the order its module wrote. An area imported early registered its
  routes early; the sort puts them back. In a process that imports the app
  first the sort changes nothing.

`tests/test_each_api_module_imports_first.py` imports each API module first
in a fresh interpreter and compares the app's route table with the one an
app-first process gets.

Nested routers (`router.include_router(area_router)`) are NOT used. FastAPI
now keeps an included router as one wrapper entry, and its inner routes keep
their own unprefixed paths (`/retirement`, not `/api/retirement`). Every
inventory test that walks `app.routes` (`tests/util.py:all_routes`) would
then see the wrong paths, and the mount order would depend on where each
`include_router` call sits.

Dependencies stay attached to the decorators that already carry them
(`Depends(_user())`, `Depends(limit(...))`, `Depends(json_fields)`); the
router has no router-level dependencies and gains none. Middleware lives in
`app.py` and is untouched.

Area modules are flat files (`web/api_<area>.py`), not a subpackage, because
some scanners read `web/*.py` without recursing.

## 4. Tests that read the API's source

These read `api.py` as text, or filter routes by `endpoint.__module__`. Each
goes through one set of helpers in `tests/util.py` that covers every API
module (`api.py`, `api_common.py`, `api_*.py`):

- `api_source_paths()`, `api_source_files()`, `api_sources()`: the files,
  per-file texts, and all of it as one text;
- `api_module_names()`, `is_api_endpoint(route)`: for module filters;
- `api_file_of(name)`: the file that defines a top-level name.

Re-pointed:

| Test | What it reads |
|---|---|
| `test_csv_cells_are_safe_in_a_semicolon_spreadsheet` | no `csv.writer` outside `_csv_writer` (each file on its own; `_csv_writer` defined exactly once) |
| `test_malformed_input_refusal` | the entity-id guard and its call count |
| `test_member_refused_at_owner_doors` | every inline `_owner_only` door (one file at a time, so a window cannot run into the next module) |
| `test_only_the_bill_engine_activates_bills` | the file allowed to take the bill claim lock by hand: `api_file_of("_hold_bill_claims")` |
| `test_recipient_editor_feedback`, `test_recipient_status_honesty`, `test_taxdocs` | settings-handler text |
| `test_async_handlers_touch_the_db_off_the_loop` | the known async doors, named `<file>:<handler>` through `api_file_of` (the scan already globs `web/*.py`) |
| `test_account_and_rule_doors_take_typed_bodies`, `test_bill_doors_refuse_wrong_types`, `test_business_doors_take_typed_bodies` | routes whose handler is an API one: `is_api_endpoint` |

These already follow a handler to wherever it lives and need nothing, as long
as the re-exports in section 2 keep `api.<name>` resolving:
`test_credential_grade_acts_send_a_security_notice`,
`test_settings_door_reads_through_typed_fields` and `test_erasure_sync_race`
(`inspect.getsource(api.<fn>)`); the `test_inventory_*` suite and
`test_elevation` (`handler_source(route)`, and helper look-ups in
`sys.modules[route.endpoint.__module__]`, which the area module's own
imports satisfy); and every `rglob("*.py")` scan.

### Patches on `api.<name>`

`mock.patch("oikonome.web.api._today")` replaces the name in `api.py`'s
globals, so it only reaches handlers that live in `api.py`. A test that
patches a name a moved handler calls patches it in the module that now
looks the name up. Known patches:
`_today` (business, ledger header, money map, transactions per-day),
`_conn` (transactions export, settings save), `_log_bulk`,
`_valid_push_endpoint`, `_plaid_straggler_ids`, `plaid_validate`, and the
transactions-export constants (`_EXPORT_CHUNK`, `_EXPORT_SEND_PIECE`,
`_EXPORT_SPOOL_MEMORY`, `_EXPORT_SLOT_TTL`, `TRANSACTIONS_EXPORT_CAP`).
`_today_for` calls `_today` in its own module, so once both live in
`api_common`, a test that depends on that call patches
`api_common._today` as well. Patches on module objects reached through `api`
(`api._activity`, `api.tenancy`, `api.demoguard`) patch the shared module
and keep working.
`tests/test_a_patch_on_an_api_module_reaches_the_code_under_test.py` fails
any patch on an API module that no longer reaches the code under test.

## 5. The proof: a pinned route table

`tests/test_api_route_table_is_unchanged_by_a_module_move.py` builds the API
router's table and compares it with `tests/fixtures/api_route_table.json`,
written from the tree BEFORE any move. For every route, in router order:
path, methods, name, unique id, the endpoint (module normalised to
`oikonome.web.api`, qualname, sync or async), the route-level dependencies
(with the values a factory such as `limit("retirement", 30, 60)` closed
over), the whole dependant tree (every path, query, header, cookie and body
parameter with its annotation, validators, default and required flag, and
every sub-dependency), response model and class, status code, schema
inclusion, tags, summary, deprecation, responses and operation id. It also
records the routers `app.py` mounts, in order (add-on routers excluded), the
middleware stack, and `overlapping_pairs`: every two API routes some single
request could match, in the order they are tried.

A module move must leave the whole table equal. Exactly one pair
overlaps: `GET /api/import/bulk/jobs/current` before
`GET /api/import/bulk/jobs/{job_id}`, both in the imports area and moved
together.

Non-contiguous areas (today, ledger, accounts, bills, access, imports) mean
a module imported at one point cannot keep EVERY route at its old index. Only
the overlapping pairs' order can change an answer, so for such a move the
exact-order check may be relaxed to "same routes, same per-route entries,
same `overlapping_pairs`". The relaxation is an edit to this test, never
a regenerated fixture. A deliberate
API change regenerates the fixture with `--write`, and the fixture diff is
the record of what changed.
