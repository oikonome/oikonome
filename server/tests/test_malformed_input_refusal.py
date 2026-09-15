"""Malformed client input is refused at the door, never surfaced as a 500.

A malformed id or a wrong-shaped JSON body must come back as a 4xx the
user can act on — reaching the DB (or .items() on a non-dict) turns it
into a raw internal error instead.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class MalformedInputRefusalTests(unittest.TestCase):

    def test_entity_routes_refuse_a_non_uuid_before_the_db_sees_it(self):
        """business_entity.id is a Postgres UUID column, so a malformed
        path segment reaches the database as a raw DB error 500 rather than
        a 404 unless a guard refuses it first.

        The guard is one helper, so this counts its call sites rather than
        any particular inline spelling — pinning the expression would make
        a pure refactor look like a regression. What the routes actually DO
        with a malformed id is proven by real requests in
        test_business_route_malformed_ids.py; this only holds the line that
        every entity route still goes through a guard at all.
        """
        src = _src("web/api.py")
        self.assertIn("def _require_entity_id(", src)
        self.assertGreaterEqual(src.count("_require_entity_id("), 10)

    def test_taxdoc_commit_refuses_malformed_rows(self):
        """`rows` is arbitrary client JSON — the docstring says so. A
        non-dict `boxes` reached .items() and 500'd."""
        src = _src("engine/taxdocs.py")
        self.assertIn("must be an object", src)
        self.assertIn("isinstance(raw_boxes, dict)", src)
