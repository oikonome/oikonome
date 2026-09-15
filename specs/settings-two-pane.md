# Settings: a section list beside one section (spec)

Supersedes the navigation half of `settings-sections.md`. That item
organized Settings into named sections behind a sticky row of jump chips and
a filter box. The sections and the filter were right and are kept. The single
scroll they sat on was what stopped working.

## What was measured first

The owner view of `/app/settings`, at 1440×741:

| | |
|---|---|
| page height | **10,118px — 13.7 screens** |
| cards | 20 |
| form controls | 106 |
| sections | 10 |

Lopsided, in both directions:

| section | height | cards |
|---|---|---|
| System health | 2,517px | 1 (a card wrapping Doctor's own 10) |
| Security | 1,944px | 5 |
| Data & maintenance | 1,415px | 5 |
| Email | 1,235px | 2 |
| Users | 244px | 1 |
| Guided setup | 229px | 1 |
| Planning & assets | 188px | 1 |

A row of chips was the only thing making 13.7 screens navigable, and three of
the destinations it offered were a single small card.

## Decisions

1. **A section is a route** — `/app/settings/email`. Deep-links, Back works,
   a bookmark survives, and the active section is URL state rather than a
   scroll position inferred by a spy listener. `/app/settings` with no
   section opens the first one on a desktop and the bare list on a phone.
2. **Two panes, not tabs across the top.** Nine to ten destinations do not
   fit a tab strip without scrolling it, and a vertical list has room for a
   second line per item (see 4).
3. **Sections stay mounted, hidden** (`display:none`), as already did
   for filtered cards. A half-typed form survives switching sections, and
   search can render matches from every section without mounting anything.
4. **Each list item carries its current state**, read off responses the page
   already has — `daily 9am · +1 recipient`, `7 linked`, the tenant's LLM
   model, whatever summary an add-on's own section supplies. **A section
   with nothing honest to say gets no
   subtitle.** No subtitle may cost a request, and none may guess: the first
   cut said "no model set" whenever `llm_model` was empty, which is false on
   every standard install, because `llm_categorize._backend` falls back to
   `OIKONOME_LLM_*` and that backend is not in this payload.
5. **Search spans sections.** With a query, every section holding a match
   renders stacked, and list items that matched nothing dim (they stay
   clickable — a query you are about to correct should not move the
   furniture). Empty query returns to one section.
6. **Phones get two screens, not two columns.** The list is the index; a
   section replaces it behind a `‹ All settings` link. Chosen in the
   component, not by shrinking the grid, because 13.5rem + content in 390px
   is neither a list nor a settings page.

## Order

**Connections · Email & Push · Users · Preferences · Data · System ·
Categorization · Merchants · AI · Wizards · Security**, with the account
section an installed add-on owns (if any) slotted in just before
Security. The order the sections were built put Wizards first, which is a
fact about the repository, not about anyone using it. This is the order of
attention: where the money comes from, what the instance sends about it,
who else sees it, what is stored and the box storing it, the engine that
sorts it. System sits with Data (both about the box), and Wizards drops
near the end because re-running a walkthrough is a rare deliberate act; an
add-on's account section is rarer still, so it lands last but one.
**Security is last on
purpose** — it holds the password, the second factor and the delete button,
and the section you land next to should not be the one that can end the
account. `/app/settings` with no section therefore opens Connections.

## Regrouping

| before | after | why |
|---|---|---|
| `setup` + `planning` | **Wizards** | 229px and 188px — a destination each for one card |
| `llm` + `rules` | **Categorization** | both answer "how does a transaction get its category" |
| `system` | **System health**, unwrapped | Doctor renders one card per check group; the wrapper made 2,474px read as a single wall |

Old ids redirect rather than 404: `setup` and `planning` → `wizards`, `llm`
and `rules` → `categorize`, and a legacy `#sec-<id>` hash is translated to
its route on arrival.

**Amended.** The merged section briefly held the guided
setup card *and* a read-only table of retirement inputs, and was called
General. The inputs are gone and the section is **Wizards**: the Retire page
already states your Social Security at your claim age and derives age from
the birthdate, and the retirement wizard one click away in this same card is
where both are set — a third copy you could only read was not a setting.
`general` joins the redirect map; it was briefly a real URL. Its
search keywords absorbed `retirement`/`retire`/`birthdate`/`business`, so the
words that used to match the removed table now find the wizard that owns
them instead of the no-hits message.

## Result

Every view is 1–3 screens. The largest is System health at 2.7 (10 cards);
the rest are 1.1–2.2.

## Mechanics

- `webapp/src/pages/Settings.tsx` (`SECS`, `OLD_SEC`, `SettingsSide`, `Sec`,
  `secOn`), `webapp/src/index.css` (`.setpane`/`.setnav`/`.setbody`),
  and one route in `App.tsx`. No API change.
- The sticky list still measures the bar above it. That measurement is `.rt`
  when the rail layout is active and `header.top` on phones — the
  header element is a full-height rail above 760px, so its `offsetHeight` is
  the viewport, not a header height.

## Fixed on the way past

- `merchants` and `deleteacct` were missing from the section-match map, so
  searching "merchant names" or "delete account" matched the card and hid
  the section around it.
- `PAGE_TITLES` is an exact-path map, so the new section routes fell through
  to the bare brand and a bookmarked section had no name.
