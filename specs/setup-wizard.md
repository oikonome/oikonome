# Guided setup wizard (spec)

Decisions:

1. **Full-screen guided wizard** after the /setup account creation —
   before this, a fresh instance dumped the user on a bare Accounts
   page. The existing Today "Welcome" checklist card stays as fallback.
2. **Steps** (in order): Connect accounts → Bring in history (Import) →
   Find recurring bills → Budgets + income. Each step embeds the real
   page (Accounts/Import/Recurring) rather than a reimplementation, so
   nothing the wizard offers is weaker than the full app; the budgets
   step is a purpose-built three-field form (food / everything-else /
   income).
3. **Skippable + resumable.** Every step has Skip; step pills are
   clickable in any order; a green "Finish setup" nav pill offers the
   wizard until every step is done or the user hits "don't show this
   again" (`wizard_done` in tenant config). Step done-ness is derived
   from data (accounts>0, transactions>0, bills>0, budgets_set — the
   /api/onboarding counts), never tracked separately, so work done
   outside the wizard counts too.
4. **Finishing (or fully skipping) lands on Today** — the verdict.

Mechanics: SPA route `/welcome` (Welcome.tsx); `/setup` submit
redirects there (Plaid-keys case included — the wizard's first step is
where the Plaid button lives; the SimpleFIN sync-error case still goes
to the accounts retry form). `/api/onboarding` gains `wizard_done`;
`/api/settings` accepts it. Badges poll onboarding every 5s while the
wizard is open so embedded-page work updates them.
