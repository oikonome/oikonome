"""The word "model" named two different things on the Rules page: the rules an
AI pass wrote and the ones the household's own trained classifier wrote — and
the first were called "model" even with no AI endpoint set up. The rules read
carries the one phrase every client prints, naming each source apart and
saying when no AI pass is set up now."""
import os
import unittest
from unittest import mock

from oikonome.web import data

from .util import make_db, write_config


class RuleSourceWords(unittest.TestCase):
    def test_each_source_is_named_apart(self):
        text = data.rule_counts_text(
            {"user": 12, "llm": 2340, "model": 45, "seed": 90}, True)
        self.assertEqual(text, "12 yours · 2,340 from an AI pass · 45 from "
                               "your household's trained model · 90 built-in")
        self.assertNotIn(" model ·", text.replace("trained model", ""))

    def test_an_ai_pass_with_no_endpoint_now_says_so(self):
        text = data.rule_counts_text(
            {"user": 1, "llm": 5, "model": 0, "seed": 2}, False)
        self.assertEqual(text, "1 yours · 5 from an AI pass (none set up "
                               "now) · 2 built-in")

    def test_the_rules_read_carries_the_phrase(self):
        conn = make_db()
        try:
            write_config(conn)
            conn.execute(
                "INSERT INTO merchant_categories (merchant, category_primary,"
                " source) VALUES ('Pine Motors', 'TRANSPORTATION', 'llm')")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("OIKONOME_LLM_")}
            with mock.patch.dict(os.environ, env, clear=True):
                page = data.list_rules(conn)
        finally:
            conn.close()
        self.assertEqual(page["counts_text"],
                         "0 yours · 1 from an AI pass (none set up now) · "
                         "0 built-in")


if __name__ == "__main__":
    unittest.main()
