# Settings page organization (spec)

Settings is otherwise one long undifferentiated scroll of cards. Goal: get
to the setting you want FAST, with everything living inside a named
section.

Decisions:

1. **Sticky jump-link nav** (not tabs, not accordion). Everything stays on
   one scrollable page; a slim sticky bar under the app header holds one
   chip per section. Clicking a chip smooth-scrolls to the section; the
   chip of the section currently in view is highlighted (scroll-spy).
   Nothing is hidden, browser find still works across all settings.
2. **Filter/search box** in the sticky bar as the fast path. Typing filters
   at card granularity — a card matches on its title + a hand-written
   keyword list (e.g. "smtp" hits Email delivery, "totp" hits Two-factor).
   Non-matching cards hide via `display:none` (state preserved — no
   unmount); a section with zero matching cards hides entirely, and its
   nav chip dims. Empty query shows everything.
3. **The shared Budgets+Daily-email form is split into two save buttons.**
   "Save budgets" (food/other/income/dynamic/custom_buckets) lives in the
   Budgets card; "Save email settings" (email_recipients/email_schedule)
   lives in the Daily email card. Same `PATCH`-style `api.settingsSave`
   partial body — no API change.

## Sections (owner view, page order)

> **Superseded in part:** the `budgets` section is gone — all three of its
> cards moved to the Budget page. The row stays here as the record of what
> shipped.

| id          | Section              | Cards |
|-------------|----------------------|-------|
| ~~budgets~~ | ~~Budgets & income~~ | ~~Budgets + custom buckets, Income scenario, Recurring-bill tweaks~~ → |
| email       | Email                | Daily email (recipients/cadence), Email delivery (SMTP) |
| connections | Connections          | Bank & provider keys (Plaid/MX/SimpleFIN) |
| llm         | Smart categorization | LLM endpoint/model |
| planning    | Planning & assets    | Retirement inputs, Property/vehicles & mortgage |
| household   | Household            | Family (members/invites) |
| security    | Security             | Change password, Two-factor, Passkeys, Sessions |
| data        | Data & maintenance   | Maintenance (run jobs now), Your data (exports), Move to a fresh environment, Feedback toggle |

Viewers keep their reduced page, organized the same way: Household +
Security only, same sticky nav + filter.

## Mechanics

- All in `webapp/src/pages/Settings.tsx` + `index.css`; **no API or server
  change** (settingsSave already accepts partial bodies).
- The jump bar is a `div.settings-nav` (NOT a `<nav>` — the global mobile
  CSS pins `nav` to the bottom of the screen). `position: sticky` with a
  JS-measured `top:` offset equal to `header.top`'s height (recomputed on
  resize); sections get the same value as `scroll-margin-top`.
- Scroll-spy: rAF-throttled scroll listener picks the last section whose
  top has passed the sticky offset; no IntersectionObserver ceremony.
- Filtering is plain computed booleans in the parent render (`hit()` on
  title+keywords per card, section shows if any card shows) — no context,
  no registry.
