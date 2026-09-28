"""the left icon rail.

The rail's correctness lives in relationships BETWEEN two files: a CSS
custom property and the JS that reads it, a padding here and a padding
there. None of it shows up in a unit test of either half, and all of it is
cheap to break by editing one side. These tests assert the relationships
rather than the values, so a deliberate change to one is only allowed when
its partner moves with it.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CSS = ROOT / "webapp" / "src" / "index.css"
APP = ROOT / "webapp" / "src" / "App.tsx"


def _rail_block() -> str:
    """The stylesheet from the rail's own declarations onward. The phone
    layout above it has its own .bar, .rt and .brand, so an unscoped regex
    silently asserts against the wrong rule."""
    css = CSS.read_text()
    return css[css.index("--rail-w:"):]


def _phone_block() -> str:
    """The @media(max-width:760px) rules — the drawer's half of the sheet.
    Brace-matched rather than regexed: the block nests a
    prefers-reduced-motion query, and `[^}]*` would stop at the first inner
    close and assert against a fragment."""
    css = CSS.read_text()
    i = css.index("@media(max-width:760px){")
    j = css.index("{", i)
    depth, k = 1, j + 1
    while depth:
        depth += 1 if css[k] == "{" else -1 if css[k] == "}" else 0
        k += 1
    return css[j:k]


def _nav_handler(app: str) -> str:
    """The rail's click handler — the .bar opening tag and its onClick body.

    It lives on .bar rather than <nav> because the brand is the Home link and
    sits in .topline, outside nav; a handler on nav never saw it, and clicking
    the logo left the panel stuck open.

    Positional, not a regex, and that is deliberate: `<nav[^>]*>` stops dead
    on the `>` in an arrow function, and a non-greedy `.*?\}\}>` stops at
    the first nested brace pair. A pattern anchored on the element's
    attributes also stops matching the day the element gains one more,
    which is the failure mode that makes a source-grep test worthless — it
    asserts nothing and reports success."""
    bar = app.index('<div className="bar"')
    return app[bar:app.index('<div className="topline">', bar)]


def _rem(v: str) -> float:
    """'.55rem' / '22px' → px, at the 16px root."""
    v = v.strip()
    return float(v[:-3]) * 16 if v.endswith("rem") else float(v.rstrip("px"))


class RailGeometry(unittest.TestCase):
    """The rail is one vertical column of icons. Every number that decides
    where that column sits has to agree, or it reads as crooked."""

    def setUp(self):
        # everything from where --rail-w is declared: the base stylesheet
        # carries its own .bar / .rt / .brand for the phone layout, and a
        # regex that finds those instead would assert nothing about the rail
        self.css = _rail_block()

    def test_rail_width_is_derived_from_its_contents(self):
        """A hand-picked rail width leaves the icons off-centre inside it —
        dead space on one side of every glyph, which reads as a bar that is
        too wide. The width must stay bar-padding + item-padding + icon,
        doubled, so the icon keeps equal air on both sides if any of them
        changes."""
        rail = _rem(re.search(r"--rail-w:([\d.]+px)", self.css).group(1))
        bar = _rem(re.search(r"\.bar\{[^}]*?padding:[\d.]+rem ([\d.]+rem)",
                             self.css, re.S).group(1))
        item = _rem(re.search(r"nav a,\.rail-pin\{[^}]*?padding:[\d.]+rem ([\d.]+rem)",
                              self.css, re.S).group(1))
        icon = _rem(re.search(r"\.nav-i\{[^}]*?width:(\d+px)",
                              self.css, re.S).group(1))
        self.assertAlmostEqual(rail, 2 * (bar + item) + icon, places=2)

    def test_the_brand_sits_in_the_same_column_as_the_icons(self):
        """The logo is the Home link, so it is one more icon in the column.
        Its own padding and image box must match the other items', or it
        sits a few pixels off the line they share."""
        brand = re.search(r"\.brand\{[^}]*\}", self.css, re.S).group(0)
        item = re.search(r"nav a,\.rail-pin\{[^}]*\}", self.css, re.S).group(0)
        for prop in ("padding", "gap"):
            b = re.search(rf"[; {{]{prop}:([^;}}]+)", brand).group(1)
            i = re.search(rf"[; {{]{prop}:([^;}}]+)", item).group(1)
            self.assertEqual(b.strip(), i.strip(), prop)
        img = re.search(r"\.brand img\{([^}]*)\}", self.css).group(1)
        icon = re.search(r"\.nav-i\{[^}]*?width:(\d+px);height:(\d+px)",
                         self.css, re.S)
        self.assertIn(f"width:{icon.group(1)}", img)
        self.assertIn(f"height:{icon.group(2)}", img)

    def test_the_top_bar_ends_where_the_content_column_ends(self):
        """sync / bell / help / identity justified to the WINDOW, while main
        is a centred max-width column, leaves the bar overhanging the page
        by the centring slack past the cap. The inset must be computed from
        the same cap main uses, or the two drift."""
        cap = re.search(r"max-width:calc\((\d+)px \+ var\(--col\)\)",
                        self.css).group(1)
        rt = re.search(r"\n  \.rt\{[^}]*\}", self.css, re.S).group(0)
        self.assertRegex(
            rt, rf"padding-right:calc\([^)]*\+ max\(0px,\(100% - {cap}px")

    def test_one_variable_carries_the_rails_layout_width(self):
        """--col is how far the rail pushes the page right, and everything
        that has to line up with the content column reads it: main's padding
        and cap, the top bar's left edge and inset, the banners' margins.
        Written out per rule instead, each copy needs its own
        .shell.rail-pinned override — and a forgotten one is not a crash, it
        is a bar or a banner sitting a rail's width off on a pinned rail
        only, which nobody sees until someone looks at that state."""
        self.assertIn("--col:var(--rail-w)", self.css)
        self.assertIn(".shell.rail-pinned{ --col:var(--rail-open) }", self.css)
        # no rule may re-derive the column from the raw widths: that is the
        # copy that stops tracking. The two declarations above are the only
        # places --rail-w/--rail-open may be read outside the rail's own size.
        for rule in ("main", ".topnote", ".rt"):
            body = re.search(rf"\n  {re.escape(rule)}\{{[^}}]*\}}",
                             self.css, re.S).group(0)
            self.assertNotIn("--rail-w", body, rule)
            self.assertNotIn("--rail-open", body, rule)
            self.assertIn("--col", body, rule)
        # …and therefore no pinned overrides for any of them
        self.assertNotRegex(self.css, r"\.shell\.rail-pinned (main|\.rt|\.topnote)\{")

    def test_the_fixed_top_bar_cannot_cover_the_top_of_the_page(self):
        """.rt is position:fixed, so it takes no space and the shell's first
        child starts at y=0 — under it. Clearance carried in main's own
        margin covers main and nothing else: the app-wide banners render as
        SIBLINGS above main, so each of them lands under the bar with only a
        sliver of its border showing.

        The clearance is the shell's, so it covers every child whatever gets
        added above main next, and it is the bar's own height rather than a
        second copy of that number."""
        # _rail_block() opens at --rail-w, which is inside .shell's own rule,
        # so the first close brace is that rule's end
        shell = self.css[:self.css.index("}")]
        self.assertIn("padding-top:var(--bar-h)", shell)
        rt = re.search(r"\n  \.rt\{[^}]*\}", self.css, re.S).group(0)
        self.assertIn("height:var(--bar-h)", rt)
        self.assertIn("position:fixed", rt)      # why the clearance is needed
        # and main must NOT also carry it, or everything sits a bar too low
        main = re.search(r"\n  main\{[^}]*\}", self.css, re.S).group(0)
        self.assertNotIn("--bar-h", main)

    def test_the_banners_ride_the_content_column(self):
        """The demo countdown, operator broadcasts, the bounce warning and
        the verify nudge sit above main and must line up with the cards
        below them. They cannot borrow main's padding — they are their own
        bordered boxes, and padding would inset the text and leave the border
        behind — so the same two edges are built from margins instead."""
        note = re.search(r"\n  \.topnote\{[^}]*\}", self.css, re.S).group(0)
        for side in ("left", "right"):
            self.assertRegex(
                note,
                rf"margin-{side}:calc\(max\(0px,\(100% - 1180px - var\(--col\)\)/2\)")
        # main's cap is the same 1180 + col, or the centring slack differs
        main = re.search(r"\n  main\{[^}]*\}", self.css, re.S).group(0)
        self.assertIn("max-width:calc(1180px + var(--col))", main)
        # the 72rem base-layout cap must be dropped here, or it wins and the
        # margins have nothing left to place
        self.assertIn("max-width:none", note)

    def test_the_rail_cannot_scroll_sideways(self):
        """overflow-x:hidden still makes a scroll CONTAINER — it only hides
        the bars — and a nav item is far wider than the collapsed rail (its
        label stays in the DOM for screen readers), so focusing one on click
        has the browser scroll it into view and every icon slides off the
        left edge until something resets it. clip hides without a scroll
        port."""
        ns = re.search(r"\.navscroll\{[^}]*\}", self.css, re.S).group(0)
        self.assertIn("min-width:0", ns)   # without it, flex holds max-content
        self.assertIn("width:100%", ns)
        # overflow-x:clip is NOT a substitute here: paired with
        # overflow-y:auto it computes back to hidden
        item = re.search(r"nav a,\.rail-pin\{[^}]*\}", self.css, re.S).group(0)
        self.assertIn("overflow:hidden", item)
        # the brand is the same hazard one element over — it is wider than
        # the rail too, so clicking the logo scrolls it like a nav item
        brand = re.search(r"\.brand\{[^}]*\}", self.css, re.S).group(0)
        for prop in ("width:100%", "min-width:0", "overflow:hidden"):
            self.assertIn(prop, brand, prop)

    def test_the_rail_scrollbar_is_not_drawn(self):
        """A classic scrollbar takes ten of the rail's sixty-odd pixels on a
        short viewport, which shoves every icon off the centre line. It must
        still scroll."""
        bar = re.search(r"\.bar\{[^}]*\}", self.css, re.S).group(0)
        self.assertIn("overflow-y:auto", bar)
        self.assertIn("scrollbar-width:none", bar)
        self.assertIn(".bar::-webkit-scrollbar{", self.css)


class RailHoverContract(unittest.TestCase):
    """.rt is a DOM CHILD of the rail's header — it has to be, since on a
    phone it shares a flex row with the brand — but position:fixed paints it
    somewhere else entirely. Everything here follows from that one fact."""

    def setUp(self):
        self.css = _rail_block()

    def test_the_rail_opens_without_needing_has(self):
        """Hanging the ONLY opening rule off :has() means an engine without
        it drops the selector and the rail never opens at all — an unusable
        nav. Written plainly first and corrected after, the worst case on
        such an engine is only that the top bar opens the rail too."""
        self.assertRegex(
            self.css,
            r"\n  header\.top:is\(:hover,:focus-within\)\{width:var\(--rail-open\)")

    def test_pointing_at_the_top_bar_does_not_open_the_rail(self):
        """hover and focus bubble to the ANCESTOR regardless of where the
        descendant is painted, so without this the sync chip, bell, help and
        identity each sweep the rail open from across the page."""
        self.assertRegex(
            self.css,
            r"header\.top:has\(\.rt:is\(:hover,:focus-within\)\)\{\s*"
            r"width:var\(--rail-w\)")

    def test_an_open_rail_paints_over_the_bar_not_under_it(self):
        """With the rail open the brand's label runs past where the bar
        starts, so the bar's background cuts it off. No z-index can fix it —
        a descendant cannot paint below its own ancestor's background — so
        the bar is clipped out from under the open panel instead."""
        self.assertRegex(
            self.css,
            r"header\.top:is\(:hover,:focus-within\) \.rt\{\s*"
            r"clip-path:inset\(0 0 0 calc\(var\(--rail-open\) - var\(--rail-w\)\)\)")

    def test_every_correction_spares_the_pinned_rail(self):
        """Pinning is an explicit stay-open. A click, or a pointer resting on
        the bell, must never collapse it."""
        for rule in re.findall(r"^  [^\n]*rail-hold[^\n{]*\{", self.css, re.M) + \
                    re.findall(r"^  [^\n]*:has\(\.rt:is[^\n{]*\{", self.css, re.M):
            self.assertIn(".shell:not(.rail-pinned)", rule, rule)

    def test_the_click_collapse_is_not_animated(self):
        """It lands on the same frame as the route's render, and a width
        transition is main-thread, so it freezes half-open for as long as
        that render blocks — which reads as the icons vanishing."""
        rule = re.search(
            r"header\.top\.rail-hold:is\(:hover,:focus-within\)\{[^}]*\}",
            self.css, re.S).group(0)
        self.assertIn("transition:none", rule)

    def test_reduced_motion_is_honoured(self):
        """A panel that slides out from under the pointer on every hover is
        exactly what the setting exists to switch off."""
        self.assertIn("@media (prefers-reduced-motion:reduce){", self.css)
        block = self.css.split("@media (prefers-reduced-motion:reduce){")[1]
        self.assertIn("header.top", block.split("\n  }")[0])


class RailHoldBehaviour(unittest.TestCase):
    """Collapsing the rail after a click has several obvious-looking
    answers that do not work. Each one is pinned here."""

    def setUp(self):
        self.app = APP.read_text()
        self.hold = re.search(r"const holdRail = useCallback\(.*?\n  \}, \[",
                              self.app, re.S).group(0)

    def test_the_hold_is_not_react_state(self):
        """State re-renders the whole app — the route's tree included — on
        every navigation, when that tree is already the most expensive thing
        on the frame, and again on release. Nothing renders from this
        flag."""
        self.assertNotIn("setRailHold", self.app)
        self.assertNotIn("useState(false)", self.hold)
        self.assertIn('classList.add("rail-hold")', self.app)
        self.assertIn('classList.remove("rail-hold")', self.app)

    def test_the_release_is_not_the_headers_mouseleave(self):
        """Circular: the collapse pulls the panel out from under the pointer
        and so fires that very mouseleave, which would clear the hold and
        let :hover reopen the rail. And once collapsed the pointer is
        already outside the header, so no later mouseleave arrives at
        all."""
        self.assertNotIn("onMouseLeave", self.app)
        self.assertIn("mousemove", self.app)

    def test_the_hold_spans_the_whole_open_footprint(self):
        """Releasing when the pointer goes LEFT of the icons treats a return
        to the icons as fresh intent — but that is where the pointer already
        sits after clicking an ICON rather than a label, which is most
        clicks, so the first twitch of the mouse would reopen the panel."""
        move = re.search(r"const move = \(e: MouseEvent\) => \{.*?\};",
                         self.hold, re.S).group(0)
        self.assertIn("e.clientX > railHold.current.open", move)
        self.assertNotIn("clientX <", move)

    def test_the_move_handler_does_no_style_reads(self):
        """getComputedStyle inside it forces a document-wide style
        recalculation on every single pointer event."""
        move = re.search(r"const move = \(e: MouseEvent\) => \{.*?\};",
                         self.hold, re.S).group(0)
        self.assertNotIn("getComputedStyle", move)
        self.assertIn("getComputedStyle", self.hold)      # read once, above
        self.assertIn("{ passive: true }", self.hold)

    def test_the_width_is_read_from_the_css_not_repeated(self):
        """A second copy of 244 in the JS is a copy that silently stops
        matching the rail the day the CSS changes."""
        self.assertIn('getPropertyValue("--rail-open")', self.hold)
        self.assertNotIn("244", self.app)

    def _nav_handler(self) -> str:
        """The rail's click handler. It lives on .bar rather than <nav>: the
        brand is the Home link and sits in .topline, outside nav, so a
        handler on nav never sees it and clicking the logo leaves the panel
        stuck open. Positional rather than a regex — `<nav [^>]*?` stops on
        the `>` of an arrow function, and anchoring on a literal
        `<nav onClick=` breaks the moment the element takes an attribute it
        has no stake in."""
        return _nav_handler(self.app)

    def test_only_a_chosen_destination_collapses_the_rail(self):
        """The handler covers the whole rail, and nav stretches to its
        bottom, so clicking the empty space below the last item would
        collapse the panel as though a destination had been picked."""
        self.assertIn('closest("a[href]")', self._nav_handler())

    def test_the_brand_is_covered_too(self):
        """The logo is the Home link but sits in .topline, outside <nav>, so
        a handler on nav never sees it: clicking it leaves focus behind and
        the panel stuck open. The handler is on .bar, which contains both — and
        excludes .rt, whose links are painted outside the rail and must not
        suppress a later hover over it."""
        handler = _nav_handler(self.app)
        self.assertIn('closest(".rt")', handler)
        # nav must NOT carry its own competing handler any more
        nav_tag = self.app[self.app.index('<nav id="mainnav"'):][:60]
        self.assertNotIn("onClick", nav_tag)

    def test_a_mouse_click_does_not_leave_focus_in_the_rail(self):
        """Otherwise the rail opens itself with the pointer already gone: a
        click leaves focus on the link it activated, :focus-within stays true
        indefinitely, and .rail-hold masks it only while the pointer is over
        the rail — so the panel springs open the moment the hold releases.
        Fixed at the source: mouse activation drops the focus. NOT narrowed
        to :focus-visible, which would make keyboard access depend on that
        plus :has(), neither degrading to anything usable when absent."""
        nav = _nav_handler(self.app)
        self.assertIn(".blur()", nav)
        self.assertIn("e.detail > 0", nav)   # mouse only — see below

    def test_keyboard_activation_never_collapses_the_rail(self):
        """e.detail is 0 for a keyboard activation, which leaves focus inside
        the rail — collapsing the labels out from under someone navigating by
        keyboard trades an annoyance for an accessibility bug."""
        self.assertIn("e.detail > 0", self._nav_handler())

    def test_the_phone_layout_has_no_rail_to_collapse(self):
        """--rail-open only exists above the breakpoint; below it the nav is
        a drawer with its own open state, and the hold must be inert rather
        than comparing pointer positions against NaN."""
        self.assertIn("if (!open) return;", self.hold)

    def test_the_labels_survive_for_a_screen_reader(self):
        """The icon carries no accessible name. Collapsing with display:none
        would silently delete every destination's name from the a11y tree."""
        rule = re.search(r"\.nav-t\{([^}]*)\}", _rail_block()).group(1)
        self.assertIn("opacity:0", rule)
        self.assertNotIn("display:none", rule)
        self.assertNotIn("visibility:hidden", rule)


