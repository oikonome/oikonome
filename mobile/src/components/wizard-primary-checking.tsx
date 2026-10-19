// "Which checking account does the cash forecast anchor on?" — the
// wizard's Connect step, once the banks are in. One eligible checking
// account is pinned by the server when the step is left, so this only
// says so; two or more are a choice made here, on the tap. The web's
// components/PrimaryCheckingPick.tsx, words and all; the Accounts
// screen's edit sheet keeps the same switch for later.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Text, View } from "react-native";

import { type Account, errText } from "../lib/api";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { PillRow, ws } from "./wizard-ui";

export function accountLabel(a: { name: string; mask: string | null;
                                  institution_name?: string | null }): string {
  return `${a.institution_name ? `${a.institution_name} · ` : ""}${a.name}`
    + (a.mask ? ` ····${a.mask}` : "");
}

export function PrimaryCheckingPick({ candidates, pinned, compact }: {
  // compact: under the Today checklist's step, whose own label and text
  // stand in for the heading (the web's `compact`)
  candidates: string[]; pinned: string | null; compact?: boolean;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const accts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(),
    enabled: !!client && candidates.length > 0 });
  const pick = useMutation({
    mutationFn: (a: Account) => client!.accountClassify(a.id, a.kind, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  if (!candidates.length) return null;
  const rows = (accts.data?.accounts ?? [])
    // kind is "type/subtype" (the web's kindIs rule): never a bare type
    .filter((a) => candidates.includes(a.id)
                   && a.kind.startsWith("depository/checking"));
  // one candidate already pinned: nothing to choose; one candidate and no
  // live pin is still a tap (the web's rule)
  if (candidates.length === 1 && pinned === candidates[0]) {
    const only = rows[0];
    return (
      <View style={compact ? undefined : ws.section}>
        {!compact && <Text style={ws.h2}>Primary checking</Text>}
        <Text style={ws.mut}>
          {only ? <Text style={{ color: C.text, fontWeight: "700" }}>
                    {accountLabel(only)} — </Text> : null}
          the cash forecast anchors on your one checking account. Change
          it any time from Accounts.
        </Text>
      </View>
    );
  }
  return (
    <View style={compact ? undefined : ws.section}>
      {!compact && (
        <>
          <Text style={ws.h2}>Primary checking</Text>
          <Text style={ws.mut}>
            Which checking account does the cash forecast anchor on? Runway,
            low point and the timeline all read from this one.
          </Text>
        </>
      )}
      <PillRow choices={rows.map((a) => [a.id, accountLabel(a)])}
               value={pinned ?? ""}
               label="Primary checking account"
               disabled={() => pick.isPending}
               onPick={(id) => {
                 const a = rows.find((r) => r.id === id);
                 if (a && a.id !== pinned) pick.mutate(a);
               }} />
      {pick.isError ? (
        <Text style={[ws.mut, { color: C.warn }]}>{errText(pick.error)}</Text>
      ) : null}
    </View>
  );
}
