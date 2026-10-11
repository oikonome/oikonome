"""The categorizer's run record never stores the endpoint's credential.

The record lives in job_progress, which is bookkeeping that archives and
support bundles can carry. A backend URL with a password or an api key in
it is masked as the record is written, and a record written unmasked
earlier is masked on the way out.
"""

import json
import unittest

from oikonome.engine import llm_categorize

from .util import make_db

URL = "https://reader:hunter2-fixture@models.example.dev/v1?api_key=fixture-key"


class RunRecordMasksTheEndpoint(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _stored(self):
        row = self.conn.execute(
            "SELECT progress FROM job_progress WHERE id='llm'").fetchone()
        return json.dumps(row["progress"])

    def test_the_written_record_holds_no_credential(self):
        llm_categorize.record_run(self.conn, {"merchants_classified": 1},
                                  {"url": URL, "model": "m"})
        stored = self._stored()
        self.assertNotIn("hunter2-fixture", stored)
        self.assertNotIn("fixture-key", stored)
        self.assertIn("models.example.dev", stored)

    def test_an_older_unmasked_record_is_masked_when_read(self):
        self.conn.execute(
            "INSERT INTO job_progress (id, state, progress) "
            "VALUES ('llm', 'done', %s::jsonb)",
            (json.dumps({"url": URL, "produced": 1,
                         "error_classified": True}),))
        last = llm_categorize.last_run(self.conn)
        self.assertNotIn("hunter2-fixture", last["url"])
        self.assertNotIn("fixture-key", last["url"])


if __name__ == "__main__":
    unittest.main()
