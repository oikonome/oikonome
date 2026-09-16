#!/usr/bin/env python3
"""Coinbase community script — pull wallets + full transaction history from
the Coinbase App API (/v2, CDP ES256 keys) and push them into a self-hosted
Oikonome through the container-exec door.

============================================================================
NOTE: Oikonome has a NATIVE Coinbase connector (server/oikonome/sync/
coinbase.py, linked in-app via POST /api/accounts/coinbase/link). If you
just want Coinbase data, USE THAT — it stores your key encrypted at rest,
syncs hourly with everything else, and maintains crypto_holdings for net
worth. This script exists as the REFERENCE IMPLEMENTATION of the
community-script plug-in shape (docs/community-scripts.md): a real,
end-to-end example of collect → payload → container-exec push that you can
copy for sources the app has no connector for.
============================================================================

Two subcommands, matching the systemd ExecStart / ExecStartPost split:

  pull   Collect from the Coinbase API (or a local --fixture file) and
         write a payload JSON to the state path. Nothing touches the app.
  push   Pipe the payload into the app container and upsert it via the
         product's own oikonome.sync.base helpers. --dry-run prints what
         would be pushed without running the container.

Environment (all machine-specific config comes from env, nothing hardcoded):

  OIKONOME_CREDENTIALS     JSON file {"url": ..., "api_token": "oik_..."}
                           for the PREFERRED push path — over HTTP with a
                           scoped script token, no container access, from
                           any machine that can reach the app. Mint one
                           under Settings → Connections → Script tokens.
                           Default: credentials.json beside this script.
                           The container-exec knobs below are the fallback
                           for when this file is absent.
  OIKONOME_COINBASE_KEYS   directory of CDP key files, one <label>.json per
                           Coinbase login, exactly as Coinbase hands them
                           out: {"name": "organizations/.../apiKeys/...",
                           "privateKey": "-----BEGIN EC PRIVATE KEY..."}.
                           The filename stem is your label; item/account
                           ids become coinbase-<label>.
                           Default: ~/.config/oikonome-scripts/coinbase
                           (OUTSIDE this folder — never commit keys).
  OIKONOME_COINBASE_STATE  payload file written by pull, read by push.
                           Default: ~/.local/state/oikonome-scripts/
                           coinbase/payload.json
  OIKONOME_APP_CONTAINER   app container name (default oikonome-app-1;
                           podman-compose names it oikonome_app_1).
  OIKONOME_CONTAINER_ENGINE  podman (default) or docker.
  OIKONOME_TENANT_ID       optional; required only if the instance has
                           more than one active tenant.

Dependencies: stdlib only for `push` and `pull --fixture`; live `pull`
needs `httpx` and `cryptography` (pip install httpx cryptography).

Conventions honored (see docs/community-scripts.md):
  * stable original ids — item/account coinbase-<label>, transactions
    coinbase:<coinbase txn uuid> — so re-runs upsert, never duplicate.
    These match the native connector's ids, so if you later switch to it,
    it takes over the same rows instead of duplicating them.
  * every transaction is mapped to TRANSFER_IN/OUT (spend-excluded);
    crypto txn signs are informational, Coinbase's native_amount is kept
    verbatim. The account balance carries net worth.
  * restatements: rows with this script's id prefix that disappear from a
    pull are marked removed=1 in the app (soft, reversible), mirroring
    what the sync layer does for aggregator-removed rows.

API gotchas baked in:
  * The JWT `uri` claim must be METHOD + HOST + PATH only, NO query
    string, even though the request carries ?limit=/&starting_after=.
    Signing the query string yields 401.
  * Signature must be ES256 (ECDSA); Ed25519 keys are rejected by /v2.
  * /v2/accounts does not return native_balance, so holdings are valued
    via /v2/prices/<code>-USD/spot — and a failed spot lookup aborts the
    WHOLE balance computation rather than valuing that coin at $0, which
    would silently understate the account.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

HOST = "api.coinbase.com"
INSTITUTION = "Coinbase"
PREFIX = "coinbase:"           # transaction-id prefix; drives stale removal

KEY_DIR = Path(os.environ.get(
    "OIKONOME_COINBASE_KEYS",
    Path.home() / ".config" / "oikonome-scripts" / "coinbase"))
STATE_FILE = Path(os.environ.get(
    "OIKONOME_COINBASE_STATE",
    Path.home() / ".local" / "state" / "oikonome-scripts" / "coinbase"
    / "payload.json"))
CONTAINER = os.environ.get("OIKONOME_APP_CONTAINER", "oikonome-app-1")
ENGINE = os.environ.get("OIKONOME_CONTAINER_ENGINE", "podman")


# -- auth / client (live mode; imports deferred so stdlib-only paths work) --

def _make_client(key_name: str, priv_pem: str):
    import secrets

    import httpx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
    )

    def b64u(b: bytes) -> str:
        import base64
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    key = serialization.load_pem_private_key(priv_pem.encode(), password=None)

    def jwt_es256(path_only: str) -> str:
        """CDP App API JWT, ES256, raw r||s signature per RFC 7518."""
        now = int(time.time())
        header = {"typ": "JWT", "alg": "ES256", "kid": key_name,
                  "nonce": secrets.token_hex(16)}
        payload = {"sub": key_name, "iss": "cdp", "nbf": now,
                   "exp": now + 120, "uri": f"GET {HOST}{path_only}"}
        signing_input = (
            b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
            b64u(json.dumps(payload, separators=(",", ":")).encode()))
        der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        size = (key.curve.key_size + 7) // 8
        return signing_input + "." + b64u(r.to_bytes(size, "big") +
                                          s.to_bytes(size, "big"))

    class LiveClient:
        def __init__(self):
            self._http = httpx.Client(timeout=30)

        def get(self, full_path: str, tries: int = 6) -> dict:
            path_only = urlparse(full_path).path   # uri claim: never query
            last = ""
            for i in range(tries):
                tok = jwt_es256(path_only)
                r = self._http.get(
                    f"https://{HOST}{full_path}",
                    headers={"Authorization": f"Bearer {tok}",
                             "Accept": "application/json"})
                if r.status_code == 200:
                    return r.json()
                last = f"{r.status_code} {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                    time.sleep(min(2 ** i, 20))
                    continue
                raise RuntimeError(f"Coinbase {full_path}: {last}")
            raise RuntimeError(f"Coinbase retries exhausted {full_path}: {last}")

        def paginate(self, path: str):
            while path:
                body = self.get(path)
                yield from body.get("data", [])
                path = (body.get("pagination") or {}).get("next_uri") or None

        def spot(self, code: str, cache: dict):
            if code in cache:
                return cache[code]
            try:
                v = float(self.get(f"/v2/prices/{code}-USD/spot")
                          ["data"]["amount"])
            except Exception:                    # noqa: BLE001 — None = unpriced
                v = None
            cache[code] = v
            return v

    return LiveClient()


class FixtureClient:
    """Same interface as the live client, backed by a JSON file — lets you
    exercise the whole pull→payload→dry-run flow offline (and lets the
    repo unit-test this script without network or credentials). Schema:
    {"wallets": [...], "transactions": {"<wallet-id>": [...]},
     "spot": {"BTC": 60000.0}} — see example-fixture.json."""

    def __init__(self, data: dict):
        self._d = data

    def paginate(self, path: str):
        p = urlparse(path).path
        if p == "/v2/accounts":
            yield from self._d.get("wallets", [])
        else:                                   # /v2/accounts/<id>/transactions
            wid = p.split("/")[3]
            yield from (self._d.get("transactions") or {}).get(wid, [])

    def spot(self, code: str, cache: dict):
        v = (self._d.get("spot") or {}).get(code)
        cache[code] = v
        return v


# -- collect ---------------------------------------------------------------

def load_keys(key_dir: Path) -> dict[str, tuple[str, str]]:
    """label -> (key_name, private_key_pem) from <key_dir>/<label>.json"""
    out: dict[str, tuple[str, str]] = {}
    if key_dir.is_dir():
        for f in sorted(key_dir.glob("*.json")):
            d = json.loads(f.read_text())
            out[f.stem] = (d["name"], d["privateKey"])
    return out


def collect_label(client, label: str) -> tuple[dict, dict, list[dict]]:
    """One Coinbase login -> (item row, account row, transaction rows) in
    the bridge payload shape (same field names oikonome.sync.base expects)."""
    iid = aid = f"coinbase-{label}"
    spot_cache: dict = {}
    wallets = list(client.paginate("/v2/accounts?limit=100"))

    # Balance = sum of holdings × spot. Compute FIRST, all-or-nothing: a
    # missing spot price must abort rather than write a partial total.
    total_usd = 0.0
    missing: list[str] = []
    for w in wallets:
        code = w["balance"]["currency"]
        qty = float(w["balance"]["amount"])
        if qty == 0:
            continue
        is_crypto = (w.get("currency") or {}).get("type") == "crypto"
        px = client.spot(code, spot_cache) if is_crypto else 1.0  # fiat = USD
        if px is None:
            missing.append(code)
            continue
        total_usd += qty * px
    if missing:
        raise RuntimeError(
            f"coinbase {label}: no spot price for {missing} — refusing to "
            f"publish a partial balance")

    txns = []
    for w in wallets:  # zero-balance wallets still hold history
        for t in client.paginate(f"/v2/accounts/{w['id']}/transactions?limit=100"):
            nat = t.get("native_amount") or {}
            amount = float(nat.get("amount") or 0)  # CB sign: + = into wallet
            code = (t.get("amount") or {}).get("currency")
            ttype = t.get("type") or "unknown"
            txns.append({
                "id": f"{PREFIX}{t['id']}",         # stable original id
                "account_id": aid,
                "date": (t.get("created_at") or "")[:10],
                "amount": amount,                    # verbatim; spend-excluded
                "name": f"Coinbase {ttype}" + (f" {code}" if code else ""),
                "merchant_name": "Coinbase",
                # TRANSFER_* keeps crypto out of spend math; sign picks label
                "category_primary": "TRANSFER_OUT" if amount > 0
                                    else "TRANSFER_IN",
                "category_detailed": f"COINBASE_{ttype.upper()}",
                "pending": 0 if t.get("status") == "completed" else 1,
                "raw": t,                            # full object, kept in app
            })

    item = {"id": iid, "institution_name": INSTITUTION}
    account = {"id": aid, "item_id": iid, "name": f"Coinbase ({label})",
               "type": "investment", "subtype": "crypto", "mask": None,
               "balance_current": round(total_usd, 2),
               "balance_available": None, "currency": "USD", "raw": {}}
    return item, account, [t for t in txns
                           if t["date"] and t["amount"] is not None]


def build_payload(clients: dict[str, object]) -> dict:
    items, accounts, txns = [], [], []
    for label, client in clients.items():
        it, acct, ts = collect_label(client, label)
        items.append(it)
        accounts.append(acct)
        txns.extend(ts)
    # full_replace: this script always collects the COMPLETE transaction
    # history per label, so absence from the payload really means Coinbase
    # restated the row away. Scripts that push partial windows must omit
    # it — the app then only prunes inside each account's pushed window
    # and an empty pull can never wipe history.
    return {"source": "coinbase", "generated_at":
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prefix": PREFIX, "full_replace": True, "items": items,
            "accounts": accounts, "transactions": txns}


# -- push (endpoint door — preferred) -----------------------------------------

def _creds() -> dict | None:
    """credentials.json (OIKONOME_CREDENTIALS or alongside the script):
    {"url": ..., "api_token": "oik_..."} → push over HTTP with a scoped
    script token instead of exec'ing into the container. Recommended: no
    container access needed, works from any machine that reaches the app."""
    import json as _json, os as _os
    from pathlib import Path as _Path
    path = _os.environ.get("OIKONOME_CREDENTIALS") or str(
        _Path(__file__).resolve().parent / "credentials.json")
    try:
        c = _json.loads(open(path).read())
    except OSError:
        return None
    return (c if c.get("url") and str(c.get("api_token") or "").startswith("oik_") else None)


# A User-Agent is not decoration: an instance behind Cloudflare refuses the
# default "Python-urllib/3.x" signature outright (error 1010) before the
# request reaches the app, so a push to a proxied URL fails with a 403 that
# reads like a bad token. A push to localhost never shows it, because that
# does not cross the edge.
UA = "oikonome-collector/1 (+https://oikonome.com)"


def _extra_targets(creds: dict) -> list[dict]:
    """Optional `targets: [{"name", "url", "api_token"}]` in the
    credentials file — the same payload delivered to a second instance,
    e.g. a staging or mirror copy carrying the same data. Kept as one
    script with two doors rather than two scripts with one config each,
    because the second copy drifts the moment either is edited. The
    primary is what decides the exit code; an extra that is down must not
    make the collection look failed."""
    out = []
    for t in (creds.get("targets") or []):
        if isinstance(t, dict) and t.get("url") \
                and str(t.get("api_token") or "").startswith("oik_"):
            out.append({"name": t.get("name") or t["url"], "url": t["url"],
                        "api_token": t["api_token"]})
    return out


def _push_mirrors(payload: dict, creds: dict) -> None:
    """Deliver the same payload to every extra target, after the primary.

    Each is independent and none of them can change the exit code: a
    staging copy mid-deploy must not make a nightly timer report that the
    real collection failed. A failure is still SAID OUT LOUD on stderr —
    a mirror that quietly receives nothing looks exactly like a mirror
    that is working."""
    for t in _extra_targets(creds):
        try:
            print(json.dumps({"target": t["name"],
                              "result": push_http(payload, t)}))
        except Exception as exc:                       # noqa: BLE001
            print(json.dumps({"target": t["name"],
                              "error": str(exc)[:300]}), file=sys.stderr)


def push_http(payload: dict, creds: dict) -> dict:
    import json as _json, urllib.request as _rq
    req = _rq.Request(
        creds["url"].rstrip("/") + "/api/import/coinbase",
        data=_json.dumps(dict(payload, kind="api")).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Authorization": "Bearer " + creds["api_token"]})
    with _rq.urlopen(req, timeout=300) as resp:
        return _json.loads(resp.read().decode())


# -- push (container-exec door) ---------------------------------------------
#
# Runs INSIDE the app container via `<engine> exec -i <container> python -c`.
# The payload goes straight into the product's own push door,
# oikonome.sync.coinbase_push.import_payload — the exact code the HTTP
# token door runs. A second copy of the upsert+prune here would drift
# from the door's, so the bridge delegates rather than duplicating.
# That buys the door's namespace
# enforcement, row cap, override-preserving upserts and restatement
# semantics for free, forever in sync. Prints the door's JSON verification
# summary (plus the resolved tenant) on stdout.

BRIDGE_CODE = r"""
import json, sys
from oikonome.db import tenancy
from oikonome.sync import coinbase_push

