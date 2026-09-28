// The pull-to-refresh control, driven by the PULL and nothing else.
//
// Binding `refreshing` to a query's isRefetching is the trap: a
// background refetch — the wizard's 5-second poll, a refocus, a mutation's
// invalidate — raises the spinner as if the person had dragged. Android's
// spinner overlays the list and the glitch goes unseen; iOS pushes the
// content down, so the Setup wizard sinks every five seconds while a bank
// is being added. The spinner here shows only between the person's pull
// and the refetch it started coming back.
import { useEffect, useRef, useState } from "react";
import { RefreshControl, type RefreshControlProps } from "react-native";
import { C } from "../lib/theme";

type Props = Omit<RefreshControlProps, "refreshing" | "onRefresh"> & {
  // whatever the pull should re-fetch; a promise keeps the spinner up
  // until it settles, anything else drops it on the next tick
  onRefresh: () => unknown;
};

export default function PullRefresh({ onRefresh, ...rest }: Props) {
  const [pulling, setPulling] = useState(false);
  // a refetch outliving the screen must not set state on an unmounted
  // control
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  // ScrollView clones this element with the scroll content as children
  // on Android, so `rest` (children, style) is forwarded untouched
  return (
    <RefreshControl tintColor={C.accent} colors={[C.accent]}
                    progressBackgroundColor={C.card} {...rest}
                    refreshing={pulling}
                    onRefresh={() => {
                      setPulling(true);
                      Promise.resolve().then(onRefresh)
                        .catch(() => undefined)
                        .finally(() => { if (alive.current) setPulling(false); });
                    }} />
  );
}
