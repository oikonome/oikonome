// The one banner for what happened: an error, a result, a warning that
// appeared because of something the person did (specs/accessibility.md).
// A screen reader hears it when it appears: an error is an alert (read at
// once), anything else a status (read when the reader is free). The phone
// twin is `Note` in mobile/src/components/ui.tsx.
import type { CSSProperties, ReactNode, Ref } from "react";

export type NoteTone = "bad" | "good" | "info" | "warn";

export default function Note({ tone, as = "div", id, className, style, title,
                               onDismiss, ref, children }: {
  tone?: NoteTone;
  /** lets a field name this note as its error (aria-describedby) */
  id?: string;
  as?: "div" | "p" | "span";
  className?: string;
  style?: CSSProperties;
  title?: string;
  /** Closes the note. A click anywhere on it still closes it, as before;
   *  the keyboard gets a real Dismiss button, out of sight until it takes
   *  focus, so the note looks the same. */
  onDismiss?: () => void;
  ref?: Ref<HTMLElement>;
  children: ReactNode;
}) {
  const Tag = as as "div";
  return (
    <Tag ref={ref as Ref<HTMLDivElement>} id={id}
         role={tone === "bad" ? "alert" : "status"}
         className={["note", tone, className].filter(Boolean).join(" ")}
         style={style} title={title} onClick={onDismiss}>
      {children}
      {onDismiss && (
        <button type="button" className="note-x" aria-label="Dismiss"
                onClick={(e) => { e.stopPropagation(); onDismiss(); }}>✕</button>
      )}
    </Tag>
  );
}
