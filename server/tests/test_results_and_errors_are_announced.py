"""A result or an error that appears on screen is heard, not only seen.

About fifty web banners ("note" / "note bad") and the phone's "msg && …"
lines appeared with no live region, so a screen reader stayed silent on a
failed save or a wrong password; the web dismissed most of them by a click
on the banner itself, which no keyboard can make. Both apps now say these
through one shared piece: the web `Note` (role alert for an error, status
otherwise, a real Dismiss button) and the phone `Note` (alert role for an
error, a polite live region otherwise). The shared failed and stale lines
go through it too, and the server-rendered sign-in error is an alert.
(specs/accessibility.md, requirement 2)
"""

import re
import unittest

from tests.a11y_source import (APP, MOB, WEB, elements, guarded_by_message,
                               sources)

_NOTE_CLASS = re.compile(r'className=(?:"note[\s"]|\{["`]note)')


def _web_notes():
    for p in sources(WEB):
        if p.name == "Note.tsx":
            continue
        src = p.read_text()
        for e in elements(p, ("div", "p", "span"), src):
            if _NOTE_CLASS.search(e.attrs):
                yield src, e


class ResultsAndErrorsAreAnnounced(unittest.TestCase):
    def test_the_web_note_is_a_live_region_with_a_real_dismiss(self):
        src = (WEB / "components" / "Note.tsx").read_text()
        self.assertIn('role={tone === "bad" ? "alert" : "status"}', src)
        self.assertRegex(src, r'<button type="button" className="note-x" '
                              r'aria-label="Dismiss"')
        css = (WEB / "index.css").read_text()
        # hidden until it has focus, so the look does not change
        self.assertIn(".note-x:not(:focus){position:absolute", css)

    def test_no_web_banner_is_dismissed_by_a_click_on_the_banner(self):
        bad = [e.where() for _src, e in _web_notes() if e.has("onClick")]
        self.assertEqual(bad, [])

    def test_every_web_banner_shown_for_a_result_is_a_note(self):
        bad = [f"{e.where()} under {guarded_by_message(src, e)}"
               for src, e in _web_notes() if guarded_by_message(src, e)]
        self.assertEqual(bad, [])

    def test_the_shared_failed_and_stale_lines_are_notes(self):
        for path in (WEB / "components" / "Loaded.tsx",
                     MOB / "components" / "loaded.tsx"):
            src = path.read_text()
            for fn in ("export function FailedLine", "export function StaleLine"):
                body = src[src.index(fn):]
                body = body[:body.index("\n}\n")]
                self.assertIn("<Note", body, f"{path.name} {fn}")
        web = (WEB / "components" / "Loaded.tsx").read_text()
        self.assertIn('<Note as="p" tone="bad">', web)

    def test_the_sign_in_error_is_an_alert(self):
        html = (APP / "server" / "oikonome" / "web" / "templates"
                / "login.html").read_text()
        self.assertRegex(html, r'<div id="login-err" class="note" role="alert"')
        web = (WEB / "pages" / "Login.tsx").read_text()
        self.assertNotIn('className="note bad"', web)
        self.assertIn('<Note tone="bad">{error}</Note>', web)
        phone = (MOB / "app" / "login.tsx").read_text()
        self.assertIn("{err && <Note bad style={s.err}>{err}</Note>}", phone)

    def test_the_phone_note_is_an_alert_or_a_live_region(self):
        src = (MOB / "components" / "ui.tsx").read_text()
        fn = src[src.index("export function Note("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('accessibilityRole={bad ? "alert" : undefined}', fn)
        self.assertIn('accessibilityLiveRegion={bad ? "assertive" : "polite"}', fn)

    def test_every_phone_line_shown_for_a_result_is_a_note(self):
        bad = []
        for p in sources(MOB):
            src = p.read_text()
            for e in elements(p, ("Text",), src):
                var = guarded_by_message(src, e)
                if not var or e.has("accessibilityRole"):
                    continue
                # a control drawn beside the message is not the message
                if "{" in e.children and not re.search(
                        r"\b" + re.escape(var) + r"\b", e.children):
                    continue
                bad.append(f"{e.where()} under {var}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
