"""The settings save answers every wrongly typed key with a 4xx, takes every
body the released clients send, and reads its converted key families only
through the typed reader.

POST /api/settings takes a PARTIAL settings document: a key left out is
left alone, null or "" often clears, and a masked echo of a secret keeps
what is stored. Its body was read as a plain dict, one key at a time, so
each key's type check was a decision of its own, and a missed one is a 500.
The key families whose acceptance the typed reader can state exactly now
read through `web.fields.Fields`; the rest keep their own cleaners, and
the source check below names which is which, so a family moves from the
second list to the first and never back.

Three checks:

* the matrix: every key the door reads, sent alone as an int, a list, an
  object, a 10 kB string and a NUL character, by the owner and by a
  member, never answers 5xx;
* the clients' own bodies: one per writer in the web and mobile apps,
  older shapes included, saves (200) and stores what the client meant;
* the shape: no converted key is read raw (`body.get("k")`, `body["k"]`,
  `"k" in body`) inside the handler.
"""

import ast
import inspect
import re
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"

BAD_VALUES = (7, [1], {"a": 1}, "x" * 10240, "\u0000")
STORED = object()     # stands for "the section as the page read it"

# every top-level key the door reads, the ones it reads in loops over a
# tuple included; the harvest test below fails when the source grows a
# literal key missing here
KEYS = """
password totp_code recovery_code llm_backends smtp_host llm_url list_versions
food_monthly other_monthly budgeted_income_monthly dynamic_variable_budget
plaid_enrich_cap wizard_done wizard_steps biz_wizard_steps ret_wizard_steps
ret_wizard_answers ret_wizard_ssage ret_wizard_roth_mo ret_answers_v email_schedule
merchant_logos plaid_recurring
feedback_enabled timezone timezone_adopted home_web home_mobile theme
history_months bill_detection_auto setup_mode today_view smtp_port smtp_user
smtp_password smtp_from smtp_starttls custom_buckets email_recipients
email_recipients_added email_recipients_removed email_muted email_muted_add
email_muted_remove email_prefs_set email_send_hour_utc birthdate
retirement_saving income_biweekly_saving income_biweekly_not_saving
income_scenarios active_scenario savings_goals manual_assets ss_estimates
advisory_fees advisory_fee_exempt_masks investment_basis_override
advisory_fees_base advisory_fee_exempt_masks_base
investment_basis_override_base llm_model llm_api_key llm_vision_model
llm_extra_body llm_roles taxdocs_allow_remote_llm
""".split()

# Every key is read only through `fields` now (typed accessors, or
# `opaque`/`opaque_rows` handed to the cleaner that checks each entry).
CONVERTED = frozenset(KEYS)

_KEY_PATTERNS = (
    r'body\.(?:get)\(\s*"([A-Za-z_]+)"',
    r'body\[\s*"([A-Za-z_]+)"\s*\]',
    r'"([A-Za-z_]+)"\s+(?:not\s+)?in\s+body\b',
    r'fields\.(?:text|ident|flag|money|int_in|date|one_of|list_of|present'
    r'|sub|opaque|opaque_rows|json_type|money_given'
    r'|text_or_int|text_or_list|number)\(\s*"([A-Za-z_]+)"',
)


def _handler_src() -> str:
    from oikonome.web import api
    return inspect.getsource(api.settings_set)


def _raw_literal_reads(src: str) -> dict[str, list[int]]:
    """Literal keys read raw off `body`: `body.get("k")`, `body["k"]`,
    `"k" in body` / `not in body`, with the lines they sit on."""
    out: dict[str, list[int]] = {}
    for node in ast.walk(ast.parse(src)):
        key = None
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "body" and node.args
                and isinstance(node.args[0], ast.Constant)):
            key = node.args[0].value
        elif (isinstance(node, ast.Subscript)
              and isinstance(node.value, ast.Name)
              and node.value.id == "body"
              and isinstance(node.slice, ast.Constant)):
            key = node.slice.value
        elif (isinstance(node, ast.Compare)
              and isinstance(node.left, ast.Constant)
              and any(isinstance(op, (ast.In, ast.NotIn))
                      for op in node.ops)
              and any(isinstance(c, ast.Name) and c.id == "body"
                      for c in node.comparators)):
            key = node.left.value
        if isinstance(key, str):
            out.setdefault(key, []).append(node.lineno)
    return out


