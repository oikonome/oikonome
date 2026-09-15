// The app's activity indicator: the omicron-door mark drawing itself.
// The arc strokes from one foot round to the other, then unwinds, and
// repeats — the brand in motion where a generic circle would spin. Same
// path as the canonical icon.svg, so the shape can never drift from the
// logo; only the tile behind it is dropped, because on the app's own
// surfaces the tile is the surface. `color` exists for the one place the mark sits on a filled
// button, where brand blue on brand blue would vanish.
//
// Reduced motion (the OS setting) leaves the arc whole and still: a
// static mark still says "working" next to its label, and nothing
// flickers for someone who asked for that.
import React, { useEffect } from "react";
import Svg, { Path } from "react-native-svg";
import Animated, { Easing, useAnimatedProps, useReducedMotion,
                   useSharedValue, withRepeat, withSequence,
                   withTiming } from "react-native-reanimated";
import { C } from "../lib/theme";

const AnimatedPath = Animated.createAnimatedComponent(Path);
// arc length of `M39.7 78.2 A30 30 0 1 1 60.3 78.2` (r=30, 320° sweep)
const ARC = 168;

export function LogoSpinner({ size = 22, color = C.accent }:
                            { size?: number; color?: string }) {
  const still = useReducedMotion();
  const off = useSharedValue(ARC);
  useEffect(() => {
    if (still) { off.value = 0; return; }
    // 55% of the cycle draws the arc in, the rest unwinds it past the far
    // foot; one continuous motion read on device as the calmer of the
    // rotating options
    off.value = ARC;
    off.value = withRepeat(
      withSequence(
        withTiming(0, { duration: 880, easing: Easing.inOut(Easing.ease) }),
        withTiming(-ARC, { duration: 720, easing: Easing.inOut(Easing.ease) })),
      -1, false);
  }, [still, off]);
  const props = useAnimatedProps(() => ({ strokeDashoffset: off.value }));
  return (
    <Svg width={size} height={size} viewBox="0 0 100 100"
         accessibilityRole="progressbar" accessibilityLabel="loading">
      <AnimatedPath d="M39.7 78.2 A30 30 0 1 1 60.3 78.2" fill="none"
                    stroke={color} strokeWidth={11} strokeLinecap="round"
                    strokeDasharray={`${ARC} ${ARC}`}
                    animatedProps={props} />
    </Svg>
  );
}
