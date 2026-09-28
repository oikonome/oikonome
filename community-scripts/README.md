# Community scripts

Bring-your-own data-source scripts: small, self-contained collectors that
run on **your** machine, on **your** credentials, and push results into
your Oikonome instance through its documented doors (import hub,
purpose-built endpoints, or container exec).

**Read [docs/community-scripts.md](../docs/community-scripts.md) first** —
it defines the contract, the folder convention, the rules (stable ids,
sign convention, credentials never in a shared checkout), and the support
boundary: these are examples, not supported product surface.

| Script | Status | Door | Notes |
|--------|--------|------|-------|
| [coinbase](coinbase/) | working example | container-exec | Reference implementation of the full bridge pattern. **The app has a native Coinbase connector — prefer it**; this exists to show the plug-in shape. |

Each folder is one script: `script.toml` manifest, `README.md` with setup,
the script itself, and a sample systemd `.service`/`.timer` pair whose
`ExecStartPost=` does the push.
