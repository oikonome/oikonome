# Rules page: provenance groups + pagination

A lived-in ledger has thousands of categorization rules; rendering them all
at once would bury the handful the user actually authored. So the Rules
page groups them by provenance and pages each group.

## Shape

- One page, THREE provenance sections:
  - **Your rules** (`source='user'`) — expanded by default.
  - **Model-learned (N)** (`source='llm'`) — collapsed to a count +
    expand control.
  - **Built-in (N)** (`source='seed'`) — collapsed to a count + expand
    control.
- Rules stay individually-listed rows; groups load **lazily** with
  server-side search + pagination (~50/page). Nothing may render the
  whole rule set at once.
- `GET /api/rules` takes `source` (user/llm/seed), `q` (searches
  merchant AND category), `page`; returns
  `{rules, total, page, per_page, counts}` where `counts` is the
  q-filtered per-source totals — collapsed headers show counts without
  loading rows.

## Promotion

- **Editing or disabling a model/seed rule PROMOTES it to
  `source='user'`** and it moves to "Your rules". The Model-learned
  group therefore stays purely *untouched* inferences.
- Re-enabling a promoted rule keeps `source='user'` (a promoted rule
  gains the user-rule apply semantics — the user made a call about it).
- Deleting stays deleting — no promotion, the rule is gone.
- Edit promotion is `upsert_user_rule`'s ON CONFLICT setting
  `source='user'`; disable promotion is in `set_rule_disabled`.

## Explicit non-features

- **No group-level kill switch** — per-rule disable only.
- **No hand-authoring** — rules come from the seed set, the model, or an
  edit to an existing rule.

## Empty states

- The **Model-learned group hides entirely at zero rules** — that is the
  normal state of an instance with no LLM configured, not an error.
- A fresh instance with zero rules total gets friendly empty-state copy
  (how rules come to exist) instead of a bare table.

## Misc

- Search spans all groups (each group shows its q-filtered rows and the
  headers show q-filtered counts).
- Viewers are read-only; writes are owner-only.

Tests: `tests/test_rules_groups.py` (pagination, search params,
per-source counts, promotion on edit and on disable, all-zero counts for
the empty state); `tests/test_rules.py` still covers
disable-stops-applying / re-enable-resweeps / delete.
