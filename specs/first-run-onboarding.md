# First-run onboarding

The path a brand-new instance walks: one-time setup link → account → wizard
→ Today. Both roads through the wizard are supported — an aggregator
connection, and a files-only path that imports CSV/OFX history and never
links a bank.

## What the first run has to get right

- **One onboarding, not two.** While `wizard_done` is unset, Today shows a
  single "Resume setup →" card into the wizard; the four-step checklist
  card takes over only once the wizard is done or dismissed (`Today.tsx`).
  Two entry points compete, and the checklist's links bypass the wizard.
- **The setup link is one-time.** A bad token is a 403; reusing a spent one
  redirects to the login page and refuses the POST.
- **Bulk import can create the account it needs.** The files-only path
  proposes account creation ("no existing account matched — creates a new
  one"), so it needs no manual pre-setup.
- **Imported rows get categorized.** The sync-time categorize pass only
  runs after an aggregator sync, so `_categorize_after_import()` (api.py)
  fires the same best-effort `categorize_new` pass in a background thread
  after bulk, single and mapped imports. Without it a files-only first run
  reaches the budgets step with every row uncategorized — an empty Food
  suggestion and no food/other split anywhere.
- **A $0 balance is not a crisis.** Manual accounts created by import start
  at balance 0, which would otherwise make a brand-new instance's first
  Today shout about being out of money tomorrow. When checking reads $0 AND
  every depository account is manual AND transactions exist,
  `runway.balance_unreliable` is set (report.py): the Today cash block
  swaps the alarm for a "set today's checking balance on Accounts" nudge,
  and the shared email context suppresses headroom the same way
  (todayview.py). A live source reporting a real $0 is still believed.
- **The recurring step scans on entry**, so the step opens with proposals
  to confirm rather than a button to hunt for. Coincidental-cadence false
  positives are exactly what the approve/reject gate is for.
- **The budgets step pre-fills income** from the just-approved paycheck.
- **Resume is real**: per-step skip links, an exit-and-resume pill, and a
  resume point that lands on the first incomplete step.

## Deliberately not built

- The single-file import form's "Into account" select offers no "create new
  account" option (it is empty on a fresh instance until the bulk path or
  the Accounts page creates one). Bulk is the promoted path.
- The wizard could ask for the current balance right after a files-only
  import, which would make the nudge unnecessary; the Accounts-page nudge
  is the smaller change.
- The forecast card and the checking-allocation bar still render $0-based
  curves while the balance is unreliable; the nudge in the verdict card
  explains the state.

Tests: `tests/test_onboarding.py` — the unreliable-balance flag set,
cleared and believed, headroom suppression on both surfaces, and the
categorize-hook wiring.
