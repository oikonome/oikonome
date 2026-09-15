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

    def test_the_asker_stays_put(self):
        self.assertIn('className="card asker"', self.src)
        self.assertIn(".card.asker{position:sticky", _src("index.css"))

    def test_the_examples_do_not_vanish_after_the_first_question(self):
        self.assertNotIn("{log.length === 0 && (", self.src)

    def test_an_answer_can_be_taken_somewhere(self):
        self.assertIn("clipboard?.writeText", self.src)
        self.assertIn("ask a follow-up", self.src)

    def test_the_unconfigured_copy_does_not_promise_a_bundled_model(self):
        """Hosted instances have no LLM, and the old copy told those users to
        'run the bundled model' — which is not a thing they can do."""
        self.assertNotIn("or run the bundled model", self.src)
        self.assertIn("/settings/categorize", self.src)
