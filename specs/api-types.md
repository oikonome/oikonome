# Generated API types for web and mobile

Today the web client (`webapp/src/api/client.ts`) and the mobile client
(`mobile/src/lib/api.ts`) each describe what the server sends in their own
hand-written TypeScript: about 147 exported types on web and 125 on mobile,
written by reading the server and copied between the two. Every call is
`req<TodayFull>("/api/today/full")`: the type parameter is an assertion, not a
check. A field the server renames, drops or starts sending as a string is
invisible to both type checkers, and the two clients can disagree with each
other as easily as with the server.

This note describes how the server becomes the one source of those types, how
the generated file reaches both clients, how the hand-written types move over
area by area, and how a stale generated file fails the suite. Nothing a user
sees changes, and nothing on the wire changes: the work adds descriptions of
what the server already sends and checks that the descriptions are true.

The precedent was the ledger row: a hand-kept JSON Schema
(`specs/ledger-row.schema.json`) from which a small script wrote `Txn` into
both clients, with a test that failed when the copies were stale or the
API's keys differed from the schema. This design is that pattern, widened
to the whole client-facing API; the row is now the `Txn` model like every
other shape, and the schema and its script are gone.

## 1. Where the server stands today

Measured on the route table after the router split (`specs/api-routers.md`),
FastAPI 0.162, pydantic 2.14, on an instance with no add-on installed:

| | Count |
|---|---|
| `/api` operations | 288 (158 POST, 108 GET, 18 DELETE, 2 PUT, 2 PATCH) |
| in the area modules `api_*.py` | 202 |
| elsewhere under `/api` (`app.py` 52, `integrations.py` 15, `reporting_api.py` 8, `pages.py` 4, others 7) | 86 |
| client-facing (all but the dev-only test doors and the two provider webhooks) | 284 |
| operations with a `response_model`, a `responses=` model or a return annotation | **0** |
| operations whose OpenAPI 200 response schema says anything | **0** (all `{}`) |
| JSON bodies read through the typed reader (`Depends(json_fields)`) | 86 doors |
| JSON bodies declared as a plain `dict = Body(...)` (outside the areas) | 27 |
| form / multipart bodies (`Form`, `File`) | 17 operations, already fully described in OpenAPI |
| pydantic models anywhere in the server | none |

So the "typed bodies" are typed **inside** the handler, by `web/fields.py`'s
`Fields` accessors, which OpenAPI cannot see: every JSON body appears in the
schema as `{"type": "object", "additionalProperties": true}`. Every response
is a dict built by hand and appears as `{}`. Query and path parameters are the
one part OpenAPI already describes correctly, from the handler signatures.

Generating TypeScript from today's OpenAPI would therefore produce `unknown`
for every response. The real work is **declaring the shapes**; the generator
is the small part.

## 2. The shape of the solution

```
pydantic models (server/oikonome/web/schemas/<area>.py)
      │  attached to each route as documentation only (section 2.1)
      ▼
app.openapi()  ── filtered to the client-facing /api paths, stripped of prose
      ▼
specs/openapi.json                         (checked in, sorted keys)
      ▼  openapi-typescript, pinned
webapp/src/api/api.generated.ts  ═══  mobile/src/lib/api.generated.ts
                                (byte-identical copies)
      ▼
client.ts / api.ts: `export type TodayFull = S["TodayFull"]` — call sites unchanged
```

### 2.1 Models describe; they never filter

A route gets its shape through `@documented(response=TodayFull, body=...)`
from `web/schemas`, placed under the route decorator. It only sets two
attributes on the handler; the generator reads them into the schema, and the
test-only conformance hook reads them to validate replies. It is **not**
`response_model=TodayFull`, and not a `-> TodayFull` return annotation (FastAPI
turns a return annotation into a `response_model`). It is not `responses=`
either: that would work, but FastAPI would name and emit the schemas itself,
the route table the router-split test pins would change with every model,
and request bodies would still need the generator's help. A `response_model` runs the
handler's dict through the model on every request: undeclared keys are
dropped, values are coerced (`1` can become `True`, a numeric string a
number), and a validation error becomes a 500. Any of those is a wire change
for an installed phone build, and a model that is slightly wrong would break
production instead of failing a test. FastAPI never sees a documented
model. A guard test pins it: every route has `response_model is None` and
no handler has a return annotation.

Request bodies work the same way. The doors keep reading through `Fields`
(its 400-with-the-field-name answers, `blank_ok`, null-versus-absent and the
older body shapes the typed-body tests pin all stay exactly as they are).
The request model is recorded the same way (`body=`), so the schema and the
generated TypeScript know the body; nothing validates a live request
against it. Switching a
door to a real pydantic body parameter would turn its 400s into 422s with a
different detail shape, so it is out of scope.

