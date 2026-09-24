// The SPA's design tokens, verbatim (webapp/src/index.css :root) — the
// app must read as the same product. Dark is the design baseline; the
// light palette is the web's data-theme=light block. The palette is
// resolved ONCE, synchronously, at bundle load (every screen's
// StyleSheet.create captures these values at import), so a theme
// change applies on the next app launch — the setting says so.
import { Appearance } from "react-native";
import { MMKV } from "react-native-mmkv";

const store = new MMKV({ id: "oikonome-theme" });

export type ThemeMode = "auto" | "light" | "dark";

const DARK = {
  bg: "#0f141a",
  card: "#1a212b",
  border: "#2a323d",
  hover: "#232c38",
  text: "#dbe3ea",
  mut: "#8b98a3",
  accent: "#4f9bd6",
  good: "#5cb56b",
  warn: "#f0b429",
  bad: "#e0695d",
  // a business entity's own money — the web's --entity, no other mark uses it
  entity: "#b79af0",
  r: 12,
  rs: 8,
};
const LIGHT = {
  ...DARK,
  bg: "#f2f5f7",
  card: "#ffffff",
  border: "#d8dfe6",
  hover: "#e7edf2",
  text: "#1f2933",
  mut: "#5b6875",
  accent: "#2673b8",
  good: "#2f8f46",
  warn: "#a97b0d",
  bad: "#c44b3e",
  entity: "#6f47c4",
};

export function getThemeMode(): ThemeMode {
  const v = store.getString("mode");
  return v === "light" || v === "dark" ? v : "auto";
}
export function setThemeMode(mode: ThemeMode) {
  store.set("mode", mode);
}
/** true when the palette in use differs from what the current setting
 *  + OS scheme would resolve to — i.e. a restart is pending */
export function themeRestartPending(): boolean {
  return resolveDark(getThemeMode()) !== IS_DARK;
}
const resolveDark = (mode: ThemeMode) =>
  mode === "dark" ? true
    : mode === "light" ? false
    : Appearance.getColorScheme() !== "light";

const IS_DARK = resolveDark(getThemeMode());
export const isDarkTheme = IS_DARK;
export const C = IS_DARK ? DARK : LIGHT;
// both palettes by name, for a surface drawn OUTSIDE the app's theme
// setting — the home-screen widget follows the launcher's light/dark
export const PALETTES = { dark: DARK, light: LIGHT } as const;

// money lives in pure.ts (no RN imports) so plain node can unit-test
// the banker's rounding that keeps mobile's whole-dollar figures in
// lock-step with the server, the web and the daily email
export { money } from "./pure";

export const mmddyy = (iso: string | null | undefined) => {
  // a missing date renders as nothing — never as a crashed screen
  if (!iso || iso.length < 10) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${m}/${d}/${y.slice(2)}`;
};