payload = json.load(sys.stdin)
admin = tenancy.admin_connect()
try:
    tids = [str(r["id"]) for r in admin.execute(
        "SELECT id FROM tenants WHERE status='active'").fetchall()]
finally:
    admin.close()
tid = payload.get("tenant_id") or (tids[0] if len(tids) == 1 else None)
if not tid:
    sys.exit("bridge: need exactly one active tenant or an explicit "
             "tenant_id; active tenants: %r" % tids)

conn = tenancy.tenant_connect(tid)  # autocommit; RLS-scoped to the tenant
try:
    out = coinbase_push.import_payload(conn, payload)
    print(json.dumps(dict(out, tenant=tid), default=str))
finally:
    conn.close()
"""


def push(payload: dict, container: str, engine: str) -> dict:
    p = subprocess.run(
        [engine, "exec", "-i", container, "python", "-c", BRIDGE_CODE],
        input=json.dumps(payload).encode(), capture_output=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(
            f"push to {container} failed (rc={p.returncode}): "
            f"{p.stderr.decode()[-800:]}")
    return json.loads(p.stdout.decode())


# -- cli ---------------------------------------------------------------------

def cmd_pull(args) -> int:
    if args.fixture:
        data = json.loads(Path(args.fixture).read_text())
        clients = {args.label or "example": FixtureClient(data)}
    else:
        keys = load_keys(Path(args.keys))
        if not keys:
            print(f"no CDP key files in {args.keys} — see README.md",
                  file=sys.stderr)
            return 2
        if args.label:
            if args.label not in keys:
                print(f"unknown label '{args.label}'; have: "
                      f"{', '.join(keys)}", file=sys.stderr)
                return 2
            keys = {args.label: keys[args.label]}
        clients = {lbl: _make_client(name, pem)
                   for lbl, (name, pem) in keys.items()}

    payload = build_payload(clients)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload))
    print(json.dumps({"state": str(out),
                      "accounts": len(payload["accounts"]),
                      "transactions": len(payload["transactions"])}))
    return 0


def cmd_push(args) -> int:
    payload = json.loads(Path(getattr(args, "in")).read_text())
    if args.tenant_id:
        payload["tenant_id"] = args.tenant_id
    creds = _creds()
    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "door": (creds["url"] + "/api/import/coinbase (token)"
                     if creds else f"exec {args.container}"),
            "mirrors": [t["name"] for t in _extra_targets(creds or {})],
            "items": [i["id"] for i in payload["items"]],
            "accounts": {a["id"]: a["balance_current"]
                         for a in payload["accounts"]},
            "transactions": len(payload["transactions"])}))
        return 0
    if creds is not None:
        print(json.dumps(push_http(payload, creds)))
        _push_mirrors(payload, creds)
        return 0
    print(json.dumps(push(payload, args.container, args.engine)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Coinbase -> Oikonome community script "
                    "(see README.md; prefer the native connector)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="collect from Coinbase into a payload file")
    p.add_argument("--keys", default=str(KEY_DIR),
                   help="dir of CDP key files ($OIKONOME_COINBASE_KEYS)")
    p.add_argument("--label", default=None,
                   help="only this key label (default: all)")
    p.add_argument("--fixture", default=None,
                   help="offline: read wallets/txns from this JSON instead "
                        "of the live API (see example-fixture.json)")
    p.add_argument("--out", default=str(STATE_FILE),
                   help="payload path ($OIKONOME_COINBASE_STATE)")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("push", help="upsert the payload into the app container")
    p.add_argument("--in", default=str(STATE_FILE),
                   help="payload path ($OIKONOME_COINBASE_STATE)")
    p.add_argument("--container", default=CONTAINER,
                   help="app container name ($OIKONOME_APP_CONTAINER)")
    p.add_argument("--engine", default=ENGINE, choices=("podman", "docker"),
                   help="container engine ($OIKONOME_CONTAINER_ENGINE)")
    p.add_argument("--tenant-id", default=os.environ.get("OIKONOME_TENANT_ID"),
                   help="required only with >1 active tenant")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be pushed; no container call")
    p.set_defaults(fn=cmd_push)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
