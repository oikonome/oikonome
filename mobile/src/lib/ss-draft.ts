// What the Social Security boxes show while they are open. Saving replaces
// the whole schedule, so a box left showing an older amount would overwrite a
// newer one: a box the person has not touched follows what is saved, and one
// they typed in stays theirs.
type Draft = Record<string, string>;

export const ssDraftOf = (saved: Record<string, number> | null): Draft =>
  Object.fromEntries(Object.entries(saved ?? {})
    .map(([age, amt]) => [age, String(amt)]));

export function rebaseSsDraft(draft: Draft, was: Draft, now: Draft): Draft {
  const next: Draft = {};
  for (const age of new Set([...Object.keys(draft), ...Object.keys(now)])) {
    const untouched = (draft[age] ?? "") === (was[age] ?? "");
    const text = untouched ? now[age] : draft[age];
    if (text !== undefined) next[age] = text;
  }
  return next;
}
