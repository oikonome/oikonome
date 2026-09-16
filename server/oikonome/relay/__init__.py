"""The Oikonome push relay — a standalone service, not part of an
instance. Run it with ``uvicorn oikonome.relay.app:app``.

Phones are pushed through the platform services (FCM/APNs), which
require the app publisher's credentials, so instances — hosted and
self-hosted alike — forward through this relay instead of pushing
directly. It accepts content-free tickles ``{token, platform, event,
badge}`` and fans them out; it stores nothing, and it never logs a
token. What the relay can observe is exactly the payload shape: which
push tokens get which kind of tickle, from which source address, under
which instance key (a fingerprint of the key is logged with every push
— it names the sending SERVER, never a person or a device). That
statement is load-bearing for the self-host docs — code changes that
widen it need the docs changed in the same commit.

A key proves the sender is an opted-in instance; it does not prove the
tokens in the batch are that instance's own. `app.py`'s docstring
carries the exposure that leaves and what closing it would cost.

Configuration, all environment:

* ``OIKONOME_RELAY_KEYS`` — comma-separated instance keys; a push must
  carry one in ``X-Relay-Key`` or it is refused. Unset leaves the relay
  open (logged once at startup) — only right for a relay that serves
  your own instance and nobody else can reach.
* ``OIKONOME_TRUSTED_PROXIES`` — the TLS proxy in front, same meaning as
  on an instance; without it every push behind the proxy shares the
  proxy's address for rate limiting.
* ``OIKONOME_RELAY_FCM_CREDS`` and ``OIKONOME_RELAY_APNS_*`` — the
  platform legs (see ``fcm.py`` / ``apns.py``).
"""
