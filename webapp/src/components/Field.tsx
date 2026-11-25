// A form field's name, hint and error, wired the way a screen reader reads
// them (WCAG 1.3.1, 3.3.1, 3.3.2, 4.1.2): the name from a <label htmlFor>,
// or from aria-label when the words a person sees are not a label element
// (a column header, the text beside an amount, a placeholder); the hint
// and the error through aria-describedby; aria-invalid while there is an
// error. The phone twin is mobile/src/components/field.tsx. See
// specs/accessibility.md.
import { Children, cloneElement, isValidElement, useId,
         type CSSProperties, type ReactElement, type ReactNode } from "react";

type Wiring = {
  "aria-label"?: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
};

/** The attributes that name a field and tie its hint and error to it, for
 *  a field whose visible words sit elsewhere: spread on the input. `error`
 *  marks it invalid; `describedBy` lists the ids of the elements that show
 *  the hint or the error. */
export function fieldProps(label: string, opts: {
  error?: unknown; describedBy?: (string | false | null | undefined)[];
} = {}): Wiring {
  const ids = (opts.describedBy ?? []).filter(Boolean).join(" ");
  return { "aria-label": label,
           ...(opts.error ? { "aria-invalid": true as const } : {}),
           ...(ids ? { "aria-describedby": ids } : {}) };
}

/** A visible label over its one field, with an optional hint and error
 *  shown under it and tied to it. When the error is already on screen
 *  elsewhere (a form's one error banner), `errorId` names that element
 *  and `error` only says whether it is about this field. */
export default function Field({ label, hint, error, errorId, className, style,
                                children }: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  errorId?: string;
  className?: string;
  style?: CSSProperties;
  children: ReactElement;
}) {
  const base = useId();
  const child = Children.only(children);
  const own = isValidElement<{ id?: string; "aria-describedby"?: string }>(child)
    ? child.props : {};
  const id = own.id ?? base;
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = !error ? undefined : errorId ?? `${id}-err`;
  const describedBy = [own["aria-describedby"], hintId, errId]
    .filter(Boolean).join(" ") || undefined;
  return (
    <label htmlFor={id} className={className} style={style}>
      {label}
      {cloneElement(child as ReactElement<Record<string, unknown>>, {
        id, "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined })}
      {hint && <span id={hintId} className="sub">{hint}</span>}
      {error && !errorId && (
        <span id={errId} className="field-err" role="alert">{error}</span>)}
    </label>
  );
}
