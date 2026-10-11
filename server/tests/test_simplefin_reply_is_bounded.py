"""A SimpleFIN reply is read with a ceiling on its size and on the whole
exchange.

Both SimpleFIN servers are whichever the setup token names. Read whole,
a reply of gigabytes is held in the worker's memory, and a server that
sends one byte just inside each read timeout keeps the hourly sync, and
every household queued behind it, waiting for as long as it likes.
"""
import base64
import socket
import threading
import time
import unittest
from unittest import mock

import httpx

from oikonome.sync import simplefin


class _Huge(httpx.SyncByteStream):
    def __init__(self):
        self.pulled = 0

    def __iter__(self):
        while self.pulled < 10 * 1024 * 1024:
            self.pulled += 65536
            yield b"a" * 65536


class _Trickle:
    """A loopback server that answers a status line and then one header
    byte at a time, fast enough that no single read ever times out."""

    def __init__(self, life: float = 8.0):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.life = life
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        end = time.monotonic() + self.life
        self.sock.settimeout(self.life)
        try:
            c, _ = self.sock.accept()
        except OSError:
            return
        try:
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while time.monotonic() < end:
                c.sendall(b"a")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            c.close()
            self.sock.close()


class SimpleFinReplyBounds(unittest.TestCase):

    def test_a_fetch_reply_past_the_ceiling_is_refused_unread(self):
        body = _Huge()
        t = httpx.MockTransport(lambda req: httpx.Response(200, stream=body))
        with mock.patch.object(simplefin, "FETCH_READ_MAX", 1024 * 1024):
            with self.assertRaises(simplefin.ReplyTooLarge):
                simplefin.fetch("https://u:p@bridge.example.dev/simplefin",
                                transport=t)
        self.assertLess(body.pulled, 2 * 1024 * 1024)

    def test_a_claim_reply_past_the_ceiling_is_refused(self):
        body = _Huge()
        t = httpx.MockTransport(lambda req: httpx.Response(200, stream=body))
        token = base64.b64encode(
            b"https://bridge.example.dev/claim/abc").decode()
        with self.assertRaises(ValueError):
            simplefin.claim_setup_token(token, transport=t)
        self.assertLess(body.pulled, 1024 * 1024)

    def test_a_trickling_server_is_cut_off_at_the_deadline(self):
        srv = _Trickle()
        started = time.monotonic()
        with mock.patch.object(simplefin, "FETCH_DEADLINE", 1.0):
            with self.assertRaises(httpx.HTTPError):
                simplefin.fetch(f"http://u:p@127.0.0.1:{srv.port}/simplefin")
        self.assertLess(time.monotonic() - started, 4.0)

    def test_an_error_reply_is_raised_on_its_status_unread(self):
        # an error page is never read: decoding it whole would let a
        # compressed body inflate in memory past every ceiling above
        body = _Huge()
        t = httpx.MockTransport(lambda req: httpx.Response(500, stream=body))
        with self.assertRaises(httpx.HTTPStatusError) as cm:
            simplefin.fetch("https://u:p@bridge.example.dev/simplefin",
                            transport=t)
        self.assertEqual(cm.exception.response.status_code, 500)
        self.assertEqual(body.pulled, 0)

    def test_a_normal_reply_still_reads(self):
        t = httpx.MockTransport(lambda req: httpx.Response(
            200, json={"accounts": [], "errors": []}))
        out = simplefin.fetch("https://u:p@bridge.example.dev/simplefin",
                              transport=t)
        self.assertEqual(out["accounts"], [])


if __name__ == "__main__":
    unittest.main()
