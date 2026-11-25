// The one collapsible section. A native <details> (keyboard and screen
// reader behaviour for free) drawn one way everywhere: the caret on the
// LEFT, before the title, so the eye finds "this opens" where it starts
// reading, and the title sits inside the border with room around it. The
// pages used to draw it four ways — a full-width button with a caret flush
// right and the text touching the border, a browser-default marker, a ▸
// glyph, a bare muted line.
//
// `card` draws it as a page-level card; without it, a bordered box that
// sits inside a card. `open` + `onToggle` make it controlled (a section
// that fetches its rows only once opened); `defaultOpen` alone leaves it
// to the browser.
import type { CSSProperties, ReactNode } from "react";

export default function Disclosure({ summary, children, open, defaultOpen,
                                     onToggle, card, className, style }: {
  summary: ReactNode; children?: ReactNode;
  open?: boolean; defaultOpen?: boolean;
  onToggle?: (open: boolean) => void;
  card?: boolean; className?: string; style?: CSSProperties;
}) {
  return (
    <details className={`disclosure${card ? " card" : ""}${
               className ? ` ${className}` : ""}`}
             style={style} open={open ?? defaultOpen}
             onToggle={(e) => onToggle?.(e.currentTarget.open)}>
      <summary>{summary}</summary>
      {children}
    </details>
  );
}
