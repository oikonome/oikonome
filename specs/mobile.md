# Mobile apps (iOS + Android)

Native companion apps for an Oikonome instance — self-hosted or hosted.
The app is a client, never a second brain: every number on every screen
comes from the instance's `/api`, and no business logic is duplicated on
the device.

## Principles

- **Bring your own server.** The app is one client for any instance;
  nothing in it assumes who operates the server. A build carries its
  server as a build-time pin (`EXPO_PUBLIC_SERVER_URL`): a build made for
  one instance never asks for a URL, an operator building from source pins
  their own, and a build with no pin asks for the instance URL on first
  run.
- **Native UI, shared contract.** Expo / React Native with real native
  navigation — the web SPA's components do not carry over, but the API
  client, types, and each page's agreed design do. A page's design is the
  spec for both its web and native builds.
- **Login-only.** Accounts exist on the server; the app authenticates
  against one — there is no signup in the app. The account section of
  Settings is an extension slot (`mobile/src/ext/index.tsx`), so an
  operator running the instance for other people can render whatever
  their own add-on needs there; the tracked stub renders nothing.
- **Read everywhere, write deliberately.** The read cache keeps
  last-fetched data visible offline behind a staleness banner; writes
  require a live connection and arrive page by page.

## Auth: device tokens

Session cookies are a browser credential; the app uses a **mobile device
token** instead (`oikd_` prefix — distinct from `oik_` script tokens):

- The app signs in through the normal login flow (password + TOTP, or
  passkey), so every login defense — rate limits, lockout, human check,
  second factor — runs unchanged. The authenticated session then mints
  the device token (`POST /api/devices`); the session itself is
  discarded.
- That mint is carried by a **one-shot `mint_ticket`** returned in the
  login response, not by the session cookie. iOS exposes no `Set-Cookie`
  to the app's fetch and its jar did not carry the session to the very
  next request, so the cookie-only exchange authenticated and then failed
  to mint — a login that would not stick. The ticket is bound to the
  user, single-use, five minutes, sha256 at rest, and redeemed in the
  same transaction as the mint, so a refused mint (closed account, device
  ceiling) leaves it spendable. It meets the same account-standing gates
  every other write meets — suspension, pending deletion, any lockout the
  installed gate reports, the hosted-mode second-factor requirement —
  because a device
  token is 90-day full-access persistence and those gates exist to stop
  exactly that being planted. Browsers ignore the ticket and keep using
  the cookie.
- Passkey sign-in on Android is native: the server publishes
  `/.well-known/assetlinks.json` naming the app package and both
  signing certs (upload key + Play app-signing key), Credential
  Manager verifies it before offering that domain's passkeys, and the
  assertion carries an `android:apk-key-hash:` origin the WebAuthn
  verifier accepts alongside the https page origin. Https servers
  only — the button is hidden for plain-http self-hosts, and a server
  without passkey login answers 404 to the options call.
- The token is stored in the platform secure enclave
  (Keychain/Keystore) and unlocked with Face ID / Touch ID /
  fingerprint on app open. It is sha256-hashed at rest server-side.
  The gate is the credential boundary, not a cover: while locked the
  token is not in JS memory (never read on launch, dropped on lock),
  there is no API client, and the on-disk query cache is frozen out of
  memory. Where the device has strong biometrics enrolled the token is
  bound to them in the enclave (`requireAuthentication`) and the
  biometric prompt IS the read — no code running as the app gets the
  bearer without presence; a change of biometric enrollment
  invalidates it and the phone signs in again. A device without strong
  biometrics (or a person who turns the gate off in Settings) keeps the
  token in a plain enclave item behind the device-credential check (or
  no check, by their choice). A fresh login lands in the plain item;
  the first unlock binds it. The unlock screen's "sign in again" is the
  only way past a biometric that can't be produced — it forgets the
  device's token, never bypasses the gate.
