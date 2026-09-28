"""The golden merchant corpus: descriptor shapes and the name each must
canonicalise to. A fixed naming bug adds a line to
tests/fixtures/merchant_corpus.jsonl."""

import json
import pathlib
import unittest

from oikonome.engine import merchant_dedup

CORPUS = pathlib.Path(__file__).with_name("fixtures") / "merchant_corpus.jsonl"


class MerchantCorpusTests(unittest.TestCase):
    def test_every_corpus_line_canonicalizes_as_recorded(self):
        rows = [json.loads(l) for l in CORPUS.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        self.assertGreater(len(rows), 20)
        bad = []
        for r in rows:
            if r.get("verbatim"):
                continue          # the resolver keeps these as written
            got = merchant_dedup.canonical_merchant(r["raw"])
            if got != r["merchant"]:
                bad.append(f"{r['raw']!r}: got {got!r}, want {r['merchant']!r}"
                           + (f"  ({r['note']})" if r.get("note") else ""))
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
