// iOS: reload the widget timelines, and drop the extension's cached
// payload. Optional on purpose — absent on Android, in Expo Go and in a
// build without the extension — so the caller's try/catch is the only
// guard needed.
import { requireOptionalNativeModule } from "expo";

type WidgetReload = { reload(): void; clearCache(): void;
                      placedCount?: () => Promise<number> };

export function reloadWidgets() {
  requireOptionalNativeModule<WidgetReload>("WidgetReload")?.reload();
}

/** Forget the last glance the extension kept for an offline redraw. It
 *  belongs to the household that was signed in when it was fetched, so
 *  it must go before another household's credential is written. */
export function clearWidgetCache() {
  requireOptionalNativeModule<WidgetReload>("WidgetReload")?.clearCache();
}

/** How many of this app's widgets are on the home screen, or null when the
 *  build's module cannot say (older native build, Expo Go, Android) or
 *  WidgetKit refused to answer. */
export async function placedWidgets(): Promise<number | null> {
  const m = requireOptionalNativeModule<WidgetReload>("WidgetReload");
  if (!m?.placedCount) return null;
  const n = await m.placedCount();
  return n >= 0 ? n : null;
}
