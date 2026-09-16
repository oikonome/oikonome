"""The Business page must hide its mutating controls from the viewer role.

Viewers are read-only everywhere; a page that renders write controls for
them offers actions the server will refuse.
"""

import pathlib
import unittest

import oikonome


class BusinessPageViewerGateTests(unittest.TestCase):

    def test_the_business_page_respects_the_viewer_role(self):
        spa = (pathlib.Path(oikonome.__file__).parent.parent.parent
               / "webapp" / "src" / "pages" / "Business.tsx")
        if not spa.exists():
            self.skipTest("webapp/ not present")
        s = spa.read_text()
        # The gate itself, not a spelling of it: the page must consult the
        # shared helper, which fails CLOSED while /api/me is in flight. An
        # inline `meQ.data?.role !== "viewer"` reads TRUE during that window
        # and hands a viewer the write controls for as long as the query
        # takes, which is the failure this file exists to prevent.
        self.assertIn("isViewer(meQ)", s)
        self.assertIn('from "../role"', s)
        self.assertNotIn('meQ.data?.role', s,
                         "no inline role check may come back — unknown is "
                         "not owner, and only the helper says so")
