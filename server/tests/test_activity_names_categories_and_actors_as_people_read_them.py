"""The activity log reads as the rest of the app does. A category is named the
way the Categorization pane names it ("Transportation", "General Services"),
never as the stored key ("TRANSPORTATION") — for rows written from now on and
for rows already in the log. And an edit made by a maintenance script, with no
person behind it, reads as a script: the "Who" filter must not offer a name
that belongs to nobody in the household."""
import unittest

from oikonome.engine import activity

from .util import make_db, write_config


class ActivityWords(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _raw(self, actor, summary, detail, kind="rule", action="set"):
        from oikonome.engine.compat import jsonb
        self.conn.execute(
            """INSERT INTO activity_log (actor, kind, action, target, label,
                                         summary, detail)
               VALUES (%s, %s, %s, NULL, 'Pine Motors', %s, %s)""",
            (actor, kind, action, summary, jsonb(detail)))

    def test_a_new_rule_row_names_the_category(self):
        activity.record(self.conn, "owner@example.dev", "rule", "set",
                        label="Pine Motors",
                        detail={"after": "TRANSPORTATION", "count": 3})
        row = activity.list_activity(self.conn)["rows"][0]
        self.assertEqual(row["summary"],
                         "set the rule Pine Motors → Transportation (3 rows)")

    def test_a_row_already_in_the_log_reads_with_the_names_too(self):
        self._raw("owner@example.dev",
                  "changed the category of Pine Motors $12.00 on 01/02/26 "
                  "from GENERAL SERVICES to MEDICAL",
                  {"before": "GENERAL_SERVICES", "after": "MEDICAL"},
                  kind="category", action="set")
        row = activity.list_activity(self.conn)["rows"][0]
        self.assertEqual(row["summary"],
                         "changed the category of Pine Motors $12.00 on "
                         "01/02/26 from General Services to Medical")

    def test_a_name_somebody_typed_passes_through(self):
        activity.record(self.conn, "owner@example.dev", "rule", "set",
                        label="Pine Motors", detail={"after": "Car stuff"})
        row = activity.list_activity(self.conn)["rows"][0]
        self.assertIn("→ Car stuff", row["summary"])

    def test_an_edit_with_no_person_behind_it_reads_as_a_script(self):
        self._raw("maintenance-bot", "renamed the merchant A to B", {},
                  kind="merchant", action="renamed")
        activity.record(self.conn, "owner@example.dev", "rule", "set",
                        label="Pine Motors", detail={"after": "MEDICAL"})
        page = activity.list_activity(self.conn)
        actors = page["actors"]
        self.assertIn(activity.MAINTENANCE, actors)
        self.assertNotIn("maintenance-bot", actors)
        self.assertIn("owner@example.dev", actors)
        mine = [r for r in page["rows"] if r["kind"] == "merchant"]
        self.assertEqual(mine[0]["actor"], activity.MAINTENANCE)
        # the filter the "Who" picker sends finds those rows, and only them
        only = activity.list_activity(self.conn, actor=activity.MAINTENANCE)
        self.assertEqual([r["kind"] for r in only["rows"]], ["merchant"])

    def test_a_script_token_and_a_person_keep_their_own_names(self):
        activity.record(self.conn, {"email": "script:collector",
                                    "script_token": True},
                        "note", "added", label="Pine Motors",
                        detail={"after": "hi"})
        self.assertEqual(activity.list_activity(self.conn)["rows"][0]["actor"],
                         "script:collector")


if __name__ == "__main__":
    unittest.main()
