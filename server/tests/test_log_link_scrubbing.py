"""Every one-time link the app mints is scrubbed from BOTH log scrubbers.

Two places copy access-log lines somewhere an operator (or support) can
read them: ``./oikonome.sh logs`` run non-interactively by the host agent
(a sed pipeline in cmd_logs) and the feedback bundle's ring logger
(feedback._redact). Each keeps its own list of token-bearing URL shapes,
and a link shape added to the app but not to a list rides out verbatim —
the admin console's ``/signup?invite=<token>`` is exactly that shape. Every
shape the app mints gets one sample line here, run through both
scrubbers, and the two lists are asserted equal so they cannot drift
apart.
"""

import re
import subprocess
import unittest
from pathlib import Path

from oikonome.web import feedback

ROOT = Path(__file__).resolve().parents[2]


def _sh_sed_block() -> str:
    """The exact sed invocation cmd_logs pipes the snapshot through."""
    src = (ROOT / "oikonome.sh").read_text()
    body = src[src.index("cmd_logs()"):]
    start = body.index("sed -E")
    end = body.index("|| true", start)
    return body[start:end].rstrip().rstrip("\\")


def _sh_scrub(line: str) -> str:
    r = subprocess.run(["bash", "-c", _sh_sed_block()], input=line + "\n",
                       capture_output=True, text=True, check=True)
    return r.stdout


# (sample access-log line, the secret that must not survive, text that
# must survive — proving the rule stops at the token and not the route)
SHAPES = [
    # admin-console signup invite (adminconsole.py) and the approval flow
    # (app.py): a live single-use account-creation token in `invite=`
    ("GET /signup?invite=FAKEINVITE0000000001&email=alice@example.com HTTP/1.1",
     "FAKEINVITE0000000001", "/signup?invite="),
    # `?token=` on routes other than /reset and /setup
    ("GET /unsubscribe?token=FAKEUNSUB00000000001 HTTP/1.1",
     "FAKEUNSUB00000000001", "/unsubscribe?token="),
    ("GET /recipient-invite?token=FAKERECIP00000000001 HTTP/1.1",
     "FAKERECIP00000000001", "/recipient-invite?token="),
    ("GET /verify-email?token=FAKEVERIFY0000000001 HTTP/1.1",
     "FAKEVERIFY0000000001", "/verify-email?token="),
    # a button in the daily email (act from the mail) — a signed statement
    # that can confirm a bill or file a charge for a week
    ("GET /act?token=FAKEACT0000000000001 HTTP/1.1",
     "FAKEACT0000000000001", "/act?token="),
    # Plaid link token in the URL PATH (pages.py), with and without the
    # /status poll suffix
    ("GET /accounts/plaid/link/link-sandbox-FAKE0001-abcd-ef HTTP/1.1",
     "link-sandbox-FAKE0001-abcd-ef", "/accounts/plaid/link/"),
    ("GET /accounts/plaid/link/link-sandbox-FAKE0002-abcd-ef/status HTTP/1.1",
     "link-sandbox-FAKE0002-abcd-ef", "/status"),
    # the remaining token-bearing shapes
    ("GET /reset?token=FAKERESET00000000001 HTTP/1.1",
     "FAKERESET00000000001", "/reset?token="),
    ("GET /setup?token=FAKESETUP00000000001 HTTP/1.1",
     "FAKESETUP00000000001", "/setup?token="),
    ("GET /invite/FAKELEGACYINVITE0001 HTTP/1.1",
     "FAKELEGACYINVITE0001", "/invite/"),
    ("GET /export/download?t=FAKETICKET0000000001 HTTP/1.1",
     "FAKETICKET0000000001", "/export/download?t="),
    ("GET /admin/console/enrol?ticket=FAKEENROL00000000001&x=1 HTTP/1.1",
     "FAKEENROL00000000001", "&x=1"),
    ("GET /verify?code=99887766 HTTP/1.1", "99887766", "/verify?code="),
    ("Authorization: Bearer FAKEBEARER0000000001",
     "FAKEBEARER0000000001", "Authorization: Bearer "),
    ("stored enc:v1:RkFLRUNJUEhFUlRFWFQwMDAx= ok",
     "RkFLRUNJUEhFUlRFWFQwMDAx", "enc:v1:"),
]

# lines with no secret must come back byte-for-byte: an over-broad rule
# that ate `?msg=` or a plain path would hide the very lines an operator
# reads the snapshot for
PLAIN = [
    "GET /app/accounts?msg=Saved. HTTP/1.1 303",
    "GET /accounts/links HTTP/1.1 200",
    "worker synced 3 items",
]


class FeedbackScrubberTests(unittest.TestCase):
    def test_every_shape_is_redacted(self):
        for line, secret, keep in SHAPES:
            out = feedback._redact(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("<redacted>", out, line)
            self.assertIn(keep, out, line)

    def test_plain_lines_are_untouched(self):
        for line in PLAIN:
            self.assertEqual(feedback._redact(line), line)


class ShellScrubberTests(unittest.TestCase):
    def test_every_shape_is_redacted(self):
        for line, secret, keep in SHAPES:
            out = _sh_scrub(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("<redacted>", out, line)
            self.assertIn(keep, out, line)

    def test_plain_lines_are_untouched(self):
        for line in PLAIN:
            self.assertEqual(_sh_scrub(line), line + "\n")


class ListsStayInLockstepTests(unittest.TestCase):
    """oikonome.sh cannot import feedback.py, so each carries its own copy
    of the query-param and path-segment lists. Read the sh copy back out
    of the sed expressions and hold it to the Python one."""

    def test_query_param_names_agree(self):
        m = re.search(r"\[\?&\]\(([a-z|]+)\)=", _sh_sed_block())
        self.assertIsNotNone(m, "cmd_logs has no query-param rule")
        self.assertEqual(set(m.group(1).split("|")),
                         set(feedback.TOKEN_QUERY_PARAMS))

    def test_token_path_segments_agree(self):
        m = re.search(r"\(/\(([a-z|]+)\)/\)", _sh_sed_block())
        self.assertIsNotNone(m, "cmd_logs has no token-path rule")
        self.assertEqual(set(m.group(1).split("|")),
                         set(feedback.TOKEN_PATH_SEGMENTS))


if __name__ == "__main__":
    unittest.main()
