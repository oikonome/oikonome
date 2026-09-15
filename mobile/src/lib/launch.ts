// "Did the app just open?" — one answer per session, for the launch-only
// behaviours (the default home tab). A notification tap that opened the
// app is itself the launch intent and consumes it first; a sign-out
// resets it so the next household on the same launch gets its own home.
//
// The tap that cold-started the process is only known once the
// notifications module has loaded and answered, which is after the first
// render. So the launch has two states: whether the intent is settled
// (the root layout has looked for a launching tap), and whether it has
// been consumed. The home redirect waits for the first before asking
// the second, or it would beat the tap to the router.
let consumed = false;
let settled = false;
let resolveSettled: (() => void) | null = null;
const settledPromise = new Promise<void>((res) => { resolveSettled = res; });

export function consumeLaunch(): boolean {
  if (consumed) return false;
  consumed = true;
  return true;
}

export function markLaunchConsumed(): void { consumed = true; }

export function resetLaunch(): void { consumed = false; }

/** The root layout has looked for a launching notification tap (found
 *  one or not). Idempotent. */
export function settleLaunch(): void {
  if (settled) return;
  settled = true;
  resolveSettled?.();
}

/** Resolves once the launch intent is settled. A module that never
 *  settles (the notifications import failed) must not hold the home
 *  redirect hostage, so the wait is capped. */
export function launchSettled(maxWaitMs = 1500): Promise<void> {
  if (settled) return Promise.resolve();
  return Promise.race([
    settledPromise,
    new Promise<void>((res) => setTimeout(res, maxWaitMs)),
  ]);
}
