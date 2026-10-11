"""A relay on port 465 is spoken to over TLS from the first byte.

Port 465 is implicit-TLS submission: the relay waits for a ClientHello and
never sends the plaintext greeting a STARTTLS client waits for. Delivery
there must open TLS at once, verify the certificate for the relay's real
name (not the pinned address it connected to), and not try STARTTLS on top.
Other ports keep the plaintext-then-STARTTLS conversation.
"""

import email.message
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from oikonome.web import report


def _serve_one(listener, ctx, got):
    try:
        raw, _ = listener.accept()
    except OSError:
        return
    raw.settimeout(3)
    try:
        conn = ctx.wrap_socket(raw, server_side=True)
    except (ssl.SSLError, OSError):
        raw.close()
        return
    f = conn.makefile("rwb")

    def say(line):
        f.write(line.encode() + b"\r\n")
        f.flush()

    say("220 test ESMTP")
    in_data = False
    try:
        for line in f:
            text = line.decode().rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    got["delivered"] = True
                    say("250 queued")
                continue
            verb = text.split(" ")[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250 test")
            elif verb == "DATA":
                in_data = True
                say("354 go")
            elif verb == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
    except OSError:
        pass
    finally:
        conn.close()


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class ImplicitTlsOn465(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert = os.path.join(cls.dir, "c.pem")
        cls.key = os.path.join(cls.dir, "k.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", cls.key, "-out", cls.cert, "-days", "1",
             "-subj", "/CN=relay.test",
             "-addext", "subjectAltName=DNS:relay.test"],
            check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _deliver(self):
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(self.cert, self.key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        got = {}
        t = threading.Thread(target=_serve_one, args=(listener, sctx, got),
                             daemon=True)
        t.start()
        cctx = ssl.create_default_context(cafile=self.cert)
        msg = email.message.EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "a@x.test", "b@x.test", "s"
        msg.set_content("hi")
        try:
            with mock.patch.object(report, "IMPLICIT_TLS_PORT", port), \
                    mock.patch.object(report, "_smtp_tls_context",
                                      return_value=cctx):
                report._smtp_deliver(msg, {"starttls": True}, "relay.test",
                                     "127.0.0.1", port, None, None)
        finally:
            t.join(5)
            listener.close()
        return got

    def test_delivery_opens_tls_at_once_and_verifies_the_relay_name(self):
        self.assertTrue(self._deliver().get("delivered"))

    def test_the_relay_probe_opens_tls_at_once_too(self):
        # the half-hourly probe must speak to the relay the way delivery
        # does, or the mail banner reads red for a relay that works
        from oikonome.notify import mailhealth
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(self.cert, self.key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        t = threading.Thread(target=_serve_one, args=(listener, sctx, {}),
                             daemon=True)
        t.start()
        cctx = ssl.create_default_context(cafile=self.cert)
        try:
            with mock.patch.object(report, "IMPLICIT_TLS_PORT", port), \
                    mock.patch.object(report, "_smtp_tls_context",
                                      return_value=cctx), \
                    mock.patch("oikonome.web.netguard.pinned_smtp_host",
                               return_value="127.0.0.1"):
                res = mailhealth.probe({"configured": True,
                                        "host": "relay.test", "port": port,
                                        "starttls": True})
        finally:
            t.join(5)
            listener.close()
        self.assertTrue(res["ok"], res.get("error"))


if __name__ == "__main__":
    unittest.main()
