// Where should your data come from? — the web ConnectHub, native: the
// provider comparison grid (cost / setup effort / data richness, with
// a hosted instance's included/1-click overrides), and the bring-your-
// own-key flows for self-host — Plaid keys validated live against
// Plaid before anything saves, MX keys validated-on-save with the MX
// Connect widget and a sync-now, SimpleFIN's token paste, and pointers
// for scripts and file imports. Demo instances are locked out the way
// the web locks them out: the credentials are shared.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Alert, Linking, Pressable, RefreshControl, ScrollView, StyleSheet,
         Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, H, Pill } from "../components/ui";
import { patchQuery } from "../lib/cache";
import { ago } from "../lib/dates";
import { plaidHostedLink } from "../lib/plaid";
import { institutionAllowance, showsProviderDoor } from "../lib/pure";
import { useSession } from "../lib/session";
import { ext } from "../ext";
import { C } from "../lib/theme";
import { useOwner } from "../lib/viewer";
import { errText } from "../lib/api";

const ROWS: { key: string; name: string; cost: string; effort: string;
              richness: string; hostedCost?: string;
              hostedEffort?: string }[] = [
  { key: "plaid", name: "Plaid", cost: "Free tier",
    effort: "Hard", richness: "Best",
    hostedCost: "Included", hostedEffort: "1 click" },
  { key: "mx", name: "MX", cost: "Free sandbox",
    effort: "Medium", richness: "Better",
    hostedCost: "Included", hostedEffort: "1 click" },
  { key: "simplefin", name: "SimpleFIN", cost: "$1.50/mo",
    effort: "Easy", richness: "Good" },
  { key: "scripts", name: "API scripts", cost: "Free",
    effort: "Technical", richness: "Varies" },
  { key: "files", name: "File imports", cost: "Free",
    effort: "None", richness: "Full history" },
];

export default function ConnectHub() {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const d = settings.data as (Record<string, unknown> | undefined);
  const hosted = !!me.data?.hosted;
  const owner = useOwner();
  const [open, setOpen] = useState<string | null>(null);

  // every door on this screen links a bank or saves a provider key —
  // the account owner's, and the server refuses the rest. Reachable by
  // deep link even though nothing offers it to a member.
  if (!owner)
    return (
      <ScrollView style={s.wrap}>
        <Text style={s.center}>
          Bank connections are the account owner&apos;s — ask them to link
          or repair one. Everything they pull is here for you either way.
        </Text>
      </ScrollView>
    );

  if (me.data?.demo)
    return (
      <ScrollView style={s.wrap}>
        <Text style={s.center}>
          Connections are locked on the demo — the login is shared, so a
          linked bank would follow the next visitor. Everything else is
          free to explore.
        </Text>
      </ScrollView>
    );

  // Hosted never sees the provider grid: the platform runs the
  // aggregators under its own credentials, so there is no cost to
  // compare, no key to paste, and which aggregator answered is not the
  // user's problem (best live source serves, the rest shadow). One
  // "Connect your accounts" door plus the bring-your-own lane, exactly
  // as the web's hosted ConnectHub. Self-host keeps the grid below —
  // BYO keys legitimately need the comparison.
  if (hosted)
    return <HostedHub bankLink={!!me.data?.bank_link}
                      cap={me.data?.features?.institution_cap}
                      used={me.data?.features?.institutions_used} />;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => settings.refetch()} />}>
      <StaleBanner query={settings} />
      <Card>
        <H>Where should your data come from?</H>
        <Text style={s.mut}>
          {hosted
            ? "This instance connects Plaid and MX for you — one click, no "
              + "developer accounts. The other doors work too."
            : "Every door works; they differ in cost, setup effort and "
              + "how rich the data is. Tap one to set it up."}
        </Text>
      </Card>
      {/* hosted shows no SimpleFIN door (web parity): the server 403s a
          BYO token there, so the paste form was a dead end */}
      {ROWS.filter((r) => showsProviderDoor(r.key, hosted)).map((r) => (
        <Card key={r.key}>
          <Pressable onPress={() => setOpen(open === r.key ? null : r.key)}>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 8 }}>
              <Text style={{ color: C.text, fontSize: 15,
                             fontWeight: "700", flex: 1 }}>
                {open === r.key ? "▾ " : "▸ "}{r.name}
              </Text>
              <Pill text={hosted && r.hostedCost ? r.hostedCost : r.cost} />
            </View>
            <Text style={s.mut}>
              setup: {hosted && r.hostedEffort ? r.hostedEffort : r.effort}
              {" · data: "}{r.richness}
            </Text>
          </Pressable>
          {open === r.key && (
            r.key === "plaid" ? <PlaidFlow d={d} />
            : r.key === "mx" ? <MxFlow d={d} />
            : r.key === "simplefin" ? <SimplefinFlow />
            : r.key === "scripts" ? (
              <Text style={s.mut}>
                Host-side collector scripts push data in over the API
                with per-script tokens — mint them under Settings →
                Script tokens; examples live in the repo&apos;s
                community-scripts folder.
              </Text>
            ) : (
              <Text style={s.mut}>
                CSV, OFX/QFX, QIF, PDF statements, or Mint / YNAB /
                Monarch / Copilot / Simplifi exports — all through{" "}
                <Text style={{ color: C.accent }}
                      onPress={() => router.push("/imports" as never)}>
                  Import &amp; sync ›
                </Text>
              </Text>
            )
          )}
        </Card>
      ))}
    </ScrollView>
  );
}

