"""The server's access log never writes a live one-time link token.

Every mailed link — password reset, unlock, email change and revert,
recovery cancel, signup reclaim, email verification, recipient and
household invites — carries its token in the query string (or, for the
legacy invite form, in the path). uvicorn's access logger writes the full
path to stdout with its own handler, past the app's log redaction, so
without a filter every click lands a usable token in the container log.
These tests drive a synthetic access record through the exact logging
configuration `oikonome serve` hands uvicorn.
"""

import io
import logging
import logging.config
import time
import unittest

from oikonome import cli

_TOKEN = "Zq7xWm3pLk9vRt2y"


class AccessLogWithholdsLinkTokensTests(unittest.TestCase):
    def _emit(self, path: str) -> str:
        config = cli.serve_log_config()
        logging.config.dictConfig(config)
        logger = logging.getLogger("uvicorn.access")
        out = io.StringIO()
        handler = logger.handlers[0]
        handler.setStream(out)
        # uvicorn's own call shape (httptools/h11 impl): five positional args
        logger.info('%s - "%s %s HTTP/%s" %d',
                    "203.0.113.9:51234", "GET", path, "1.1", 200)
        return out.getvalue()

    def test_every_mailed_link_shape_is_written_without_its_token(self):
        for path in (f"/reset?token={_TOKEN}",
                     f"/unlock?token={_TOKEN}",
                     f"/account/email-change?token={_TOKEN}",
                     f"/account/email-revert?token={_TOKEN}",
                     f"/recover/cancel?token={_TOKEN}",
                     f"/signup/reclaim?token={_TOKEN}",
                     f"/verify-email?token={_TOKEN}",
                     f"/recipient-invite?token={_TOKEN}",
                     f"/app/invite?token={_TOKEN}",
                     f"/invite/{_TOKEN}",
                     f"/signup?invite={_TOKEN}&email=a%40example.test"):
            with self.subTest(path=path):
                line = self._emit(path)
                self.assertIn("GET", line)
                self.assertIn("200", line)
                self.assertNotIn(_TOKEN, line)
                self.assertIn("<redacted>", line)

    def test_an_oversized_request_line_costs_a_bounded_time(self):
        """The access record is written on the event loop for every
        request, any path, unauthenticated: redaction whose cost grows
        faster than the URL would let one long URL stall every household."""
        f = cli._AccessLinkTokenFilter()
        cases = ("/" + "a-" * 32768,
                 "/" + "a." * 32768,
                 "/nope?q=" + ("%40" + "1." * 20) * 1500)
        for path in cases:
            with self.subTest(shape=path[:12]):
                # the rules alone, on the full untruncated input
                started = time.perf_counter()
                cli._redact_link_tokens(path)
                self.assertLess(time.perf_counter() - started, 0.2)
                # and the filter as the access log runs it
                record = logging.LogRecord(
                    "uvicorn.access", logging.INFO, __file__, 1,
                    '%s - "%s %s HTTP/%s" %d',
                    ("203.0.113.9:1", "GET", path, "1.1", 404), None)
                started = time.perf_counter()
                f.filter(record)
                self.assertLess(time.perf_counter() - started, 0.2)
                self.assertLess(len(record.args[2]), 2200)

    def test_a_token_past_the_length_bound_is_cut_with_the_rest(self):
        line = self._emit("/x?pad=" + "b" * 5000 + f"&token={_TOKEN}")
        self.assertNotIn(_TOKEN, line)
        self.assertIn("truncated", line)

    def test_a_token_straddling_the_length_bound_is_still_redacted(self):
        # "&token=" and the token's first four characters fit under the cut
        pad = "/reset?pad=" + "b" * (2048 - len("/reset?pad=") - 11)
        line = self._emit(pad + f"&token={_TOKEN}")
        self.assertIn("&token=<redacted>", line)
        self.assertNotIn(_TOKEN[:4], line.split("&token=")[-1])

    def test_an_ordinary_request_line_is_left_alone(self):
        line = self._emit("/api/transactions?limit=50")
        self.assertIn("/api/transactions?limit=50", line)


if __name__ == "__main__":
    unittest.main()