class PhoneDrawer(unittest.TestCase):
    """The phone half. The rail reveals its labels on :hover, which a touch
    screen does not have, so below 761px the SAME <nav> slides in from the
    left behind a ☰. Everything pinned here follows from two facts: it is
    one element serving two shapes, and while it is open it covers the top
    bar that opened it."""

    def setUp(self):
        self.css = CSS.read_text()
        self.phone = _phone_block()
        self.app = APP.read_text()

    def test_there_is_only_one_nav(self):
        """A second phone-only <nav> is how a destination comes to exist in
        one shape and not the other. The drawer must BE the rail's markup,
        so the destination list has exactly one home."""
        self.assertEqual(self.app.count("<nav "), 1)
        self.assertEqual(self.app.count("</nav>"), 1)
        # one list of destinations, not two
        self.assertEqual(self.app.count('to="/transactions"'), 1)

    def test_the_phone_shows_the_rails_icons(self):
        """The phone strip must not hide `.nav-i`: the icons are what make
        the drawer recognisable as the same navigation as the desktop rail,
        and a `display:none` there erases the whole nav on a phone while
        every desktop viewport still looks right."""
        self.assertNotRegex(self.phone, r"\.nav-i\{[^}]*display:none")
        icon = re.search(r"\n  \.nav-i\{([^}]*)\}", self.phone).group(1)
        self.assertIn("width:22px", icon)      # same box as the rail's

    def test_the_labels_are_up_on_a_phone(self):
        """The rail hides them at opacity:0 until hover. Nothing on a touch
        screen would ever raise them again, so an icon-only drawer would be
        nine unlabelled glyphs with no way to learn them."""
        self.assertRegex(self.phone, r"\.nav-t\{[^}]*opacity:1")

    def test_a_closed_drawer_is_out_of_the_tab_order(self):
        """Parked off-screen it still holds eleven focusable controls.
        transform alone leaves every one of them tabbable, so the first Tab
        from the ☰ walks into destinations nobody can see. visibility is
        what removes them — and it must be DELAYED by the transform's own
        duration, or the panel vanishes instead of sliding out."""
        nav = re.search(r"\n  nav\{.*?\n      [^}]*\}", self.phone, re.S).group(0)
        self.assertIn("visibility:hidden", nav)
        dur = re.search(r"transform ([\d.]+s) ease", nav).group(1)
        self.assertIn(f"visibility 0s {dur}", nav)
        opened = re.search(r"\.shell\.drawer-open nav\{[^}]*\}",
                           self.phone, re.S).group(0)
        self.assertIn("visibility:visible", opened)
        self.assertIn("visibility 0s}", opened)   # no delay on the way IN

    def test_the_drawer_does_not_scroll_the_page_behind_it(self):
        """Flicking past the last destination chains the scroll to the
        document, so the content moves under the scrim and the drawer is
        left floating over a page that shifted for no visible reason."""
        nav = re.search(r"\n  nav\{.*?\n      [^}]*\}", self.phone, re.S).group(0)
        self.assertIn("overscroll-behavior:contain", nav)
        self.assertIn("document.body.style.overflow", self.app)

    def test_the_scrim_cannot_paint_over_the_drawer(self):
        """Otherwise the drawer comes up DIMMED, unreadable behind its own
        scrim. header.top is position:sticky with z-index:5, so it is a
        stacking context and nav's z-index:40 is only ever "40 within the
        header's 5"; a scrim rendered as a sibling of the header at 39 beat
        that 5 outright. The same trap .rt hits one element over. The scrim
        has to live INSIDE the header, where the two compare in one
        context — a relationship no z-index value can express on its own,
        which is why it is pinned as DOM placement."""
        header = self.app[self.app.index('<header className="top"'):
                          self.app.index("</header>")]
        self.assertIn("navscrim", header)
        scrim = re.search(r"\.navscrim\{([^}]*)\}", self.phone).group(1)
        nav = re.search(r"\n  nav\{.*?\n      [^}]*\}", self.phone, re.S).group(0)
        self.assertLess(int(re.search(r"z-index:(\d+)", scrim).group(1)),
                        int(re.search(r"z-index:(\d+)", nav).group(1)))

    def test_the_close_control_lives_inside_the_panel(self):
        """The open drawer covers the top bar, and the scrim sits over it,
        so the ☰ that opened it cannot be pressed again. No z-index fixes
        that: the button is a descendant of header.top, which is its own
        stacking context. The panel carries its own ✕ (and its own Home
        link, the one destination the drawer does not otherwise list)."""
        nav = self.app[self.app.index("<nav "):self.app.index("</nav>")]
        self.assertIn("drawer-close", nav)
        self.assertIn("drawer-brand", nav)
        self.assertIn("navtoggle", self.app[:self.app.index("<nav ")])

    def test_escape_and_the_scrim_return_focus_to_the_button(self):
        """Closing while focus sits on a control that just slid off-screen
        strands the keyboard: the next Tab resumes from a hidden element."""
        for closer in ("Escape", "navscrim"):
            i = self.app.index(closer)
            self.assertIn("burgerRef.current?.focus()", self.app[i:i + 500],
                          closer)

    def test_choosing_the_current_route_still_closes_the_drawer(self):
        """loc.pathname does not change when you pick the page you are
        already on, so the effect that closes on navigation never fires and
        the panel sits open over the answer. The rail's click handler covers
        it — and unlike the rail's collapse it must NOT be gated on e.detail,
        because a keyboard activation closes the drawer too."""
        h = _nav_handler(self.app)
        close = h[h.index("setDrawerOpen"):]
        self.assertNotIn("e.detail", close)
        self.assertIn('closest("a[href]")',
                      h[:h.index("setDrawerOpen")].rsplit("if", 1)[-1] + close)

    def test_the_drawers_chrome_is_off_by_default(self):
        """Declared display:none in the base sheet and switched ON inside
        the phone query — not the other way round. A shell that renders the
        header outside that query then shows a stray ☰ next to a rail that
        already opens on hover."""
        base = self.css[:self.css.index("@media(max-width:760px){")]
        self.assertRegex(base, r"\.navtoggle,\.drawer-head\{display:none\}")

    def test_the_bottom_tab_bar_is_gone(self):
        """Two nav layouts fighting over one element is how a rule survives
        that nobody can explain. The docked strip's rules — the fixed bottom
        bar, its edge fades, and the 4.8rem gutter main carried to clear it
        — must be deleted, not left for the drawer to override."""
        rules = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)   # narrative
        self.assertNotIn("pointer:", rules)
        self.assertNotIn("padding-bottom:4.8rem", rules)
        self.assertNotIn("nav::before", rules)
        self.assertNotIn("nav::after", rules)

    def test_reduced_motion_is_honoured_on_the_phone_too(self):
        """A panel that slides is still a panel that slides. The visibility
        delay has to shrink with the transform it was covering for, or the
        drawer stays tabbable for 180ms after it is switched away."""
        rm = self.phone[self.phone.index("@media (prefers-reduced-motion:reduce){"):]
        self.assertRegex(rm, r"nav\{transition:visibility 0s \.0\ds\}")
        self.assertNotIn("transform .18s", rm.split("\n  }")[0])


if __name__ == "__main__":
    unittest.main()
