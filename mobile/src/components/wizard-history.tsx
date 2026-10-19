// "How much history should come in?" — one household setting that every
// new bank link and every file import reads, shown in the wizard's
// Connect step and on the Import screen (the web renders the same
// control on both). It saves on the tap rather than on a Continue: the
// bank link that follows reads it when its token is minted, so a choice
// still sitting in the form would not apply.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { type StyleProp, Text, View, type ViewStyle } from "react-native";

import { errText } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { useSession } from "../lib/session";
import { useOwner } from "../lib/viewer";
import { C } from "../lib/theme";
import { HISTORY_CHOICES, type HistoryMonths, historyMonthsOf, historyPhrase,
  latestWins, windowNarrower } from "./wizard-logic";
import { PillRow, ws } from "./wizard-ui";

/** A screen's handle on latestWins: the saver lives as long as the
 *  screen, so a re-render never starts a second queue beside the first,
 *  and `busy` re-renders whatever must wait for the save to land. */
export function useLatestWins<T>(
  send: (v: T) => Promise<unknown>,
  settled: (v: T, failed: boolean, error?: unknown) => void,
): { save: (v: T) => void; busy: boolean } {
  // the newest closures, read when a queued save finally runs
  const sendRef = useRef(send);
  const settledRef = useRef(settled);
  sendRef.current = send;
  settledRef.current = settled;
  const [busy, setBusy] = useState(false);
  const saver = useRef<ReturnType<typeof latestWins<T>> | null>(null);
  if (!saver.current)
    saver.current = latestWins<T>((v) => sendRef.current(v), {
      busy: setBusy,
      settled: (v, failed, e) => settledRef.current(v, failed, e),
    });
  return { save: saver.current.save, busy };
}

export function HistoryChoice({ fallback, footnote = true, onBusy,
                                onSaved, style }: {
  // the wizard's onboarding view also carries the stored window, for a
  // settings view that does not
  fallback?: unknown;
  // the Import screen is where "older history later" happens, so it drops
  // the line that points there
  footnote?: boolean;
  // a bank link must wait for the save, or its token is minted from the
  // window the person just changed away from
  onBusy?: (busy: boolean) => void;
  onSaved?: () => void;
  // the Import screen sets it in a card of its own, without the wizard's
  // dashed section rule
  style?: StyleProp<ViewStyle>;
}) {
  const { client } = useSession();
  const owner = useOwner();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const sv = settings.data as { history_months?: unknown } | undefined;
  // the tapped value shows at once; once the saves drain it is the value
  // the server stored, and a failed last save puts the stored one back
  const [picked, setPicked] = useState<HistoryMonths | undefined>();
  const [err, setErr] = useState<string | null>(null);
  const known = !!sv || fallback !== undefined;
  const stored = historyMonthsOf(sv && "history_months" in sv
    ? sv.history_months : fallback);
  const saver = useLatestWins<HistoryMonths>(
    (m) => saveSettings(qc, client!, { history_months: m }),
    (_m, failed, e) => {
      if (failed) { setPicked(undefined); setErr(errText(e)); return; }
      // the reply seeded ["settings"]: the pills read what was stored
      setPicked(undefined);
      onSaved?.();
    });
  const busy = saver.busy;
  useEffect(() => { onBusy?.(busy); }, [busy, onBusy]);
  const current = picked !== undefined ? picked : stored;
  // the banks already linked keep the window they were linked with; the
  // ones narrower than the choice shown are named, with the way out
  const conns = useQuery({ queryKey: ["connections"],
    queryFn: () => client!.connections(), enabled: !!client });
  const narrower = (conns.data?.connections ?? []).filter((c) =>
    c.aggregator === "plaid" && (c.status || "") !== "reaped"
    && windowNarrower(c.history_months, current));
  return (
    <View style={[ws.section, style]}>
      <Text style={ws.h2}>How much history should come in?</Text>
      <Text style={ws.mut}>Applies to every bank you link and every
        file you import. More history means better bill detection and
        a budget seeded from real months; less keeps the first sync
        quick.</Text>
      {/* the window is the owner's: the server refuses anyone else's
          change with a 403, so a member reads it rather than tapping
          pills that snap back */}
      {owner ? (
        <PillRow<HistoryMonths> label="How much history should come in?"
                                choices={HISTORY_CHOICES} value={current}
                                disabled={() => !known || !client}
                                onPick={(m) => {
                                  setPicked(m); setErr(null); saver.save(m);
                                }} />
      ) : known ? (
        <View>
          <Text style={{ color: C.text, fontSize: 14 }}>
            History: {historyPhrase(stored)}</Text>
          <Text style={ws.mut}>the household owner sets this</Text>
        </View>
      ) : null}
      {footnote && (
        <Text style={ws.mut}>Banks share up to two years. You can pull
          in older history later from Accounts → Import.</Text>)}
      {narrower.length > 0 && (
        <Text style={[ws.mut, { color: C.warn }]}>
          A bank keeps the window it was linked with:{" "}
          {narrower.map((c) => `${c.institution_name || c.id} (${
            historyPhrase(c.history_months ?? null)})`).join(", ")}.
          Use <Text style={{ fontWeight: "700" }}>pull more history</Text>
          {" "}on the bank's row to relink it with this window.
        </Text>)}
      {err && <Text style={[ws.mut, { color: C.warn }]}>{err}</Text>}
    </View>
  );
}
