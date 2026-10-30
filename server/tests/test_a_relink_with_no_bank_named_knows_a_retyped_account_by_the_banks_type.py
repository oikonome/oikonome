"""A relink Plaid did not name a bank for still knows a retyped account.

When a relink's new Item comes back without an institution (a transient
lookup failure), the accounts are the evidence it is the same login: every
masked account the new login reports must be held by the Item being
replaced. A held account the person retyped by hand must be compared by
the bank's own type, which the feed keeps in the account's raw record —
compared by the hand-set type it never matches, the old Item is kept, and
both copies of every account sync side by side. A held account whose bank
type is unknown vouches for nothing, so it can only keep the old Item.
"""

import json
import unittest
import uuid

import httpx

from oikonome.engine.compat import jsonb
from oikonome.sync import plaid

from .util import make_db, write_config


def _acct(mask, kind="credit"):
    return {"account_id": f"a-{uuid.uuid4().hex[:8]}", "name": "Card",
            "type": kind, "subtype": "credit card", "mask": mask,
            "balances": {"current": 10.0}}


class SameAccountsByTheBanksType(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status,"
            " access_token) VALUES ('old-item','plaid','Contoso Bank',"
            "'ITEM_LOGIN_REQUIRED','tok-old')")

    def _held(self, feed_type):
        raw = {"type": feed_type} if feed_type else None
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask,"
            " type_user_set, raw) VALUES ('held-card','old-item','Card',"
            "'loan','4242',TRUE,%s)", (jsonb(raw) if raw else None,))

    def _same(self, fresh):
        def handler(request):
            return httpx.Response(200, text=json.dumps({"accounts": fresh}))
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        return plaid._same_accounts(self.conn, client, "tok-new", "old-item")

    def test_a_retyped_card_is_matched_by_the_banks_type(self):
        self._held("credit")
        self.assertTrue(self._same([_acct("4242")]))

    def test_a_retyped_card_never_answers_for_a_checking_on_its_mask(self):
        self._held("credit")
        self.assertFalse(self._same([_acct("4242", kind="depository")]))

    def test_an_unknown_bank_type_never_releases_the_old_item(self):
        self._held(None)
        self.assertFalse(self._same([_acct("4242", kind="loan")]))


if __name__ == "__main__":
    unittest.main()
