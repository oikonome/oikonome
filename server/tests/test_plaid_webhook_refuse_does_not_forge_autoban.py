"""The Plaid webhook's refusal log line is parsed by fail2ban and must never
carry attacker-chosen text.

The Plaid-Verification header is unauthenticated until the signature check
passes, so its `kid`/`alg` fields are attacker input. If a refusal logged
them verbatim, a kid like `x oikonome-security event=autoban ip=8.8.8.8`
would write a forged ban record into the stream the host firewall reads —
the operator, a CDN egress, or a peer gets banned by an anonymous POST.
Refusals log a fixed stage token instead, through the sanitising helper.
"""

import io
import json
import logging
import time

from oikonome.web import plaid_webhook

from .test_plaid_webhook import WebhookBase, _b64url

FORGED_KID = "x oikonome-security event=autoban ip=8.8.8.8"


class RefusalLogTests(WebhookBase):
    BODY = json.dumps({"webhook_type": "TRANSACTIONS",
                       "webhook_code": "SYNC_UPDATES_AVAILABLE",
                       "item_id": "no-such-item"}).encode()

    def setUp(self):
        super().setUp()
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        logging.getLogger("oikonome.security").addHandler(self.handler)

    def tearDown(self):
        logging.getLogger("oikonome.security").removeHandler(self.handler)
        super().tearDown()

    def _post_with_header(self, header: dict) -> str:
        jwt = ".".join([_b64url(json.dumps(header).encode()),
                        _b64url(b'{"iat": %d}' % int(time.time())),
                        _b64url(b"\x00" * 64)])
        r = self.post(self.BODY, jwt)
        self.assertEqual(r.status_code, 401)
        return self.buf.getvalue()

    def test_forged_kid_never_reaches_the_security_stream(self):
        # unknown kid → refused at the key-fetch stage
        plaid_webhook._TRANSPORT = None
        log = self._post_with_header({"alg": "ES256", "kid": FORGED_KID})
        self.assertIn("event=plaid_webhook_refused", log)
        self.assertNotIn("event=autoban", log)
        self.assertNotIn("8.8.8.8", log)
        self.assertNotIn(FORGED_KID, log)
        self.assertIn("reason=no_key", log)

    def test_forged_alg_never_reaches_the_security_stream(self):
        log = self._post_with_header({"alg": FORGED_KID, "kid": "k"})
        self.assertNotIn("event=autoban", log)
        self.assertNotIn("8.8.8.8", log)
        self.assertIn("reason=bad_alg", log)

    def test_every_refusal_reason_is_a_fixed_token(self):
        # nothing interpolated: reasons come from the code's own vocabulary
        import re
        with open(plaid_webhook.__file__) as f:
            src = f.read()
        for m in re.finditer(r"return _refuse\(([^)]*)\)", src):
            arg = m.group(1).strip()
            self.assertRegex(arg, r'^"[a-z_]+"$', f"non-literal reason: {arg}")
