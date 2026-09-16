// One breakpoint for "this is a phone": the same 760px the stylesheet uses
// to move the nav into the bottom bar, so a page that re-arranges itself
// for phones changes shape at the exact width the chrome does.
import { useEffect, useState } from "react";

const QUERY = "(max-width:760px)";

export function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(
    () => window.matchMedia(QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(QUERY);
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return narrow;
}