### 2.2 Models describe what is sent, not what would be tidier

`pending` is `int` because the server sends `0`/`1`; it does not become
`bool`. A field that is sometimes absent is optional; a field that is present
but sometimes `null` is `X | None`. Where web and mobile's hand-written types
disagree today, what the server actually emits decides, and the conformance
check (section 4.2) is the proof. Any change of shape is a separate, reviewed
change, never a side effect of writing the model.

Models use `ConfigDict(extra="forbid")`, so the schema says
`additionalProperties: false` and the TypeScript has no index signature;
field docstrings become descriptions and then JSDoc. Names are the names the clients already use (`TodayFull`,
`CashNotice`, `ServerPlanRow`, …) so a migration changes a declaration, not
the call sites.

### 2.2a Mobile also talks to older servers

A self-hosted instance may run an older server than the phone. The mobile
types mark fields "absent from an older server" as optional where today's
server always sends them. The generated type describes today's server, so
mobile aliases it through a small helper that makes the named fields
optional again (`OlderServer<T, "spend_source" | "saving_source">`); web,
which is always served by its own server, aliases the generated type as is.

### 2.2b A field a screen reads that the server does not send

Replacing a hand-written type with the generated one makes the type
checker name every field a screen reads that the server never sends. Such
a field is not quietly added to the model (that would describe a wire that
does not exist) and the screen is not changed in the same change (that would
change what someone sees). Until the read is resolved the client's
alias keeps it optional through a `ReadButNotSent<{ field: type }>`
helper with a comment naming who reads it, and it is listed here:

| Field | Read by | Resolution |
|---|---|---|
| `forecast.no_checking` on `/api/today/full` | web Today (no-checking notice), mobile `noCheckingLinked` | **The reads were dead and are dropped.** It was never true in any reply: a household without checking is sent no forecast at all (the walk is suppressed before the reply is built), and no day but the live one has a forecast; `runway.no_checking` carries the fact on the live day. The protection is pinned by `test_a_household_without_checking_gets_no_cash_forecast` |
| `ok` on each `/api/doctor/scripts` row | mobile Doctor (the status dot of a script source) | **Mobile reads `status` as web does.** The rows carry `status` (`fresh`/`stale`/`off`), never `ok`; mobile's fallback to `ok` drew every unwatched source as a red dot where web shows it plain. The dot takes the web pill's tone from `status` alone (`scriptStatusTone` in `mobile/src/lib/pure.ts`, pinned by the pure-logic tests). No `ReadButNotSent` use remains |

### 2.2c Who reads the household's setup

Of the 22 keys only the owner may change
(permissions.OWNER_ONLY_SETTINGS), which both clients show to the owner
only, 21 are the owner's alone to read too — the mail relay (`smtp_host`,
`smtp_port`, `smtp_from`, `smtp_starttls`), where the mail goes and when
(`email_recipients`, `email_muted`, `email_schedule`,
`email_send_hour_utc`), the AI endpoint (`llm_url`, `llm_model`,
`llm_vision_model`, `llm_backends`, `llm_roles`, `llm_extra_body`,
`taxdocs_allow_remote_llm`) and the bank-connection keys and billed
cross-checks (`plaid_client_id`, `plaid_env`, `mx_client_id`, `mx_env`,
`plaid_enrich_cap`, `plaid_recurring`), and the views of the AI setup that
the owner's AI card reads (`llm_effective` and `llm_bundled`, which name
the endpoint and model that answer each task, and `categorizer_active`). `timezone` is readable by
everyone, because every screen dates by it. The rows are `read="owner"`
in web/settings_exposure.py; a member or a viewer learns what they need
from their own keys (`email_list_configured`, `email_on_list`, the `*_set`
booleans). test_settings_exposure_table and the settings shape test prove
neither a member's nor a viewer's reply carries any of them, and that the
owner-only write list and the read agree but for the clock.

### 2.3 What enters `specs/openapi.json`

`scripts/gen-api-types.py` imports the app in-process (no server, no
database, no network) and calls `app.openapi()`, then:

- keeps only client-facing `/api` paths: drops `/admin`, the HTML pages, the
  dev-only testing doors and the provider webhooks (an explicit exclusion
  list, checked by a test so a new excluded prefix is a deliberate change);
- imports the app **as an instance with no add-on**, whatever the
  environment has installed (the add-on entry points are hidden before the
  first import), and refuses if an add-on's routers made it in anyway, so
  an installed add-on's routes can never enter the public file and the
  schema does not depend on whose machine ran it;
