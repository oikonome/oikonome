// The theme a page other than the shell picks (the setup wizard's
// Appearance step). The shell owns the theme: it holds the current value
// for its nav control, knows whether this login keeps the choice per device
// (viewer, demo) or saves it to the household, and applies it. So a page
// asks the shell through this event rather than writing the setting itself
// — two writers would disagree about the per-device rule, and the nav
// control would keep showing the old choice.

export type Theme = "auto" | "light" | "dark";

export const THEME_EVENT = "oiko-theme-choice";

export function chooseTheme(t: Theme): void {
  window.dispatchEvent(new CustomEvent<Theme>(THEME_EVENT, { detail: t }));
}