def _tuple_loop_keys(src: str) -> set[str]:
    """Keys a `for k in ("a", "b"): ... k in body / body[k]` loop reads raw."""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        var = node.target.id
        raw = False
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Subscript)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "body"
                    and isinstance(inner.slice, ast.Name)
                    and inner.slice.id == var):
                raw = True
            if (isinstance(inner, ast.Compare)
                    and isinstance(inner.left, ast.Name)
                    and inner.left.id == var
                    and any(isinstance(c, ast.Name) and c.id == "body"
                            for c in inner.comparators)):
                raw = True
        if raw:
            for elt in node.iter.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys.add(elt.value)
                elif isinstance(elt, ast.Tuple) and elt.elts and isinstance(
                        elt.elts[0], ast.Constant):
                    keys.add(elt.elts[0].value)
    return keys


def _tuple_loop_fields_keys(src: str) -> set[str]:
    """Keys a loop or a comprehension over a literal tuple hands to a typed
    read — `for k in ("a", "b"): fields.<accessor>(k)`, `any(fields.present(k)
    for k in (...))`, `{k: _scalar_text(body, k) for k in (...)}`."""
    keys: set[str] = set()
    loops = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.For):
            loops.append((node.target, node.iter, node))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            for gen in node.generators:
                loops.append((gen.target, gen.iter, node))
    for target, it, scope in loops:
        if not (isinstance(target, ast.Name)
                and isinstance(it, (ast.Tuple, ast.List))):
            continue
        var = target.id
        if any(isinstance(inner, ast.Call)
               and any(isinstance(a, ast.Name) and a.id == var
                       for a in inner.args)
               for inner in ast.walk(scope)):
            keys |= {e.value for e in it.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return keys


class SettingsDoorReadsThroughTypedFields(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.web import security
        cls.security = security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app, raise_server_exceptions=False)
        cls.owner_email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.owner.post("/api/signup", data={
            "email": cls.owner_email, "password": PW})
        assert r.status_code == 200, r.text
        cls.owner.post("/api/auth/elevate", json={"password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        r = cls.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        cls.member = TestClient(appmod.app, raise_server_exceptions=False)
        cls.me = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = cls.member.post("/api/invite/claim", json={
            "token": token, "email": cls.me, "password": MEMBER_PW})
        assert got.status_code == 200, got.text

    def _post(self, client, body):
        self.security._limiter._hits.clear()
        return client.post("/api/settings", json=body)

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    # ---- the matrix ----------------------------------------------------

    def test_the_key_list_covers_every_key_the_handler_names(self):
        src = _handler_src()
        named = set()
        for pat in _KEY_PATTERNS:
            named |= set(re.findall(pat, src))
        named |= _tuple_loop_keys(src)
        # `k + "_base" in body` is the three *_base keys, listed by name
        named.discard("_base")
        self.assertEqual(sorted(named - set(KEYS)), [])

    def test_every_key_sent_wrongly_typed_never_answers_5xx(self):
        failures = []
        for who, client in (("owner", self.owner), ("member", self.member)):
            for key in KEYS:
                for bad in BAD_VALUES:
                    r = self._post(client, {key: bad})
                    if r.status_code >= 500:
                        failures.append(f"{who} {key}={str(bad)[:20]!r} -> "
                                        f"{r.status_code}")
        self.assertEqual(failures, [])

    # ---- the clients' own bodies ---------------------------------------

    # (writer, body, what the stored document then holds). One row per call
    # site that builds a body for a converted family, older shapes included.
    # `None` as an expected value means the key is absent once stored.
    CLIENT_BODIES = [
        ("web App theme", {"theme": "light"}, {"theme": "light"}),
        ("mobile welcome theme auto", {"theme": "auto"}, {"theme": "auto"}),
        ("web Today face", {"today_view": "detail"}, {"today_view": "detail"}),
        ("mobile Today face back", {"today_view": "summary"},
         {"today_view": None}),
        ("legacy Today alias", {"today_view": "advanced"},
         {"today_view": "detail"}),
        ("legacy Today alias simple", {"today_view": "simple"},
         {"today_view": None}),
        ("web home pages", {"home_web": "/bills", "home_mobile": "index"},
         {"home_web": "/bills", "home_mobile": None}),
        ("web App adopts the device zone",
         {"timezone": "America/Denver", "timezone_adopted": True},
         {"timezone": "America/Denver"}),
        ("web Settings follows the instance", {"timezone": ""},
         {"timezone": None}),
        ("mobile notifications zone", {"timezone": "Europe/Berlin"},
         {"timezone": "Europe/Berlin"}),
        ("web Welcome step mark",
         {"wizard_steps": {"appearance": "done", "connect": "skipped"}},
         {"wizard_steps": {"appearance": "done", "connect": "skipped"}}),
        ("web restart sends all nulls",
         {"wizard_done": False, "wizard_steps": {
             s: None for s in ("appearance", "connect", "sync", "import",
                               "bills", "budgets", "email", "finish")}},
         {"wizard_done": False, "wizard_steps": {}}),
        ("mobile welcome finish",
         {"wizard_done": True, "wizard_steps": {"finish": "done"}},
         {"wizard_done": True, "wizard_steps": {"finish": "done"}}),
        # a blank step map has always been an empty one: nothing changes
        ("a script's blank step maps",
         {"wizard_steps": "", "biz_wizard_steps": "",
          "ret_wizard_steps": "", "ret_wizard_answers": ""}, {}),
        ("business wizard", {"biz_wizard_steps": {"what": "done"}},
         {"biz_wizard_steps": {"what": "done"}}),
        # an older app build (1.2.52 to 1.2.62) marks the retirement setup
        # with this body alone, and saves its schedule card on its own
        ("an older app build finishes the retirement setup",
         {"ret_wizard_steps": {"done": "done"}},
         {"ret_wizard_steps": {"done": "done"}}),
        ("an older app build saves only its schedule",
         {"email_schedule": {"daily": {"on": True, "hour": 7,
                                       "summary": False}}}, {}),
        ("retirement setup done", {"ret_wizard_steps": {"done": "skipped"}},
         {"ret_wizard_steps": {"done": "skipped"}}),
        ("retirement wizard answers, a blank box is null",
         {"ret_wizard_steps": {"age": "done"},
          "ret_wizard_answers": {"age": 61, "spend": None}},
         {"ret_wizard_steps": {"age": "done"},
          "ret_wizard_answers": {"age": 61}}),
        ("retirement wizard assumptions step, marked, claim age beside",
         {"ret_wizard_steps": {"assumptions": "done"}, "ret_answers_v": 2,
          "ret_wizard_answers": {"ret": 6.5, "infl": 3, "end": None},
          "ret_wizard_ssage": 66},
         {"ret_wizard_steps": {"assumptions": "done"},
          # the age the previous row saved unmarked stays, unmarked
          "ret_wizard_answers": {"age": 61, "ret": 6.5, "infl": 3,
                                 "ssage": 66,
                                 "answers_v2": ["infl", "ret", "ssage"]}}),
        ("legacy retirement step with the birthdate",
         {"ret_wizard_steps": {"done": "done"}, "birthdate": "1971-04-02"},
         {"ret_wizard_steps": {"done": "done"}, "birthdate": "1971-04-02"}),
        ("mobile birthdate", {"birthdate": " 1980-12-31 "},
         {"birthdate": "1980-12-31"}),
        ("fine-tune flex budget", {"dynamic_variable_budget": True},
         {"dynamic_variable_budget": True}),
        ("display toggles off",
         {"merchant_logos": False, "plaid_recurring": False,
          "feedback_enabled": False},
         {"merchant_logos": False, "plaid_recurring": False,
          "feedback_enabled": False}),
        # a script's blank has always read as the default, on
        ("a blank display toggle is back to on",
         {"merchant_logos": "", "plaid_recurring": "",
          "feedback_enabled": ""},
         {"merchant_logos": None, "plaid_recurring": None,
          "feedback_enabled": None}),
        ("display toggles off again",
         {"merchant_logos": False, "plaid_recurring": False,
          "feedback_enabled": False},
         {"merchant_logos": False}),
        ("display toggles on",
         {"merchant_logos": True, "plaid_recurring": True,
          "feedback_enabled": True},
         {"merchant_logos": None, "plaid_recurring": None,
          "feedback_enabled": None}),
        ("history choice", {"history_months": 6}, {"history_months": 6}),
        ("history choice everything", {"history_months": None},
         {"history_months": None}),
        ("history choice", {"history_months": 3}, {"history_months": 3}),
        ("a blank history choice is everything", {"history_months": ""},
         {"history_months": None}),
        ("bills page detection off", {"bill_detection_auto": False},
         {"bill_detection_auto": False}),
        ("welcome manual setup",
         {"setup_mode": "manual", "bill_detection_auto": True},
         {"setup_mode": "manual", "bill_detection_auto": True}),
        ("welcome auto setup", {"setup_mode": "auto"}, {"setup_mode": "auto"}),
        ("enrich cap", {"plaid_enrich_cap": 250},
         {"plaid_enrich_cap": 250}),
        ("enrich cap typed as text by a script", {"plaid_enrich_cap": "40"},
         {"plaid_enrich_cap": 40}),
        ("web schedule card",
         {"email_schedule": {
             "daily": {"on": True, "hour": 7, "sms": False, "push": True,
                       "summary": True},
             "monthly": {"on": False, "hour": 8, "sms": False,
                         "push": False},
             "yearly": {"on": True, "hour": 9, "sms": False,
                        "push": False}}},
         {"email_schedule": {
             "daily": {"on": True, "hour": 7, "push": True,
                       "summary": True},
             "monthly": {"on": False, "hour": 8},
             "yearly": {"on": True, "hour": 9}}}),
        # the money knobs: a text box's string, "" for a blank box, or a
        # number from a stepper or a script
        ("web budget card typed", {"food_monthly": "1,250.50",
                                   "other_monthly": "300"},
         {"food_monthly": 1250.5, "other_monthly": 300.0}),
        ("mobile budget numbers", {"food_monthly": 640, "other_monthly": 0},
         {"food_monthly": 640.0, "other_monthly": 0.0}),
        ("a blank budget box is 0", {"other_monthly": ""},
         {"other_monthly": 0.0}),
        ("a negative budget is held at 0", {"food_monthly": "-5"},
         {"food_monthly": 0.0}),
        ("budgeted income typed", {"budgeted_income_monthly": "4,200"},
         {"budgeted_income_monthly": 4200.0}),
        ("budgeted income cleared by a blank box",
         {"budgeted_income_monthly": ""}, {"budgeted_income_monthly": None}),
        ("legacy biweekly pair",
         {"income_biweekly_saving": "1,900.25",
          "income_biweekly_not_saving": 2100},
         {"income_biweekly_saving": 1900.25,
          "income_biweekly_not_saving": 2100.0}),
        ("legacy biweekly pair cleared",
         {"income_biweekly_saving": "", "income_biweekly_not_saving": 0},
         {"income_biweekly_saving": None,
          "income_biweekly_not_saving": None}),
        ("legacy schedule with a weekly cadence echoed",
         {"email_schedule": {
             "daily": {"on": True, "hour": 6},
             "weekly": {"on": True, "hour": 7, "weekday": 1},
             "monthly": {"on": True, "hour": 8},
             "yearly": {"on": False, "hour": 9}}},
         {"email_schedule": {
             "daily": {"on": True, "hour": 6},
             "monthly": {"on": True, "hour": 8},
             "yearly": {"on": False, "hour": 9}}}),
    ]

    def test_every_client_body_saves_and_stores_what_it_meant(self):
        for writer, body, want in self.CLIENT_BODIES:
            with self.subTest(writer):
                if "timezone_adopted" in body:
                    conn = tenancy.tenant_connect(self.tid)
                    try:
                        with budget.config_txn(conn) as cfg:
                            cfg.pop("timezone", None)
                    finally:
                        conn.close()
                r = self._post(self.owner, body)
                self.assertEqual(r.status_code, 200, r.text)
                stored = self._stored()
                for k, v in want.items():
                    if isinstance(v, dict) and v and k.endswith("_steps"):
                        # step marks merge: each step posts its own
                        self.assertEqual(
                            {s: stored.get(k, {}).get(s) for s in v}, v, k)
                    else:
                        self.assertEqual(stored.get(k), v, k)

    def test_the_list_mail_and_ai_families_take_every_shipped_body(self):
        """The bodies the web Settings page and the mobile app (1.2.52 to
        1.2.63 build each of these the same way) send for the families
        read last: the mail relay, the AI card, the recipient and mute
        lists, the plan lists and the investment sections."""
        friend = "friend@example.dev"
        me = self.owner_email
        relay = {"smtp_host": "192.0.2.10", "smtp_port": "2525",
                 "smtp_user": "relay@example.dev", "smtp_from": "",
                 "smtp_starttls": False, "smtp_password": "relay-secret"}
        steps = [
            # the relay card: the port is the text box's string
            (relay, {"smtp_host": "192.0.2.10", "smtp_port": 2525,
                     "smtp_user": "relay@example.dev",
                     "smtp_from": "relay@example.dev",
                     "smtp_starttls": False}),
            # a blank port box is the submission port; no password keeps
            # the stored one
            (dict(relay, smtp_port="", smtp_password=None), {
                "smtp_port": 587}),
            ({k: v for k, v in relay.items() if k != "smtp_password"},
             {"smtp_port": 2525}),
            # the AI card: backends, roles, and the tax consent as "1"/""
            ({"llm_backends": [{"id": "lab", "name": "Lab",
                                "url": "http://192.0.2.20:11434/v1",
                                "model": "example-model",
                                "vision_model": "", "api_key": "",
                                "extra_body": ""}],
              "llm_roles": {"categorize": "lab", "assistant": "lab"},
              "taxdocs_allow_remote_llm": "1"},
             {"taxdocs_allow_remote_llm": "1"}),
            ({"llm_backends": [{"id": "lab", "name": "Lab",
                                "url": "http://192.0.2.20:11434/v1",
                                "model": "example-model"}],
              "llm_roles": {"categorize": "lab"},
              "taxdocs_allow_remote_llm": ""},
             {"taxdocs_allow_remote_llm": None}),
            # recipients: one-address delta beside the whole list
            ({"email_recipients_added": [friend],
              "email_recipients": [friend]}, {}),
            ({"email_muted_add": [friend], "email_muted": [friend]},
             {"muted": True}),
            ({"email_muted_remove": [friend], "email_muted": []},
             {"muted": False}),
            ({"email_prefs_set": {"address": me, "kind": "monthly",
                                  "on": False}}, {}),
            ({"email_recipients_removed": [friend],
              "email_recipients": []}, {}),
            # plan lists, whole-list replace
            ({"custom_buckets": [{"name": "Coffee", "parent": "food",
                                  "monthly": "55", "categories": [],
                                  "merchants": []}]}, {}),
            ({"income_scenarios": [{"name": "Base", "take_home": "2,100",
                                    "cadence": "biweekly"}],
              "active_scenario": "Base"}, {"active_scenario": "Base"}),
            ({"savings_goals": [{"name": "Trip", "target": "2,400",
                                 "monthly_plan": 200, "tokens": []}]}, {}),
            ({"manual_assets": [{"name": "Car", "kind": "vehicle",
                                 "value": 9000}]}, {}),
            ({"ss_estimates": {"62": 1800, "67": 2400}},
             {"ss_estimates": {"62": 1800.0, "67": 2400.0}}),
            ({"ss_estimates": {}}, {"ss_estimates": None}),
            ({"retirement_saving": True}, {"retirement_saving": True}),
            # the investment sections, each with the stored copy it began
            # from; an empty section clears
            ({"advisory_fees": {"example brokerage": 0.0025},
              "advisory_fees_base": STORED,
              "advisory_fee_exempt_masks": ["1234"],
              "advisory_fee_exempt_masks_base": STORED,
              "investment_basis_override": {"5678": 1250.5},
              "investment_basis_override_base": STORED},
             {"advisory_fees": {"example brokerage": 0.0025},
              "advisory_fee_exempt_masks": ["1234"],
              "investment_basis_override": {"5678": 1250.5}}),
            ({"advisory_fees": {},
              "advisory_fees_base": {"example brokerage": 0.0025},
              "advisory_fee_exempt_masks": [],
              "advisory_fee_exempt_masks_base": ["1234"],
              "investment_basis_override": {},
              "investment_basis_override_base": {"5678": 1250.5}},
             {"advisory_fees": None, "advisory_fee_exempt_masks": None,
              "investment_basis_override": None}),
        ]
        for body, want in steps:
            with self.subTest(keys=sorted(body)):
                self.owner.post("/api/auth/elevate", json={"password": PW})
                was = self._stored()
                # a section's base is the copy the page read
                body = {k: (was.get(k[:-len("_base")]) if v is STORED else v)
                        for k, v in body.items()}
                r = self._post(self.owner, body)
                self.assertEqual(r.status_code, 200, r.text)
                stored = self._stored()
                for k, v in want.items():
                    if k == "muted":
                        self.assertEqual(
                            friend in (stored.get("email_muted") or []), v)
                    else:
                        self.assertEqual(stored.get(k), v, k)
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                for k in ("smtp_host", "smtp_port", "smtp_user",
                          "smtp_password", "smtp_from", "llm_backends",
                          "llm_roles"):
                    cfg.pop(k, None)
        finally:
            conn.close()

    def test_a_wrong_kind_in_the_last_families_is_refused_by_name(self):
        for body, words in (
                ({"smtp_host": ["192.0.2.10"]}, "smtp_host must be a string"),
                ({"smtp_host": "192.0.2.10", "smtp_password": 1234},
                 "smtp_password must be a string"),
                ({"email_recipients": 7},
                 "email_recipients must be a list of addresses"),
                ({"email_muted_add": {"a": 1}},
                 "email_muted_add must be a list of addresses"),
                ({"custom_buckets": {"a": 1}}, "custom_buckets must be a list"),
                ({"ss_estimates": [1]},
                 "ss_estimates must be an object {age: monthly $}"),
                ({"llm_roles": [1]}, "llm_roles must be an object"),
                ({"taxdocs_allow_remote_llm": [1]},
                 "taxdocs_allow_remote_llm must be true or false")):
            with self.subTest(body=body):
                self.owner.post("/api/auth/elevate", json={"password": PW})
                r = self._post(self.owner, body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"], words)
                # a secret is never echoed back
                self.assertNotIn("1234", r.text)

    def test_a_money_knob_keeps_every_refusal_it_had(self):
        # null is what a client's NaN becomes on the wire: refused, never
        # stored as 0; and each refusal keeps its words, income's included
        for body, words in (
                ({"food_monthly": None}, "food_monthly must be a number"),
                ({"other_monthly": [1]}, "other_monthly must be a number"),
                ({"food_monthly": True}, "food_monthly must be a number"),
                ({"budgeted_income_monthly": None}, "income must be a number"),
                ({"budgeted_income_monthly": "lots"},
                 "income must be a number"),
                ({"income_biweekly_saving": {"a": 1}},
                 "income_biweekly_saving must be a number"),
                ({"food_monthly": 1e13}, "food_monthly is too large")):
            with self.subTest(body=body):
                r = self._post(self.owner, body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"], words)

    # ---- the shape -----------------------------------------------------

    def test_no_converted_key_is_read_raw(self):
        from oikonome.web import api

        from .test_bill_doors_refuse_wrong_types import _raw_reads
        self.assertEqual(_raw_reads(api.settings_set), [])
        src = _handler_src()
        raw = _raw_literal_reads(src)
        offenders = {k: v for k, v in raw.items() if k in CONVERTED}
        offenders.update({k: "tuple loop" for k in _tuple_loop_keys(src)
                          if k in CONVERTED})
        self.assertEqual(offenders, {})

    def test_every_converted_key_is_read_through_fields(self):
        src = _handler_src()
        named = set(re.findall(_KEY_PATTERNS[-1], src))
        named |= _tuple_loop_fields_keys(src)
        # the two helpers that read one key through the typed reader
        named |= set(re.findall(
            r'_(?:scalar_text|yes_word)\(\s*fields\s*,\s*"([A-Za-z_]+)"', src))
        # the helpers the door hands its typed body to read their keys
        # through it too
        from oikonome.web import api
        for helper in (api._refuse_moved_lists, api._merge_unseen_people,
                       api._apply_recipient_deltas):
            named |= set(re.findall(
                r'body\.\w+\(\s*"([A-Za-z_]+)"', inspect.getsource(helper)))
            named |= _tuple_loop_fields_keys(inspect.getsource(helper))
        # `<key>_base` is read beside its section, `key + "_base"`
        named |= {k + "_base" for k in named}
        # the homepage keys are read in a loop over homepages.VOCAB
        from oikonome import homepages
        named |= set(homepages.VOCAB)
        self.assertEqual(sorted(CONVERTED - named), [])


if __name__ == "__main__":
    unittest.main()
