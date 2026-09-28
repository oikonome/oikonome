// A tab that is not on screen is frozen (react-freeze), so a query it
// holds does not refetch when a write elsewhere marks it stale: the
// screens that own a heavy query invalidate it with refetchType "none"
// and every screen that shows the key catches up here — the moment it is
// shown again, and when a write invalidates the key while it has focus.
// The decisions themselves live in shown.ts (see there for why TanStack's
// own cache events are not enough).
import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useFocusEffect } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";

import { type Live, shownRefetcher } from "./shown";

export function useRefetchWhenShown(q: Live, queryKey: QueryKey) {
  const qc = useQueryClient();
  const live = useRef(q);
  live.current = q;
  // one watcher per mount: the key is a module constant or a literal the
  // caller rebuilds each render, and a new watcher each render would
  // forget a write announced while the screen was hidden
  const [w] = useState(() => shownRefetcher(qc, queryKey, () => live.current));
  useEffect(() => w.attach(), [w]);
  // focus gain: stable deps, so once per focus, never on a flip
  useFocusEffect(useCallback(() => {
    w.focus();
    return () => w.blur();
  }, [w]));
}