- strips operation `summary` and `description` (the handler docstrings), so
  editing a comment does not make the file stale; model field descriptions
  stay, because they are written for the reader of the type;
- writes with sorted keys and a trailing newline, so the diff of a change is
  the change.

### 2.4 One source, two byte-identical copies

The clients are separate packages: Metro bundles from `mobile/` and nothing
in `mobile/src` imports from outside it today; Vite bundles from `webapp/`.
Teaching Metro to watch a shared folder (`watchFolders`, a symlink, a
workspace package) adds a moving part to the mobile build for one file. The
ledger-row precedent wrote two identical copies, and so does this:
`specs/openapi.json` is the one source, and the generator writes the same
bytes to `webapp/src/api/api.generated.ts` and `mobile/src/lib/api.generated.ts`.
A test asserts the two are identical. Neither is edited by hand (a header
says so).

## 3. The generator and its pinned dependency

**`openapi-typescript` 7.13.0**, exact version, in its own small tool
package `tools/api-types/package.json` with its own `package-lock.json`,
installed with `npm ci --ignore-scripts`.

Why its own package: 7.13.0 declares `peerDependencies: typescript ^5.x`,
and both clients are on TypeScript 6 (`~6.0.2` web, `~6.0.3` mobile). Adding
it to either client's devDependencies would fail peer resolution or need a
`--legacy-peer-deps` that applies to everything else too. The tool package
pins **`typescript` 5.9.3** alongside it, also exact, and touches neither
client's lockfile. The generated `.ts` is plain type declarations that
TypeScript 6 reads unchanged.

The generator's flags are fixed in `scripts/gen-api-types.py`: one,
`--default-non-nullable=false`, so a field with a default (one the server
may leave out) stays optional in TypeScript instead of becoming required;
no root-type aliases. The clients alias what they need in one place
(`type S = components["schemas"]`), so nothing depends on flags that change
between generator versions. `specs/openapi.json` has no remote `$ref`s, so
the generator reads nothing but that file.

Rejected: extending `gen-ledger-row-types.py` into a
full JSON-Schema-to-TypeScript renderer. It would keep generation in pure
Python, but `anyOf`-with-null, `$ref` chains, enums, `additionalProperties`
maps and discriminated unions are exactly what pydantic v2 emits and exactly
what a home-grown renderer gets subtly wrong. It stays the fallback if the
pinned generator ever has to be dropped.

No new Python dependency: pydantic already ships with FastAPI.

## 4. How drift fails

### 4.1 Staleness (no network at test time)

`scripts/gen-api-types.py --check` exits non-zero when anything is stale.
Two halves, because the suite is Python and the generator is Node:

1. **Server → `specs/openapi.json`** (pure Python, always runs): a suite test
   regenerates the filtered schema in-process and compares it to the
   checked-in file. A model edit, a new route, a renamed parameter without
   a regenerate fails here.
2. **`specs/openapi.json` → both `.ts` files**: the same test runs the pinned
   generator from `tools/api-types/node_modules` and compares. It follows the
   suite's existing convention for node-run checks (skip only when `node`
   itself is absent); when node is present but the tool is not installed it
   **fails**, naming the install command, so a missing install never reads
   as a pass. It also asserts the two copies are identical.

`make api-types` writes everything; `make api-types-check` runs `--check`.
The checks that already run before a dev deploy (the suite, then the web
build and the mobile type check) therefore stop a stale file, and the client
type checks stop a client that no longer matches a regenerated file. A store
build needs nothing new: the generated files are checked in.

### 4.2 Truth: the model matches what the handler sends

A stale file is the easy failure. The real one is a model that is current
but wrong. Under test only, a response hook validates **every** JSON
response of every `/api` request the suite makes against the route's
declared model with `model_validate(..., strict=True)` and `extra="forbid"`.
The whole existing suite (about eleven thousand tests, most of which call the
API) becomes the conformance check without new per-route tests. The hook is
installed by the test utilities, never by the app's own startup, so
production never pays for it or depends on it. Streaming and file responses
are skipped by content type.

Because a route the suite never calls proves nothing, each migrated area
also gets one contract test (the ledger-row `ApiShapeTests` pattern): a
seeded tenant, each route in the area called once, the shape validated. A
per-area list says which areas are migrated; a test fails when a route in a
migrated area has no declared model, so a new route cannot arrive
undescribed.

For request bodies, the typed-body tests already harvest the keys each door
reads (`_harvest` in `test_bill_doors_refuse_wrong_types.py`). The new check
asserts that, for every migrated door, those keys equal the request model's
properties: a door that starts reading a key its model does not declare fails.
The older body shapes the typed-body tests replay must validate against the
model too, so documenting a body never narrows what an installed client may
send.

