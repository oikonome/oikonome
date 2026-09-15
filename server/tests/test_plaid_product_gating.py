"""tier-gated Plaid product attachment + the never-refresh
rule.

Liabilities and Investments are separately-billed per-Item subscriptions,
so a HOSTED link requests ["transactions"] only until the plan signal (or
OIKONOME_PLAID_PRODUCTS_EXTRA) says otherwise — the operator pays that bill.
Off-hosted the owner pays their own, and the link asks for both by default,
because that is what `sync_products` pulls there: the two must agree, or
every Item answers ADDITIONAL_CONSENT_REQUIRED to a product nothing ever
asked consent for. Setting the variable empty opts out of both halves.
And /transactions/refresh is billed per request: the client must have NO
code path that touches it."""

import inspect
import json
import os
import unittest

import httpx

from oikonome.sync import plaid

from .util import make_db, write_config


def _capture_transport(captured: dict, inst_products=None, fail=()):
    def handler(request):
        path = request.url.path
        captured[path] = json.loads(request.content.decode() or "{}")
        if path in fail:
            return httpx.Response(500, text=json.dumps(
                {"error_code": "INTERNAL_SERVER_ERROR",
                 "error_type": "API_ERROR",
                 "error_message": "unexpected"}))
        if path == "/link/token/create":
            return httpx.Response(200, text=json.dumps(
                {"link_token": "lt-1",
                 "hosted_link_url": "https://hosted.plaid.com/x"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_x"}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"products": inst_products
                                 if inst_products is not None
                                 else ["transactions", "liabilities",
                                       "investments"]}}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class EnvMixin:
    _KEYS = ("OIKONOME_PLAID_PRODUCTS_EXTRA", "OIKONOME_HOSTED")

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()


class LinkTokenProductTests(EnvMixin, unittest.TestCase):
    def _create(self, inst_products=None, fail=(), **kw) -> dict:
        captured: dict = {}
        client = plaid.Client(
            "cid", "sec", plaid.ENV_URLS["sandbox"],
            transport=_capture_transport(captured, inst_products, fail))
        client.create_hosted_link("tenant-1", client_name="Oikonome", **kw)
        return captured["/link/token/create"]

    def test_hosted_default_is_transactions_only(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        body = self._create()
        self.assertEqual(body["products"], ["transactions"])
        self.assertNotIn("required_if_supported_products", body)
        self.assertEqual(body["transactions"],
                         {"days_requested": plaid.DAYS_REQUESTED})

    def test_self_host_asks_consent_for_what_its_sync_will_pull(self):
        """Off-hosted `sync_products` pulls liabilities and holdings on the
        owner's own Plaid account, so the link must ask for them. Linking
        without the consent and then requesting the product daily earns
        ADDITIONAL_CONSENT_REQUIRED on every Item, which the Accounts page
        reports as a "not shared" pill beside a grant button that carried
        the same empty list and so could never satisfy it."""
        body = self._create()
        self.assertEqual(body["products"], ["transactions"])
        self.assertEqual(body["required_if_supported_products"],
                         ["liabilities", "investments"])

    def test_self_host_opt_out_asks_for_nothing(self):
        """The opt-out is explicit and silences BOTH halves — see
        HostedProductsPullGateTests for the sync side."""
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "none"
        body = self._create()
        self.assertNotIn("required_if_supported_products", body)

    def test_extra_products_env_attaches(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities"
        body = self._create()
        self.assertEqual(body["products"], ["transactions"])
        self.assertEqual(body["required_if_supported_products"],
                         ["liabilities"])

    def test_extra_products_comma_list(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = \
            " liabilities , investments "
        self.assertEqual(plaid.extra_products(),
                         ["liabilities", "investments"])
        body = self._create()
        self.assertEqual(body["required_if_supported_products"],
                         ["liabilities", "investments"])

    def test_a_list_naming_no_product_asks_for_nothing(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = " , "
        body = self._create()
        self.assertNotIn("required_if_supported_products", body)

    def test_update_mode_sends_no_products(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        body = self._create(access_token="access-x")
        self.assertNotIn("products", body)
        self.assertNotIn("required_if_supported_products", body)

    def test_update_mode_requests_missing_consent(self):
        # An Item linked before the plan attached a billed product answers
        # ADDITIONAL_CONSENT_REQUIRED to that product forever; update mode
        # is the only door that can add the consent, so the update-mode
        # link token must ask for the plan's extra products.
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities,investments"
        body = self._create(access_token="access-x")
        self.assertEqual(body.get("additional_consented_products"),
                         ["liabilities", "investments"])

    def test_update_mode_consent_filtered_to_institution_support(self):
        # Unlike required_if_supported_products at link creation, update
        # mode HARD-REJECTS an unsupported product (INVALID_FIELD:
        # "investments not supported by <institution>"), so the request must
        # intersect with the institution's own product list.
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities,investments"
        body = self._create(access_token="access-x",
                            inst_products=["transactions", "liabilities"])
        self.assertEqual(body.get("additional_consented_products"),
                         ["liabilities"])

    def test_update_mode_no_supported_extra_omits_the_field(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "investments"
        body = self._create(access_token="access-x",
                            inst_products=["transactions"])
        self.assertNotIn("additional_consented_products", body)

    def test_a_failed_item_lookup_still_asks_for_the_full_consent(self):
        """The filter needs two Plaid calls, and either can fail on a
        hiccup, a rate limit, or a shape we did not expect. Dropping the
        consent request then would mean the repair door quietly stops
        repairing the missing consent — forever, since only update mode
        can add it. The deliberate choice is the opposite: send the full
        list and let Link name the unsupported product, which at worst
        fails the same repair that was already failing."""
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities,investments"
        body = self._create(access_token="access-x", fail=("/item/get",))
        self.assertEqual(body.get("additional_consented_products"),
                         ["liabilities", "investments"])

    def test_a_failed_institution_lookup_still_asks_for_the_full_consent(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities,investments"
        body = self._create(access_token="access-x",
                            fail=("/institutions/get_by_id",))
        self.assertEqual(body.get("additional_consented_products"),
                         ["liabilities", "investments"])

    def test_update_mode_no_extra_no_consent_key(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "none"
        body = self._create(access_token="access-x")
        self.assertNotIn("additional_consented_products", body)


class ContainerEnvironmentTests(EnvMixin, unittest.TestCase):
    """The knob has to work in the shape a container actually delivers.

    compose materialises every key it declares, so "the variable left
    commented out" — the documented self-host default — reaches the app as
    an empty string. A test that deletes the key from os.environ measures a
    state no containerized install can reach, which is how a default that
    was dead in the standard deployment path stayed green in the suite."""

    def test_an_empty_forwarded_value_keeps_the_self_host_default(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = ""
        self.assertEqual(plaid.extra_products(),
                         ["liabilities", "investments"])

    def test_whitespace_is_the_same_as_empty(self):
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "   "
        self.assertEqual(plaid.extra_products(),
                         ["liabilities", "investments"])

    def test_an_empty_forwarded_value_attaches_nothing_on_hosted(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = ""
        self.assertEqual(plaid.extra_products(), [])

    def test_the_opt_out_is_a_value_an_operator_can_write_in_a_file(self):
        for spelling in ("none", "None", " NONE "):
            os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = spelling
            self.assertEqual(plaid.extra_products(), [], spelling)

    def test_the_example_env_documents_the_opt_out_the_code_honours(self):
        """`.env.example` is the contract an operator reads. If it names a
        spelling the code does not honour, the operator's opt-out silently
        becomes a product list."""
        from pathlib import Path
        example = (Path(__file__).resolve().parents[2]
                   / "docker" / ".env.example").read_text()
        self.assertIn(f"OIKONOME_PLAID_PRODUCTS_EXTRA={plaid.PRODUCTS_OPT_OUT}",
                      example)


class NeverRefreshTests(unittest.TestCase):
    def test_no_code_path_hits_transactions_refresh(self):
        """Hard rule: /transactions/refresh is billed per request.
        No string constant in the connector's CODE may name the endpoint
        (docstrings — which document the prohibition — are exempt); if
        this fails, someone added a refresh call: use /transactions/sync."""
        import ast
        tree = ast.parse(inspect.getsource(plaid))
        bad = "/transactions/" + "refresh"
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and \
                    isinstance(node.value, ast.Constant):
                continue                         # docstring statement
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Constant) and \
                        isinstance(child.value, str):
                    self.assertNotIn(bad, child.value)
        self.assertNotIn("refresh", [m for m in dir(plaid.Client)
                                     if not m.startswith("_")])


class HostedProductsPullGateTests(EnvMixin, unittest.TestCase):
    """sync_products on HOSTED must not touch /liabilities/get or
    /investments/holdings/get unless the plan signal enables them —
    requesting the endpoint can ATTACH the billed product to the Item."""

    def setUp(self):
        super().setUp()
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _run(self) -> list[str]:
        from oikonome.sync import base as sync_base
        sync_base.upsert_item(self.conn, "it-gate", "plaid", "Demo", "tok")
        hit: list[str] = []

        def handler(request):
            hit.append(request.url.path)
            return httpx.Response(200, text=json.dumps(
                {"liabilities": {"credit": [], "mortgage": [], "student": []},
                 "accounts": [], "securities": [], "holdings": []}))
        r = plaid.sync_products(self.conn, "it-gate",
                                transport=httpx.MockTransport(handler))
        self.assertEqual(r, {"liabilities": 0, "holdings": 0})
        return hit

    def test_hosted_ledger_skips_billed_products(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertEqual(self._run(), [])

    def test_hosted_with_extra_pulls_liabilities_only(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "liabilities"
        hit = self._run()
        self.assertIn("/liabilities/get", hit)
        self.assertNotIn("/investments/holdings/get", hit)

    def test_self_host_pulls_by_default(self):
        hit = self._run()
        self.assertIn("/liabilities/get", hit)
        # holdings endpoint is only queried when an investment account
        # exists (existing behavior) — no investment account seeded here
        self.assertNotIn("/investments/holdings/get", hit)

    def test_self_host_opt_out_pulls_nothing(self):
        """The opt-out reaches the sync too. It did not before: off-hosted
        this method ignored the variable entirely and pulled regardless,
        which is the half of the disagreement that spent the owner's money
        on a product their links had never consented to."""
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = "none"
        self.assertEqual(self._run(), [])

    def test_self_host_pulls_by_default_under_a_container_environment(self):
        """The variable an operator never set is PRESENT and empty in a
        container, because compose declares every key it forwards. That is
        the only shape a containerized install can produce for the
        documented default, and it must pull both products — reading it as
        the opt-out took liability detail and holdings away from every
        self-host that followed the docs."""
        os.environ["OIKONOME_PLAID_PRODUCTS_EXTRA"] = ""
        self.assertIn("/liabilities/get", self._run())


if __name__ == "__main__":
    unittest.main()
