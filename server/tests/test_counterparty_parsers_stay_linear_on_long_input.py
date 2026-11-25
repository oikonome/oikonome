"""The statement-line name parsers answer in bounded time on any input.

venmo_counterparty, zelle_counterparty and the fuel-arm reader run on text
an import file or a bridge supplies, so their cost must not depend on the
caller remembering to cut it first. Each one clips its own input to a
descriptor's length before any pattern runs, and its patterns do not
retry a whitespace or separator run from every position inside it.

What these protect: a 50,000-character hostile line answers within 50 ms
in each parser; a line past the cap reads exactly as its first
MAX_DESCRIPTOR characters do; and on a few hundred realistic statement
lines every parser answers exactly what the patterns it replaced did.
"""

import random
import re
import time
import unittest

from oikonome.sync import base
from oikonome.sync.base import (MAX_DESCRIPTOR, fuel_arm, venmo_counterparty,
                                zelle_counterparty)

# ---- the parsers as they read before the clip and the linear patterns ----
_R_VENMO_ADDRESS_TAIL = re.compile(r"\s+\d.*$")
_R_VENMO_GEO_TAIL = re.compile(r",?\s+[A-Z]{2},?\s+US\s*$")
_R_ZELLE_REST = re.compile(r"zelle payment from (?P<rest>.+)$", re.IGNORECASE)
_R_ZELLE_PAREN_SPLIT = re.compile(
    r"^(?P<frm>.+?\([^)]*\))\s+to\s+(?P<to>.+)$", re.IGNORECASE)
_R_ZELLE_PAREN_TAIL = re.compile(r"\s*\(.*$")
_R_ZELLE_RECEIVER_TAG = re.compile(r"\s+RECEIVER\s*$", re.IGNORECASE)
_R_FUEL_AFTER_RE = re.compile(
    r"[^A-Za-z0-9]+(gas|fuel)(?:[^A-Za-z0-9]|$)", re.IGNORECASE)


def _ref_venmo(orig_desc):
    if not orig_desc:
        return None
    s = orig_desc.strip()
    if not s.upper().startswith("VENMO"):
        return None
    rest = s[len("VENMO"):].strip()
    if not rest.startswith("*"):
        return None
    name = _R_VENMO_GEO_TAIL.sub("", _R_VENMO_ADDRESS_TAIL.sub("", rest[1:]))
    words = name.split()
    if any(any(c.islower() for c in w) for w in words):
        while words and words[-1].isupper() and len(words[-1]) >= 2:
            words.pop()
    return base._finish_counterparty("Venmo", " ".join(words))


def _ref_zelle(desc, money_in):
    if not desc:
        return None
    m = _R_ZELLE_REST.search(desc)
    if not m:
        return None
    rest = m.group("rest")
    pm = _R_ZELLE_PAREN_SPLIT.match(rest)
    if pm:
        frm, to = pm.group("frm"), pm.group("to")
    else:
        seps = list(re.finditer(r" to ", rest, flags=re.IGNORECASE))
        if not seps:
            return None
        sep = seps[-1] if money_in else seps[0]
        frm, to = rest[:sep.start()], rest[sep.end():]
    side = frm if money_in else to
    side = _R_ZELLE_RECEIVER_TAG.sub("", _R_ZELLE_PAREN_TAIL.sub("", side))
    return base._finish_counterparty("Zelle", side)


def _ref_brand_then_fuel(letters, text):
    if not _R_FUEL_AFTER_RE.search(text):
        return None
    alnum = base._ALNUM_CHARS
    where = [i for i, ch in enumerate(text) if ch in alnum]
    read = "".join(text[i] for i in where).lower()
    k = read.find(letters)
    while k >= 0:
        first = where[k]
        if first == 0 or text[first - 1] not in alnum:
            m = _R_FUEL_AFTER_RE.match(text, where[k + len(letters) - 1] + 1)
            if m:
                return m
        k = read.find(letters, k + 1)
    return None


def _span(m):
    return None if m is None else (m.span(), m.group(1))


