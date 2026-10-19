"""Alerts + Assistant: an alert should offer more than "hide me".

Alerts listed a problem and gave exactly one affordance — dismiss — so the
only supported response to "your bank stopped syncing" was to make the
message go away. Every kind now names where it is fixed. Three date columns
(first seen / last seen / status) collapsed into one honest span.

The Assistant's asker scrolled off after two questions, on the one page whose
whole purpose is asking another, and its example chips disappeared exactly
when someone had learned what it could answer.
"""

import pathlib
import unittest

import oikonome
from oikonome.engine import alerts


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class AlertsPageShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Alerts.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_every_alert_offers_a_way_to_fix_it(self):
        # the verb varies — a queue gets "review", breakage gets "fix this" —
        # but every row still renders a link with one of them
        self.assertIn('KIND_CTA[a.kind] || "fix this"', self.src)
        self.assertIn("KIND_ROUTE[a.kind]", self.src)

    def test_history_rows_route_client_side(self):
        """/alerts/history stores identity columns only — no link — so a
        dismissed alert cannot be resurrected by retargeting it. The map is
        the fallback for those rows."""
        self.assertIn("const KIND_ROUTE", self.src)
        for kind in ("stale-pull", "connection-removed", "reimb", "proposals",
                     "merges"):
            self.assertIn(kind, self.src)

    def test_the_three_date_columns_became_one_span(self):
        self.assertIn("{mmdd(r.first_seen)}", self.src)
        self.assertNotIn("<th>last seen</th>", self.src)


class AlertLinkTests(unittest.TestCase):
    """The live alerts carry their own destination — Today's strip renders it.

    Before this, only `drift` had one, so the alert strip on the page people
    actually read every day was almost entirely unclickable.
    """

    def test_the_kinds_that_have_a_destination_declare_it(self):
        src = pathlib.Path(alerts.__file__).read_text()
        for kind in ("reimb", "setup", "proposals", "merges", "funding",
                     "anomaly", "stale-pull", "connection-removed"):
            self.assertIn(kind, src)
        self.assertGreaterEqual(src.count("link="), 7,
                                "each linkable kind states its own target")

    def test_history_does_not_persist_the_link(self):
        """A stored link would be a second place to keep this map correct,
        and rows already in the table would keep the stale one forever."""
        src = pathlib.Path(alerts.__file__).read_text()
        self.assertNotIn("link", src.split("def history(")[1])


class AssistantShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Assistant.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_box_stays_put_under_the_open_conversation(self):
        """The page is a chat: the open conversation scrolls inside its
        pane and the box to continue it is pinned under the messages,
        never scrolled away on the one page whose purpose is asking the
        next question."""
        self.assertIn('className="chat-compose"', self.src)
        self.assertIn('className="chat-msgs"', self.src)
        css = _src("index.css")
        self.assertIn(".chat-msgs{flex:1;min-height:0;overflow:auto", css)
        self.assertIn(".chat-compose{border-top", css)

    def test_a_conversation_is_continued_in_place_and_is_a_link(self):
        """No "follow up" button: typing into an open conversation continues
        it, and the open one is named in the address (`?c=`) so it can be
        linked and left with the back button."""
        self.assertIn("Continue the conversation", self.src)
        self.assertNotIn('"follow up"', self.src)
        self.assertIn('sp.get("c")', self.src)

    def test_the_prose_is_drawn_not_shown_raw(self):
        """The model writes **bold** and "- " bullets; the page draws them
        (RichText over richtext.ts) and copy strips the markers."""
        self.assertIn("<RichText text={t.answer} />", self.src)
        self.assertIn("plainText(t.answer)", self.src)
        self.assertIn("parseRich", _src("components/AssistantBlocks.tsx"))

    def test_the_examples_do_not_vanish_after_the_first_question(self):
        self.assertNotIn("{log.length === 0 && (", self.src)

    def test_an_answer_can_be_taken_somewhere(self):
        self.assertIn("clipboard?.writeText", self.src)
        self.assertIn("save as report", self.src)

    def test_the_unconfigured_copy_does_not_promise_a_bundled_model(self):
        """Hosted instances have no LLM, and the old copy told those users to
        'run the bundled model' — which is not a thing they can do."""
        self.assertNotIn("or run the bundled model", self.src)
        self.assertIn("/settings/categorize", self.src)
