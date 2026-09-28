"""A restore must never cost this instance a credential it already had.

The export scrubs every credential-shaped value, so a ZIP's copy of the
config is always credential-free. For a flat key that is harmless — the key
is simply absent from the ZIP and the destination's own value survives the
merge. For `llm_backends` it is not: the key IS present (a list of backend
objects), so a whole-value merge replaces the destination's backends with
the scrubbed copies and every API key and extra_body configured here is
gone — smart categorisation silently stops working, on the ordinary
self-host path of exporting a backup and restoring it onto the same
instance.
"""

import csv
import io
import json
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore

from .util import make_db


def _settings_zip(config: dict) -> bytes:
    """A restore ZIP carrying nothing but tenant_settings.csv, shaped the
    way a real export writes it."""
    csv_buf = io.StringIO()
    w = csv.writer(csv_buf)
    w.writerow(["config"])
    w.writerow([json.dumps(config)])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tenant_settings.csv", csv_buf.getvalue())
    return buf.getvalue()


class RestoreKeepsBackendCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _backends(self):
        return budget.load_config(self.conn)["llm_backends"]

    def test_configured_backend_keeps_its_key_and_extra_body(self):
        budget.save_config(self.conn, {
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small", "api_key": "key-fast",
                 "extra_body": '{"tier":"a"}'},
                {"id": "smart", "name": "Smart", "url": "https://b.dev/v1",
                 "model": "big", "api_key": "key-smart"},
            ],
            "food_monthly": 1, "other_monthly": 1})

        # what an export really produces: the same list, scrubbed
        restore.restore_zip(self.conn, _settings_zip({
            "food_monthly": 999,
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small"},
                {"id": "smart", "name": "Smart", "url": "https://b.dev/v1",
                 "model": "big"},
            ]}))

        by_id = {b["id"]: b for b in self._backends()}
        self.assertEqual(by_id["fast"]["api_key"], "key-fast")
        self.assertEqual(by_id["fast"]["extra_body"], '{"tier":"a"}')
        self.assertEqual(by_id["smart"]["api_key"], "key-smart")
        # the non-secret half of the ZIP still lands
        self.assertEqual(budget.load_config(self.conn)["food_monthly"], 999)

    def test_zip_may_still_rename_and_repoint_a_backend(self):
        """Carrying the destination's secret forward must not freeze the
        rest of the entry — the ZIP's non-secret fields still win."""
        budget.save_config(self.conn, {
            "llm_backends": [
                {"id": "fast", "name": "Old", "url": "https://old.dev/v1",
                 "model": "small", "api_key": "key-fast"}],
            "food_monthly": 1, "other_monthly": 1})

        restore.restore_zip(self.conn, _settings_zip({
            "llm_backends": [
                {"id": "fast", "name": "New", "url": "https://new.dev/v1",
                 "model": "large"}]}))

        b = self._backends()[0]
        self.assertEqual((b["name"], b["url"], b["model"]),
                         ("New", "https://new.dev/v1", "large"))
        self.assertEqual(b["api_key"], "key-fast")

    def test_backend_the_destination_never_had_arrives_without_a_key(self):
        """A ZIP entry with no counterpart here has no credential to carry
        forward — it must land as a keyless backend, not fail the restore
        and not inherit some other entry's key."""
        budget.save_config(self.conn, {
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small", "api_key": "key-fast"}],
            "food_monthly": 1, "other_monthly": 1})

        restore.restore_zip(self.conn, _settings_zip({
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small"},
                {"id": "other", "name": "Other", "url": "https://c.dev/v1",
                 "model": "mid"}]}))

        by_id = {b["id"]: b for b in self._backends()}
        self.assertEqual(by_id["fast"]["api_key"], "key-fast")
        self.assertNotIn("api_key", by_id["other"])

    def test_removing_a_backend_in_the_zip_removes_it_here(self):
        """The list is still the ZIP's to define — carrying secrets forward
        must not resurrect an entry the exported config had dropped."""
        budget.save_config(self.conn, {
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small", "api_key": "key-fast"},
                {"id": "gone", "name": "Gone", "url": "https://d.dev/v1",
                 "model": "mid", "api_key": "key-gone"}],
            "food_monthly": 1, "other_monthly": 1})

        restore.restore_zip(self.conn, _settings_zip({
            "llm_backends": [
                {"id": "fast", "name": "Fast", "url": "https://a.dev/v1",
                 "model": "small"}]}))

        self.assertEqual([b["id"] for b in self._backends()], ["fast"])


if __name__ == "__main__":
    unittest.main()
