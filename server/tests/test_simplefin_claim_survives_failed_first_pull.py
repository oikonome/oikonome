"""A SimpleFIN setup token is one-time: the claim spends it and returns the
access URL, which exists nowhere else. The connect doors must save that URL
before the first pull. The failure this protects against: the first 90-day
pull times out or the bridge answers 5xx, nothing was saved, and the person
is left with a spent token, no connection, and "check the token" advice
that sends them to the bridge for a new one."""

import base64
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.web import setup

from .util import make_db, write_config

ACCESS = "https://u:p@bridge.test/simplefin"
TOKEN = base64.b64encode(b"https://bridge.test/claim/invented").decode()


def _claim_ok_pull_fails(request):
    if request.method == "POST":
        return httpx.Response(200, text=ACCESS)
    return httpx.Response(503, text="bridge busy")


class ClaimSurvivesFailedFirstPull(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_access_url_is_saved_when_the_first_pull_fails(self):
        with self.assertRaises(setup.SimplefinFirstPullFailed):
            setup.connect_simplefin(
                self.conn, TOKEN,
                transport=httpx.MockTransport(_claim_ok_pull_fails))
        self.assertEqual(
            sync_base.get_access_token(self.conn, setup.SIMPLEFIN_ITEM),
            ACCESS)
        row = self.conn.execute(
            "SELECT aggregator, status FROM items WHERE id=%s",
            (setup.SIMPLEFIN_ITEM,)).fetchone()
        self.assertEqual(row["aggregator"], "simplefin")
        # marked failing, not archived: the hourly sweep picks it up again
        self.assertNotEqual(row["status"], "archived")

    def test_a_refused_claim_saves_nothing_and_is_not_a_pull_failure(self):
        def refuse(request):
            return httpx.Response(403, text="spent")
        with self.assertRaises(Exception) as cm:
            setup.connect_simplefin(self.conn, TOKEN,
                                    transport=httpx.MockTransport(refuse))
        self.assertNotIsInstance(cm.exception, setup.SimplefinFirstPullFailed)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id=%s",
            (setup.SIMPLEFIN_ITEM,)).fetchone())


if __name__ == "__main__":
    unittest.main()
