import { useIsMutating, useMutation, useQuery, useQueryClient }
  from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, errText, type Connection, type HistoryMonths }
  from "../api/client";
import { saveSettings, SETTINGS_SAVES } from "../api/cache";
import { isOwner } from "../role";

/** The five reaches, in the words the wizard, the Import page and the
 *  phone all use. null = everything the source offers. */
export const HISTORY_CHOICES: [HistoryMonths, string][] = [
  [1, "Last month"], [3, "3 months"], [6, "6 months"], [12, "1 year"],
  [null, "Everything available"],
];

/** The chosen reach as the wizard's closing summary words it. */
export function historyLabel(m: HistoryMonths | undefined): string {
  const hit = HISTORY_CHOICES.find(([v]) => v === (m ?? null));
  return (hit ? hit[1] : "Everything available").toLowerCase();
}

/** Is a bank's window narrower than the household's choice? A bank
 *  linked with a shorter window keeps it (Plaid fixes the window when
 *  the Item is created), so a wider choice made afterwards reaches that
 *  bank only through a relink. null = everything. Mobile keeps the same
 *  rule in wizard-logic.ts (windowNarrower). */
export function windowNarrower(linked: HistoryMonths | null | undefined,
                               household: HistoryMonths | null | undefined
): boolean {
  if (linked === null || linked === undefined) return false;
  return household === null || household === undefined || linked < household;
}

/** The row's words for a bank's own window ("history: last month"). */
export function historyRowNote(m: HistoryMonths | null | undefined): string {
  return `history: ${historyLabel(m ?? null)}`;
}

/** The history write's key, so the doors that read the stored reach can
 *  tell a write is still on its way — queued behind other settings saves
 *  counts as on its way. */
const HISTORY_SAVE = ["history-months-save"];

/** The line a waiting door shows while a history choice is saving. */
export const HISTORY_SAVING = "saving your history choice…";

/** True while a tapped history choice has not reached the server yet. A
 *  bank link's token and a file import's cutoff are both taken from the
 *  STORED reach at the moment they start, so either one started now would
 *  use the choice the household just changed away from — and a bank's
 *  window can never be narrowed once Plaid has it. Mobile gates its link
 *  button the same way (historyBusy). */
export function useHistorySaving(): boolean {
  return useIsMutating({ mutationKey: HISTORY_SAVE }) > 0;
}

/** "How much history should come in?" — one household setting that every
 *  new bank link and every file import reads. It saves on the tap rather
 *  than on a Continue: the bank link that follows reads it when its token
 *  is minted, so a choice still sitting in the form would not apply. */
export default function HistoryChoice({ title = true, footnote = true }: {
  title?: boolean;
  // the Import page is where "older history later" happens, so it drops
  // the line that points there
  footnote?: boolean;
}) {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // The tapped value shows while its write is on the way; once the last
  // write settles the pills read the stored value again, so a failed one
  // shows what is really saved. Writes go one at a time on the settings
  // queue, in order: two in flight could land out of order and leave the
  // stored reach on an earlier tap than the pills show. A tap made while
  // one is in flight is only remembered — the queued write sends the
  // newest pick when its turn comes, so the last tap wins and the taps in
  // between never reach the server.
  const [picked, setPicked] = useState<HistoryMonths | undefined>(undefined);
  const want = useRef<{ v: HistoryMonths } | null>(null);
  const inFlight = useRef(0);
  const save = useMutation({
    mutationKey: HISTORY_SAVE,
    scope: SETTINGS_SAVES,
    mutationFn: async () => {
      const w = want.current;
      // an earlier queued write already sent the newest pick
      if (!w) return null;
      want.current = null;
      return saveSettings(qc, { history_months: w.v });
    },
    // the wizard's closing summary reads the reach from onboarding
    onSuccess: (r) => {
      if (r) qc.invalidateQueries({ queryKey: ["onboarding"] });
    },
    onSettled: () => {
      inFlight.current -= 1;
      if (inFlight.current <= 0) {
        inFlight.current = 0;
        setPicked(undefined);
      }
    },
  });
  const tap = (v: HistoryMonths) => {
    setPicked(v);
    want.current = { v };
    inFlight.current += 1;
    save.mutate();
  };
  const saving = useHistorySaving();
  const current: HistoryMonths = picked !== undefined ? picked
    : (s.data?.history_months ?? null);
  // the banks already linked keep the window they were linked with; the
  // ones narrower than the choice shown are named, with the way out
  const conns = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const narrower = (conns.data?.connections ?? []).filter((c: Connection) =>
    c.aggregator === "plaid" && (c.status || "") !== "reaped"
    && windowNarrower(c.history_months, current));
  // The reach is the owner's: it is what the owner's next bank link asks
  // the aggregator for, and the server refuses anyone else a change. A
  // member or viewer reads where it stands rather than tapping pills that
  // can only answer 403.
  if (!isOwner(me)) {
    const hit = HISTORY_CHOICES.find(([v]) => v === current);
    return (
      <div className="hist-choice">
        {title && <b>How much history comes in</b>}
        <p className="mut" style={{ margin: ".2rem 0 0", fontSize: 13 }}>
          {s.data
            ? <><b style={{ color: "var(--ink)" }}>
                {hit ? hit[1] : "Everything available"}</b> for every bank
                linked and every file imported — the household owner sets
                this.</>
            : "Loading…"}</p>
      </div>
    );
  }
  return (
    <div className="hist-choice">
      {title && <b>How much history should come in?</b>}
      <p className="mut" style={{ margin: ".2rem 0 .4rem", fontSize: 13 }}>
        Applies to every bank you link and every file you import. More
        history means better bill detection and a budget seeded from real
        months; less keeps the first sync quick.</p>
      <div className="wiz-pills" role="radiogroup"
           aria-label="How much history should come in">
        {HISTORY_CHOICES.map(([v, label]) => (
          <button key={label} type="button" role="radio"
                  aria-checked={current === v}
                  className={"wiz-pill" + (current === v ? " sel" : "")}
                  disabled={!s.data}
                  onClick={() => tap(v)}>
            {label}</button>
        ))}
      </div>
      {footnote && (
        <p className="mut" style={{ fontSize: 12, margin: ".2rem 0 0" }}>
          Banks share up to two years. You can pull in older history later
          from Accounts → Import.</p>)}
      {narrower.length > 0 && (
        <p style={{ fontSize: 13, margin: ".3rem 0 0", color: "var(--amber)" }}>
          A bank keeps the window it was linked with:{" "}
          {narrower.map((c) => `${c.institution_name || c.id} (${
            historyLabel(c.history_months ?? null)})`).join(", ")}.
          Use <b>pull more history</b> on the bank's row to relink it with
          this window.</p>)}
      {saving && (
        <p className="mut" style={{ fontSize: 13, margin: ".3rem 0 0" }}>
          {HISTORY_SAVING}</p>)}
      {save.isError && (
        <p className="neg" style={{ fontSize: 13, margin: ".3rem 0 0" }}>
          {errText(save.error)}</p>)}
    </div>
  );
}
