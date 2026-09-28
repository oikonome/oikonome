# Collectors as a first-class install feature

A self-hoster can enable a host-side collector the same way they connect
Plaid/SimpleFIN — without breaking the community-script contract
(collection stays on the user's machine, on their credentials; the app
only ever sees pushes through its documented doors).

## Design

1. **Collection stays host-side.** No bundled scraping containers: the
   browser-scrape halves (bank portals, bot detection, MFA) are exactly
   the brittleness the contract keeps out of the product. What the
   product ships is the *rail*: auth, install, observability.
2. **Script tokens authenticate pushes.** Control-plane table
   `api_tokens` (migration 017; like sessions — lookup precedes tenant
   scope): `oik_` + 32 random bytes, sha256 at rest, shown once,
   revocable, last-used stamped. The auth layer accepts them ONLY on
   `/api/import*` and `/api/accounts/balance` — a leaked collector token
   can push data in, never read the ledger or change settings. Minted in
   Settings → Connections → Script tokens. email+password login remains
   a script-side fallback.
3. **One-command install**: `./oikonome.sh script [list|add|remove]` —
   copies the community script to `~/.local/share/oikonome/scripts/<n>`,
   writes a `credentials.json` skeleton pointed at the instance (0600),
   installs the systemd user units with sample paths rewritten and
   `OIKONOME_CREDENTIALS` pinned per script. Nothing runs until the user
   enables the timer. `remove` disables the timer but keeps the folder
   (it may hold credentials).
4. **Coinbase**: the native connector is the recommended path; the
   standalone script is the container-exec reference implementation.

Tests cover mint/list (no hash shown), bearer imports through the hub,
ledger/settings/tokens 403 on token auth, revoked → 401, garbage → 401/404.

## Not built

- Import-hub heartbeats stamp by ITEM (`manual`), not by script name —
  a push could carry an optional `X-Oikonome-Source` label so Doctor's
  collectors panel names the script rather than the account's item.
- Per-token rate limits for instances that expose the doors publicly
  (tokens otherwise work unchanged on a multi-tenant instance).
