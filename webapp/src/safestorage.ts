// Web Storage behind a guard. Touching localStorage / sessionStorage
// throws, or finds null, when the browser blocks site data (Safari's
// block-all-cookies, Firefox with storage disabled, some managed
// policies, a partitioned frame) while the session cookie still works.
// The shell reads these during render, outside the boundary around the
// routes, so one unguarded read would put the error card over every page. A
// blocked store reads as
// empty and a write to it is dropped: these hold preferences, never data.

type Kind = "local" | "session";
const store = (k: Kind): Storage =>
  k === "local" ? window.localStorage : window.sessionStorage;

export function readStore(k: Kind, key: string): string | null {
  try { return store(k).getItem(key); } catch { return null; }
}

export function writeStore(k: Kind, key: string, value: string): void {
  try { store(k).setItem(key, value); } catch { /* storage blocked */ }
}
