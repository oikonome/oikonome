"""LLM categorization: the LOAD-BEARING flow-guard (LLM output must never
reclassify transfers/payments/income into counted spend), pluggable
backend (no-op when unconfigured; tenant config wins over OIKONOME_LLM_*
env), merchant/Amazon/summary/proposal-tag flows. Mocked HTTP — no live
model."""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from oikonome.engine import llm_categorize
from oikonome.engine.compat import as_date, jsonb

from .util import add_txn, make_db

ENV = {"OIKONOME_LLM_URL": "http://llm.test",
       "OIKONOME_LLM_MODEL": "test-model"}

PLAID_GENERIC = {"personal_finance_category": {
    "primary": "GENERAL_MERCHANDISE", "confidence_level": "HIGH"}}
PLAID_SHARP = {"personal_finance_category": {
    "primary": "FOOD_AND_DRINK", "confidence_level": "HIGH"}}
PLAID_SHARP_LOW = {"personal_finance_category": {
    "primary": "RENT_AND_UTILITIES", "confidence_level": "LOW",
    "detailed": "RENT_AND_UTILITIES_OTHER_UTILITIES"}}


def add_raw_txn(conn, tid, date, amount, merchant, *, primary=None,
                raw=None, override=None, account="card",
                plaid=None, plaid_detailed=None, plaid_confidence=None):
    raw = raw if raw is not None else {}
    pfc = raw.get("personal_finance_category") or {}
    plaid = plaid if plaid is not None else pfc.get("primary")
    plaid_detailed = (plaid_detailed if plaid_detailed is not None
                      else pfc.get("detailed"))
    plaid_confidence = (plaid_confidence if plaid_confidence is not None
                        else pfc.get("confidence_level"))
    conn.execute(
        """INSERT INTO transactions (id, account_id, date, amount, name,
               merchant_name, category_primary, category_override,
               category_plaid, category_plaid_detailed,
               category_plaid_confidence, pending, removed, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s)""",
        (tid, account, as_date(date), amount, merchant, merchant,
         primary, override, plaid, plaid_detailed, plaid_confidence,
         jsonb(raw)))
    return tid


def cache(conn, merchant, category):
    conn.execute(
        "INSERT INTO merchant_categories (merchant, category_primary) "
        "VALUES (%s,%s)", (merchant, category))


def reply_transport(reply: dict, calls=None, requests=None):
    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(reply)}}]})
    return httpx.MockTransport(handler)


def boom_transport():
    def handler(request):
        raise AssertionError("LLM must not be called")
    return httpx.MockTransport(handler)