- On-device data: the query cache (the offline read layer) is an MMKV
  file encrypted with a random key held in the enclave, Android backup
  is off (`allowBackup=false`), and screenshots / recents thumbnails
  are blocked while the session is ready. Plain `http://` servers are
  refused for public hosts and warned about for LAN / loopback ones —
  Android's network security config cannot express address ranges, so
  that rule lives at the connect screen; https traffic trusts the
  system CA store only.
- It grants the same access as a session for that user (a viewer's
  device is read-only like their browser), with a sliding 90-day idle
  expiry — a device unused for 90 days re-authenticates.
- It is revocable: individually from Settings → Sessions ("Mobile
  devices"), and wholesale on password change, password reset, and
  email change — a credential rotation ends every device.
- A device token cannot mint another device token (no
  self-propagating persistence), and script tokens cannot reach the
  device endpoints at all.
- Live tokens are capped per user; minting past the cap is refused
  until an old device is revoked.

## Push

Only the party that publishes the app to the stores can hold its
APNs/FCM credentials, so a self-hosted server cannot push a phone
directly — notification delivery goes through a **relay run by the app's
publisher**:

- The instance sends the relay only `{device push token, event kind,
  badge count}` — **never financial content**. The app wakes on the
  push and pulls the actual content from its own server.
- Self-host relay use is opt-in, with the metadata it sees documented
  plainly.

Implementation (server `notify/push_native.py`, relay `relay/`):

- The app registers its platform push token at `POST /api/devices/push`
  (device-token callers only); the token rides the device's own row, so
  revoking the device also silences it. Rotation clears any dead mark.
- The channel fans out under the same per-cadence "push" preference as
  browser push and posts `{token, platform, event, badge}` batches to
  `OIKONOME_PUSH_RELAY_URL` + `/v1/push`. Unset URL = channel off (the
  self-host default). Events are a fixed enum (`daily`, `weekly`,
  `monthly`, `yearly`, `alert`, `test`) — free text is refused on both
  ends, which is what makes the content-free property structural.
- The relay (`uvicorn oikonome.relay.app:app`, same image, its own
  container) validates the same shape, caps batches, rate-limits per
  source AND per instance key (an open relay, configured with no keys,
  also carries one global ceiling — a key is the only identity a
  sprayer cannot mint), never logs a token, and reports unroutable
  tokens back as
  `dead` so the instance stops sending to them. It also BINDS each device
  token to the instance key that first pushed it and refuses (403) a push
  for that token from any other key — a key alone says the caller is an
  opted-in instance, not that the phones in the batch are its own. The
  binding lapses after 30 days of silence so a household that moves
  instances is not locked out, and a key removed from
  `OIKONOME_RELAY_KEYS` takes its claims with it, which is how a key
  rotation completes (while both keys are configured the relay cannot
  tell a rotation from a second instance). Two env vars shape who
  may push: `OIKONOME_RELAY_KEYS` (comma-separated instance keys; when
  set, `POST /v1/push` requires a matching `X-Relay-Key`, which the
  instance sends from its `OIKONOME_PUSH_RELAY_KEY`; unset = open, and
  the relay says so once at startup) and `OIKONOME_TRUSTED_PROXIES` (the
  relay's own TLS proxy — without it the source address is the socket
  peer, so every instance behind that proxy shares one rate bucket).
  `/healthz` is liveness only; it does not disclose which legs are
  configured. FCM delivers as
  data-only, high-priority messages. APNs delivers as alerts (fixed
  relay-side text keyed on the event, same content-free rule) signed
  with the app publisher's APNs key via ES256 provider tokens over HTTP/2; a
  device token carries no environment marker, so delivery tries
  production first and falls back to sandbox on `BadDeviceToken`,
  calling a token `dead` only when both environments refuse it. The
  key/IDs arrive via `OIKONOME_RELAY_APNS_KEY` (path to the `.p8`),
  `_KEY_ID`, `_TEAM_ID`, and `_TOPIC` (the bundle id) — unset, the leg
  is a declared `unavailable`.
