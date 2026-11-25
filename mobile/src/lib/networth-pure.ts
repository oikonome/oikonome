// Net worth screen helpers, runnable under plain node (scripts/pure-logic-tests.mjs).
//
// Every figure the screen prints is the server's (specs/counted-figures.md):
// the headline's and the tiles' changes, each list's net and the Over-time
// gain. What is left here is presentation — which of the server's windows
// to draw.

export type OverTimeWindow = {
  start: number; end: number; gain: number | null; pct: number | null;
  full: boolean;
  /** The server's words when a typed-in property entered the series
   *  inside the window; the gain is then the financial accounts' alone. */
  note?: string | null;
  /** The server's words for where the gain is measured from, sent when
   *  there is no percent to say it ("from −$4,200 in 03/08"). */
  from_text?: string | null;
};

/** The Over-time card's window for a timeframe: the server's, from
 *  `trend_windows`, over `full_trend`. A server too old to send the windows
 *  draws the whole series and prints no gain — never a gain worked out
 *  here, which would be a second definition of the same figure. */
export function overTimeWindow(
  windows: Record<string, OverTimeWindow> | null | undefined,
  range: string, points: number,
): OverTimeWindow {
  const w = windows?.[range];
  if (w) return w;
  return { start: 0, end: Math.max(points - 1, 0), gain: null, pct: null,
           full: true };
}