class FlowGuardTests(unittest.TestCase):
    """The load-bearing rule: importer-assigned flow categories
    (TRANSFER_*, LOAN_PAYMENTS, INCOME) read as generic 'OTHER' from raw
    (no aggregator category), so without the guard the LLM would turn
    brokerage, retirement and crypto transfers into counted spend."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_flow_rows_never_pending(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 5000, "Meridian Brokerage",
                    primary="TRANSFER_OUT", raw={})
        add_raw_txn(self.conn, "t2", "2025-07-01", 800, "Loan Servicer",
                    primary="LOAN_PAYMENTS", raw={})
        add_raw_txn(self.conn, "t3", "2025-07-01", 100, "Employer",
                    primary="INCOME", raw={})
        add_raw_txn(self.conn, "t4", "2025-07-01", 42, "Mystery Shop",
                    primary=None, raw={})            # genuinely generic spend
        pend = {m["merchant"] for m in
                llm_categorize.pending_merchants(self.conn)}
        self.assertEqual(pend, {"Mystery Shop"})

    def test_apply_never_reclassifies_flow_rows(self):
        # adversarial cache: even a poisoned merchant_categories row must
        # not move a transfer into spend
        cache(self.conn, "Meridian Brokerage", "ENTERTAINMENT")
        cache(self.conn, "Mystery Shop", "FOOD_AND_DRINK")
        add_raw_txn(self.conn, "t1", "2025-07-01", 5000, "Meridian Brokerage",
                    primary="TRANSFER_OUT", raw={})
        add_raw_txn(self.conn, "t2", "2025-07-01", 42, "Mystery Shop",
                    primary="GENERAL_MERCHANDISE", raw=PLAID_GENERIC)
        n = llm_categorize.apply(self.conn)
        self.assertEqual(n, 1)                       # only the generic row
        rows = {r["id"]: r["category_primary"] for r in self.conn.execute(
            "SELECT id, category_primary FROM transactions")}
        self.assertEqual(rows["t1"], "TRANSFER_OUT")  # GUARD held
        self.assertEqual(rows["t2"], "FOOD_AND_DRINK")

    def test_generic_judged_on_original_raw_category(self):
        # HIGH-confidence sharp Plaid → never machine-reclassified,
        # even when the current column looks generic
        cache(self.conn, "Cafe X", "ENTERTAINMENT")
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "Cafe X",
                    primary="GENERAL_MERCHANDISE", raw=PLAID_SHARP)
        self.assertEqual(llm_categorize.apply(self.conn), 0)
        self.assertEqual(llm_categorize.pending_merchants(self.conn), [])

    def test_llm_rule_never_contradicts_a_concrete_plaid_primary(self):
        """Trust guard. An LLM rule may not move a row OUT of a concrete
        Plaid family, even at LOW confidence: most such moves contradict
        Plaid's own concrete label and are wrong (rent → Transportation, a
        creamery → Rent & Utilities because its name reads like a
        telecom). Accuracy is not served by letting a name-guessing layer
        overrule a concrete one."""
        cache(self.conn, "Lakeshore County Service", "GOVERNMENT_AND_NON_PROFIT")
        add_raw_txn(self.conn, "t1", "2025-07-01", 893, "Lakeshore County Service",
                    primary="RENT_AND_UTILITIES", raw=PLAID_SHARP_LOW)
        self.assertEqual(llm_categorize.apply(self.conn), 0)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "RENT_AND_UTILITIES")
        # with a cached rule the merchant is still not re-queued
        pend = {m["merchant"] for m in llm_categorize.pending_merchants(self.conn)}
        self.assertNotIn("Lakeshore County Service", pend)

    def test_llm_rule_still_refines_generic_plaid(self):
        """The guard must not kill the feature: GENERAL_*/OTHER buckets are
        exactly what LLM refinement exists for."""
        cache(self.conn, "Harvest Fresh Market", "FOOD_AND_DRINK")
        add_raw_txn(self.conn, "t2", "2025-07-02", 84, "Harvest Fresh Market",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category": {
                        "primary": "GENERAL_MERCHANDISE",
                        "confidence_level": "LOW"}})
        self.assertEqual(llm_categorize.apply(self.conn), 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t2'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_seed_rules_take_the_trust_guard_too(self):
        """Seed is not exempt for being "curated": the generated seed
        layer matches bare brand words by substring (a cafe sharing a word
        with a phone carrier takes the carrier's utilities label). No
        machine source outranks a concrete Plaid primary."""
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Volt Espresso', "
            "'RENT_AND_UTILITIES', 'seed')")
        add_raw_txn(self.conn, "t3", "2025-07-03", 7,
                    "Volt Espresso",
                    primary="FOOD_AND_DRINK",
                    raw={"personal_finance_category": {
                        "primary": "FOOD_AND_DRINK",
                        "confidence_level": "LOW"}})
        self.assertEqual(llm_categorize.apply(self.conn), 0)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t3'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_seed_rule_still_refines_generic_plaid(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Lakeshore County Service', "
            "'GOVERNMENT_AND_NON_PROFIT', 'seed')")
        add_raw_txn(self.conn, "t3b", "2025-07-03", 893,
                    "Lakeshore County Service",
                    primary="GENERAL_SERVICES",
                    raw={"personal_finance_category": {
                        "primary": "GENERAL_SERVICES",
                        "confidence_level": "LOW"}})
        self.assertEqual(llm_categorize.apply(self.conn), 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t3b'"
        ).fetchone()["category_primary"], "GOVERNMENT_AND_NON_PROFIT")

    def test_user_rule_still_beats_everything(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Telstar Creamery', 'FOOD_AND_DRINK', 'user')")
        add_raw_txn(self.conn, "t4", "2025-07-04", 26, "Telstar Creamery",
                    primary="RENT_AND_UTILITIES",
                    raw={"personal_finance_category": {
                        "primary": "RENT_AND_UTILITIES",
                        "confidence_level": "HIGH"}})
        self.assertEqual(llm_categorize.apply(self.conn), 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t4'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_override_and_inflow_rows_excluded(self):
        cache(self.conn, "Shop A", "TRAVEL")
        add_raw_txn(self.conn, "t1", "2025-07-01", 20, "Shop A",
                    raw=PLAID_GENERIC, override="Amazon - Apparel")
        add_raw_txn(self.conn, "t2", "2025-07-01", -20, "Shop B", raw={})
        self.assertEqual(llm_categorize.apply(self.conn), 0)
        self.assertEqual(llm_categorize.pending_merchants(self.conn), [])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_unconfigured_is_noop_but_still_applies_cache(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            cache(self.conn, "Mystery Shop", "FOOD_AND_DRINK")
            add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop",
                        raw={})
            stats = llm_categorize.run(self.conn, transport=boom_transport())
        self.assertFalse(stats["configured"])
        self.assertEqual(stats["rows_updated"], 1)   # cached map still applied
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_merchant_classification_end_to_end(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})
        calls = []
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn,
                transport=reply_transport({"1": "FOOD_AND_DRINK"}, calls))
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertEqual(stats["rows_updated"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='Mystery Shop'").fetchone()["category_primary"],
            "FOOD_AND_DRINK")
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")
        # request shape: model + JSON response format + the merchant listing
        self.assertEqual(calls[0]["model"], "test-model")
        self.assertIn("Mystery Shop", calls[0]["messages"][1]["content"])

    def test_flow_category_from_llm_is_rejected(self):
        """Even if the model answers with a flow category, it is not in
        CATEGORIES → dropped as unparseable, never cached."""
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, transport=reply_transport({"1": "TRANSFER_OUT"}))
        self.assertEqual(stats["merchants_classified"], 0)
        self.assertEqual(stats["unparseable"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='Mystery Shop'"
        ).fetchone())

    def test_extra_body_env_merged(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})
        calls = []
        env = dict(ENV, OIKONOME_LLM_EXTRA_BODY=(
            '{"chat_template_kwargs": {"enable_thinking": false}}'))
        with patch.dict(os.environ, env):
            llm_categorize.run(
                self.conn, transport=reply_transport({"1": "TRAVEL"}, calls))
        self.assertEqual(calls[0]["chat_template_kwargs"],
                         {"enable_thinking": False})

    def test_stored_non_object_extra_body_is_ignored_not_fatal(self):
        # settings saves refuse a non-object now, but env values and rows
        # stored before that check still exist — the call must degrade to
        # ignoring the value, not crash the pass with a TypeError
        calls = []
        be = {"url": "http://llm.test", "model": "m", "api_key": "",
              "extra_body": "[1, 2]", "vision_model": "", "source": "env"}
        content = llm_categorize._chat(
            [{"role": "user", "content": "hi"}], 50,
            transport=reply_transport({"1": "TRAVEL"}, calls), backend=be)
        self.assertTrue(content)
        self.assertEqual(calls[0]["model"], "m")        # body intact
        msg = llm_categorize.chat_tools(
            [{"role": "user", "content": "hi"}], tools=[], max_tokens=50,
            transport=reply_transport({"1": "TRAVEL"}, calls), backend=be)
        self.assertIn("content", msg)

    def test_dry_run_writes_nothing(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, dry_run=True,
                transport=reply_transport({"1": "TRAVEL"}))
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='Mystery Shop'"
        ).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], None)


class CategorizeNewTests(unittest.TestCase):
    """categorize_new is the sync-time pass — it classifies new unknown
    merchants (the sparse-SimpleFIN case), applies the cached map, and
    runs the Amazon chain (classify → match → summaries) so items show
    within the hour. Proposal tags stay nightly."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sfin_row(self, tid, merchant, amount=42, payee=None):
        # SimpleFIN shape: category_primary NULL, raw is the bank txn dict
        # with NO personal_finance_category, merchant_name = payee (often null)
        conn = self.conn
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, category_override,
                   pending, removed, raw)
               VALUES (%s,'card',%s,%s,%s,%s,NULL,NULL,0,0,%s)""",
            (tid, as_date("2025-07-01"), amount, merchant, payee,
             jsonb({"id": tid, "description": merchant, "amount": str(-amount)})))

    def test_simplefin_row_categorized_end_to_end(self):
        # opaque SimpleFIN merchant (NOT in the seed rules), no aggregator
        # category → classified by the LLM + applied
        self._sfin_row("s1", "BODEGA LUZ 42")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, transport=reply_transport({"1": "FOOD_AND_DRINK"}))
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertEqual(stats["rows_updated"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='s1'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_applies_cached_map_when_unconfigured(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            cache(self.conn, "BODEGA LUZ 42", "FOOD_AND_DRINK")
            self._sfin_row("s1", "BODEGA LUZ 42")
            stats = llm_categorize.categorize_new(
                self.conn, transport=boom_transport())   # LLM must not be hit
        self.assertFalse(stats["configured"])
        self.assertEqual(stats["rows_updated"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='s1'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_flow_guard_holds(self):
        # an importer transfer row reads generic from raw but must never be
        # classified/counted — same guard as run()
        self._sfin_row("s1", "Meridian Brokerage")
        self.conn.execute(
            "UPDATE transactions SET category_primary='TRANSFER_OUT' WHERE id='s1'")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, transport=boom_transport())
        self.assertEqual(stats["merchants_classified"], 0)

    def test_sync_pass_is_capped(self):
        # the first pass after enabling an LLM inherits the whole backlog —
        # the sync-time cap keeps that off the hourly sweep (nightly mops up)
        for i in range(3):
            self._sfin_row(f"s{i}", f"SHOP {i}")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, limit=2, transport=reply_transport(
                    {"1": "FOOD_AND_DRINK", "2": "TRAVEL"}))
        self.assertEqual(stats["merchants_classified"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM merchant_categories").fetchone()["n"], 2)

    def test_progress_advances_even_when_batches_fail(self):
        # a dead/misconfigured LLM failed every batch and the wizard's
        # progress row froze at 0/N — attempted merchants must still tick
        for i in range(3):
            self._sfin_row(f"s{i}", f"SHOP {i}")
        calls: list[tuple[int, int]] = []
        boom = httpx.MockTransport(lambda req: httpx.Response(500))
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, batch_size=2, transport=boom,
                progress=lambda d, t: calls.append((d, t)))
        self.assertEqual(calls, [(0, 3), (2, 3), (3, 3)])
        self.assertEqual(stats["unparseable"], 3)
        self.assertEqual(stats["merchants_classified"], 0)

    def test_amazon_chain_runs_hourly_proposals_stay_nightly(self):
        # a pending Amazon order + its bank charge exist: the sync-time pass
        # classifies the order, matches it to the charge and writes the item
        # summary — so items show within the hour, not only after the nightly
        # run. Proposal tags depend on nightly detection and must stay put.
        self.conn.execute(
            """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                   category, category_source, is_refund, items_json)
               VALUES ('o1','a',%s,-10,'Shopping','rules',0,%s)""",
            (as_date("2025-07-01"), jsonb([{"title": "thing", "price": 10}])))
        add_raw_txn(self.conn, "t1", "2025-07-02", 10, "AMAZON.COM", raw={})
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                   amount, frequency, "interval", evidence, status, created_at)
               VALUES ('prop:1','add','occurrence','Netflix',15.49,'MONTHLY',1,
                       %s,'pending',now())""", (jsonb({}),))
        with patch.dict(os.environ, ENV):
            # the mock answers every call with {"1": "Shopping"} — a valid
            # category for the classify pass, free text for the summary pass
            stats = llm_categorize.categorize_new(
                self.conn, transport=reply_transport({"1": "Shopping"}))
        self.assertEqual(self.conn.execute(
            "SELECT category_source FROM amazon_orders WHERE dedup_key='o1'"
        ).fetchone()["category_source"], "llm")
        self.assertEqual(self.conn.execute(
            "SELECT dedup_key FROM amazon_matches WHERE transaction_id='t1'"
        ).fetchone()["dedup_key"], "o1")
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM amazon_summaries WHERE dedup_key='o1'"
        ).fetchone()["summary"], "Shopping")
        self.assertEqual(stats["summaries_written"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT llm_tag FROM bill_proposals WHERE id='prop:1'"
        ).fetchone()["llm_tag"])                       # untouched

    def test_no_amazon_orders_is_a_noop_probe(self):
        # tenants without Amazon data must not pay for the chain — no match
        # recompute, no LLM traffic beyond the merchant pass
        self._sfin_row("s1", "BODEGA LUZ 42")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, transport=reply_transport({"1": "FOOD_AND_DRINK"}))
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertNotIn("summaries_written", stats)


