"""Every form field has a name, and an error says which field it is about.

Every web input, select and textarea needs an accessible name (a label
element, aria-label/aria-labelledby, or Field/fieldProps); without one a
screen reader announces only "edit text" or "combo box". Every phone
TextInput needs an accessibilityLabel (or fieldA11y); a placeholder is
spoken only while the field is empty, so a filled field reads as just
its value plus "text field". Each error is tied to its field. The web's
`components/Field.tsx` (`Field`, `fieldProps`) and the phone's
`components/field.tsx` (`fieldA11y`) are the shared shapes.
(specs/accessibility.md, requirement 8)
"""

import re
import unittest

from tests.a11y_source import APP, MOB, WEB, elements, sources

_SKIP_TYPES = ('"hidden"', '"submit"', '"button"')


def _in_comment(src, e):
    line = src[src.rfind("\n", 0, e.start) + 1:e.start]
    return "//" in line or line.lstrip().startswith("*") or "{/*" in line


class EveryFieldHasAName(unittest.TestCase):
    def test_every_web_field_is_named(self):
        bad = []
        for p in sources(WEB):
            src = p.read_text()
            spans = [(e.start, e.end) for e in elements(p, ("label", "Field"), src)]
            for e in elements(p, ("input", "select", "textarea"), src):
                if e.attr("type") in _SKIP_TYPES or _in_comment(src, e):
                    continue
                if any(a < e.start < b for a, b in spans):
                    continue
                if e.has("aria-label", "aria-labelledby") or "{...fieldProps(" in e.attrs:
                    continue
                fid = (e.attr("id") or "").strip('"{}')
                if fid and re.search(r"htmlFor=\{?\"?" + re.escape(fid), src):
                    continue
                bad.append(e.where())
        self.assertEqual(bad, [])

    def test_the_web_field_wires_label_hint_and_error(self):
        src = (WEB / "components" / "Field.tsx").read_text()
        for want in ('<label htmlFor={id}', '"aria-describedby": describedBy',
                     '"aria-invalid": error ? true : undefined',
                     'errorId ?? `${id}-err`', '{ "aria-label": label,',
                     '{ "aria-invalid": true as const }'):
            self.assertIn(want, src)

    def test_web_errors_point_at_their_field(self):
        for path, marks in (
                ("components/WizardBills.tsx",
                 ['error: errOn === "amount"', '<p id={errId} className="neg"']),
                ("pages/NetWorth.tsx",
                 ['<Field label="Value $" error={!!err && errAt === i} errorId={errId}>',
                  '<Note tone="bad" id={errId}']),
                ("pages/Settings.tsx",
                 ['<Field label="Current password" error={errOn("current")} errorId={errId}>',
                  'bad: true, on: "next" }', '<Note tone={msg.bad ? "bad" : "good"} id={errId}']),
                # a tax row's amount that is not a number: its cell
                ("pages/Import.tsx",
                 ["bad: true, cell: k });", '<Note tone={msg.bad ? "bad" : "good"} id={msgId}',
                  "msg?.cell === `${i}:${f as string}`",
                  "? { error: true, describedBy: [msgId] } : {})}"])):
            src = (WEB / path).read_text()
            for m in marks:
                self.assertIn(m, src, path)
        note = (WEB / "components" / "Note.tsx").read_text()
        self.assertIn("ref={ref as Ref<HTMLDivElement>} id={id}", note)

    def test_the_sign_in_fields_read_the_error(self):
        tpl = (APP / "server" / "oikonome" / "web" / "templates" / "login.html").read_text()
        fields = re.findall(r"<input (?:type=\"(?:email|password)\" )?name=\"(?:email|password|totp_code)\"[^>]*>",
                            tpl)
        self.assertEqual(len(fields), 5)
        for f in fields:
            self.assertRegex(f, r'aria-describedby="(login-2fa-hint )?login-err"')
        js = (APP / "server" / "oikonome" / "web" / "static" / "pages.js").read_text()
        show = js[js.index("var showErr = function (m) {"):]
        show = show[:show.index("\n    };")]
        self.assertIn('[emailEl, pwEl, totpEl].forEach', show)
        self.assertIn('el.setAttribute("aria-invalid", "true")', show)
        self.assertIn('el.removeAttribute("aria-invalid")', show)

    def test_every_phone_field_is_named(self):
        bad = [e.where() for p in sources(MOB)
               for e in elements(p, ("TextInput",))
               if not (e.has("accessibilityLabel") or "{...fieldA11y(" in e.attrs)]
        self.assertEqual(bad, [])

    def test_the_phone_field_shape_and_sign_in(self):
        src = (MOB / "components" / "field.tsx").read_text()
        self.assertIn("return { accessibilityLabel: label, accessibilityHint: error || undefined };",
                      src)
        login = (MOB / "app" / "login.tsx").read_text()
        for name in ('"Email", err', '"Password", err', '"Sign-in code", err'):
            self.assertIn("fieldA11y(" + name + ")", login)


if __name__ == "__main__":
    unittest.main()
