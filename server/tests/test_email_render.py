"""Smoke test for the shared-template email pipeline (todayview.render_email).

The email HTML is today_body.html plus the CSS inliner, so a template typo
or an inliner regression would otherwise ship silently at 7am. Renders the real gather() output for a fixture tenant
end-to-end and asserts the email-safety invariants.
"""

import datetime as dt
import os
import pathlib
import re
import unittest
from unittest import mock

from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config


class EmailRenderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # these tests assert the tile/plan anatomy, which only the
        # DETAIL face renders — the account opts into it the way a real
        # one does, through the stored setting (default is simple)
        write_config(self.conn, today_view="detail")
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_bill(self.conn, "Paycheck Co", 5000.0, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=2), income=True,
                 last_seen=TODAY - dt.timedelta(days=12))
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 19.99, "TARGET")
        self.d = report.gather(self.conn, TODAY)
        self.subject, self.plain, self.html = report.build(self.d)

    def tearDown(self):
        self.conn.close()

    def test_email_safety_invariants(self):
        """No scripts/SVG/classes/CSS-vars/rem may reach a mail client, and
        no image may ever be FETCHED.

        Images travel inside the message: the rendered HTML embeds them as
        data: URIs and report.send moves each into an inline cid: part. A
        remote src is never allowed — a fetch on open is an open-tracking
        pixel, and it breaks for a LAN self-host behind an image proxy."""
        for forbidden in ("<script", "<svg", "class=", "var(--"):
            self.assertNotIn(forbidden, self.html, forbidden)
        self.assertIsNone(re.search(r"[\d.]+rem\b", self.html), "rem units leaked")
        base = "https://budget.example.test"
        with mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": base}):
            _, _, html = report.build(self.d)
        srcs = re.findall(r'<img[^>]*\bsrc="([^"]*)"', html)
        self.assertTrue(srcs, "fixture should carry the mark and the chart")
        for src in srcs:
            self.assertTrue(src.startswith("data:image/png;base64,"),
                            f"an image must be embedded, not linked: {src[:40]}")
        self.assertNotIn("cid:", html, "cid: refs are made at send time")
        # and the TEMPLATE cannot link an image in any branch — a render of
        # one fixture cannot prove a conditional branch (merchant logos: the
        # pages show them, the email must not)
        tpl = (pathlib.Path(report.__file__).with_name("templates")
               / "today_body.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'<img[^>]*src="(?!\{\{ fc_email_chart \}\})', tpl),
                          "today_body.html may only embed the generated chart")

    def test_email_carries_the_forecast_chart(self):
        """Mail clients strip SVG, so the web chart travels as a PNG of the
        same picture — without it the email's Cash timeline has numbers and
        events but no shape."""
        import base64
        import io
        from PIL import Image
        m = re.search(r'<img src="data:image/png;base64,([^"]+)"[^>]*'
                      r'alt="Cash forecast chart[^"]*"', self.html)
        self.assertIsNotNone(m, "the cash chart image is missing")
        img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
        self.assertEqual(img.size, (1440, 440), "a 2x render of the 720x220 chart")
        self.assertNotIn("<svg", self.html)

    def test_the_email_stays_under_gmails_clip(self):
        """Gmail clips a message whose HTML passes ~102KB behind 'View entire
        message', hiding the cash timeline and everything after it. What
        counts is the HTML on the wire — the images leave it as parts."""
        wire, _ = report.inline_data_images(self.html)
        self.assertLess(len(wire.encode()), 90_000)

    def test_balanced_markup(self):
        for tag in ("table", "tr", "td", "div"):
            self.assertEqual(self.html.count(f"<{tag}"), self.html.count(f"</{tag}>"),
                             f"unbalanced <{tag}>")

    def test_sections_present(self):
        # verdict sentence + left-to-spend-today rows, the "Monthly budget"
        # map, one cash timeline. There is no top-categories footer — the
        # map's bars already say the month, so a footer would be a second
        # answer to the same question.
        for section in ("Daily budget", "Left to spend today",
                        "Monthly budget", "Cash timeline"):
            self.assertIn(section, self.html, section)
        self.assertNotIn("Top categories this month", self.html)

    def test_why_speaks_only_when_over(self):
        """A good day is QUIET — no Driving-it chips, no
        per-category sentences. Over budget: the chips row + the same
        server-composed sentences on the page, the HTML and the plain
        text, so the mirrors cannot drift. Each tile carries its own
        plan-vs-now daily instead of a combined hold sentence."""
        self.assertNotIn("Driving it", self.html)
        self.assertNotIn("Driving it", self.plain)
        # over-budget: chips + sentences on both mirrors
        add_txn(self.conn, TODAY, 4000.0, "YACHT SUPPLY",
                primary="GENERAL_MERCHANDISE")
        d2 = report.gather(self.conn, TODAY)
        _, plain2, html2 = report.build(d2)
        self.assertEqual(d2["verdict"], "OVER BUDGET")
        w2 = d2["why"]
        self.assertIn("Over because", w2["headline"])
        over = next(e for e in w2["entries"] if e["tone"] == "over")
        self.assertIn("YACHT SUPPLY", over["text"])
        for surface in (html2, plain2):
            self.assertIn("Driving it", surface)
            self.assertIn("YACHT SUPPLY", surface)
            self.assertIn(over["text"], surface)
        # Gmail Android keeps display:flex but STRIPS flex-wrap, so an
        # unwrappable chip row runs off the phone screen.
        # The chips must be inline-block — wrapping that works everywhere —
        # and the verdict row floats its month context for the same reason.
        chips_at = html2.index("Driving it")
        self.assertIn("display:inline-block",
                      html2[chips_at:chips_at + 600])
        self.assertNotIn("display:flex",
                         html2[chips_at - 200:chips_at + 600])
        self.assertIn("float:right", html2)

    def test_over_budget_recovery_sentence_reaches_both_email_mirrors(self):
        """month_status composes a way back when over budget ("To get
        back on track over the last N days: …") for the page, the app —
        and the email. The HTML and the plain text must both carry the
        same server-composed sentence — computed but unrendered on the
        email surfaces is exactly the drift the one-source rule exists to
        prevent. A good day stays quiet."""
        self.assertNotIn("To get back on track", self.html)
        self.assertNotIn("To get back on track", self.plain)
        add_txn(self.conn, TODAY, 4000.0, "YACHT SUPPLY",
                primary="GENERAL_MERCHANDISE")
        d2 = report.gather(self.conn, TODAY)
        self.assertEqual(d2["verdict"], "OVER BUDGET")
        recovery = (d2.get("why") or {}).get("recovery")
        self.assertTrue(recovery, "no recovery sentence composed when over")
        _, plain2, html2 = report.build(d2)
        for surface in (html2, plain2):
            self.assertIn(recovery.split(":")[0], surface)

    def test_hero_context_is_built_once_per_send(self):
        """build_context aggregates the whole hero (allowances, bill
        cards, why chips, MTD rollups); the HTML and the plain-text
        mirror of one send must share a single build, not each pay for
        their own."""
        from oikonome.web import todayview
        calls = []
        real = todayview.build_context

        def counting(st):
            calls.append(1)
            return real(st)
        with mock.patch.object(todayview, "build_context",
                               side_effect=counting):
            report.build(self.d)
        self.assertEqual(len(calls), 1,
                         f"hero context built {len(calls)}x for one send")

    def test_each_tile_says_its_plan_vs_now_daily(self):
        """Instead of one combined hold sentence, every
        tile says what its daily budget WAS and what the month's spending
        has moved it to — "daily reduced from $X to $Y" when overspent,
        "raised" when running ahead — identically in the HTML and plain
        text. A bucket spent past its month budget reads "to $0"."""
        add_txn(self.conn, TODAY, 1180.0, "GADGET BARN",
                primary="GENERAL_MERCHANDISE")
        d = report.gather(self.conn, TODAY)
        self.assertEqual(d["verdict"], "OVER BUDGET")
        _, plain, html = report.build(d)
        # other: 1000/31 ≈ $32/day plan, spent past the budget → $0 forward
        for surface in (html, plain):
            self.assertIn("daily reduced from $32 to $0", surface)
            # food (only $30 spent of $1,000) is AHEAD → raised
            self.assertIn("daily raised from $32 to $56", surface)

    def test_note_and_pace_carry_their_polarity_color(self):
        """The pace phrase beside the verdict word and each tile's daily
        note are signals, not captions — they carry the same good/bad
        colors as every sibling signal: pace tinted like the verdict, a
        reduced daily red, a raised one green. Same tinting as the SPA."""
        add_txn(self.conn, TODAY, 1180.0, "GADGET BARN",
                primary="GENERAL_MERCHANDISE")
        d = report.gather(self.conn, TODAY)
        self.assertEqual(d["verdict"], "OVER BUDGET")
        _, _, html = report.build(d)
        red, green = "#e0695d", "#5cb56b"
        self.assertRegex(
            html, rf'<div style="[^"]*color:{red}[^"]*">daily reduced')
        self.assertRegex(
            html, rf'<div style="[^"]*color:{green}[^"]*">daily raised')
        self.assertRegex(
            html, rf'<span style="[^"]*color:{red}[^"]*">\$[\d,]+ ahead of pace')

    def test_carveout_child_note_uses_its_own_numbers(self):
        """A carve-out child's daily note speaks in the CHILD's plan and
        rate, and the parent's note in the carved remainder — never the raw
        parent numbers, which would count the child's dollars twice."""
        from oikonome.engine import budget as _b
        from oikonome.web import todayview
        cfg = _b.load_config(self.conn)
        cfg["custom_buckets"] = [{"name": "Coffee", "parent": "food",
                                  "monthly": 100,
                                  "merchants": ["STARBUCKS"]}]
        _b.save_config(self.conn, cfg)
        self.conn.commit()
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 80.0, "STARBUCKS",
                primary="FOOD_AND_DRINK")
        d = report.gather(self.conn, TODAY)
        tiles = dict(todayview.build_context(d)["day"]["allow_tiles"])
        # child: $100 over 31 days = $3/day plan; $20 left over 17 days = $1
        self.assertEqual(tiles["Coffee"]["daily_note"],
                         "daily reduced from $3 to $1")
        self.assertEqual(tiles["Coffee"]["daily_note_tone"], "neg")
        # parent: the carved remainder ($900 budget, $42.50 own spend) —
        # raw numbers ($1,000 / $122.50) would read "from $32 to $52"
        self.assertEqual(tiles["Food"]["daily_note"],
                         "daily raised from $29 to $50")
        self.assertEqual(tiles["Food"]["daily_note_tone"], "pos")
        # both notes reach the email surfaces verbatim
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("daily reduced from $3 to $1", surface)
            self.assertIn("daily raised from $29 to $50", surface)

    def test_why_names_custom_buckets_as_first_class(self):
        """Every budget category is an entry — food, everything-else AND
        each custom bucket, with the parent showing its remainder so a
        dollar never appears in two sentences."""
        from oikonome.engine import budget as _b
        cfg = _b.load_config(self.conn)
        cfg["custom_buckets"] = [{"name": "Coffee", "parent": "food",
                                  "monthly": 100,
                                  "merchants": ["SAFEWAY"]}]
        _b.save_config(self.conn, cfg)
        self.conn.commit()
        d2 = report.gather(self.conn, TODAY)
        names = [e["name"] for e in d2["why"]["entries"]]
        self.assertIn("Coffee", names)
        self.assertIn("Food", names)
        coffee = next(e for e in d2["why"]["entries"]
                      if e["name"] == "Coffee")
        self.assertIn("SAFEWAY", coffee["text"])
        food = next(e for e in d2["why"]["entries"] if e["name"] == "Food")
        self.assertNotIn("SAFEWAY", food["text"],
                         "the carved-out merchant leaked into the parent")

    def test_pill_follows_the_engine_verdict(self):
        """The email's verdict pill must be the ENGINE's verdict, not a
        second derivation of it.

        A template that recomputes variance-vs-tolerance itself holds only
        until the engine's rule grows a clause it does not know about: with
        the early-month guard in budget.py, the 9am email on the 1st would
        announce UNDER BUDGET while the Today page says ON BUDGET off the
        same gather().
        """
        first = TODAY.replace(day=1)
        d = report.gather(self.conn, first)
        _, plain, html = report.build(d)
        self.assertEqual(d["verdict"], "ON BUDGET")   # early-month guard
        self.assertNotIn("Under budget", html)
        self.assertNotIn("Under budget", plain)
        self.assertIn(f"day {first.day} of {d['days_in_month']}", html)

    def test_grid_cards_stack_single_column(self):
        """Mobile-first (Gmail on Android is the reference client): the
        Today page's side-by-side panes STACK in the email — full-width
        cards, one under the other. Converted as a table, two 49% cells are
        ~170px each on a phone. No grid class and no multi-column td row may
        survive into the mail."""
        self.assertNotIn('"grid', self.html)
        self.assertNotRegex(self.html, r'<td style="vertical-align:top;width:\d+%')
        # the hero card's pieces are present, full width
        self.assertIn("to spend", self.html)                # plan flow line
        self.assertIn("Left to spend today", self.html)     # tiles

    def test_no_flex_gap_reaches_the_mail(self):
        """Gmail supports flex but not `gap` — children jammed together.
        The inliner strips it and re-applies it as child margins."""
        self.assertNotRegex(self.html, r"[^-\w]gap:")
        # the spacing survives as child margins instead (the verdict row)
        self.assertRegex(self.html, r"margin:0 \d+(?:\.\d+)?px \d+(?:\.\d+)?px 0")

    def test_plain_mirror(self):
        state = {"OVER BUDGET": "Over budget", "UNDER BUDGET": "Under budget",
                 "ON BUDGET": "On budget"}[self.d["verdict"]]
        self.assertIn(state, self.plain)
        self.assertIn("LEFT TO SPEND TODAY", self.plain)
        self.assertTrue(self.subject.startswith(("🔴", "🟡", "🟢")))

    def test_brand_header_links_to_instance(self):
        """The clickable brand header — the wordmark — points at
        OIKONOME_BASE_URL's app root (login → Today when signed out); the
        plain text mirrors it as a top 'Oikonome — <url>' line. No tokens or
        magic links, ever."""
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "https://budget.example.test/"}):
            _, plain, html = report.build(self.d)
        self.assertIn('href="https://budget.example.test/app/"', html)
        self.assertIn(">Oikonome</span></a>", html)
        self.assertTrue(plain.startswith(
            "Oikonome — https://budget.example.test/app/"))

    def test_brand_header_without_base_url(self):
        """Self-host without a MAILABLE base URL: the header is NOT a link
        at all.

        A fallback to a relative href="/app/" is pointless in an email
        client, and a LAN base embeds a LAN URL, which is the exact string
        that gets the whole message spam-filtered by Gmail. Linkless is the
        correct rendering there; an instance on a public HTTPS domain keeps
        the link, covered below."""
        env = {k: v for k, v in os.environ.items() if k != "OIKONOME_BASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            _, plain, html = report.build(self.d)
        self.assertIn(">Oikonome</span>", html)
        self.assertNotIn('href="/app/"', html,
                         "a relative link is dead weight in a mail client")
        self.assertTrue(plain.startswith("Daily budget"))

    def test_brand_header_with_lan_base_url_is_linkless(self):
        """A LAN base is treated exactly like no base — never linked."""
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "http://192.168.1.50:8042"}):
            _, plain, html = report.build(self.d)
        self.assertNotIn("192.168.1.50", html)
        self.assertNotIn("192.168.1.50", plain)

    def test_brand_header_with_public_base_url_keeps_the_link(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "https://money.example.org"}):
            _, plain, html = report.build(self.d)
        self.assertIn('href="https://money.example.org/app/"', html)
        self.assertIn(">Oikonome</span></a>", html)

    def test_images_travel_inline_and_are_never_fetched(self):
        """The mark and the chart go out as multipart/related parts —
        inline, no filename, referenced by cid — and the HTML on the wire
        carries neither a data: URI (Gmail and Outlook do not display one)
        nor a remote src (a fetch on open is an open-tracking pixel).
        Proton and Gmail also list these parts among the attachments; that
        is accepted."""
        import smtplib as _smtplib
        sent = {}

        class _FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg): sent["msg"] = msg

        _, plain, html = report.build(self.d)
        with mock.patch.object(_smtplib, "SMTP", _FakeSMTP), \
             mock.patch.dict(os.environ, {
                 "OIKONOME_SMTP_HOST": "smtp.example.test",
                 "OIKONOME_SMTP_FROM": "test@example.test"}):
            report.send("s", plain, html, ["to@example.test"])
        msg = sent["msg"]
        imgs = [p for p in msg.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(len(imgs), 2, "the brand mark and the cash chart")
        for p in imgs:
            self.assertIsNone(p.get_filename(), "an inline part carries no filename")
            self.assertEqual(p.get_content_disposition(), "inline")
        wire = next(p for p in msg.walk()
                    if p.get_content_type() == "text/html").get_content()
        cids = {p["Content-ID"].strip("<>") for p in imgs}
        self.assertEqual(set(re.findall(r'src="cid:([^"]+)"', wire)), cids)
        self.assertNotIn("data:image", wire)
        self.assertIsNone(re.search(r'<img[^>]*src="https?:', wire))

    def test_explicit_attachments_still_work(self):
        """Removing the auto-attached mark must not break the callers that
        genuinely send a file (the doctor bundle, data export)."""
        import smtplib as _smtplib
        sent = {}

        class _FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg): sent["msg"] = msg

        with mock.patch.object(_smtplib, "SMTP", _FakeSMTP), \
             mock.patch.dict(os.environ, {
                 "OIKONOME_SMTP_HOST": "smtp.example.test",
                 "OIKONOME_SMTP_FROM": "test@example.test"}):
            report.send("s", "plain", "<p>hi</p>", ["to@example.test"],
                        attachments=[("bundle.zip", b"PK\x03\x04",
                                      "application/zip")])
        names = [p.get_filename() for p in sent["msg"].walk()
                 if p.get_filename()]
        self.assertEqual(names, ["bundle.zip"])

    def test_postmark_link_tracking_disabled(self):
        """One-time links (password reset / welcome) break when Postmark link
        tracking wraps them, so report.send must force tracking off per-message
        for Postmark hosts — and leave other relays' headers untouched."""
        import smtplib as _smtplib
        def _send_via(host):
            sent = {}
            class _FakeSMTP:
                def __init__(self, *a, **k): pass
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def starttls(self, **k): pass
                def login(self, *a): pass
                def send_message(self, msg): sent["msg"] = msg
            with mock.patch.object(_smtplib, "SMTP", _FakeSMTP), \
                 mock.patch.dict(os.environ, {
                     "OIKONOME_SMTP_HOST": host,
                     "OIKONOME_SMTP_FROM": "test@example.test"}):
                report.send("s", "plain", "<p>hi</p>", ["to@example.test"])
            return sent["msg"]
        pm = _send_via("smtp.postmarkapp.com")
        self.assertEqual(pm["X-PM-TrackLinks"], "None")
        self.assertEqual(pm["X-PM-TrackOpens"], "false")
        other = _send_via("smtp.example.test")
        self.assertIsNone(other["X-PM-TrackLinks"])
        self.assertIsNone(other["X-PM-TrackOpens"])

    def test_dates_are_mmddyy(self):
        """Regression: all dates render MM/DD/YY, never ISO."""
        body = re.sub(r'<[^>]+>', ' ', self.html)
        self.assertIsNone(re.search(r"\b20\d\d-\d\d-\d\d\b", body),
                          "ISO date leaked into the rendered email")


