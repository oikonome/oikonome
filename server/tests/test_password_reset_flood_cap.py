"""Password-reset sends are capped per target address, oracle-free.

A per-IP throttle alone lets a distributed caller mail an unbounded
number of reset links into a chosen victim's inbox; and the cap itself
must not change the response or its timing, or it becomes an
account-existence oracle.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class PasswordResetFloodCapTests(unittest.TestCase):

    def test_password_reset_is_capped_per_address(self):
        """A per-IP throttle alone lets a distributed caller mail an
        unbounded number of reset links into a chosen victim's inbox, so
        the route is also capped per address, like the sibling login-unlock
        door."""
        src = _src("web/app.py")
        self.assertIn("_RESET_PER_HOUR", src)
        self.assertIn("def _reset_send_allowed(", src)
        self.assertIn("_reset_send_allowed(conn, u[\"id\"])", src)

    def test_the_cap_cannot_become_an_account_oracle(self):
        """The response and the timing must not differ — that is the
        property the out-of-band delivery exists to protect."""
        src = _src("web/app.py")
        i = src.index("def _reset_send_allowed(")
        self.assertIn("IDENTICALLY", src[i:i + 900])
        # a counter failure must never block a genuine reset
        self.assertIn("return True", src[i:i + 1400])
