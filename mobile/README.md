# Oikonome mobile

Native companion app (iOS + Android, Expo / React Native) for an
Oikonome instance. Design and auth model: [`../specs/mobile.md`](../specs/mobile.md).

The app is a client: first run asks for your server URL, login mints a
device token kept in the secure enclave behind a biometric unlock, and
every screen renders what the server's `/api` returns.

## Develop

```bash
npm install
npm start          # Expo dev server; scan the QR with Expo Go / a dev build
npm run typecheck  # tsc --noEmit — a REAL check here, run it before pushing
npm run lint

# unit tests for src/lib/pure.ts and the pure helpers in src/lib/api.ts
node --experimental-strip-types scripts/pure-logic-tests.mjs
```

There is no JS test framework: the money and merge logic that fails
silently lives in dependency-free modules, and the script above runs
those cases under plain node (v22.6+). The server suite invokes it too,
so `make test` at the repo root goes red on a regression here.

Point the connect screen at any running instance — a local dev server
works (`http://<lan-ip>:8042`; use the LAN address, not localhost, so
the phone can reach it).

## Pinning a build to your server

A build made with `EXPO_PUBLIC_SERVER_URL` set never shows the connect
screen — it signs in to that one origin and nothing else:

```bash
EXPO_PUBLIC_SERVER_URL=https://oikonome.example.com npx expo run:android --variant release
# or, for a cloud build, put it under build.<profile>.env in eas.json
```

Metro reads the variable when it bundles the JS, so it has to be set for
the build command itself. Left unset, the app is the bring-your-own-server
build and asks for the URL on first run.

## Building your own

The tree builds as-is for your own instance, but the identity in it is a
placeholder: `app.json` carries `org.example.oikonome` as the iOS bundle
identifier and Android package, build number 1, no `owner`, no EAS
project id, no associated domains and no `googleServicesFile`. Before a
build you distribute, set those to yours — an EAS project of your own
(`eas init` fills `extra.eas.projectId`), your Firebase project's
`google-services.json` if you want Android push, and, for iOS passkeys,
`ios.associatedDomains` naming your server (which must serve
`/.well-known/apple-app-site-association` for it; `src/ext/index.tsx`
→ `webcredentialHosts` lists the same hosts). `eas.json` has one
`production` profile pinning `EXPO_PUBLIC_SERVER_URL`; change the URL, and
add `submit` profiles for your own store accounts if you publish.

`src/ext/index.tsx` is the one slot an operator running the product for
other people fills with their own client add-on — extra screens, a
Settings section, lockout screens, wording around an institution allowance,
legal links. Left as it is, the app is a plain client for one instance.

## Layout

```
src/app/          expo-router routes — one file per screen
  connect.tsx     first-run server URL
  login.tsx       password or native passkey → device token mint
  unlock.tsx      biometric gate
  (tabs)/         Today, Transactions, Accounts, Bills, More
  …               the rest of the product hangs off More: budget, month,
                  year, spending, cash flow, net worth, retire, reimburse,
                  rules, merchants, business, imports, assistant, items,
                  alerts, doctor, notifications, settings, security, help,
                  feedback (plus the detail screens txn, bill,
                  bill-history, and the welcome / business / connect-hub
                  walkthroughs) — `ls src/app` is the current list
src/lib/          api client + types, secure storage, session state,
                  push, passkeys, theme, and pure.ts (money/merge logic
                  with no React Native imports, so node can test it)
src/components/   shared UI (ledger row, money map, staleness banner)
```

Every screen mirrors a page of the web app (`../webapp`) — same naming,
wording and semantics. When you change one, change the other.
