"""A release never removes or narrows what an installed client reads.

A phone build is compiled against the generated types of its day and
cannot notice a later change; scripts/check-api-compat.py compares
specs/openapi.json with the copy every supported release carried
(specs/api-compat.json names the oldest). Pinned here:

* the rules, on small before/after schemas: a removed reply field, a field
  no longer always sent, a narrowed body type, a reply value of a type the
  old client never saw, a newly required body key or query parameter, and
  a removed door each fail; an added optional field, a new door, a
  widened body and a reply union sending fewer forms each pass;
* the checked-in schema is compatible with every supported release.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-api-compat.py"


def _checker():
    spec = importlib.util.spec_from_file_location("check_api_compat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json(ref: str) -> dict:
    return {"application/json": {"schema": {
        "$ref": f"#/components/schemas/{ref}"}}}


def _base() -> dict:
    """One reply shape, one body shape, one query parameter, one union."""
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/thing": {
                "get": {
                    "parameters": [{"in": "query", "name": "month",
                                    "required": False,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"content": _json("Thing")}},
                },
                "post": {
                    "requestBody": {"required": True,
                                    "content": _json("ThingSave")},
                    "responses": {"200": {"content": _json("Thing")}},
                },
            },
            "/api/blocks": {"get": {"responses": {"200": {
                "content": _json("Blocks")}}}},
        },
        "components": {"schemas": {
            "Thing": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "amount", "kind", "owner"],
                "properties": {
                    "id": {"type": "string"},
                    "amount": {"type": "number"},
                    "kind": {"type": "string", "enum": ["a", "b"]},
                    "note": {"anyOf": [{"type": "string"},
                                       {"type": "null"}]},
                    "owner": {"$ref": "#/components/schemas/Person"},
                },
            },
            "Person": {
                "type": "object", "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
            "ThingSave": {
                "type": "object", "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "amount": {"anyOf": [{"type": "number"},
                                         {"type": "string"}]},
                    "kind": {"type": "string", "enum": ["a", "b"]},
                },
            },
            "Blocks": {
                "type": "object", "additionalProperties": False,
                "required": ["blocks"],
                "properties": {"blocks": {"type": "array", "items": {
                    "oneOf": [{"$ref": "#/components/schemas/TextBlock"},
                              {"$ref": "#/components/schemas/BarBlock"}]}}},
            },
            "TextBlock": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "text"],
                "properties": {"kind": {"const": "text", "type": "string"},
                               "text": {"type": "string"}},
            },
            "BarBlock": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "values"],
                "properties": {"kind": {"const": "bar", "type": "string"},
                               "values": {"type": "array",
                                          "items": {"type": "number"}}},
            },
        }},
    }


class TheRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = _checker()

    def _breaks(self, change) -> list[str]:
        old = _base()
        new = copy.deepcopy(old)
        change(new["components"]["schemas"], new["paths"])
        return self.c.compare(old, new)

    def assertBreaks(self, change, *words):
        problems = self._breaks(change)
        self.assertTrue(problems, "expected a break")
        text = "\n".join(problems)
        for w in words:
            self.assertIn(w, text)

    def assertPasses(self, change):
        self.assertEqual(self._breaks(change), [])

    def test_an_unchanged_schema_passes(self):
        self.assertPasses(lambda s, p: None)

    # what fails

    def test_a_removed_reply_field_fails(self):
        def change(s, p):
            del s["Thing"]["properties"]["note"]
        self.assertBreaks(change, "GET /api/thing reply.note",
                          "removed from the reply")

    def test_a_removed_nested_field_fails(self):
        def change(s, p):
            del s["Person"]["properties"]["name"]
            s["Person"]["required"] = []
        self.assertBreaks(change, "reply.owner.name: removed")

    def test_a_field_no_longer_always_sent_fails(self):
        def change(s, p):
            s["Thing"]["required"].remove("amount")
        self.assertBreaks(change, "reply.amount: no longer always sent")

    def test_a_narrowed_body_type_fails(self):
        def change(s, p):
            s["ThingSave"]["properties"]["amount"] = {"type": "number"}
        self.assertBreaks(change, "POST /api/thing body.amount",
                          "no longer accepts string")

    def test_a_shrunk_body_choice_fails(self):
        def change(s, p):
            s["ThingSave"]["properties"]["kind"]["enum"] = ["a"]
        self.assertBreaks(change, 'body.kind: no longer accepts "b"')

    def test_a_reply_type_the_old_client_never_saw_fails(self):
        def change(s, p):
            s["Thing"]["properties"]["amount"] = {"type": "string"}
        self.assertBreaks(change, "reply.amount: now sends string")

    def test_a_reply_value_becoming_nullable_fails(self):
        def change(s, p):
            s["Thing"]["properties"]["id"] = {
                "anyOf": [{"type": "string"}, {"type": "null"}]}
        self.assertBreaks(change, "reply.id: now sends null")

    def test_a_new_member_of_a_closed_reply_set_fails(self):
        def change(s, p):
            s["Thing"]["properties"]["kind"]["enum"] = ["a", "b", "c"]
        self.assertBreaks(change, 'reply.kind: now sends "c"')

    def test_a_newly_required_body_key_fails(self):
        def change(s, p):
            s["ThingSave"]["required"].append("kind")
        self.assertBreaks(change, "body.kind: newly required in the body")

    def test_a_new_required_body_key_fails(self):
        def change(s, p):
            s["ThingSave"]["properties"]["memo"] = {"type": "string"}
            s["ThingSave"]["required"].append("memo")
        self.assertBreaks(change, "body.memo: newly required")

    def test_a_newly_required_query_parameter_fails(self):
        def change(s, p):
            p["/api/thing"]["get"]["parameters"][0]["required"] = True
        self.assertBreaks(change, "query parameter month: newly required")

    def test_a_removed_door_fails(self):
        def change(s, p):
            del p["/api/thing"]["post"]
        self.assertBreaks(change, "POST /api/thing: the door is gone")

    def test_a_new_union_form_in_a_reply_fails(self):
        def change(s, p):
            s["PieBlock"] = copy.deepcopy(s["BarBlock"])
            s["PieBlock"]["properties"]["kind"]["const"] = "pie"
            s["Blocks"]["properties"]["blocks"]["items"]["oneOf"].append(
                {"$ref": "#/components/schemas/PieBlock"})
        self.assertBreaks(change, 'reply.blocks[].kind: now sends "pie"')

    # what passes

    def test_an_added_optional_field_passes(self):
        def change(s, p):
            s["Thing"]["properties"]["memo"] = {"type": "string",
                                                "default": ""}
        self.assertPasses(change)

    def test_an_added_optional_body_key_passes(self):
        def change(s, p):
            s["ThingSave"]["properties"]["memo"] = {"type": "string"}
        self.assertPasses(change)

    def test_a_new_door_and_a_new_optional_parameter_pass(self):
        def change(s, p):
            p["/api/other"] = {"get": {"responses": {"200": {
                "content": _json("Person")}}}}
            p["/api/thing"]["get"]["parameters"].append(
                {"in": "query", "name": "year", "required": False,
                 "schema": {"type": "integer"}})
        self.assertPasses(change)

    def test_a_widened_body_and_a_narrowed_reply_pass(self):
        def change(s, p):
            # the server takes more; it sends less variety than before
            s["ThingSave"]["properties"]["kind"]["enum"] = ["a", "b", "c"]
            s["ThingSave"]["required"] = []
            s["Thing"]["properties"]["note"] = {"type": "string"}
            s["Thing"]["properties"]["amount"] = {"type": "integer"}
        self.assertPasses(change)

    def test_reordered_or_fewer_reply_union_forms_pass(self):
        def change(s, p):
            items = s["Blocks"]["properties"]["blocks"]["items"]["oneOf"]
            items.reverse()
        self.assertPasses(change)

        def fewer(s, p):
            s["Blocks"]["properties"]["blocks"]["items"]["oneOf"].pop()
        self.assertPasses(fewer)

    def test_a_door_the_old_schema_left_undocumented_passes(self):
        old = _base()
        old["paths"]["/api/thing"]["get"]["responses"]["200"]["content"] = {
            "application/json": {"schema": {}}}
        old["paths"]["/api/thing"]["post"]["requestBody"]["content"] = {
            "application/json": {"schema": {
                "type": "object", "additionalProperties": True}}}
        self.assertEqual(self.c.compare(old, _base()), [])

    def test_a_decided_break_is_waived_by_its_exact_line_only(self):
        old, new = _base(), _base()
        del new["components"]["schemas"]["Thing"]["properties"]["note"]
        problems = self.c.compare(old, new)
        self.assertEqual(len(problems), 2)   # GET and POST both reply Thing
        accepted = [{"problem": problems[0], "reason": "no build reads it"}]
        code, left, _ = self._check_with(old, new, accepted)
        self.assertEqual(code, 1)
        self.assertEqual([x.split("  [since")[0] for x in left],
                         [problems[1]])

    def test_a_decided_break_is_waived_by_the_hash_of_its_exact_line(self):
        # a waiver that would otherwise have to spell a removed name names
        # the line by its sha256 instead
        import hashlib
        old, new = _base(), _base()
        del new["components"]["schemas"]["Thing"]["properties"]["note"]
        problems = self.c.compare(old, new)
        accepted = [{"problem_sha256": hashlib.sha256(
                        problems[0].encode()).hexdigest(),
                     "reason": "no build reads it"},
                    {"problem_sha256": "0" * 64, "reason": "matches nothing"}]
        code, left, _ = self._check_with(old, new, accepted)
        self.assertEqual(code, 1)
        self.assertEqual([x.split("  [since")[0] for x in left],
                         [problems[1]])

    def _check_with(self, old, new, accepted):
        from unittest import mock
        with mock.patch.object(self.c, "baselines",
                               return_value=([("v9.9.9", json.dumps(old))],
                                             [])):
            return self.c.check({"oldest_supported": "v9.9.9",
                                 "accepted": accepted}, json.dumps(new))


class TheSchemaKeepsEverySupportedRelease(unittest.TestCase):
    def test_the_checked_in_schema_breaks_no_installed_client(self):
        if subprocess.run(["git", "-C", str(ROOT), "rev-parse"],
                          capture_output=True).returncode != 0:
            self.skipTest("not a git checkout: no release copy to read")
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("compared against", r.stdout)

    def test_the_oldest_supported_release_is_a_release_tag(self):
        c = _checker()
        cfg = c.load_config()
        self.assertIsNotNone(c._version(cfg["oldest_supported"]))
        for a in cfg.get("accepted", ()):
            self.assertTrue((a.get("problem") or a.get("problem_sha256"))
                            and a.get("reason"), a)


if __name__ == "__main__":
    unittest.main()