class SimpleFaceTests(unittest.TestCase):
    """The email face is the EMAIL settings' summary/detail choice.

    summary=True renders the verdict pane with the ONE simple number and
    chips; the default (detail) email carries the full tile anatomy, the
    plan flow chain and the sections below. Deliberately DECOUPLED from
    the page's today_view toggle: what lands in the inbox and what the
    page opens to are separate preferences, so these tests pin that
    today_view no longer steers the mail in either direction.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)   # no today_view → the PAGE's simple default
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        self.d = report.gather(self.conn, TODAY)
        # the summary EMAIL — the email_schedule.daily.summary choice
        self.subject, self.plain, self.html = report.build(self.d,
                                                           summary=True)

    def tearDown(self):
        self.conn.close()

    def test_gather_stamps_the_face(self):
        self.assertEqual("summary", self.d["today_view"])

    def test_summary_email_shows_one_number_and_chips_not_tiles(self):
        from oikonome.web.todayview import build_context
        ctx = build_context(self.d)
        simple = ctx["day"]["simple"]
        # the one number is the sum of what each bucket still allows today
        want = sum(max(0.0, a["left_today"])
                   for _, a in ctx["day"]["allow_tiles"])
        self.assertAlmostEqual(simple["left_today"], want, places=2)
        self.assertIn(f"${simple['left_today']:,.0f}", self.html)
        for c in simple["chips"]:
            self.assertIn(c["text"], self.html)
        # per-tile daily wording is detail-only anatomy
        self.assertNotIn("today · nothing spent yet", self.html)
        self.assertNotIn("Savings goals", self.html)

    def test_plain_mirrors_the_simple_line(self):
        from oikonome.web.todayview import build_context
        simple = build_context(self.d)["day"]["simple"]
        line = [l for l in self.plain.splitlines()
                if l.startswith(">> LEFT TO SPEND TODAY")]
        self.assertTrue(line)
        self.assertIn(f"${simple['left_today']:,.0f}", line[0])
        for c in simple["chips"]:
            self.assertIn(c["text"], line[0])
        self.assertFalse([l for l in self.plain.splitlines()
                          if l.startswith("Plan:")],
                         "the plan chain is detail-email anatomy")

    def test_detail_email_ignores_the_page_toggle(self):
        # today_view is summary (setUp default), yet the DEFAULT email is
        # the full detail report — the email face is the email setting
        # alone, never the page's toggle
        _, plain2, html2 = report.build(self.d)
        self.assertIn("today · ", html2)     # per-tile daily wording
        self.assertTrue([l for l in plain2.splitlines()
                         if l.startswith("Plan:")])

    def test_legacy_advanced_spelling_still_maps_for_the_page(self):
        from oikonome.engine import budget
        with budget.config_txn(self.conn) as cfg:
            cfg["today_view"] = "advanced"   # legacy spelling still honoured
        d2 = report.gather(self.conn, TODAY)
        self.assertEqual("detail", d2["today_view"])


if __name__ == "__main__":
    unittest.main()


class EmailMobileWidthTests(unittest.TestCase):
    """The email must fit a phone — nothing runs off to the right.

    The Today page hides its two least-essential columns on a narrow screen
    via a media query, and the email has to drop them outright: a mail
    client on a phone is the NARROWEST viewport this HTML ever renders in,
    and no mail client honours a media query. Five columns, three of them
    white-space:nowrap, inside a 680px shell on a ~380px screen run the row
    straight out of the card.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY, 42.50,
                "A VERY LONG MERCHANT DESCRIPTOR THAT WOULD OVERFLOW A PHONE",
                primary="FOOD_AND_DRINK")
        self.d = report.gather(self.conn, TODAY)
        _, _, self.html = report.build(self.d)

    def tearDown(self):
        self.conn.close()

    def test_hide_m_columns_are_dropped_not_kept(self):
        self.assertNotIn("hide-m", self.html, "classes are inlined away")
        # Each row's OWN cells, nesting-aware: a regex from <tr to the first
        # </tr> runs into a table nested in the row's first cell and counts
        # the inner row's cells as the outer row's.
        # Only rows with visible text can overflow a phone — the cash
        # chart's plot row is dozens of textless cells and is exactly as
        # wide as its container by construction.
        from html.parser import HTMLParser

        class _Rows(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.rows = [], []   # open rows: [cells, text]

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self.stack.append([0, ""])
                elif tag in ("td", "th") and self.stack:
                    self.stack[-1][0] += 1

            def handle_endtag(self, tag):
                if tag == "tr" and self.stack:
                    self.rows.append(self.stack.pop())

            def handle_data(self, data):
                if self.stack:
                    self.stack[-1][1] += data

        p = _Rows()
        p.feed(self.html)
        widest = max(cells for cells, text in p.rows
                     if re.sub(r"\s|\xa0", "", text))
        self.assertLessEqual(
            widest, 4,
            "the transactions/forecast tables carried 5 columns into the "
            "email; the two the web hides on mobile must be dropped")

    def test_tables_cannot_exceed_the_shell(self):
        for t in re.findall(r"<table[^>]*>", self.html):
            if "role=\"presentation\"" in t:
                continue                      # grid-layout tables
            self.assertIn("max-width:100%", t)

    def test_narrow_columns_still_shrink_wrap(self):
        """`width:1%` on the date and amount cells is the shrink-wrap idiom:
        under AUTO table layout it means "only as wide as the content".

        table-layout:fixed takes it literally instead — 1% of the table,
        ~6px against 14px of cell padding — so the nowrap date and amount
        overflow their boxes and paint on top of the payee. The transactions
        table has no <thead>, so under fixed layout its first DATA row sizes
        every column from those 1% widths.

        The two must never ship together again.
        """
        self.assertIn("width:1%", self.html,
                      "fixture should exercise the shrink-wrap idiom")
        # The money-map BAR tables are the opposite case and keep fixed
        # layout on purpose: their cells declare percentage widths that must
        # be taken literally, because those percentages ARE the bar. They are
        # role="presentation"; the data tables are not.
        for t in re.findall(r"<table[^>]*>", self.html):
            if 'role="presentation"' in t:
                continue
            self.assertNotIn(
                "table-layout:fixed", t,
                "a data table went back to fixed layout — its width:1% "
                "cells collapse to ~6px and the nowrap date/amount then "
                "overflow onto the payee")

    def test_a_long_payee_can_wrap(self):
        """nowrap + ellipsis on the forecast payee cell needs a computed
        width no mail client provides, so it pushes the row wider instead of
        truncating."""
        self.assertIn("word-break:break-word", self.html)
        # the payee cell carries a trailing category span (the cash
        # timeline) — match the opening tag + text,
        # which is where a nowrap style would live
        cells = re.findall(r"<td[^>]*>[^<]*A VERY LONG MERCHANT[^<]*",
                           self.html)
        self.assertTrue(cells, "fixture payee should render")
        for c in cells:
            self.assertNotIn("nowrap", c,
                             "a long payee must wrap, not widen the table")


class UnreliableBalanceSuppressesPlainCashTests(unittest.TestCase):
    """todayview drops the whole forecast — SPA card, email card, cash
    notice — when the stored balance is known to be stale, rather than
    present a number seeded from a lie. The plain-text mirror has to drop it
    too, or the one surface with no room for a caveat is the one still
    printing a shortfall."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        self.d = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def _plain(self, unreliable: bool) -> str:
        d = dict(self.d)
        rw = dict(d.get("runway") or {})
        rw["balance_unreliable"] = unreliable
        rw["checking"] = 100.0                     # deeply short →
        rw["required"] = 4000.0                    # the loud CASH: line
        d["runway"] = rw
        return report.build(d)[1]

    def test_no_cash_figure_when_the_balance_is_unreliable(self):
        plain = self._plain(True)
        self.assertNotIn("CASH:", plain)
        self.assertNotIn("Cash gets tight", plain)
        self.assertNotIn("Cash is tight", plain)

    def test_the_same_shortfall_does_speak_when_the_balance_is_good(self):
        self.assertIn("CASH:", self._plain(False),
                      "the guard must not silence a real shortfall")


class OneNameForTheCashQuantityTests(unittest.TestCase):
    """checking − card debt − bills still due has ONE name. A tile saying
    "after cards & bills" over a sentence saying "after cards + bills" is
    the same number under two labels, on the page whose whole job is to say
    one number clearly."""

    def test_no_surface_still_says_cards_plus_bills(self):
        import pathlib
        import oikonome
        root = pathlib.Path(oikonome.__file__).parent
        spa = root.parent.parent / "webapp" / "src" / "pages" / "Today.tsx"
        files = [root / "web" / "report.py",
                 root / "web" / "templates" / "today_body.html"]
        if spa.exists():
            files.append(spa)
        offenders = [f.name for f in files
                     if "after cards + bills" in f.read_text()
                     or "cards + bills" in f.read_text()]
        self.assertEqual([], offenders,
                         "these still use the second spelling of the same "
                         "quantity: " + ", ".join(offenders))


class EmailChartPngTests(unittest.TestCase):
    """emailchart.render — the Today page's cash chart as the email's PNG."""

    def setUp(self):
        start = dt.date(2026, 9, 11)
        days = [(start + dt.timedelta(days=i)).isoformat() for i in range(60)]
        # a paycheck jump on day 10, a dip below $0 on day 40
        self.stmt = [(d, 3000 - 100 * i + (2500 if i >= 10 else 0)
                      - (6000 if i >= 40 else 0)) for i, d in enumerate(days)]
        self.now = [(d, v - 800) for d, v in self.stmt]

    def _img(self, png):
        import io
        from PIL import Image
        return Image.open(io.BytesIO(png))

    def test_a_retina_png_of_the_chart_box(self):
        from oikonome.web import emailchart
        png = emailchart.render(self.stmt, second=self.now,
                                events=[(5, "Card autopay")],
                                mark_x=40, mark_label="runs out 10/21/26")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(self._img(png).size, (1440, 440))

    def test_the_same_forecast_draws_the_same_bytes(self):
        """Deterministic, so an unchanged chart is an unchanged part."""
        from oikonome.web import emailchart
        self.assertEqual(emailchart.render(self.stmt, second=self.now),
                         emailchart.render(self.stmt, second=self.now))

    def test_the_series_colours_are_drawn(self):
        from oikonome.web import emailchart
        img = self._img(emailchart.render(self.stmt, second=self.now)).convert("RGB")
        colours = {c for _, c in img.getcolors(maxcolors=1 << 16)}
        # anti-aliased strokes after the 4x → 2x reduction are blends, so
        # "near" allows for the card colour mixed into a thin line
        near = lambda t: any(sum(abs(a - b) for a, b in zip(c, t)) < 60 for c in colours)
        for name, rgb in (("blue", emailchart.BLUE), ("amber", emailchart.AMBER),
                          ("red", emailchart.RED)):
            self.assertTrue(near(rgb), f"no {name} in the chart")

    def test_nothing_to_draw(self):
        from oikonome.web import emailchart
        self.assertEqual(emailchart.render([]), b"")
        self.assertTrue(emailchart.render(self.stmt[:1]).startswith(b"\x89PNG"))


class PlanFlowAddsUpTests(unittest.TestCase):
    """The plan flow reads income → bills → to spend → excess, and the excess
    is income − avg_bills − food − other − SAVINGS PLAN. A savings term in
    the arithmetic but not on the line lands the chain on a number the terms
    shown cannot produce — off by exactly the savings plan."""

    def test_the_savings_term_is_on_the_line_when_it_is_in_the_sum(self):
        from oikonome.engine import budget, savings
        conn = make_db()
        try:
            # the plan flow line renders on the detail face only
            write_config(conn, today_view="detail")
            with budget.config_txn(conn) as cfg:
                cfg["income_monthly"] = 6000.0
                cfg["savings_goals"] = [{"name": "Vacation", "target": 5000,
                                 "monthly_plan": 400.0}]
            d = report.gather(conn, TODAY)
            plan = float(savings.monthly_plan_total(
                budget.load_config(conn)) or 0)
            self.assertGreater(plan, 0, "fixture must have a savings plan")
            self.assertAlmostEqual(d.get("savings_plan") or 0, plan, places=2)
            plain = report.build(d)[1]
            line = [l for l in plain.splitlines() if l.startswith("Plan:")]
            self.assertTrue(line, "no plan flow line")
            self.assertIn("saved", line[0],
                          "the savings term is subtracted in the surplus but "
                          "missing from the flow: " + line[0])
        finally:
            conn.close()
