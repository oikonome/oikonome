"""The app's published port can be pinned to one host address.

Behind a reverse proxy on the same host (the remedy for rootless Podman,
whose port forwarder hides every client's address) the app port must be
reachable on loopback ONLY, or a LAN client can skip the proxy and forge
X-Forwarded-For. A compose override cannot do that: compose merges an
override's `ports` list into the base list instead of replacing it, so the
every-interface binding stays and the loopback one then fails to bind. The
knob is therefore an env setting in the base file, and these tests render
it the way both compose engines do and assert exactly one binding.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker" / "compose.yaml"
ENV_EXAMPLE = REPO / "docker" / ".env.example"
DOC = REPO / "docs" / "reverse-proxy.md"
SCRIPT = REPO / "oikonome.sh"


def _app_ports_block() -> str:
    """The text of the app service's `ports:` entry, up to the next key at
    the same indent."""
    text = COMPOSE.read_text()
    app = text[text.index("\n  app:\n"):]
    app = app[:re.search(r"\n  [a-z_]+:\n", app[1:]).start() + 1]
    m = re.search(r"\n    ports:(.*?)(?=\n    [a-z_]+:)", app, re.S)
    assert m, "app service has no ports entry"
    return m.group(1)


def _interpolate(value: str, env: dict) -> str:
    """Compose's `${VAR:-default}`, the only form used here — every
    podman-compose release and Docker Compose read it the same way."""
    def sub(m):
        return env.get(m.group(1)) or m.group(2)
    out = re.sub(r"\$\{([A-Z_][A-Z0-9_]*):-([^}]*)\}", sub, value)
    assert "$" not in out, f"unsupported interpolation in {value!r}"
    return out


def _bindings(env: dict) -> list[str]:
    """Each published binding in the host-side form the engine hands to the
    runtime: host_ip:published:target, the host_ip left out when empty."""
    block = _app_ports_block()
    entries = re.split(r"\n\s+- ", "\n" + block.strip("\n"))[1:]
    if not entries:                       # a flow list: ["..."]
        return [_interpolate(v, env)
                for v in re.findall(r'"([^"]+)"', block)]
    out = []
    for e in entries:
        fields = dict(re.findall(r'(\w+):\s*"?([^"\n]*)"?', e))
        fields = {k: _interpolate(v, env) for k, v in fields.items()}
        host = fields.get("host_ip", "")
        port = f"{fields['published']}:{fields['target']}"
        out.append(f"{host}:{port}" if host else port)
    return out


class AppPortBindAddressTests(unittest.TestCase):

    def test_unset_publishes_on_every_interface_as_before(self):
        self.assertEqual(_bindings({}), ["8042:8080"])

    def test_loopback_setting_is_the_only_binding(self):
        self.assertEqual(
            _bindings({"OIKONOME_BIND_ADDRESS": "127.0.0.1",
                       "OIKONOME_PORT": "9042"}),
            ["127.0.0.1:9042:8080"])

    def test_podman_compose_renders_the_same_binding(self):
        try:
            import podman_compose
        except ImportError:
            self.skipTest("podman-compose not importable here")
        for host, want in (("", "8042:8080"),
                           ("127.0.0.1", "127.0.0.1:8042:8080")):
            self.assertEqual(podman_compose.port_dict_to_str(
                {"host_ip": host, "published": "8042", "target": 8080}),
                want)

    def test_the_setting_is_documented_where_operators_look(self):
        self.assertRegex(ENV_EXAMPLE.read_text(),
                         r"(?m)^#\s*OIKONOME_BIND_ADDRESS=127\.0\.0\.1")
        doc = DOC.read_text()
        self.assertIn("OIKONOME_BIND_ADDRESS=127.0.0.1", doc)
        self.assertIn("OIKONOME_BIND_ADDRESS=127.0.0.1", SCRIPT.read_text())
        # the override-file recipe must not come back as advice
        self.assertNotRegex(
            doc, r"compose override with\s+`ports:")


if __name__ == "__main__":
    unittest.main()
