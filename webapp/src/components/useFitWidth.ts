// A drawing as wide as the space it is given, down to a phone's card.
// The charts are SVG with a viewBox: at a fixed width scaled to fit, every
// label shrank with the card and a phone-width chart had an axis nobody
// could read. Returns the width to draw at, between 280 and `max`.
import { useEffect, useState, type RefObject } from "react";

export function useFitWidth(ref: RefObject<Element | null>, max: number,
                            // pass what decides whether the element is
                            // mounted, so a late mount is measured too
                            remountKey?: unknown): number {
  const [w, setW] = useState(max);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const fit = () => {
      const got = el.getBoundingClientRect().width;
      if (got > 0) setW(Math.round(Math.max(280, Math.min(max, got))));
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, max, remountKey]);
  return w;
}
