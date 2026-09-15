"""Settings: the list pane is the page's status board.

Its subtitles were the best idea on the page and only four sections had one —
Connections, Email, Categorization and Plan carried state; Users, Data,
System, Wizards and Security showed a bare label. So the pane answered "what
is my setup?" for half of itself, and two real problems (a dead connection,
2FA off) were reachable only by opening the section that owned them.

Rows were forms first: a cadence was a control cluster you had to read to
learn its current value.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class SettingsNavSummaryTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Settings.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_every_section_can_state_its_own_state(self):
        for sec in ("household", "data", "system", "wizards", "security"):
            self.assertRegex(self.src, rf"\n    {sec}: ",
                             f"{sec} showed a bare label")

    def test_a_subtitle_can_state_a_problem(self):
        self.assertIn("warn: true", self.src)
        self.assertIn(".setnav-s.warn", _src("index.css"))

    def test_the_problems_it_can_state(self):
        self.assertIn("needs attention", self.src)   # a dead connection
        self.assertIn("2FA off", self.src)
        self.assertIn("not arriving — paused", self.src)

    def test_the_summaries_reuse_cached_queries_not_new_requests(self):
        """Every key here is one a card inside the page already fetches, so
        React Query serves it from cache."""
        for key in ('["members"]', '["invites"]', '["passkeys"]',
                    '["onboarding"]', '["doctor"]'):
            self.assertGreaterEqual(self.src.count(key), 1)

    def test_doctor_is_not_fetched_where_the_section_does_not_exist(self):
        """System health is operator-owned on hosted, and viewers have no
        section at all — asking anyway would be a guaranteed 403."""
        self.assertRegex(
            self.src,
            r'queryFn: api\.doctor,\s*\n\s*enabled: !meQ\.data\?\.hosted\s*\n?'
            r'\s*&& isOwner\(meQ\)')


class SettingsRowShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Settings.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_a_cadence_reads_label_value_change(self):
        self.assertIn('className="setrow"', self.src)
        self.assertIn('className="lab"', self.src)
        self.assertIn('className="val"', self.src)
        self.assertIn('"change"', self.src)
        self.assertIn(".setrow{", _src("index.css"))

    def test_the_checkbox_matrix_is_gone_but_every_channel_survives(self):
        """The per-cadence email/SMS/push choice is the feature; the
        matrix was only its resting state."""
        self.assertNotIn("<th>Summary</th>", self.src)
        self.assertIn("const channels = (cad: Cad)", self.src)
        for ch in ('"email"', '"SMS"', '"push"'):
            self.assertIn(ch, self.src)

    def test_smtp_collapses(self):
        """An override most installs never touch, sitting as a full card of
        six fields above the recipients it serves."""
        self.assertIn("<details className=\"card\" style={{ marginTop: \"1rem\" }}\n"
                      "             open={!!data.smtp_host}>", self.src)
        self.assertIn("using the operator's OIKONOME_SMTP_* settings", self.src)

    def test_removing_a_recipient_is_named_not_a_glyph(self):
        self.assertNotIn(">×</button>", self.src)
        self.assertIn(">remove</button>", self.src)


class RecipientDraftTests(unittest.TestCase):
    """Email settings AUTO-SAVE: a change is the save.
    The old guard here protected the Save-button flush of a typed-but-
    never-Added address; with no Save button that machinery is gone, and
    the invariants are: changes post as they happen carrying the complete
    next form, a lingering draft says so ON SCREEN instead of being
    silently discarded, and Add still validates before committing.
    """

    def setUp(self):
        try:
            self.src = _src("pages/Settings.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_every_change_saves_itself_with_the_full_next_form(self):
        self.assertIn(
            "onChange={(recipients) => applyEmail({ ...form, recipients })}",
            self.src)
        self.assertIn(
            "onChange={(sched) => applyEmail({ ...form, sched })}", self.src)
        self.assertIn("saveEmail.mutate(next)", self.src)
        # the flush-on-save machinery must not linger half-wired
        self.assertNotIn("flushRecipient", self.src)

    def test_a_lingering_draft_is_visible_not_silently_dropped(self):
        self.assertIn("Not added yet", self.src)

    def test_add_still_validates_before_committing(self):
        add_body = self.src[self.src.index("const add = () => {"):
                            self.src.index("const rows = [")]
        self.assertIn("That doesn't look like an email address.", add_body)
