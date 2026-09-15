"""The Settings view's endpoint-locality verdict must be earned by address.

"local" beside an LLM endpoint tells the user their data never leaves the
machine, and clients render consent framing around it — so a name that
merely looks internal (dotless, ``.local``, ``.internal``) but resolves to
the public internet must not be judged local. Genuinely-local endpoints —
loopback, RFC-1918, a compose service name that resolves privately or not
at all — must keep the local verdict so self-host on-box LLMs read
correctly.
"""
import unittest
from unittest import mock

from oikonome.web import api, netguard


def _resolver(mapping):
    """Pin DNS for the test: hosts absent from the mapping don't resolve."""
    return mock.patch.object(netguard, "_resolved_ips",
                             side_effect=lambda h: mapping.get(h, []))


class LlmEndpointLocalityTests(unittest.TestCase):

    def setUp(self):
        api._LLM_DNS_CACHE.clear()

    def test_public_resolving_name_is_not_local(self):
        # a local-looking NAME pointing at the public internet: the data
        # leaves the box, so the verdict must not say otherwise
        with _resolver({"ollama": ["8.8.8.8"],
                        "llm.local": ["1.1.1.1"],
                        "ai.internal": ["9.9.9.9"]}):
            for url in ("http://ollama:11434", "http://llm.local/v1",
                        "https://ai.internal/v1"):
                self.assertFalse(api._llm_is_local(url), url)

    def test_partly_public_resolution_is_not_local(self):
        # one private and one public address: data can still leave
        with _resolver({"dual": ["10.0.0.5", "8.8.8.8"]}):
            self.assertFalse(api._llm_is_local("http://dual:11434"))

    def test_ipv4_mapped_public_literal_is_not_local(self):
        # the mapped spelling routes to the embedded v4 destination, so it
        # must be judged as that address, not as an exotic v6 range
        self.assertFalse(
            api._llm_is_local("http://[::ffff:8.8.8.8]:11434/v1"))

    def test_loopback_and_private_literals_stay_local(self):
        for url in ("http://127.0.0.1:11434", "http://[::1]:11434",
                    "http://192.168.1.20:8080/v1",
                    "http://[::ffff:10.0.0.5]:11434"):
            self.assertTrue(api._llm_is_local(url), url)

    def test_private_resolving_name_is_local(self):
        with _resolver({"ollama": ["10.89.0.4"]}):
            self.assertTrue(api._llm_is_local("http://ollama:11434"))

    def test_unresolvable_compose_and_mdns_names_stay_local(self):
        # invisible from this process (compose DNS, mDNS): nothing can be
        # reached, so nothing leaves — the name heuristic survives only here
        with _resolver({}):
            self.assertTrue(api._llm_is_local("http://ollama:11434"))
            self.assertTrue(api._llm_is_local("http://box.local:11434"))
            self.assertTrue(api._llm_is_local("http://localhost:8080/v1"))
            # an unresolvable dotted public-style name was never local
            self.assertFalse(api._llm_is_local("https://api.example.com/v1"))

    def test_verdict_reuses_netguards_address_policy(self):
        # the classification lives in netguard (including the IPv4-mapped
        # fold); the verdict must call it, not carry a private copy
        with _resolver({"ollama": ["10.0.0.5"]}), \
             mock.patch.object(netguard, "_check_ip",
                               wraps=netguard._check_ip) as chk:
            api._llm_is_local("http://ollama:11434")
        chk.assert_called_once_with("10.0.0.5", what=mock.ANY, hosted=True)

    def test_resolution_is_cached_briefly(self):
        with _resolver({"ollama": ["10.0.0.5"]}) as res:
            api._llm_is_local("http://ollama:11434")
            api._llm_is_local("http://ollama:11434/v1")
        self.assertEqual(res.call_count, 1)


if __name__ == "__main__":
    unittest.main()
