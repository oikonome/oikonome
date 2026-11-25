"""The typed body reader's accessors for rows handed whole to an engine,
for a null told apart from a blank, for an amount that must be stated, for
a string-or-number field and for a body handed whole to one reader.

Each was a local workaround in a door before it was an accessor: a door
that could not say "a list of whatever the importer reads" or "was this
null?" through `Fields` either kept reading its body raw or caught one
accessor's refusal to try another. Pinned here so the doors that now rely
on them keep the answers they gave.
"""

import unittest

from fastapi import HTTPException

from oikonome.web.fields import MONEY_MAX, Fields


class TypedBodyReaderShapes(unittest.TestCase):

    def _refused(self, fn) -> str:
        with self.assertRaises(HTTPException) as ctx:
            fn()
        self.assertEqual(ctx.exception.status_code, 400)
        return ctx.exception.detail

    # ---- rows handed whole -------------------------------------------

    def test_rows_come_back_exactly_as_sent(self):
        rows = [{"a": 1}, 7, None, "x", [1]]
        f = Fields({"rows": rows})
        self.assertIs(f.opaque_rows("rows", 5), rows)
        self.assertIsNone(Fields({}).opaque_rows("rows", 5))
        self.assertIsNone(Fields({"rows": None}).opaque_rows("rows", 5))
        self.assertEqual(Fields({"rows": []}).opaque_rows("rows", 5), [])

    def test_rows_refuse_a_non_list_and_a_list_past_the_limit(self):
        for bad in ({"a": 1}, "rows", 3, True):
            self.assertEqual(self._refused(
                lambda: Fields({"files": bad}).opaque_rows("files", 9)),
                "files must be a list")
        self.assertEqual(self._refused(
            lambda: Fields({"files": [0] * 10}).opaque_rows("files", 9)),
            "too many files (max 9)")
        self.assertEqual(self._refused(
            lambda: Fields({"files": [0] * 10}).opaque_rows(
                "files", 9, msg="nope")), "nope")

    # ---- the JSON type, value withheld ---------------------------------

    def test_json_type_tells_null_from_blank_from_absent(self):
        f = Fields({"n": None, "s": "", "b": False, "i": 0, "x": 1.5,
                    "l": [], "o": {}})
        self.assertEqual(
            {k: f.json_type(k) for k in "nsbixlo"},
            {"n": "null", "s": "string", "b": "bool", "i": "number",
             "x": "number", "l": "list", "o": "object"})
        self.assertIsNone(f.json_type("absent"))

    # ---- an amount that must be stated ---------------------------------

    def test_a_stated_amount_reads_text_and_numbers(self):
        f = Fields({"a": "1,234.56", "b": 640, "c": "", "d": "  ",
                    "e": 12.5, "g": "-3"})
        self.assertEqual(f.money_given("a"), 1234.56)
        self.assertEqual(f.money_given("b"), 640.0)
        self.assertEqual(f.money_given("c"), 0.0)
        self.assertEqual(f.money_given("d", blank=2.0), 2.0)
        self.assertEqual(f.money_given("e"), 12.5)
        self.assertEqual(f.money_given("g"), -3.0)

    def test_a_stated_amount_refuses_null_and_every_non_amount(self):
        for v in (None, True, [1], {"a": 1}, "lots"):
            self.assertEqual(self._refused(
                lambda v=v: Fields({"k": v}).money_given("k", label="income")),
                "income must be a number")
        self.assertEqual(self._refused(
            lambda: Fields({}).money_given("k")), "k must be a number")
        self.assertEqual(self._refused(
            lambda: Fields({"k": MONEY_MAX * 10}).money_given("k")),
            "k is too large")

    # ---- a string, or a whole number as its digits ---------------------

    def test_text_or_int_reads_digits_sent_as_a_number(self):
        self.assertEqual(Fields({"t": 4417}).text_or_int("t"), "4417")
        self.assertEqual(Fields({"t": " 0042 "}).text_or_int("t"), "0042")
        self.assertEqual(
            Fields({"t": " 0042 "}).text_or_int("t", strip=False), " 0042 ")
        self.assertIsNone(Fields({"t": ""}).text_or_int("t"))
        self.assertIsNone(Fields({"t": None}).text_or_int("t"))
        self.assertIsNone(Fields({}).text_or_int("t"))

    def test_text_or_int_refuses_a_bool_a_fraction_and_containers(self):
        for v in (True, 12.5, [1], {"a": 1}):
            self.assertEqual(self._refused(
                lambda v=v: Fields({"t": v}).text_or_int("t")),
                "t must be a string or a whole number")
            self.assertEqual(self._refused(
                lambda v=v: Fields({"t": v}).text_or_int("t", msg="no")), "no")

    # ---- the whole body -------------------------------------------------

    def test_the_whole_body_is_a_copy_of_its_top_level(self):
        inner = {"id": "x"}
        sent = {"items": [inner], "kind": "api"}
        f = Fields(sent)
        whole = f.as_dict()
        self.assertEqual(whole, sent)
        whole["kind"] = "changed"
        self.assertEqual(f.text("kind"), "api")
        self.assertIs(whole["items"][0], inner)


    # ---- copies a door makes of its body ---------------------------------

    def test_a_copy_changes_only_the_key_it_names(self):
        # the settings save checks the relay host and every AI URL before
        # its lock, then reads the same keys from copies made under it
        # (one list merged back, one dropped): those copies must hand back
        # every other value unchanged, the very object that was checked
        backends = [{"id": "lab", "url": "http://192.0.2.20:11434/v1"}]
        sent = {"smtp_host": " 192.0.2.10 ", "llm_url": "http://x.test/v1",
                "llm_backends": backends, "email_muted": ["a@example.dev"],
                "email_recipients": ["b@example.dev"]}
        f = Fields(dict(sent))
        merged = f.with_value("email_muted", ["c@example.dev"])
        dropped = merged.without("email_recipients")
        for copy in (merged, dropped):
            self.assertEqual(copy.text("smtp_host"), f.text("smtp_host"))
            self.assertEqual(copy.text("llm_url"), f.text("llm_url"))
            self.assertIs(copy.opaque_rows("llm_backends", 8), backends)
        self.assertFalse(dropped.present("email_recipients"))
        self.assertEqual(merged.text_or_list("email_muted"),
                         ["c@example.dev"])
        # the original is not touched by either copy
        self.assertEqual(f.text_or_list("email_muted"), ["a@example.dev"])
        self.assertTrue(f.present("email_recipients"))

if __name__ == "__main__":
    unittest.main()