### 4.3 What an older mobile build sees

Nothing changes. Models are documentation only (2.1), so every response is
byte-for-byte what it was, and every body the server accepted it still
accepts. The generated types change what the **next** build compiles
against, not what an installed build receives.

What the generated types do not do on their own is stop the server from
removing a field an installed build still reads: the current clients would
fail to compile, but the installed ones are already compiled. The existing
rule (the server keeps serving the shapes released builds use) still holds.
The compatibility check makes it mechanical: `scripts/check-api-compat.py`
(`make api-types-compat`, and a suite test) compares `specs/openapi.json`
with the copy each supported release carried and fails when a client built
against the old copy would break:

- a door (path and method) that is gone;
- a reply field that is gone or no longer always sent, at any depth;
- a reply value of a type the old client was not compiled for (a number
  that becomes a string, a value that becomes nullable, a new member of a
  closed set or a new form of a union);
- a body key or query parameter that became required;
- a body or parameter value an old client may send that is no longer
  accepted (a narrowed type, a shrunk closed set).

The direction matters: a reply may send less variety than before (a union
form dropped, a nullable field always set) and a body may accept more;
both pass, as do a new optional field, a new door and a new optional
parameter. A door the old copy left undocumented (`{}`, or a body with no
named keys) has nothing to compare.

`specs/api-compat.json` sets `oldest_supported`, the oldest release tag
whose builds the server still serves. Every release tag from it on that
carries the schema file is a baseline, so a field added in a later release
is protected too, not only the oldest release's. A tag older than the
schema file has no copy; the first commit that added the file stands in
for it, and the check says so. Moving `oldest_supported` forward is what
retires old builds. `accepted` lists deliberate breaks, each as the exact
line the check prints with a `reason`; nothing
else is waived.

## 5. Migrating the hand-written types

Each area moves the same way:

1. write the response and request models for every route in the area;
2. attach them (`@documented`); regenerate;
3. replace the hand-written web and mobile declarations with aliases of the
   generated ones, keeping the exported names; fix whatever the type checkers
   now find (each mismatch is either a client bug or a model that is
   wrong, fixed against the conformance check);
4. add the area to the migrated list and its contract test.

A route not yet described keeps `{}` (generated as `unknown`) and the
clients keep their hand-written type for it.

| Piece | What it covers |
|---|---|
| Tooling | `scripts/gen-api-types.py`, `tools/api-types/` (pinned), `specs/openapi.json`, both generated files, the staleness test, the guard tests (no `response_model`, no return annotations, no add-on routes, copies identical, exclusion list), `make api-types` / `api-types-check`. |
| Conformance | The test-only response conformance hook, the migrated-areas list and the request-key check. |
| Small areas | retirement (3), continuity (3), access (6), reimburse (7), assistant (7), notify (7), receipts (13), bills (14). |
| Today | Today (12): `TodayFull` and the lens payloads are the largest shapes and are shared with the email render, so its models also document what the mail reads. |
| Ledger | ledger (23); the ledger row is a pydantic `Txn` model, replacing `specs/ledger-row.schema.json` and `gen-ledger-row-types.py`, and the generated name stays `Txn`. |
| Large areas | imports (27), accounts (30), settings (5 routes, but the largest single read and write shapes), business (45). |
| Outside the areas | The `/api` routes in `app.py` (sign-in, account, me, …), `integrations.py`, `reporting_api.py` and the rest; the 27 plain-`dict` bodies are documented without changing how they are read. |
| Compatibility | The check against the released schemas (4.3), which protects installed phones. |

Each area's change is moves of types plus new declarations; none changes a
handler's behaviour, and a real server bug a model exposes is fixed in its
own change, not inside the type move.

Once every route is described, `req<T>` can also be offered keyed by path
(`get("/api/today/full")` inferring its type from the generated `paths`), so
the path string is checked too. That is client ergonomics, not part of the
drift guarantee.

## 6. Choices made

It is internal, changes nothing users see, and adds no runtime dependency.
The choices that shaped it:

- **Description-only models** (2.1) over `response_model` or `responses=`,
  because filtering and coercion are wire changes.
- **Two byte-identical copies** (2.4) over a shared package Metro must be
  taught to bundle, matching the ledger-row precedent.
- **openapi-typescript in its own tool package** (3) over a home-grown
  renderer, because of the TypeScript 5 peer range against the clients'
  TypeScript 6.
- **The compatibility check** (4.3): it is the one piece that protects
  installed builds rather than the next build.
