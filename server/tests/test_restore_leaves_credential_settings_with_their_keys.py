"""A restore never re-pairs this instance's aggregator keys with another
instance's environment or aggregator user.

An archive never carries aggregator keys, so the destination keeps its own.
The settings that DESCRIBE a set of keys — the environment they belong to,
the MX user they created — must stay with them too. Letting the archive's
copies in would send this instance's MX keys after another client's user
(every MX call then fails) and this instance's production Plaid keys to the
sandbox host.
"""

import csv
import io
import json
import os
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import mx, plaid, restore

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


ARCHIVE = {"mx_env": "sandbox", "mx_user_guid": "USR-from-elsewhere",
           "plaid_env": "sandbox", "theme": "dark"}


class RestoreCredentialCompanionTests(unittest.TestCase):

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_own_keys_keep_their_own_environment_and_user(self):
        write_config(self.conn, mx_client_id="mine", mx_api_key="k",
                     mx_env="production", mx_user_guid="USR-mine",
                     plaid_client_id="mine", plaid_secret="s",
                     plaid_env="production")
        restore.restore_zip(self.conn, _settings_zip(ARCHIVE))
        c = mx.creds(self.conn)
        self.assertEqual((c["env"], c["user_guid"]),
                         ("production", "USR-mine"))
        _, _, url = plaid.credentials(self.conn)
        self.assertEqual(url, plaid.ENV_URLS["production"])
        self.assertEqual(budget.load_config(self.conn)["theme"], "dark",
                         "the household's own settings still restore")

    def test_an_instance_without_them_does_not_inherit_them(self):
        write_config(self.conn)
        restore.restore_zip(self.conn, _settings_zip(ARCHIVE))
        cfg = budget.load_config(self.conn)
        for k in ("mx_env", "mx_user_guid", "plaid_env"):
            self.assertNotIn(k, cfg)
        # keys entered after the restore start their own MX user
        write_config(self.conn, mx_client_id="new", mx_api_key="k")
        self.assertIsNone(mx.creds(self.conn)["user_guid"])


if __name__ == "__main__":
    unittest.main()
