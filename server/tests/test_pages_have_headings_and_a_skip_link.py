"""A screen reader can find its way around a page by its structure.

Heading navigation found nothing on any phone screen (the shared `H` was
plain text), the sign-in page and the setup wizard had no `<h1>`, the
header and the eleven-item nav came before the page on every visit with
no way past them, and action columns had header cells with no text, so a
cell was announced under nothing. (specs/accessibility.md, requirement 4)
"""

import unittest

from tests.a11y_source import APP, MOB, WEB, elements, sources


class PagesHaveHeadingsAndASkipLink(unittest.TestCase):
    def test_the_phone_heading_is_a_header(self):
        src = (MOB / "components" / "ui.tsx").read_text()
        fn = src[src.index("export function H("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('accessibilityRole="header"', fn)

    def test_the_web_skip_link_comes_first_and_lands_on_main(self):
        src = (WEB / "App.tsx").read_text()
        shell = src.index('<div className={"shell"')
        skip = src.index('<a href="#main" className="skip-link"')
        header = src.index('<header className="top"')
        self.assertLess(shell, skip)
        self.assertLess(skip, header)
        self.assertIn('document.getElementById("main")?.focus()', src[skip:header])
        self.assertIn('<main id="main" tabIndex={-1}>', src)
        css = (WEB / "index.css").read_text()
        self.assertIn(".skip-link:not(:focus){", css)

    def test_sign_in_and_the_wizard_have_a_page_heading(self):
        login = (APP / "server" / "oikonome" / "web" / "templates"
                 / "login.html").read_text()
        self.assertIn('<h1 class="sr-only">Sign in</h1>', login)
        base = (APP / "server" / "oikonome" / "web" / "templates"
                / "base.html").read_text()
        self.assertIn(".sr-only{position:absolute", base)
        wiz = (WEB / "pages" / "Welcome.tsx").read_text()
        page = wiz[wiz.index('<div className="card wiz-card"') - 400:]
        self.assertIn('<h1 className="sr-only">Setup</h1>', page)

    def test_no_web_table_header_cell_is_empty(self):
        bad = [e.where() for p in sources(WEB)
               for e in elements(p, ("th",))
               if not e.text().strip() and not e.has("aria-label")
               and "<input" not in e.children]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
