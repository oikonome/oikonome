# Accessibility: what the web app and the phone app guarantee

The web app (`webapp/`) and the phone app (`mobile/src`) meet WCAG 2.2
level AA. Each requirement below holds on the web and the phone together,
so the two apps stay alike (see `specs/mobile.md`), and each names the
test that guards it.

## How this is measured

- **Web, measured in a browser.** `scripts/a11y-audit.py` loads each page
  in headless Chromium, once in the dark theme and once in the light. It
  runs axe-core 4.10 with the WCAG 2.0, 2.1 and 2.2 A and AA rules plus
  axe's best-practice rules. It also presses Tab 30 times on each page and
  records whether every stop has a name and paints a focus indicator. It
  checks reflow at a 320 CSS px viewport, which is what 400 % zoom gives
  on a 1280 px window. The tool serves the SPA from a local build
  (`--dist`) and reads the API from a signed-in instance.
  - The tool blocks every non-GET `/api/` request and lists it. No page
    writes on load, so the list stays empty.
  - Its output carries rule ids, colours and CSS selectors with attribute
    values stripped. It never carries element text, so nothing shown on
    screen leaves the browser.
- **Pages covered:**
  - sign-in (`/login`)
  - Today, plus the Month and Year lenses (`/app/?lens=…`)
  - Transactions, Accounts, Bills, Budget, Spending and Cash Flow
  - Net Worth, Business, Settings and Reimburse
  - the setup wizard (`/app/welcome`)
- **Web, read in code.** The source tests read what a page scan cannot see:
  - how dialogs move, trap and return focus
  - controls the keyboard cannot reach
  - chart text alternatives
  - how labels and errors are associated with their fields
  - live regions, reduced motion and font sizing
- **Phone, read in code.** The source tests read every `Pressable`,
  `TextInput`, `Switch`, `Modal` and `Svg` in `mobile/src` and its
  accessibility props. On-device screen-reader order is inferred from
  code, not heard.
- **Contrast, computed.** Every text/background pair is computed from the
  theme tokens with the WCAG 2.x relative-luminance formula
  (`server/tests/test_text_and_fields_have_contrast.py`). The web tokens
  are in `webapp/src/index.css` `:root` and `[data-theme=light]`. The phone
  tokens are in `mobile/src/lib/theme.ts` `DARK` and `LIGHT`. The two sets
  are identical except for the phone's light `btn`.

### What the browser pass holds

On every page above, in both themes: no axe rule fails (`color-contrast`
included), every Tab stop has a name and paints a focus indicator (the
global `:focus-visible` outline), and no page scrolls sideways at 320 px.
Details that keep it so:

- Dark red on a hovered row: the dark `--hover` (and the phone's
  `hover`) is `#1f2833`, so the dark red text reads at 4.50 on it, with
  the text colours untouched.
- Pills: each lays its tint over the card colour itself, so it reads at
  4.5:1 or more on a card, the page and a hovered row.
- Past days on the Bills calendar recede by losing their fill and edge;
  their date reads at full `--mut`.
- The Import page's tax rows: the amount that is not a number marks its
  own cell invalid and points at the message.
- Visually hidden text (`.sr-only`) is pinned to its containing block's
  left edge, so an "Actions" header inside Accounts' sideways-scrolling
  table never widens the page.

## Established patterns

- **Focus.** There is a global `:focus-visible` outline, and every Tab
  stop measured showed it. The icon rail opens on `:focus-within`, so
  keyboard users see its labels.
- **Choice controls on the web.**
  - `SegmentedControl` is a `role="group"` of `<button aria-pressed>`.
  - `RangePicker` always labels itself "Timeframe".
  - Wizard choices are `radiogroup`/`radio` with `aria-checked`.
- **Choice controls on the phone.** `SegmentedControl`, `Select` and the
  wizard `PillRow` carry role, `selected` state, labels and `hitSlop`.
  `TAP = 44` sets the wizard's target size.
- **Page structure.** Landmarks are in place (`header`, one `nav`, one
  `main`), `document.title` is set per route, and `<html lang>` is set.
  The viewport allows zoom.
- **Status regions.** `PageSkeleton`, `Loaded` failures, the error
  boundary, the sync chip and the budget save bar are live regions on
  the web. On the phone, `FailedLine` is an alert and the offline banner
  is a polite live region.
- **Bars that are text.** The money map writes "over by $X" and "$X left",
  so it never relies on colour alone, on either app.
- **Sign-in page.** Its fields have real `<label>`s and correct
  `autocomplete`.
