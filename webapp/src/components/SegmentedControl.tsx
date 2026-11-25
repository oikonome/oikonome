// The one control for "pick one of these": a timeframe, a page's tabs, a
// view, a method, a filter. Every such choice used to draw its own way —
// pills with the primary fill, bordered tab buttons, a hand-styled pair,
// plain buttons, chips — and on several pages the chosen option looked
// exactly like the card's primary action button beside it, so a reader
// could not tell the state of a choice from a thing to press. One shape
// now: a sunken track whose chosen segment is raised and outlined, never
// the primary fill. At phone width the track wraps (index.css, .segctl):
// scrolled sideways, the options past the edge (5y, All, Dismissed) were
// a sliver nobody read as more.
//
// A group of toggle buttons (aria-pressed), not a radiogroup: every option
// stays an ordinary button to keyboards, screen readers and the page
// walkers that find options by their button name.
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties,
         type ReactNode } from "react";

export type SegOption<T extends string> = {
  value: T; label: ReactNode; disabled?: boolean; title?: string;
};

export default function SegmentedControl<T extends string>({
  options, value, onChange, label, labelledBy, size, className, style,
}: {
  options: readonly SegOption<T>[];
  /** null = nothing chosen (a filter whose typed value matches no option) */
  value: T | null;
  onChange: (v: T) => void;
  /** accessible name of the group — a label or the id of one */
  label?: string; labelledBy?: string;
  /** "sm" for the dense spots (a filter line, a card's header) */
  size?: "sm";
  className?: string; style?: CSSProperties;
}) {
  // the chosen option stays in view: at phone width the track scrolls
  // sideways, and a choice at its far end (All, 1y) would start hidden
  const track = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const t = track.current;
    const b = t?.querySelector<HTMLElement>("button.on");
    if (!t || !b || t.scrollWidth <= t.clientWidth) return;
    const tr = t.getBoundingClientRect(), br = b.getBoundingClientRect();
    if (br.right > tr.right) t.scrollLeft += br.right - tr.right + 8;
    else if (br.left < tr.left) t.scrollLeft -= tr.left - br.left + 8;
  }, [value]);
  // options past the track's edge are said by a fade on that edge: cut
  // off with nothing to show it, "5y" read as "5" and "All" was not there
  const [more, setMore] = useState({ left: false, right: false });
  useEffect(() => {
    const t = track.current;
    if (!t) return;
    const read = () => setMore((m) => {
      const left = t.scrollLeft > 1;
      const right = t.scrollLeft + t.clientWidth < t.scrollWidth - 1;
      return m.left === left && m.right === right ? m : { left, right };
    });
    read();
    t.addEventListener("scroll", read, { passive: true });
    const ro = typeof ResizeObserver === "undefined"
      ? null : new ResizeObserver(read);
    ro?.observe(t);
    return () => { t.removeEventListener("scroll", read); ro?.disconnect(); };
  }, [options.length]);
  return (
    <div role="group" aria-label={label} aria-labelledby={labelledBy}
         ref={track}
         className={`segctl${size === "sm" ? " sm" : ""}${
           more.left ? " more-l" : ""}${more.right ? " more-r" : ""}${
           className ? ` ${className}` : ""}`} style={style}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button key={o.value} type="button" aria-pressed={on}
                  className={on ? "on" : undefined}
                  disabled={o.disabled} title={o.title}
                  onClick={() => { if (!on) onChange(o.value); }}>
            {o.label}</button>
        );
      })}
    </div>
  );
}
