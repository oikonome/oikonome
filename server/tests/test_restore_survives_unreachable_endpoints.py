"""An export restores even when the endpoints inside it are unreachable here.

An export ZIP carries the tenant's settings, and settings name endpoints:
the AI backends, an SMTP relay. Those are the most instance-LOCAL thing an
archive holds — a self-hoster's bundled model lives at
`http://ollama:11434` on a container network, or on a private LAN address —
so from anywhere else they either fail to resolve or resolve somewhere the
SSRF guard refuses. Both are correct refusals to *use* that endpoint. What
they must not be is a refusal to restore a large ledger — an upload
answering "the AI backend 'lanbox' in this file host does not resolve" and
restoring nothing at all.

So: unusable endpoints are dropped, any role pointing at one falls back to
the instance's own default, the person is told, and their
ledger lands. A MALFORMED payload is still refused — that is a corrupt
file, not an unreachable one.
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

UNRESOLVABLE = "http://ollama:11434/v1"


def _zip(config: dict, txns: list[dict] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
        if txns:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=list(txns[0].keys()))
            w.writeheader()
            w.writerows(txns)
            z.writestr("transactions.csv", s.getvalue())
    return buf.getvalue()


class UnreachableEndpointTests(unittest.TestCase):

    def setUp(self):
        # hosted is where "does not resolve" is a refusal at all
        os.environ["OIKONOME_HOSTED"] = "1"
        self.addCleanup(os.environ.pop, "OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_unreachable_backend_is_dropped_and_the_ledger_still_lands(self):
        data = _zip({"llm_backends": [{"id": "lanbox", "url": UNRESOLVABLE,
                                       "model": "lanbox-model:30b"}],
                     "llm_roles": {"categorize": "lanbox", "vision": "bundled"}})
        counts = restore.restore_zip(self.conn, data)
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["llm_backends"], [])
        notes = " ".join(counts.get("_notes") or [])
        self.assertIn("lanbox", notes)
        # the role that pointed at it no longer routes nowhere: it is
        # unset, so this instance's own default answers for it
        self.assertNotIn("categorize", cfg["llm_roles"])
        self.assertEqual(cfg["llm_roles"]["vision"], "bundled")
        self.assertIn("categorize", " ".join(counts.get("_notes") or []))

    def test_a_reachable_backend_beside_it_survives(self):
        data = _zip({"llm_backends": [
            {"id": "lanbox", "url": UNRESOLVABLE, "model": "m"},
            {"id": "cloud", "url": "https://93.184.216.34/v1", "model": "m"}],
            "llm_roles": {"categorize": "cloud"}})
        restore.restore_zip(self.conn, data)
        cfg = budget.load_config(self.conn)
        self.assertEqual([b["id"] for b in cfg["llm_backends"]], ["cloud"])
        self.assertEqual(cfg["llm_roles"]["categorize"], "cloud")

    def test_unreachable_smtp_host_is_dropped_not_fatal(self):
        counts = restore.restore_zip(self.conn, _zip(
            {"smtp_host": "mail.lan", "smtp_port": 587}))
        self.assertNotIn("smtp_host", budget.load_config(self.conn))
        self.assertIn("mail server",
                      " ".join(counts.get("_notes") or []))

    def test_a_malformed_backend_list_is_still_refused(self):
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, _zip({"llm_backends": "nonsense"}))
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, _zip({"llm_backends": [
                {"id": "bundled", "url": "https://x.example/v1",
                 "model": "m"}]}))

    def test_nothing_is_reported_when_everything_survives(self):
        counts = restore.restore_zip(self.conn, _zip({"llm_backends": [
            {"id": "cloud", "url": "https://93.184.216.34/v1",
             "model": "m"}]}))
        self.assertNotIn("_notes", counts)


if __name__ == "__main__":
    unittest.main()
