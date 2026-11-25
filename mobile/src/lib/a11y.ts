// What a touchable says to a screen reader when its look is a glyph: a ☐/☑
// that toggles is a checkbox with a checked state, a ▸/▾ row that opens a
// section is a button with an expanded state. Each takes the name in
// words, so the glyph itself is never read aloud ("ballot box with
// check"). Spread onto the Pressable or Text that takes the press, the web
// counterparts being <input type=checkbox> and aria-expanded. See
// specs/accessibility.md.
import { useEffect, useState } from "react";
import { AccessibilityInfo, type AccessibilityProps } from "react-native";

export function checkbox(checked: boolean, label: string): AccessibilityProps {
  return { accessibilityRole: "checkbox", accessibilityLabel: label,
           accessibilityState: { checked } };
}

export function disclosure(expanded: boolean, label: string): AccessibilityProps {
  return { accessibilityRole: "button", accessibilityLabel: label,
           accessibilityState: { expanded } };
}

/** Whether the system's reduce-motion setting is on, kept current while
 *  the screen is up. Motion that is only decoration (a sweep over a
 *  progress bar, a sheet sliding up) stops when it is. */
export function useReduceMotion(): boolean {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    let live = true;
    AccessibilityInfo.isReduceMotionEnabled().then((v) => { if (live) setReduce(v); },
                                                  () => {});
    const sub = AccessibilityInfo.addEventListener("reduceMotionChanged", setReduce);
    return () => { live = false; sub.remove(); };
  }, []);
  return reduce;
}

/** A sheet's Modal slides up, or simply appears under reduce motion. */
export function useSheetAnimation(): "slide" | "none" {
  return useReduceMotion() ? "none" : "slide";
}