# ---- a corpus of statement lines, invented, in the shapes banks print ----
_PEOPLE = ["TAYLOR SAMPLE", "Taylor Sample", "CASEY EXAMPLE", "Jordan Q Public",
           "ACME HOLDINGS, LLC", "Farm to Table LLC", "Robin  Doe", "Ola Nord",
           "MORGAN-LEE TESTER", "Pat O'Example", "Sam Owner", "Alex"]
_TAILS = ["", " 1234 EXAMPLE PKWY 5555550100, NY, US", " NEW YORK, NY, US",
          " SPRINGFIELD NY US", ", CA, US", " 5555550100", "  OR  US",
          " NEW YORK", "\t1 MAIN ST", " US", " , NY , US ", "   "]
_OWN = ["(Acme Checking XXXXXX1234)", "(Acme Savings 5678)", "(Acme",
        "(Acme (Joint) Checking 9999)", ""]
_PREFIX = ["", "ACH DEBIT — ", "ACH CREDIT — ", "ACH Withdrawal — ",
           "Online Transfer: "]
_SUFFIX = ["", " RECEIVER", " receiver  ", " (memo)", "  "]


def _corpus(rng):
    venmo = [f"VENMO *{p}{t}" for p in _PEOPLE for t in _TAILS]
    venmo += ["VENMO PAYMENT", "VENMO CASHOUT", "VENMO", "VENMO *",
              "  venmo *Taylor Sample NEW YORK, NY, US  ", "SAFEWAY STORE 123",
              "VENMO * 1234", "VENMO *A\n1 MAIN ST", None, ""]
    zelle = []
    for _ in range(300):
        a, b = rng.choice(_PEOPLE), rng.choice(_PEOPLE)
        own = rng.choice(_OWN)
        if rng.random() < 0.5:
            line = f"Zelle payment from {a} {own} to {b}{rng.choice(_SUFFIX)}"
        else:
            line = f"Zelle payment from {a} to {b} {own}{rng.choice(_SUFFIX)}"
        zelle.append(rng.choice(_PREFIX) + line.replace("  ", rng.choice(
            [" ", "  ", "\t"])))
    zelle += ["Transfer Return — Zelle Debit Return",
              "ACH DEBIT — Zelle payment from Sam Owner (Acme",
              "zelle payment from X to", None, ""]
    return venmo, zelle


_BRANDS = ["Northwind Club", "Kroger", "A-B-C", "Lowe's", "Vega", "A+ (Market)",
           "Shell", "kbx", "Café Rouge", "7-Eleven", "Bluefin Market"]
_FUEL_TEXTS = ["NORTHWIND CLUB GAS #0123", "NORTHWIND CLUB WHSE #0123",
               "WWW NORTHWIND CLUB COM", "KROGER FUEL CTR #2020", "A B C FUEL TX",
               "A-B-C #0123", "LOWES FUEL CENTER", "VEGAS BUFFET", "VEGA GAS",
               "xVEGA GAS", "VEGA GASOLINE", "VEGA  --  gas", "A MARKET GAS #1",
               "SHELL OIL AUTO FUEL", "K-B-X GAS", "KBX GAS STATION", "kbxgas",
               "CAFE ROUGE GAS", "CAFÉ ROUGE GAS", "7 ELEVEN FUEL",
               "7-ELEVEN 7-ELEVEN GAS", "NORTHWIND NORTHWIND CLUB FUEL",
               "BLUEFIN MARKET  ---  FUEL", "BLUEFIN MARKET---", "GAS", ""]


def _letters(brand):
    return "".join(ch for ch in brand if ch in base._ALNUM_CHARS).lower()


