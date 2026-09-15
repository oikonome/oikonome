# Welcome wizard (spec)

This is the flow half of first-run setup; the `/setup` claim page and the
resume-pill mechanics are unchanged. It exists because a fresh instance
could link SimpleFIN and get no connected-state on the provider tile, no
sync progress, and no visible way forward. The causes:

- The ConnectHub provider grid has **no connection-awareness** — it
  never queries `/api/connections`, so no tile can ever show
  "connected ✓" (Welcome.tsx / ConnectHub.tsx).
- The rich sync-progress UI only fires on the "Sync all accounts"
  button; the SimpleFIN link itself runs a silent inline 90-day pull
  (`/api/accounts/simplefin`), so the real first sync shows nothing.
- On the Connect step the only forward control is the sync button — a
  literal "Next →" is unreachable there, and free-jumping step pills
  hid the intended order anyway.
- `log_sync` only stamps the hidden `simplefin` bridge item, never the
  per-institution `simplefin-org` children — every SimpleFIN
  connection displays "never synced" forever.

## Decisions

1. **Strict linear, gated flow.** One step at a time, one clear primary
   action per step; step pills become a passive progress indicator
   (●●○○○), not buttons. Back always allowed. Each step carries a small
   "Skip this step" link next to the primary Continue; skipped steps
   are marked and resumable. The global "don't show this again"
   escape hatch stays (`wizard_done`).
2. **Steps, in order:**
   1. **Connect accounts** — provider grid + guided flows (unchanged
      content), but tiles show live connected state ("✓ 2 banks
      connected" etc.) fed by `/api/connections`. The step NEVER
      auto-completes: the primary action is an explicit
      **"Done connecting →"** (enabled once ≥1 connection exists;
      before that it's the skip link only). Connecting a provider
      returns you to the grid with its tile now green.
   2. **Sync** — dedicated step, blocking, rich progress: one row per
      connection (institution, spinner/✓/✗, live transaction count)
      plus a total. Failures show ✗ + retry + a "fix connection" link
      back to step 1; **Continue enables when every source has settled
      (ok or failed)** — partial data allowed with a visible warning.
      After the pull rows finish, smart categorization (when an LLM is
      configured) appears as its own phase row: "Categorizing
      merchants… 42/100". Continue waits for it.
   3. **Import history** — gap-aware. Shows what the sync actually
      got per account ("Chase checking ← Apr 12, 2026 — 90 days") and
      pitches CSV/OFX upload specifically to fill older gaps; the
      import UI embeds only when the user takes the offer.
      "Skip — 90 days is fine" is the other path.
   4. **Review recurring** — detected bill/paycheck proposals with
      approve/dismiss (income rows badged). Continue is always
      enabled ("explicit continue, any time") — no forced zeroing of
      the proposal queue.
   5. **Budgets** — the three-field form, pre-filled from synced spend
      + approved income (seeds).
   6. **Finish** — recap of what got set up (accounts, transactions,
      bills/paychecks, budgets) + daily verdict **email opt-in**
      (recipient + hour; only offered when operator SMTP is
      configured). "Go to Today →".
3. **Re-entry**: the green "Finish setup" nav pill persists until every
   step is complete/skipped or the user dismisses; re-entering resumes
   at the first incomplete step. Adding a connection later is an
   Accounts/Settings affair, not the wizard.

## Mechanics / backend work

- **Connected tiles**: ConnectHub queries `/api/connections` (already
  includes `simplefin-org`) and decorates each provider row with its
  live connections (count + institution names + status dot).
- **Sync progress API**: `/api/jobs/sync` gains a background variant —
  POST starts the sweep and returns a job handle; the SPA polls a
  status endpoint returning per-item `{institution, status:
  pending|running|ok|error, transactions}` plus a categorization phase
  `{done, total}`. (Implementation detail open: table-backed job rows
  polled by the SPA is fine; SSE not required.)
- **Categorization progress**: `llm_categorize.categorize_new` reports
  progress (merchants done/total) into the same job status so the
  Sync step can render the phase row.
- **Gap data**: onboarding (or a small `/api/history-coverage`)
  returns per-account `earliest_transaction` dates for the Import
  step's coverage list.
- **Fix**: `log_sync` stamps the `simplefin-org` child items (or
  `_connections.last_ok` coalesces to the bridge item's log) so
  SimpleFIN connections show a real last-sync time.
- **Step done-ness** stays derived from data where possible
  (accounts>0, budgets_set…), but Connect-step completion and skips
  are explicit user actions stored in tenant config
  (`wizard_step_state`), since "done" now means "the user said so".
- The old free-jump pills, the bolted-on "Sync all accounts &
  continue" button, and the silent inline first sync inside the
  SimpleFIN connect flow are all removed — connect flows only claim
  the token/keys and register items; the Sync step owns all pulling.
  (Keep the server-side inline sync for API-driven setups; the wizard
  just doesn't rely on it.)
