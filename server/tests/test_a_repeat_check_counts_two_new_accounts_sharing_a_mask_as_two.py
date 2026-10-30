"""The repeat check counts two new accounts sharing a type and mask as two.

A link is handed back as a repeat only when every masked account it brings
is already held here. Where a held account's bank type is unknown (the
person retyped it and the feed's own word was never kept), the mask alone
may vouch for it — but only when that mask names exactly ONE account on
each side, the same fallback adoption pairs by. If the new login's
accounts were gathered into a set, two of its cards sharing a type and
the last four digits would collapse into one, the mask would look unique,
and a login bringing a second real card would be handed back as a repeat
while adoption itself refuses to pair it.
"""

import json
import unittest
import uuid

import httpx

from oikonome.sync import plaid

from .util import make_db, write_config

INST = "ins_masks"


def _acct(mask, kind="credit", sub="credit card"):
    return {"account_id": f"a-{uuid.uuid4().hex[:8]}", "name": "Card",
            "type": kind, "subtype": sub, "mask": mask,
            "balances": {"current": 10.0}}


class RepeatCheckMaskCounts(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_id, "
            "institution_name, status, access_token) VALUES "
            "('held-item','plaid',%s,'Contoso Bank','ok','tok-held')",
            (INST,))
        # retyped by hand, and the feed's own type was never kept
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask, "
            "type_user_set) VALUES ('held-card','held-item','Card','loan',"
            "'4242',TRUE)")

    def _repeat(self, fresh):
        def handler(request):
            assert request.url.path == "/accounts/get"
            return httpx.Response(200, text=json.dumps({"accounts": fresh}))
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        ctx = plaid._repeat_inputs(self.conn, client, "tok-new", "new-item",
                                   INST)
        return plaid._already_linked(self.conn, "new-item", INST, ctx)

    def test_two_new_cards_sharing_a_mask_are_not_a_repeat(self):
        self.assertIs(self._repeat([_acct("4242"), _acct("4242")]), False)

    def test_one_new_card_on_the_held_mask_is_still_a_repeat(self):
        self.assertIs(self._repeat([_acct("4242")]), True)


if __name__ == "__main__":
    unittest.main()
