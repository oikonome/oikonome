# Settings: a section list beside one section (spec)

Settings is organized into named sections, shown as a list beside one
section at a time, with a filter box that searches across all of them.

## Decisions

1. **A section is a route** — `/app/settings/email`. Deep-links, Back works,
   a bookmark survives, and the active section is URL state rather than a
   scroll position. `/app/settings` with no section opens the first one on
   a desktop and the bare list on a phone.
2. **Two panes, not tabs across the top.** Ten or so destinations do not
   fit a tab strip without scrolling it, and a vertical list has room for a
   second line per item (see 4).
3. **Sections stay mounted, hidden** (`display:none`). A half-typed form
   survives switching sections, and search can render matches from every
   section without mounting anything.
4. **Each list item carries its current state**, read off responses the page
   already has — `daily 9am · +1 recipient`, `7 linked`, the tenant's LLM
   model, whatever summary an add-on's own section supplies. **A section
   with nothing honest to say gets no subtitle.** No subtitle may cost a
   request, and none may guess: an empty `llm_model` does not mean "no
   model set", because `llm_categorize._backend` falls back to
   `OIKONOME_LLM_*` and that backend is not in this payload.
5. **Search spans sections.** A card matches on its title + a hand-written
   keyword list (e.g. "smtp" hits Email delivery, "totp" hits Two-factor).
   With a query, every section holding a match renders stacked, and list
   items that matched nothing dim (they stay clickable — a query you are
   about to correct should not move the furniture). Empty query returns
   to one section.
6. **Phones get two screens, not two columns.** The list is the index; a
   section replaces it behind a `‹ All settings` link. Chosen in the
   component, not by shrinking the grid, because 13.5rem + content in 390px
   is neither a list nor a settings page.

## Order

**Connections · Email & Push · Users · Preferences · Data · System ·
Categorization · Merchants · AI · Wizards · Security**, with the account
section an installed add-on owns (if any) slotted in just before
Security. This is the order of attention: where the money comes from, what
the instance sends about it, who else sees it, what is stored and the box
storing it, the engine that sorts it. Wizards sits near the end because
re-running a walkthrough is a rare deliberate act; an add-on's account
section is rarer still. **Security is last on purpose** — it holds the
password, the second factor and the delete button, and the section you
land next to should not be the one that can end the account.
`/app/settings` with no section therefore opens Connections.

## Groupings

| section | holds | why |
|---|---|---|
| **Wizards** | guided setup and the other walkthroughs | one card each is too small to be a destination |
| **Categorization** | LLM settings + rules | both answer "how does a transaction get its category" |
| **System health** | Doctor's check groups, one card each | a single wrapper card reads as one wall |

Old ids redirect rather than 404: `setup`, `planning` and `general` →
`wizards`, `llm` and `rules` → `categorize`, and a `#sec-<id>` hash is
translated to its route on arrival. Wizards' search keywords include
`retirement`/`retire`/`birthdate`/`business`, so those words find the
wizard that owns them.

Every section is 1–3 screens tall on a desktop.

## Mechanics

- `webapp/src/pages/Settings.tsx` (`SECS`, `OLD_SEC`, `SettingsSide`, `Sec`,
  `secOn`), `webapp/src/index.css` (`.setpane`/`.setnav`/`.setbody`),
  and one route in `App.tsx`. No API change.
- Every card id appears in the section-match map, so a search that
  matches a card never hides the section around it.
- `PAGE_TITLES` is an exact-path map, so each section route has its own
  entry.
- The sticky list measures the bar above it. That measurement is `.rt`
  when the rail layout is active and `header.top` on phones — the
  header element is a full-height rail above 760px, so its `offsetHeight` is
  the viewport, not a header height.
