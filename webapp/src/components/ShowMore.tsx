// A long list opens short. The first `limit` rows show, and one button
// below them says how many there are in all and opens the rest; once open
// it folds them back. One spelling of the pattern, so every long list on
// every page asks the same question the same way.
//
// `useShowMore` holds the open/closed state and does the slicing; the
// button renders nothing when the list already fits, so a short list
// carries no control at all.
import { useState } from "react";

export function useShowMore<T>(items: readonly T[], limit: number) {
  const [all, setAll] = useState(false);
  const fits = items.length <= limit;
  return {
    shown: all || fits ? items : items.slice(0, limit),
    all, setAll, total: items.length, limit,
  };
}

export function ShowAllButton({ total, limit, all, setAll, noun,
                                 more }: {
  total: number; limit: number; all: boolean;
  setAll: (v: boolean) => void;
  /** what the rows are, plural — "Show all 60 entries" */
  noun: string;
  /** a different opening label when the hidden rows are not simply "all"
   *  (e.g. "Show 12 closed or imported accounts") */
  more?: string;
}) {
  if (total <= limit) return null;
  return (
    <button type="button" className="show-more"
            aria-expanded={all} onClick={() => setAll(!all)}>
      {all ? "Show fewer" : more ?? `Show all ${total.toLocaleString()} ${noun}`}
    </button>
  );
}