/** The hosted door: one button that links any institution, the banks it
 *  has linked so far, and the bring-your-own lane underneath. Wording,
 *  order and the "account" (not "bank") button label are the web's. */
function HostedHub({ bankLink, cap, used }: {
  bankLink: boolean; cap?: number | null; used?: number;
}) {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  // poll while the door is on screen: Plaid Link finishes in the in-app
  // browser, and the new bank has to appear without a manual refresh
  const conns = useQuery({ queryKey: ["connections"],
    queryFn: () => client!.connections(), enabled: !!client,
    refetchOnMount: "always", refetchInterval: 5000 });
  const [msg, setMsg] = useState<string | null>(null);
  // the pull-to-refresh spinner tracks the PULL, not the query: sharing
  // isRefetching with the 5s poll above would flash it every tick
  const [pulling, setPulling] = useState(false);
  const link = useMutation({
    mutationFn: () => plaidHostedLink(client!, ""),
    onSuccess: (r) => {
      setMsg(
        r.kind === "add" ? `Linked ${r.institution || "bank"} ✓`
        : r.kind === "exited" || r.kind === "cancelled"
          ? "No bank was linked — the sign-in window was closed. Connect "
            + "again whenever you're ready."
          : String(r.error ?? "link failed"));
      if (r.kind === "add") {
        // a new bank moves the roster, the accounts it feeds, the
        // wizard's progress and its backfill wait — not every query
        for (const k of ["connections", "me", "accounts", "onboarding",
                         "onboarding-backfill"])
          qc.invalidateQueries({ queryKey: [k] });
      }
    },
    onError: (e) => setMsg(errText(e)),
  });
  const disc = useMutation({
    mutationFn: (id: string) => client!.connectionDisconnect(id),
    onSuccess: (r) => {
      // the server explains anything the disconnect could not do (a Plaid
      // item it failed to release); silence otherwise
      setMsg(r.note || null);
      // the institutions-used figure rides /api/me, and the accounts the
      // bank fed are archived with it
      for (const k of ["connections", "me", "accounts", "onboarding",
                       "today"])
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => setMsg(errText(e)),
  });
  // the bridge row a BYO SimpleFIN token would create is not a bank on
  // hosted — the web drops it from this list the same way
  const linked = (conns.data?.connections ?? [])
    .filter((c) => c.aggregator !== "simplefin");
  const accounts = linked.reduce((t, c) => t + (c.accounts ?? 0), 0);
  const { known: capKnown, full } = institutionAllowance(cap, used);
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<RefreshControl tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      refreshing={pulling}
                                      onRefresh={() => { setPulling(true);
                                        void conns.refetch()
                                          .finally(() => setPulling(false));
                                      }} />}>
      <StaleBanner query={conns} />
      <Card>
        <H>Connect your accounts</H>
        {bankLink ? (
          <>
            <Text style={s.mut}>
              Pick your bank and sign in — that&apos;s it. Transactions,
              balances and cards flow in automatically.
            </Text>
            {/* what the plan actually allows, shown before the bank is
                authorized. Both figures come from the server: the cap has
                a per-tenant override an operator can raise, so a number
                written into this copy would be wrong for exactly the
                accounts that were given more. The wording is the web
                ConnectCard's. */}
            {capKnown && (
              <Text style={[s.mut, { marginTop: 4 }]}>
                {ext.allowanceNote(used, cap, full)}
              </Text>
            )}
            <Pressable style={[s.btn, (link.isPending || full)
                                      && { opacity: 0.5 }]}
                       disabled={link.isPending || full}
                       onPress={() => { setMsg(null); link.mutate(); }}>
              {/* "account", not "bank" — a card, a brokerage and a bank
                  are all accounts; once one is in, the button invites
                  the next */}
              <Text style={s.btnText}>
                {link.isPending ? "waiting for your bank…"
                  : linked.length ? "+ Connect another account ↗"
                                  : "+ Connect an account ↗"}
              </Text>
            </Pressable>
            <Text style={s.mut}>
              Opens your bank&apos;s sign-in (Plaid). When you finish, the
              connection appears below so you can link another or carry
              on.
            </Text>
          </>
        ) : (
          <Text style={[s.mut, { color: C.warn }]}>
            Automatic bank connections are being switched on — they&apos;ll
            appear right here. Meanwhile your data can come in through file
            imports or an API script below.
          </Text>
        )}
        {msg && <Text style={s.mut}>{msg}</Text>}
        {linked.length > 0 && (
          <View style={{ marginTop: 12 }}>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 8 }}>
              <Text style={{ color: C.text, fontSize: 13,
                             fontWeight: "700", flex: 1 }}>Connected</Text>
              <Pill tone="good"
                    text={`${linked.length} `
                      + (linked.length === 1 ? "bank" : "banks")
                      + (accounts ? ` · ${accounts} accounts` : "")} />
            </View>
            {/* one row per bank with its state, account count and last
                pull — the same anatomy the web's linked list uses */}
            {linked.map((c) => {
              const name = c.institution_name || c.id;
              const bad = (c.status || "").startsWith("error")
                || c.status === "reaped";
              return (
                <View key={c.id}
                      style={{ flexDirection: "row", alignItems: "center",
                               gap: 8, marginTop: 8 }}>
                  <View style={[s.dot, bad && { backgroundColor: C.bad }]} />
                  <View style={{ flex: 1 }}>
                    <Text style={{ color: C.text, fontSize: 13 }}>{name}</Text>
                    <Text style={s.mut}>
                      {typeof c.accounts === "number"
                        ? `${c.accounts} `
                          + (c.accounts === 1 ? "account · " : "accounts · ")
                        : ""}
                      {bad ? "needs attention"
                        : c.last_ok ? `synced ${ago(c.last_ok)}`
                                    : "first sync pending"}
                      {/* the bank's own clock: sync reads Plaid's copy,
                          and the bank refreshes that copy on its own
                          schedule — this is the number that explains a
                          sync which found nothing */}
                      {!bad && c.bank_updated_at
                        ? ` · bank sent data ${ago(c.bank_updated_at)}` : ""}
                    </Text>
                  </View>
                  <Text style={{ color: C.bad, fontSize: 12 }}
                        onPress={() => !disc.isPending && Alert.alert(
                          `Disconnect ${name}?`,
                          "It stops syncing (history is kept).",
                          [{ text: "Cancel", style: "cancel" },
                           { text: "Disconnect", style: "destructive",
                             onPress: () => disc.mutate(c.id) }])}>
                    Disconnect</Text>
                </View>
              );
            })}
          </View>
        )}
      </Card>
      <Card>
        <H>Bring your own data</H>
        <Text style={s.mut}>
          For history no aggregator carries, exports from another tool, or
          accounts you&apos;d rather feed yourself.
        </Text>
        <Text style={[s.mut, { color: C.accent }]}
              onPress={() => router.push("/imports" as never)}>
          Import files ›
        </Text>
        <Text style={[s.mut, { color: C.accent }]}
              onPress={() => void Linking.openURL(
                "https://github.com/oikonome/oikonome")}>
          API scripts docs ↗
        </Text>
      </Card>
    </ScrollView>
  );
}

