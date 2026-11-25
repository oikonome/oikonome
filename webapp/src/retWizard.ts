// The retirement walkthrough's answer bounds, free of React so the mobile
// pure-logic suite checks this copy and the phone's (mobile/src/lib/pure.ts)
// against the server's table.

/** The range the server holds each walkthrough answer to (the settings
 *  save refuses anything outside it, and the projection clamps age at
 *  the same top), as [lowest, highest]. The checks and the boxes' hints
 *  read it, and the pure-logic suite compares it with the server's table,
 *  so a bound cannot drift on one side. Nothing is clamped against it. */
export const RET_WIZARD_BOUNDS: Record<string, [number, number]> = {
  age: [18, 90],
  end: [19, 110],
  spend: [0, 100_000_000],
  employer_mo: [0, 10_000_000],
  roth_mo: [0, 10_000_000],
  taxable_mo: [0, 10_000_000],
  ret: [-20, 30],
  infl: [-20, 30],
  ssage: [62, 70],
};
