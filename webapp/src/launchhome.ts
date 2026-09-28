// The household's chosen launch page, remembered in this browser so
// main.tsx can open a launch on it before React mounts. It mirrors a
// per-household server setting, so it is dropped whenever this browser
// stops being that household's — on every sign-out and on a lost session.
// Its own module so push.ts (every sign-out) and App.tsx (the setting's
// writer) share one key without importing each other.
export const HOME_KEY = "oiko-home";

export function forgetLaunchHome(): void {
  try { localStorage.removeItem(HOME_KEY); } catch { /* private mode */ }
}
