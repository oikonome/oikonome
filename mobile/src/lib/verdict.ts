// The one verdict → color/label map, shared by every screen that shows
// a verdict (Today, Month lens, Year lens). Labels and colors are the
// web's VERDICT map verbatim (webapp Today.tsx) — mobile copies web —
// and ON BUDGET reads "ON PLAN" because on-plan is good news, not a
// warning. One copy so the same verdict can never read differently on
// two screens.
import { C } from "./theme";

export const VERDICT: Record<string, { color: string; label: string }> = {
  "OVER BUDGET": { color: C.bad, label: "OVER BUDGET" },
  "UNDER BUDGET": { color: C.good, label: "UNDER BUDGET" },
  "ON BUDGET": { color: C.good, label: "ON PLAN" },
};

// the Today hero renders the verdict as a colored SENTENCE, not a pill —
// the web hero's exact wording ("On budget" there, "ON PLAN" on pills).
// Lives in pure.ts so the home-screen widget (rendered outside the app,
// tested under node) reads the very same words.
export { heroLabel } from "./pure";