class ConfigPrecedenceTests(unittest.TestCase):
    """Tenant-config llm_url/llm_model/llm_api_key (budget.load_config)
    take precedence over the OIKONOME_LLM_* env vars; the layers never mix
    (a tenant endpoint never receives the operator's env key/extra body)."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _set_cfg(self, **kw):
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg.update(kw)
        budget.save_config(self.conn, cfg)

    def _run_capture(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})
        calls, reqs = [], []
        stats = llm_categorize.run(
            self.conn,
            transport=reply_transport({"1": "TRAVEL"}, calls, reqs))
        return stats, calls, reqs

    def test_config_wins_over_env(self):
        from oikonome.db import crypto
        self._set_cfg(llm_url="http://cfg.test:9090", llm_model="cfg-model",
                      llm_api_key=crypto.encrypt(self.conn, "cfg-key"))
        env = dict(ENV, OIKONOME_LLM_API_KEY="env-key",
                   OIKONOME_LLM_EXTRA_BODY='{"env_only": true}')
        with patch.dict(os.environ, env):
            stats, calls, reqs = self._run_capture()
        self.assertTrue(stats["configured"])
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertEqual(reqs[0].url.host, "cfg.test")     # not llm.test
        self.assertEqual(reqs[0].url.port, 9090)
        self.assertEqual(calls[0]["model"], "cfg-model")   # not test-model
        self.assertEqual(reqs[0].headers["authorization"], "Bearer cfg-key")
        # operator env extra body must NOT reach a tenant endpoint
        self.assertNotIn("env_only", calls[0])

    def test_env_fallback_when_config_absent(self):
        env = dict(ENV, OIKONOME_LLM_API_KEY="env-key")
        with patch.dict(os.environ, env):
            stats, calls, reqs = self._run_capture()
        self.assertEqual(reqs[0].url.host, "llm.test")
        self.assertEqual(calls[0]["model"], "test-model")
        self.assertEqual(reqs[0].headers["authorization"], "Bearer env-key")

    def test_config_alone_configures_without_env(self):
        self._set_cfg(llm_url="http://cfg.test", llm_model="cfg-model")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(llm_categorize.configured(self.conn))
            self.assertFalse(llm_categorize.configured())   # env alone: no
            stats, calls, reqs = self._run_capture()
        self.assertTrue(stats["configured"])
        self.assertEqual(stats["merchants_classified"], 1)
        self.assertEqual(reqs[0].url.host, "cfg.test")
        self.assertNotIn("authorization", reqs[0].headers)  # no key: no header


class ParseTests(unittest.TestCase):
    def test_parse_extracts_json_and_validates(self):
        out = llm_categorize._parse(
            'Sure! {"1": "food_and_drink", "2": "PETS", "x": "TRAVEL"}', 2)
        self.assertEqual(out, {1: "FOOD_AND_DRINK"})  # PETS invalid, x invalid

    def test_parse_rejects_garbage(self):
        with self.assertRaises(llm_categorize.LLMError):
            llm_categorize._parse("no json here", 1)
        with self.assertRaises(llm_categorize.LLMError):
            llm_categorize._parse('{"1": "TRAVEL",}', 1)   # malformed

    def test_parse_freetext_zero_based(self):
        out = llm_categorize._parse_freetext('{"1": "paper towels", "2": ""}', 2)
        self.assertEqual(out, {0: "paper towels"})


class AmazonAndProposalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _order(self, key, category="Apparel", source="rules", refund=0,
               items=None, date="2025-07-01"):
        self.conn.execute(
            """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                   category, category_source, is_refund, items_json)
               VALUES (%s,'a',%s,-10,%s,%s,%s,%s)""",
            (key, as_date(date), category, source, refund,
             jsonb(items if items is not None else
                   [{"title": "blue cotton socks", "price": 10}])))

    def test_amazon_items_classified(self):
        self._order("o1", category="Shopping")       # pending (rules-sourced)
        self._order("o2", category="Apparel", source="llm")   # already done
        self._order("o3", category="Refunds", refund=1)       # refunds skipped
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, transport=reply_transport({"1": "Apparel"}))
        self.assertEqual(stats["pending_items"], 1)
        self.assertEqual(stats["items_classified"], 1)
        row = self.conn.execute(
            "SELECT category, category_source FROM amazon_orders "
            "WHERE dedup_key='o1'").fetchone()
        self.assertEqual((row["category"], row["category_source"]),
                         ("Apparel", "llm"))

    def test_amazon_categories_derived_without_refunds(self):
        self._order("o1", category="Tools")
        self._order("o2", category="Refunds", refund=1)
        cats = llm_categorize.amazon_categories(self.conn)
        self.assertIn("Tools", cats)
        self.assertIn("Shopping", cats)              # always assignable
        self.assertNotIn("Refunds", cats)

    def test_summaries_only_for_matched_orders(self):
        self._order("o1", source="llm")              # matched below
        self._order("o2", source="llm")              # NOT matched → no summary
        # a real transaction to match against — amazon_matches has an FK now
        add_txn(self.conn, "2026-07-02", 12.34, "AMAZON", account="chk",
                txn_id="t-x")
        self.conn.execute(
            "INSERT INTO amazon_matches (transaction_id, dedup_key) "
            "VALUES ('t-x', 'o1')")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, transport=reply_transport({"1": "cotton socks"}))
        self.assertEqual(stats["pending_summaries"], 1)
        self.assertEqual(stats["summaries_written"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM amazon_summaries WHERE dedup_key='o1'"
        ).fetchone()["summary"], "cotton socks")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM amazon_summaries WHERE dedup_key='o2'").fetchone())

    def _match(self, txn_id, key, date="2025-07-01", amount=10):
        add_txn(self.conn, date, amount, "AMAZON", account="chk",
                txn_id=txn_id)
        self.conn.execute(
            "INSERT INTO amazon_matches (transaction_id, dedup_key) "
            "VALUES (%s,%s)", (txn_id, key))

    def test_repeat_purchase_reuses_existing_summary(self):
        # o2 contains the same items as already-summarized o1 (listed in a
        # different order, prices drifted): the model must not be asked and
        # the exact text must carry over — repeat purchases reading 'honey'
        # one month and 'wildflower honey' the next is what this prevents
        self._order("o1", source="llm",
                    items=[{"title": "Raw Wildflower Honey", "price": 40.0},
                           {"title": "Cast Iron Pan", "price": 50.0}])
        self._order("o2", source="llm", date="2025-07-08",
                    items=[{"title": "Cast Iron Pan", "price": 52.0},
                           {"title": "Raw Wildflower Honey", "price": 41.0}])
        self._match("t-1", "o1")
        self._match("t-2", "o2", date="2025-07-08")
        self.conn.execute(
            "INSERT INTO amazon_summaries (dedup_key, summary) "
            "VALUES ('o1','wildflower honey, cast iron pan')")
        stats = {"unparseable": 0}
        with patch.dict(os.environ, ENV):
            llm_categorize._summarize_amazon(
                self.conn, limit=None, batch_size=20, dry_run=False,
                stats=stats, transport=boom_transport(),
                backend=llm_categorize._backend(self.conn))
        self.assertEqual(stats["summaries_reused"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM amazon_summaries WHERE dedup_key='o2'"
        ).fetchone()["summary"], "wildflower honey, cast iron pan")

    def test_duplicate_pending_orders_asked_once(self):
        # neither copy of the repeat purchase is summarized yet: ONE model
        # call covers both, and both rows get the identical text
        same = [{"title": "Paper Towels 12ct", "price": 55.0}]
        self._order("o1", source="llm", items=same)
        self._order("o2", source="llm", items=same, date="2025-07-08")
        self._match("t-1", "o1")
        self._match("t-2", "o2", date="2025-07-08")
        calls: list = []
        stats = {"unparseable": 0}
        with patch.dict(os.environ, ENV):
            llm_categorize._summarize_amazon(
                self.conn, limit=None, batch_size=20, dry_run=False,
                stats=stats,
                transport=reply_transport({"1": "paper towels"}, calls),
                backend=llm_categorize._backend(self.conn))
        self.assertEqual(len(calls), 1)
        self.assertEqual(stats["summaries_written"], 2)
        got = {r["summary"] for r in self.conn.execute(
            "SELECT summary FROM amazon_summaries")}
        self.assertEqual(got, {"paper towels"})

    def _proposal(self, pid="prop:1"):
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                   amount, frequency, "interval", evidence, status, created_at)
               VALUES (%s,'add','occurrence','Netflix',15.49,'MONTHLY',1,
                       %s,'pending',now())""", (pid, jsonb({})))

    def test_proposal_tags_valid_set_enforced(self):
        self._proposal("prop:1")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, transport=reply_transport({"1": "subscription"}))
        self.assertEqual(stats["tags_written"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT llm_tag FROM bill_proposals WHERE id='prop:1'"
        ).fetchone()["llm_tag"], "subscription")
        # invalid label never lands
        self._proposal("prop:2")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(
                self.conn, transport=reply_transport({"1": "scam"}))
        self.assertEqual(stats["tags_written"], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT llm_tag FROM bill_proposals WHERE id='prop:2'"
        ).fetchone()["llm_tag"])