- **Phone modals.** The native `Modal` holds focus, and every `Modal`
  screen has `onRequestClose`.
- **Font scaling.** The phone never disables font scaling, and the main
  transitions respect reduced motion on both apps.
- **The nav drawer.** At phone width the web nav drawer moves focus in
  and back out.

## Requirements

Each requirement holds in the web and the phone alike. None of them
changes a figure: every text alternative reads the figures the server
already sends for the screen.

1. **Glyph controls have names.** WCAG 4.1.2 Name, Role, Value, 2.5.3
   Label in Name.
   - Every ‹ › ✕ ↻ ⋯ control on both apps carries `aria-label` /
     `accessibilityLabel` in plain words and a button role: "Previous
     month", "Next year", "Dismiss". The Home stepper names its unit
     (day, month, year).
   - Every phone `Switch` takes its row's words as its name.
   - Guarded by `server/tests/test_glyph_controls_have_names.py`.
2. **Errors and results are announced.** WCAG 4.1.3 Status Messages,
   2.1.1 Keyboard.
   - One shared web `Note` (`components/Note.tsx`: `role="alert"` for
     errors, `role="status"` for results) carries every save result and
     error. Its dismiss control is a button, which stays out of sight
     until it has focus, so a mouse user sees no change.
   - `#login-err` on the sign-in page is `role="alert"`.
   - On the phone, the shared `Note` in `components/ui.tsx` carries
     `accessibilityRole="alert"` / `accessibilityLiveRegion="polite"`.
   - The copy-it-now panels (tokens, secrets, recovery codes) stay plain,
     so their values are not read aloud unasked.
   - Guarded by `server/tests/test_results_and_errors_are_announced.py`.
3. **Every control is reachable.** WCAG 2.1.1 Keyboard, 2.5.8 Target
   Size (Minimum), 4.1.2.
   - Web: no control is an `<a>` without `href` or a `<tr onClick>`.
     Action links are `button.as-link`, which keeps the anchor's look;
     toggles carry `aria-expanded`; the Cash Flow year row is a button in
     its first cell with `aria-expanded`, and keeps its whole-row click
     for the mouse.
   - Phone: the ✕ that clears `Select` and `DatePicker` is a labelled
     sibling `Pressable` with `hitSlop`, never a `Text onPress` nested in
     the field.
   - Guarded by `server/tests/test_every_control_is_reachable.py`.
4. **Headings and structure.** WCAG 1.3.1 Info and Relationships, 2.4.1
   Bypass Blocks, 2.4.6 Headings and Labels.
   - Phone `H` sets `accessibilityRole="header"`.
   - Web: a skip link to `<main>`; a visually hidden `<h1>` on sign-in
     and the setup wizard (the page already shows where you are); empty
     header cells read "Actions", "Select", "Source" or "Recipient" to a
     screen reader.
   - Guarded by `server/tests/test_pages_have_headings_and_a_skip_link.py`.
5. **Contrast that keeps the look.** WCAG 1.4.3 Contrast (Minimum),
   1.4.11 Non-text Contrast.
   - The footer reads at full `mut` (6.27 dark, 5.21 light).
   - The amber notice's ink is a token (`--note-ink`: the old gold in
     dark, `--amber` in light, 5.10 on its tint).
   - Fields, selects and textareas draw their edge in `--field-line`
     (dark `#6b7682`, light `#7d8894`; 3.0 or more on the card, the page
     and a hovered row); the phone's `field` token is the same colour, on
     every `TextInput` and the `Select` and `DatePicker` boxes.
   - Destructive buttons on both apps fill with `danger` `#b64438` under
     a white label (5.42). The phone lockout button's label is the shared
     `text` on `btn` (4.59).
   - The budget planner's amber segments print their percentage in the
     card colour, so it reads on the darker light amber.
   - The light-theme text colours and the pills are decisions 1 and 2
     below.
   - Guarded by `server/tests/test_text_and_fields_have_contrast.py`,
     which computes every ratio from both apps' theme tokens.
