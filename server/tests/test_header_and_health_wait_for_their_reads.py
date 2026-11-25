"""The alerts bell and System health claim nothing from a read that failed.

The web bell said "Alerts — nothing needs attention" when its read had
failed; System health kept "all checks pass · auto-refreshes — checked
10:02" while every poll after it failed, so the page reported a healthy
server that was unreachable; on a hosted instance a failed unlinked-records
read redirected to Settings as if there were none; the phone's System
health was blank apart from its Help link when the first read failed.
(specs/screen-states.md)
"""

import pathlib
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


class HeaderAndHealthWait(unittest.TestCase):
    def test_the_bell_claims_nothing_needs_attention_only_from_an_answer(self):
        app = _src(self, _WEB / "App.tsx")
        label = app[app.index("const alertLabel ="):]
        label = label[:label.index(";")]
        self.assertLess(label.index('alertsQ.data === undefined ? "Alerts"'),
                        label.index('"Alerts — nothing needs attention"'))
        # the phone's bell and More badge already say nothing without an
        # answer: the count comes only from the read's rows
        for p in (_MOB / "app" / "(tabs)" / "_layout.tsx",
                  _MOB / "app" / "(tabs)" / "more.tsx"):
            src = _src(self, p)
            self.assertNotIn("nothing needs attention", src, p)
            self.assertIn("unread > 0", src, p)

    def test_health_says_a_failed_poll_and_never_redirects_on_one(self):
        web = _src(self, _WEB / "pages" / "Doctor.tsx")
        self.assertNotIn("String(q.error)", web)
        self.assertIn('<FailedLine q={q} what="the health checks" />', web)
        self.assertIn("<StaleLine query={q} />", web)
        self.assertIn("{updated && !q.isError && <> · auto-refreshes", web)
        hosted = web[web.index("if (me.data?.hosted) {"):]
        hosted = hosted[:hosted.index("<Navigate")]
        self.assertIn("if (unlinked.data === undefined)", hosted)
        self.assertIn('<FailedLine q={unlinked} what="the unlinked records" />',
                      hosted)
        mob = _src(self, _MOB / "app" / "doctor.tsx")
        self.assertIn('<FailedLine q={query} what="the health checks" />', mob)
        self.assertIn("{!query.isError && (", mob)
        hosted = mob[mob.index("if (me.data?.hosted) {"):]
        hosted = hosted[:hosted.index("<Redirect")]
        self.assertIn("if (unlinked.data === undefined)", hosted)
        self.assertIn('<FailedLine q={unlinked} what="the unlinked records" />',
                      hosted)


if __name__ == "__main__":
    unittest.main()