function Field({ ph, v, set, secret }: {
  ph: string; v: string; set: (x: string) => void; secret?: boolean;
}) {
  return (
    <TextInput style={s.input} placeholder={ph}
               placeholderTextColor={C.mut} autoCapitalize="none"
               autoCorrect={false} secureTextEntry={secret}
               value={v} onChangeText={set} />
  );
}

function EnvChips({ v, set, opts }: {
  v: string; set: (x: string) => void; opts: [string, string][];
}) {
  return (
    <View style={{ flexDirection: "row", gap: 6, marginTop: 6 }}>
      {opts.map(([val, label]) => (
        <Pressable key={val} style={[s.chip, v === val && s.chipOn]}
                   onPress={() => set(val)}>
          <Text style={{ color: v === val ? C.text : C.mut,
                         fontSize: 11 }}>{label}</Text>
        </Pressable>
      ))}
    </View>
  );
}

// Self-host only — hosted takes HostedHub above and never renders a
// per-provider door.
function PlaidFlow({ d }: { d?: Record<string, unknown> }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [env, setEnv] = useState("production");
  const [validated, setValidated] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const validate = useMutation({
    mutationFn: () =>
      client!.plaidValidate(clientId.trim(), secret.trim(), env),
    onSuccess: () => { setErr(null); setValidated(true); },
    onError: (e) => { setValidated(false); setErr(errText(e)); },
  });
  // "Keys saved ✓" and the Link button read ["settings"]; patch the two
  // flags they read so the section appears on the tap, and refetch behind
  const save = useMutation({
    mutationFn: () =>
      client!.plaidKeys(clientId.trim(), secret.trim(), env),
    onSuccess: (r) => { setErr(null);
      patchQuery(qc, ["settings"], { plaid_secret_set: true,
                                     plaid_env: r.env ?? env });
      qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setErr(errText(e)),
  });
  const clear = useMutation({
    mutationFn: () => client!.plaidKeysClear(),
    onSuccess: () => { setErr(null);
      patchQuery(qc, ["settings"], { plaid_secret_set: false });
      qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setErr(errText(e)),
  });
  const link = useMutation({
    mutationFn: () => plaidHostedLink(client!, ""),
    onSuccess: (r) => {
      setMsg(
        r.kind === "add" ? `Linked ${r.institution || "bank"} ✓`
        : r.kind === "exited" || r.kind === "cancelled"
          ? "No change — the sign-in window was closed."
          : String(r.error ?? "link failed"));
      // without this the wizard's step-2 wait reads the backfill BEFORE
      // this bank exists (no connections → all_ready, poll stopped) and
      // never re-reads it, so the step opens without waiting for the
      // history; a new connection changes that answer
      if (r.kind === "add") {
        qc.invalidateQueries({ queryKey: ["onboarding-backfill"] });
        qc.invalidateQueries({ queryKey: ["onboarding"] });
        // the Accounts tab must learn about the new bank now, not when
        // its own poll or staleTime gets round to it
        qc.invalidateQueries({ queryKey: ["connections"] });
        // the institutions-used figure rides /api/me
        qc.invalidateQueries({ queryKey: ["me"] });
        qc.invalidateQueries({ queryKey: ["accounts"] });
      }
    },
    onError: (e) => setMsg(errText(e)),
  });
  const keysReady = !!d?.plaid_secret_set;
  return (
    <>
      <Text style={s.mut}>
        1. Create a free Plaid developer account at
        dashboard.plaid.com/signup.{"\n"}
        2. Open Developer → Keys and copy your client_id and the secret
        for your environment. Sandbox works right away with test banks;
        real banks need Production access.{"\n"}
        3. Paste the keys here — they&apos;re validated against
        Plaid&apos;s API before anything is saved:
      </Text>
      <Field ph="client_id — 5f3…" v={clientId}
             set={(v) => { setClientId(v); setValidated(false); }} />
      <Field ph="secret" secret v={secret}
             set={(v) => { setSecret(v); setValidated(false); }} />
      <EnvChips v={env} set={(v) => { setEnv(v); setValidated(false); }}
                opts={[["production", "production (real banks)"],
                       ["sandbox", "sandbox (test banks)"]]} />
      {err && <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>}
      <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
        <Pressable style={[s.btn, (validate.isPending || !clientId
                     || !secret) && { opacity: 0.5 }]}
                   disabled={validate.isPending || !clientId || !secret}
                   onPress={() => validate.mutate()}>
          <Text style={s.btnText}>
            {validate.isPending ? "checking with Plaid…"
                                : "Validate keys"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, (!validated || save.isPending)
                     && { opacity: 0.5 }]}
                   disabled={!validated || save.isPending}
                   onPress={() => save.mutate()}>
          <Text style={s.btnText}>
            {save.isPending ? "saving…" : "Save keys"}
          </Text>
        </Pressable>
        {validated && <Pill text="keys valid ✓" tone="good" />}
      </View>
      {keysReady && (
        <>
          <Text style={[s.mut, { fontWeight: "700", color: C.text,
                                 marginTop: 8 }]}>
            Keys saved ✓ — now link your bank(s):
          </Text>
          <Pressable style={[s.btn, link.isPending && { opacity: 0.5 }]}
                     disabled={link.isPending}
                     onPress={() => link.mutate()}>
            <Text style={s.btnText}>
              {link.isPending ? "waiting for your bank…"
                              : "+ Link a bank via Plaid ↗"}
            </Text>
          </Pressable>
          {msg && <Text style={s.mut}>{msg}</Text>}
          <Text style={{ color: C.bad, fontSize: 12, marginTop: 6 }}
                onPress={() => !clear.isPending && clear.mutate()}>
            {clear.isPending ? "clearing…" : "Clear the stored keys"}
          </Text>
        </>
      )}
    </>
  );
}

function MxFlow({ d }: { d?: Record<string, unknown> }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [clientId, setClientId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [env, setEnv] = useState("sandbox");
  const [saved, setSaved] = useState(!!d?.mx_api_key_set);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => client!.mxKeys(clientId.trim(), apiKey.trim(), env),
    onSuccess: () => { setErr(null); setSaved(true);
      qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setErr(errText(e)),
  });
  const connect = useMutation({
    mutationFn: () => client!.mxConnect(),
    // the widget URL comes from the server — open only https, in the
    // system browser (no opener handle back into the app)
    onSuccess: (r) => { if (/^https:/i.test(r.url))
      void Linking.openURL(r.url); },
    onError: (e) => setErr(errText(e)),
  });
  const sync = useMutation({
    mutationFn: () => client!.mxSyncNow(),
    onSuccess: (r) => { setMsg(`Synced — ${r.transactions} transactions.`);
      // what a sync can have moved — not every query in the app
      for (const k of ["accounts", "connections", "today", "transactions",
                       "holdings", "sync-status", "onboarding"])
        qc.invalidateQueries({ queryKey: [k] }); },
    onError: (e) => setErr(errText(e)),
  });
  const clear = useMutation({
    mutationFn: () => client!.mxKeysClear(),
    onSuccess: () => { setErr(null); setSaved(false);
      setMsg("Stored MX keys cleared.");
      qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <>
      <Text style={s.mut}>
        1. Create a developer account at dashboard.mx.com — the sandbox
        is free (test banks like &quot;MX Bank&quot;); production needs
        an MX agreement.{"\n"}
        2. In the dashboard copy your client_id and API key.{"\n"}
        3. Paste them here — validated live against MX before saving:
      </Text>
      <Field ph="client_id" v={clientId} set={setClientId} />
      <Field ph="API key" secret v={apiKey} set={setApiKey} />
      <EnvChips v={env} set={setEnv}
                opts={[["sandbox", "sandbox (free, test banks)"],
                       ["production", "production"]]} />
      {err && <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>}
      <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
        <Pressable style={[s.btn, (save.isPending || !clientId || !apiKey)
                     && { opacity: 0.5 }]}
                   disabled={save.isPending || !clientId || !apiKey}
                   onPress={() => save.mutate()}>
          <Text style={s.btnText}>
            {save.isPending ? "checking with MX…"
              : saved ? "Keys saved ✓" : "Validate & save keys"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, (!saved || connect.isPending)
                     && { opacity: 0.5 }]}
                   disabled={!saved || connect.isPending}
                   onPress={() => connect.mutate()}>
          <Text style={s.btnText}>
            {connect.isPending ? "…" : "Open MX Connect (link a bank)"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, (!saved || sync.isPending)
                     && { opacity: 0.5 }]}
                   disabled={!saved || sync.isPending}
                   onPress={() => sync.mutate()}>
          <Text style={s.btnText}>
            {sync.isPending ? "syncing…" : "Sync now"}
          </Text>
        </Pressable>
      </View>
      {msg && <Text style={s.mut}>{msg}</Text>}
      <Text style={s.mut}>
        Link banks in the MX window, then hit Sync now — accounts and
        transactions land on the Accounts page; the hourly worker keeps
        them fresh after that.
      </Text>
      {/* rotation/removal must not require the web app — a leaked key
          is exactly a from-the-phone emergency */}
      {saved && (
        <Text style={{ color: C.bad, fontSize: 12, marginTop: 6 }}
              onPress={() => !clear.isPending && clear.mutate()}>
          {clear.isPending ? "clearing…" : "Clear the stored keys"}
        </Text>
      )}
    </>
  );
}

function SimplefinFlow() {
  const { client } = useSession();
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const connect = useMutation({
    mutationFn: () => client!.simplefinConnect(token.trim()),
    onSuccess: (r) => { setErr(null);
      setMsg(`Bank connected — ${r.accounts} accounts, ${
        r.transactions} transactions pulled.`);
      // a new bank changes the ledger, the wizard's state and its
      // backfill wait — not every query in the app
      for (const k of ["accounts", "connections", "today", "transactions",
                       "onboarding", "onboarding-backfill"])
        qc.invalidateQueries({ queryKey: [k] }); },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <>
      <Text style={s.mut}>
        1. Sign up at bridge.simplefin.org ($1.50/mo, paid to them
        directly) and connect your banks there.{"\n"}
        2. Create a setup token (New App → copy the long code).{"\n"}
        3. Paste it here — it&apos;s claimed and your first sync runs
        immediately:
      </Text>
      <TextInput style={[s.input, { minHeight: 64,
                                    textAlignVertical: "top" }]}
                 placeholder="paste the SimpleFIN setup token"
                 placeholderTextColor={C.mut} multiline
                 autoCapitalize="none" autoCorrect={false}
                 value={token} onChangeText={setToken} />
      {err && <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>}
      {msg && <Text style={{ color: C.good, fontSize: 12 }}>{msg}</Text>}
      <Pressable style={[s.btn, (connect.isPending || !token.trim())
                   && { opacity: 0.5 }]}
                 disabled={connect.isPending || !token.trim()}
                 onPress={() => connect.mutate()}>
        <Text style={s.btnText}>
          {connect.isPending ? "claiming + first sync…"
                             : "Connect SimpleFIN"}
        </Text>
      </Pressable>
    </>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 18, marginTop: 4 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 14, paddingHorizontal: 10, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  dot: { width: 8, height: 8, borderRadius: 4, backgroundColor: C.good },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 12, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