class OutletRowsMoveWithWhatThePreviewCounted(unittest.TestCase):
    """A bulk merchant recategorize must write exactly the rows its preview
    counted and its undo snapshot captured.

    Both sides resolve a row's merchant, and they must resolve it the SAME
    way: by the DISPLAY key (outlet first), which is what the pages show and
    what data._merchant_targets — the single source of the "affected" count
    AND of the undo snapshot — groups by. Resolving rows by merchant_name
    instead would give a chain's fuel arm (outlet "Northwind Club Gas",
    merchant_name "Northwind Club") the warehouse's rule: recategorized,
    uncounted, and with no undo entry to put it back.
    """

    def setUp(self):
        self.conn = make_db()
        # same brand, two merchants as far as every page is concerned: the
        # fuel arm (identified into merchant_outlet at sync time) and the
        # warehouse it shares a descriptor with
        add_raw_txn(self.conn, "gas", "2026-07-10", 42.50, "Northwind Club",
                    primary="GENERAL_MERCHANDISE", raw=PLAID_GENERIC)
        self.conn.execute("UPDATE transactions SET merchant_outlet='Northwind Club Gas' "
                          "WHERE id='gas'")
        add_raw_txn(self.conn, "warehouse", "2026-07-11", 210.00, "Northwind Club",
                    primary="GENERAL_MERCHANDISE", raw=PLAID_GENERIC)

    def tearDown(self):
        self.conn.close()

    def _cats(self):
        return {r["id"]: r["category_primary"] for r in self.conn.execute(
            "SELECT id, category_primary FROM transactions")}

    def _moved(self, display, category):
        """Rows data.py would count/snapshot for `display`, and the rows
        apply() actually moves. They must be the same set."""
        from oikonome.web import data
        previewed = {r["id"] for r in data._merchant_targets(self.conn, display)
                     if r["category_primary"] != category}
        before = self._cats()
        llm_categorize.apply(self.conn, canon=display)
        after = self._cats()
        return previewed, {i for i, c in after.items() if c != before[i]}

    def test_a_rule_on_the_outlet_moves_the_outlet_row(self):
        cache(self.conn, "Northwind Club Gas", "TRANSPORTATION")
        previewed, moved = self._moved("Northwind Club Gas", "TRANSPORTATION")
        self.assertEqual(previewed, {"gas"})
        self.assertEqual(moved, previewed)
        self.assertEqual(self._cats()["warehouse"], "GENERAL_MERCHANDISE")

    def test_a_rule_on_the_brand_leaves_the_outlet_row_alone(self):
        # the un-undoable case: the outlet row is not in the brand's preview,
        # so the brand's write must not reach it either
        cache(self.conn, "Northwind Club", "FOOD_AND_DRINK")
        previewed, moved = self._moved("Northwind Club", "FOOD_AND_DRINK")
        self.assertEqual(previewed, {"warehouse"})
        self.assertEqual(moved, previewed)
        self.assertEqual(self._cats()["gas"], "GENERAL_MERCHANDISE")

    def test_the_categorizer_learns_the_outlet_as_its_own_merchant(self):
        # the candidate set is keyed the same way, so a rule learned for a
        # pending merchant always reaches the rows it was learned from
        pend = {m["merchant"] for m in
                llm_categorize.pending_merchants(self.conn)}
        self.assertEqual(pend, {"Northwind Club Gas", "Northwind Club"})


if __name__ == "__main__":
    unittest.main()
