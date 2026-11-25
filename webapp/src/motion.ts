// Whether the person asked their system for less motion
// (prefers-reduced-motion). CSS answers it with a media query; script
// that moves things — a smooth scroll, a number counting up — asks here.
// See specs/accessibility.md.
export function reducedMotion(): boolean {
  return typeof window !== "undefined"
    && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** "smooth" unless reduced motion is asked for, then a plain jump. */
export function scrollBehavior(): ScrollBehavior {
  return reducedMotion() ? "auto" : "smooth";
}
