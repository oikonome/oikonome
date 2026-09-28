"""A restore never rewrites this instance's Plaid Enrich spend.

`plaid_enrich_used[YYYY-MM]` is the running count of rows this instance has
had Plaid Enrich bill for this month, and it is the only thing that holds a
household to its monthly cap. It describes what THIS instance spent, not
anything the household owns. If a restore ZIP could carry it, an old export
would hand back a month's spent allowance, and a hand-edited one with a
negative count would make the cap effectively unlimited — every Enrich after
it billed to whoever owns the Plaid keys.
"""

import csv
import io
import json
import os
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore

from .util import make_db, write_config


def _settings_zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class RestoreEnrichUsageTests(unittest.TestCase):

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_the_archive_s_count_never_replaces_this_instance_s(self):
        write_config(self.conn, plaid_enrich_used={"2026-09": 1800})
        restore.restore_zip(self.conn, _settings_zip(
            {"plaid_enrich_used": {"2026-09": -100000000},
             "theme": "dark"}))
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["plaid_enrich_used"], {"2026-09": 1800})
        self.assertEqual(cfg["theme"], "dark",
                         "the household's own settings still restore")

    def test_an_instance_with_no_count_does_not_inherit_one(self):
        write_config(self.conn)
        restore.restore_zip(self.conn, _settings_zip(
            {"plaid_enrich_used": {"2026-09": 5}}))
        self.assertNotIn("plaid_enrich_used", budget.load_config(self.conn))

    def test_an_export_does_not_carry_the_count(self):
        out = restore.scrub_config({"plaid_enrich_used": {"2026-09": 12},
                                    "theme": "dark"})
        self.assertEqual(out, {"theme": "dark"})

    def test_a_restored_cap_is_held_to_the_ceiling_and_a_bad_one_dropped(self):
        """The cap itself is the household's preference and restores — but
        only inside the range the settings door would have accepted. A
        value that is not a number would otherwise break every later
        Enrich call until someone happened to save settings."""
        from oikonome.web.api import _enrich_cap_ceiling
        write_config(self.conn)
        restore.restore_zip(self.conn, _settings_zip(
            {"plaid_enrich_cap": _enrich_cap_ceiling() * 10}))
        self.assertEqual(budget.load_config(self.conn)["plaid_enrich_cap"],
                         _enrich_cap_ceiling())
        restore.restore_zip(self.conn, _settings_zip(
            {"plaid_enrich_cap": -5}))
        self.assertEqual(budget.load_config(self.conn)["plaid_enrich_cap"], 0)
        restore.restore_zip(self.conn, _settings_zip(
            {"plaid_enrich_cap": "lots"}))
        self.assertEqual(budget.load_config(self.conn)["plaid_enrich_cap"], 0,
                         "a malformed cap leaves the instance's own value")


if __name__ == "__main__":
    unittest.main()
