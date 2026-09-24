# Reverse proxy + TLS

The app publishes on host port **8042** by default (`OIKONOME_PORT` in
`docker/.env`; the installer auto-picks the next free port if 8042 is
taken — adjust the examples to yours). Put a TLS proxy in front for
anything beyond your LAN. Session cookies are marked `Secure` only when
the request reached the app over HTTPS — directly, or through a trusted
proxy that says so (below) — so a plain-HTTP LAN install still signs in.
Or skip hand-rolling entirely: `./oikonome.sh https <domain>`
stands up a Caddy container for you (see the quickstart).

## Caddy (easiest)
```
budget.example.com {
    reverse_proxy 127.0.0.1:8042
}
```

## nginx
```
server {
    listen 443 ssl;
    server_name budget.example.com;
    # ... your ssl_certificate lines ...
    location / {
        proxy_pass http://127.0.0.1:8042;
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Forward `X-Forwarded-Proto: https` from your proxy **and** list the proxy's
address in `OIKONOME_TRUSTED_PROXIES` (below) — the app sends HSTS
(`Strict-Transport-Security`) and keeps cookies `Secure` only when a
*trusted* proxy says the request came in over HTTPS. From any other peer
the header is ignored, so a client can't forge it.

Keep the `Host`/`Origin` headers intact — cross-origin write protection
compares them.

If your proxy forwards only selected paths, include
`/.well-known/assetlinks.json` and `/.well-known/apple-app-site-association` — the Android and iOS apps' passkey sign-in
depends on those files being reachable on your domain.
The links in the daily email land on top-level paths, not under `/app` or
`/api`: `/act` (the *Needs you* buttons), `/unsubscribe` and
`/recipient-invite`. Forward those too, and `/metrics` if something
outside the LAN scrapes it.

An authentication layer in front of the whole site (basic auth, an SSO
gateway) breaks everything that signs in with a bearer token instead of a
browser: the mobile app and its home-screen widget, collector scripts, and
the integrations doors (`/api/integrations/*`, `/metrics`) that
Prometheus, Home Assistant and the MCP server use. Exempt those paths, or
keep such a layer to the admin console (below). A scraper refused by the
app gets a `401` with a JSON reason, never a redirect to the sign-in page.

## Real client IPs behind the proxy

Rate limiting and the sessions panel key on the connecting address, which
behind a proxy is the proxy itself — every visitor shares one rate-limit
bucket. Tell the app which addresses are your proxy by setting
`OIKONOME_TRUSTED_PROXIES` in `docker/.env` (comma-separated IPs or
CIDRs); requests from those addresses then use the `X-Forwarded-For`
chain to find the real client, and `X-Forwarded-Proto` /
`X-Forwarded-Host` to decide HSTS, `Secure` cookies, and the passkey
origin. Leave it empty when nothing proxies the app —
the forwarded headers are all ignored, so clients can't forge their
address, scheme, or host.

```
OIKONOME_TRUSTED_PROXIES=127.0.0.1,10.89.0.0/24
```

The built-in proxy (`./oikonome.sh https <domain>`) sets this for you:
it trusts the container-network ranges (`10.88.0.0/15,172.16.0.0/12`)
Caddy connects from — addresses a LAN client can never source from —
and never overwrites a value you've set yourself.

(With a containerized proxy like `./oikonome.sh https`, use the compose
network's subnet — `podman network inspect` shows it.)

### Behind Cloudflare (orange cloud)

If the domain is proxied by Cloudflare, the connecting address is a Cloudflare
edge address and the real visitor arrives in `Cf-Connecting-Ip` instead. Set

```
OIKONOME_BEHIND_CLOUDFLARE=1
```

in `docker/.env` and re-run `./oikonome.sh https <domain>`: the generated
Caddyfile then trusts Cloudflare's published edge ranges and reads the visitor
from that header, so rate limiting, the sessions panel, auto-ban and the
security log all see the real client. This is separate from
`OIKONOME_TRUSTED_PROXIES` above, and it is spoof-safe — a request arriving
straight at the origin (a peer outside the Cloudflare ranges) can only ever
attribute its own address. The edge ranges are baked into `oikonome.sh`;
refresh them from cloudflare.com/ips if Cloudflare changes the list.

**Spelling matters for on/off settings.** Every yes/no variable here —
`OIKONOME_BEHIND_CLOUDFLARE`, `OIKONOME_AUTOBAN`, `OIKONOME_AUTO_REBOOT` and
the rest — is read as a boolean: only `1`, `true`, `yes` or `on`
(case-insensitive) turn it on. `=0`, the most natural way to write "off", is
off, and so is anything else. Both the app and `oikonome.sh` read them the
same way, so the generated proxy config and the running app never disagree.

### Auto-ban keys on the resolved client

Once `OIKONOME_TRUSTED_PROXIES` is set, the built-in **auto-ban** also keys on
the real client, so a bot hammering login/signup is short-blocked by its own
address rather than the proxy's (which would take out every visitor). Leave it
unset behind a proxy and rate limiting, auto-ban and the per-address share of
the upload slots all collapse to a single shared bucket — the one
misconfiguration to avoid, because one busy visitor then spends everyone's
budget.

## Abuse defense: auto-ban + fail2ban

A client IP that keeps tripping rate limits or failing login is **auto-banned**
— rejected with `429` before routing, cheaply, for 15 minutes (escalating for
repeat offenders). Only public IPs are eligible (loopback/LAN never), and it is
on by default (`OIKONOME_AUTOBAN=0` disables it).

Every security event is also logged to **stdout** as one stable line for an
external jail:

```
oikonome-security event=auth_fail ip=203.0.113.7 route=login
oikonome-security event=ratelimited ip=203.0.113.7 route=signup
oikonome-security event=autoban ip=203.0.113.7 ban_seconds=900 prior_bans=1
```

`ip=` is the **resolved** client (past the proxy / Cloudflare), so a host-side
`fail2ban` jail filtering on `oikonome-security event=autoban ip=<HOST>` bans the
actual offender at your firewall, not the proxy. (Filter on `event=autoban` — the
`auth_fail`/`ratelimited` lines are the individual strikes leading up to it.)
Point the jail at the app container's logs (`docker logs`/journald).

## Optional: Cloudflare Turnstile on the login page

Rate limits answer *how fast is this address knocking?* — which a credential-
stuffing run spread thinly across a botnet never trips. **Turnstile** answers a
different question, *is this a human?*, with a silent browser challenge that
only shows a checkbox when something looks off.

It is **off unless you configure it**, so a self-hosted instance loads no
third-party script and needs no Cloudflare account. To turn it on, create a
widget at dash.cloudflare.com → Turnstile (the free plan is ample — 20 widgets,
10 hostnames each, unlimited challenges) and set both keys in `.env`:

```
OIKONOME_TURNSTILE_SITE_KEY=0x4AAA…      # public, rendered in the page
OIKONOME_TURNSTILE_SECRET=0x4AAA…        # private, server-side only
```

The widget then appears on `/login` and `/signup`. Your domain does **not** need
to be on Cloudflare — Turnstile works standalone.

**Sign-in is only challenged when it looks risky.** Signing in with the right
password on the first try never has to pass the check; it arms once failed
attempts pile up against that IP address or that account, and a successful
sign-in clears it again. The reason is that a browser extension, a DNS filter
or a locked-down network can stop Cloudflare's script from ever rendering the
widget — leaving nothing to click — so an unconditional check turns those
setups into a lockout. Gated this way, that dead end can only be reached by
someone already failing to sign in, and a passkey still goes around it
entirely. **Signup is challenged every time**: it has no history to judge, and
a bot getting through costs a whole account.

One exception to the quiet path: if failed sign-ins spike across the whole
instance — a stuffing run from many addresses at many accounts, which no
single per-IP or per-account count would notice — every sign-in is challenged
until the burst subsides.

If Cloudflare is unreachable the check **fails open** and sign-in still works.
That is deliberate: a third-party outage must not lock you out of your own
finances, and the other defenses (per-IP limits, per-account lockout, auto-ban)
are all still standing. A token Cloudflare actively *rejects* is refused.

## Optional: Cloudflare Access in front of the admin console

The operator console at `/admin/console` is the highest-value door on the box —
it can back up, restore, reset and uninstall the instance. If your domain is
already on Cloudflare, you can put an **Access** policy on `/admin*` so an
unauthenticated request never reaches the app at all.

An edge policy on its own is only as good as the secrecy of the origin
address, though: if
someone learns the server's IP address and asks it directly, the policy is not
in the path. So tell the app to check for itself. In Cloudflare's Zero Trust
dashboard, create a self-hosted application for `yourdomain.com/admin`, then
copy its **Application Audience (AUD) tag** and your team name into `.env`:

```
OIKONOME_CF_ACCESS_TEAM=yourteam           # or yourteam.cloudflareaccess.com
OIKONOME_CF_ACCESS_AUD=8f2a…               # the application's AUD tag
```

With both set, every console request must carry a valid, unexpired Access
assertion issued to *that* application, or it gets the same 404 a probe gets.
Nothing else on the instance is affected — this covers the console only.

Unlike Turnstile, this **fails closed**: if the assertion is missing, forged or
its signing keys cannot be fetched, the console is shut. That is the right trade
for a door onto host operations, but it means a Cloudflare problem locks *you*
out too. The way back in is to unset the two variables and restart the app; keep
that in mind before enabling it on a box you can only reach through the console.

## Built-in proxy on busy ports (second instance, another proxy)

The built-in Caddy binds host **80/443** by default, so only one thing on
the machine can own them. If they're already taken — another web server,
or a second Oikonome instance's proxy — move this instance's proxy ports
in `docker/.env`:

```
OIKONOME_HTTP_PORT=8080
OIKONOME_HTTPS_PORT=8443
```

`./oikonome.sh https <domain>` checks the ports up front and warns with
these instructions when they're in use. Caveat: Let's Encrypt's
certificate challenges still arrive on 80/443, so with moved ports the
domain must be port-forwarded (80 → `OIKONOME_HTTP_PORT`, 443 →
`OIKONOME_HTTPS_PORT`) at your router or firewall — or put ONE host-level
proxy (nginx/Caddy, above) in front of all instances instead.

## Traefik (compose labels)
```yaml
labels:
  - traefik.http.routers.oikonome.rule=Host(`budget.example.com`)
  # 8080 is the port INSIDE the container (traefik talks to the container
  # directly, not the published host port)
  - traefik.http.services.oikonome.loadbalancer.server.port=8080
```