6. **One dialog primitive on the web.** WCAG 2.4.3 Focus Order, 2.1.2 No
   Keyboard Trap.
   - `components/useDialog.ts` is the primitive: focus moves to the first
     field (or the dialog itself), Tab and Shift+Tab wrap inside, Escape
     closes the top dialog only, and closing returns focus to the opener.
   - A modal one makes the rest of the page `inert`, keeping live regions
     and an element marked `data-dialog-keep` (the drawer's scrim)
     outside it. The step-up sheet (`ElevateSheet`) and the open drawer
     are modal; the merchant panel holds focus but is not modal, so the
     list beside it still takes a click.
   - On the phone the sheet comes first in reading order, and its
     backdrop, drawn beneath it, is a "Close" button after it.
   - Guarded by `server/tests/test_dialogs_hold_focus.py`.
7. **Phone touchables say what they are.** WCAG 4.1.2, 2.1.1.
   - Every phone touchable carries a role (`button` unless it is a
     checkbox or a link).
   - `lib/a11y.ts` holds the two shared shapes: `checkbox(checked, label)`
     and `disclosure(expanded, label)`, each naming the control in words
     so its ☐/☑ or ▸/▾ glyph is not read aloud. They are helpers rather
     than wrapper components: the props are the whole difference, and
     every touchable keeps its own layout.
   - `TxnRow` speaks the row in words (marks without their glyphs, the
     amount with its direction), carries a `selected` state while
     selecting, and offers "Select" / "Deselect" as an accessibility
     action.
   - The bill row's press target lies beneath its content as a sibling,
     so its ✕ and ↺ flags are buttons of their own; the content passes
     touches through to the row.
   - Guarded by `server/tests/test_phone_controls_say_what_they_are.py`.
8. **Every field has a name.** WCAG 1.3.1, 3.3.1 Error Identification,
   3.3.2 Labels or Instructions, 4.1.2.
   - Web: `components/Field.tsx` holds `Field`, a visible
     `<label htmlFor>` over its one field with a hint and an error tied by
     `aria-describedby` and `aria-invalid` (or, through `errorId`, a
     form's error banner already on screen), and `fieldProps`, the same
     wiring as attributes for a field whose words sit elsewhere.
   - Every web field has a name: a label element, or an `aria-label` in
     the words on screen (the column header, the text beside it, the
     placeholder when that is all there is), with a row's name where a
     field repeats per row.
   - Errors point at their field on sign-in (the server-rendered page's
     fields read `#login-err` and are marked invalid while it shows), the
     bill wizard (name, amount), Net Worth (the value that is not a
     number) and Change password.
   - Phone: `components/field.tsx` `fieldA11y(label, error)` names every
     `TextInput` from its visible label and speaks its error as the hint;
     it is props, not a wrapper, because every screen lays its field out
     differently. Sign-in and the fields with an error right under them
     (security, settings, account exits, the SimpleFIN token) speak it.
   - Guarded by `server/tests/test_every_field_has_a_name.py`.
9. **Chart text alternatives.** WCAG 1.1.1 Non-text Content, 2.1.1.
   - `chartsummary.ts` (web) and `lib/chart-summary.ts` (phone), one text
     checked by one case table, hold the words. `seriesSentence` says
     where a line starts and ends and its highest and lowest point when
     those are elsewhere ("Net worth: $41,200 in December 2025, $48,900 in
     November 2026; highest $49,300 in October."), dropping the year
     where no other point shares the month; `chartTable` is the "Show
     numbers" table, a long daily run listed by month end;
     `partsSentence` reads the flow picture's nodes.
   - Every figure is a pick from the points the chart was sent, or its
     server `shown` figures, printed with the chart's own format: nothing
     is added up, so the server sends nothing extra.
   - Web: `AreaChart` is a focusable image named by its sentence, and the
     arrow keys (Home, End) walk the same tooltip the mouse moves while a
     live line reads each point; the assistant's bar and line charts do
     the same; the flow picture and the saved-per-month bars speak every
     figure they draw; the rings' drawings are hidden behind their
     legends, with the centre total said first.
   - Phone: the trend lines, the bars, the flow picture and the answer
     charts are one image each, named by the same sentence, with the
     shared `ShowNumbers` under them; the answer ring says its total;
     `Meter` is a progress bar at the width it draws.
   - Each caller names what its chart draws (`subject`).
   - Series colours keep their values (four of the eight phone chart
     colours in `lib/answer-pure.ts` and the web series colours measure
     2.3–3.0 on the light card): every figure is also text.
   - The wording is decision 3.
   - Guarded by `server/tests/test_charts_say_their_figures.py`.
10. **The header fits a narrow window.** WCAG 1.4.10 Reflow, 1.4.12 Text
    Spacing.
    - At phone width (480 px and under, where the user menu is a round
      initial) Feedback and Help fold into the user menu as its first
      items, the sync status may break over two short lines, and the row
      may shrink; the bell stays, because it carries a count.
    - The round icons are one class, `.hdr-round`, sized by `min-width` /
      `min-height` and centred by flex, so wider text spacing grows the
      circle instead of clipping it; the desktop header is unchanged.
    - At 320 px no signed-in page scrolls sideways (the reflow check in
      `scripts/a11y-audit.py`).
    - Guarded by `server/tests/test_the_header_fits_a_narrow_window.py`.
11. **Reduced motion and field focus.** WCAG 2.3.3 Animation from
    Interactions, 2.4.7 Focus Visible, 1.4.11, 2.1.1.
    - Web: the receipt bar and the typing dots stand still under
      `prefers-reduced-motion` (the bar full and dimmed, so it still says
      "working"); `motion.ts` answers the question for script, so the
      Bills scroll jumps (`scrollBehavior()`), the wizard's count shows
      its new number at once and the restart screen's ring stops.
    - A focused field, select, textarea or the assistant's compose box
      draws a 2 px outline beside its blue edge.
    - The assistant's thread rows answer Space as well as Enter.
    - Phone: `useReduceMotion()` in `lib/a11y.ts` follows the system
      setting; every sheet's `Modal` appears instead of sliding
      (`useSheetAnimation()`), and the history import's sweep stops, its
      fill still showing how far it has got.
    - Guarded by
      `server/tests/test_motion_stops_when_asked_and_fields_show_focus.py`.
12. **Phone large text and targets.** WCAG 1.4.4 Resize Text, 2.5.8.
    - Above a text size of 1.3× (`CALENDAR_LIST_SCALE`) the Bills
      calendar lists the days that carry money in words, each a button
      that opens its events as a cell does: "Tue 12: +$1,250 in, $86 out"
      (`calendarDayLine`, `lib/dates.ts`; decision 4). The in and out are
      the server's: `/api/calendar` sends each day's `came_in` and
      `went_out` beside its `total`; an older server's day reads its net,
      the cell's figure.
    - Below the threshold a cell's amount wraps instead of ending in "…",
      and the "Show numbers" tables wrap their figures.
    - Targets reach 44 pt: the segmented control's and the date picker's
      month arrows' `hitSlop`, the sheets' footer buttons (`FOOTER_SLOP`),
      the bell's slop, and both calendars' cells at a 44 pt minimum
      height.
    - The bell's badge sizes by `minHeight`; its count is the one text
      whose growth is capped (`maxFontSizeMultiplier` 1.6), because it
      rides the icon's corner.
    - Guarded by `server/tests/test_phone_text_grows_and_targets_reach.py`
      and `test_calendar_uses_the_household_day.py`.

### Known limit

Web font sizes are fixed px (the body is 15 px, and about 470 px sizes
appear across the CSS and inline styles, some as small as 9–11 px).
Browser zoom works, but a larger default font size set in the browser is
not followed (a WCAG 1.4.4 risk). Moving to rem-based type belongs with a
type-scale change.

## Decisions

1. **Light-theme green, amber, red and blue are darkened just enough.**
   The four light tokens are darkened to pass 4.5:1
   on the card, the page and a hovered row, and the same tokens are used
   for fills, so the palette stays one set. Web and phone alike:
   - green `#2f8f46` → `#287a3c` (5.33 card, 4.87 page, 4.52 hover)
   - amber `#a97b0d` → `#89640b` (5.40, 4.93, 4.57)
   - red `#c44b3e` → `#b64438` (5.42, 4.95, 4.59)
   - blue `#2673b8` → `#246eb1` (5.33, 4.87, 4.52)

   "+$1,250.00 paid" reads as the same green, one step deeper. The dark
   theme's text colours are untouched; dark red text on a hovered row
   reads at 4.50 because the dark `--hover` is a shade deeper.
2. **Dark-theme status pills keep their tint and take a lighter text
   shade.** Each pill measures at least 4.5:1 on its
   own tint over the card:
   - red `#e0695d` → `#ec8277` (3.96 → 5.01)
   - blue `#4f9bd6` → `#69aadc` (4.20 → 5.03)
   - grey `#8b98a3` → `#9aa5af` (4.27 → 5.02)

   Green (4.83) and amber (6.22) already passed and are unchanged. The
   light pills use the light text tokens of decision 1 (4.55 or more on
   the card).
3. **What a chart says aloud.** One sentence (start,
   end, high, low) built from the figures the chart draws, plus a "Show
   numbers" table behind a button (requirement 9).
4. **Phone calendar at very large text.** Above a
   text-size threshold, the month shows as a list of days ("Tue 12:
   +$1,250 in, $86 out") instead of the grid (requirement 12).
