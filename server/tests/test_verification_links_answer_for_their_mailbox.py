"""An email-verification link speaks for the mailbox it was sent to.

* Any unexpired, unspent link mailed to the account's address verifies
  it. A resend, the day-7 reminder and the freeze notice each mint a new
  link, and mails arrive out of order: the first mail's link — the one most
  people open — must not answer "already used" because a later one exists.
* A link stops answering the moment the account is at another address, so
  a link to an address the account left can never verify the new one.
* Each link is still spent once.
"""

import unittest
import uuid

from oikonome.auth import email_verify, passwords
from oikonome.db import tenancy

from .util import _ensure_db


class VerificationLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)
        tid = tenancy.create_tenant(self.admin, f"vl-{uuid.uuid4().hex[:8]}")
        self.email = f"vl-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = self.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,%s) RETURNING id",
            (tid, self.email, passwords.hash_password("x" * 12))
        ).fetchone()["id"]

    def _verified(self):
        return self.admin.execute(
            "SELECT verified_at FROM users WHERE id=%s",
            (self.uid,)).fetchone()["verified_at"] is not None

    def test_an_earlier_link_still_verifies_after_a_newer_one_was_minted(self):
        first = email_verify.create(self.admin, self.uid)
        second = email_verify.create(self.admin, self.uid)
        row = email_verify.lookup(self.admin, first)
        self.assertIsNotNone(row, "the first mail's link must still answer")
        self.assertTrue(email_verify.consume(self.admin, row["id"], self.uid))
        self.assertTrue(self._verified())
        # the later link is unspent and still lands on the confirmed page
        self.assertIsNotNone(email_verify.lookup(self.admin, second))

    def test_a_link_is_spent_once(self):
        tok = email_verify.create(self.admin, self.uid)
        row = email_verify.lookup(self.admin, tok)
        self.assertTrue(email_verify.consume(self.admin, row["id"], self.uid))
        self.assertFalse(email_verify.consume(self.admin, row["id"], self.uid))
        self.assertIsNone(email_verify.lookup(self.admin, tok))
        self.assertTrue(email_verify.lookup_spent(self.admin, tok)["verified"])

    def test_a_link_to_an_address_the_account_left_verifies_nothing(self):
        tok = email_verify.create(self.admin, self.uid)
        row = email_verify.lookup(self.admin, tok)
        self.admin.execute("UPDATE users SET email=%s WHERE id=%s",
                           (f"moved-{uuid.uuid4().hex[:8]}@x.dev", self.uid))
        self.assertIsNone(email_verify.lookup(self.admin, tok))
        # even a caller holding the row it looked up before the move
        self.assertFalse(email_verify.consume(self.admin, row["id"],
                                              self.uid))
        self.assertFalse(self._verified())

    def test_the_link_answers_again_if_the_account_returns_to_its_address(self):
        """A revert puts the account back on the address the link was sent
        to; the mailbox is the same one, so the link means what it said."""
        tok = email_verify.create(self.admin, self.uid)
        self.admin.execute("UPDATE users SET email=%s WHERE id=%s",
                           (f"away-{uuid.uuid4().hex[:8]}@x.dev", self.uid))
        self.admin.execute("UPDATE users SET email=%s WHERE id=%s",
                           (self.email.upper(), self.uid))
        self.assertIsNotNone(email_verify.lookup(self.admin, tok))


if __name__ == "__main__":
    unittest.main()