class ParsersAnswerAsBefore(unittest.TestCase):

    def test_venmo_reads_every_line_as_before(self):
        venmo, _ = _corpus(random.Random(11))
        self.assertGreater(len(venmo), 100)
        for line in venmo:
            self.assertEqual(venmo_counterparty(line), _ref_venmo(line), line)

    def test_zelle_reads_every_line_as_before(self):
        _, zelle = _corpus(random.Random(11))
        self.assertGreater(len(zelle), 300)
        for line in zelle:
            for money_in in (True, False):
                self.assertEqual(zelle_counterparty(line, money_in),
                                 _ref_zelle(line, money_in), (line, money_in))

    def test_fuel_reader_finds_the_same_place_as_before(self):
        rng = random.Random(5)
        texts = list(_FUEL_TEXTS)
        for _ in range(200):
            b = rng.choice(_BRANDS).upper()
            sep = rng.choice([" ", "  ", "-", " - ", "\t", "*", ""])
            texts.append(f"{rng.choice(['', 'WWW ', '#12 '])}{b}{sep}"
                         f"{rng.choice(['GAS', 'FUEL', 'WHSE', 'gasoline'])}"
                         f"{rng.choice(['', ' #12', '-', ' TX'])}")
        n = 0
        for brand in _BRANDS:
            letters = _letters(brand)
            for t in texts:
                n += 1
                self.assertEqual(_span(base._brand_then_fuel(letters, t)),
                                 _span(_ref_brand_then_fuel(letters, t)),
                                 (brand, t))
        self.assertGreater(n, 2000)


# ---- hostile input: long runs of whatever the patterns quantify over ----
_N = 50_000
_HOSTILE = {
    "venmo": ["VENMO *A" + " " * _N + "x", "VENMO *A" + " , " * (_N // 3),
              "VENMO *Ab" + " AB" * (_N // 3), "VENMO *" + "\n1" * (_N // 2)],
    "zelle": ["Zelle payment from " + "(" * _N,
              "Zelle payment from A" + " " * _N + "x to B",
              "Zelle payment from A to B" + " " * _N + "x",
              "Zelle payment from A (" + "(" * _N + "\nx",
              "Zelle payment from A (b) to " + " RECEIVER" * (_N // 9)],
    "fuel": ["NORTHWIND" + "-" * _N + "x", "-" * _N + "NORTHWIND GAS",
             "NORTHWIND " * (_N // 10), " " * _N],
}


def _elapsed(fn):
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


class ParsersStayCheapOnHostileInput(unittest.TestCase):

    def _assert_fast(self, label, fn):
        # best of three: a scheduler hiccup is not a quadratic pattern
        took = min(_elapsed(fn) for _ in range(3))
        self.assertLess(took, 0.05, f"{label}: {took * 1000:.1f} ms")

    def test_venmo(self):
        for s in _HOSTILE["venmo"]:
            self._assert_fast(repr(s[:20]), lambda: venmo_counterparty(s))

    def test_zelle(self):
        for s in _HOSTILE["zelle"]:
            for money_in in (True, False):
                self._assert_fast(repr(s[:30]),
                                  lambda: zelle_counterparty(s, money_in))

    def test_fuel(self):
        for s in _HOSTILE["fuel"]:
            self._assert_fast(repr(s[:20]),
                              lambda: base._brand_then_fuel("northwind", s))
            self._assert_fast(repr(s[:20]),
                              lambda: fuel_arm("Northwind", s, s))


class ParsersClipTheirOwnInput(unittest.TestCase):
    """The guard is the parser's, not the caller's: a line past the cap
    reads as its first MAX_DESCRIPTOR characters."""

    def test_a_long_line_reads_as_its_clipped_head(self):
        venmo_line = "VENMO *TAYLOR SAMPLE" + " " * MAX_DESCRIPTOR + "JR"
        self.assertEqual(venmo_counterparty(venmo_line),
                         _ref_venmo(venmo_line[:MAX_DESCRIPTOR]))
        zelle_line = ("Zelle payment from Sam Owner (Acme Checking 1234) to "
                      "CASEY EXAMPLE" + " " * MAX_DESCRIPTOR + "OTHER NAME")
        for money_in in (True, False):
            self.assertEqual(
                zelle_counterparty(zelle_line, money_in),
                _ref_zelle(zelle_line[:MAX_DESCRIPTOR], money_in))
        fuel_line = " " * MAX_DESCRIPTOR + "NORTHWIND GAS"
        self.assertIsNone(base._brand_then_fuel("northwind", fuel_line))
        self.assertIsNotNone(base._brand_then_fuel("northwind",
                                                   fuel_line.strip()))


if __name__ == "__main__":
    unittest.main()
