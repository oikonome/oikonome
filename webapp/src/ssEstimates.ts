// The Retirement page's Social Security editor: one monthly amount per claim
// age, from the person's SSA statement. The settings door replaces the whole
// schedule on every save, so a box it cannot read stops the save rather than
// dropping that age. The money parser is passed in so this file loads under
// plain node in the test that holds web and mobile to the same cases.
export const SS_CLAIM_AGES = [62, 63, 64, 65, 66, 67, 68, 69, 70];

export function ssEstimatesBody(
  draft: Record<string, string>,
  parseAmount: (v: string) => number | null,
): { ok: true; body: Record<string, number> } | { ok: false; error: string } {
  const body: Record<string, number> = {};
  for (const age of SS_CLAIM_AGES) {
    const text = (draft[age] ?? "").trim();
    if (!text) continue;   // a blank box is an age with no estimate
    const n = parseAmount(text);
    if (n === null || n < 0) {
      return { ok: false,
               error: `Enter a monthly amount for age ${age}, like 2,400` };
    }
    body[age] = n;
  }
  return { ok: true, body };
}
