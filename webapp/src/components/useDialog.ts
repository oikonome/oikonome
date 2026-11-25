import { useEffect, useRef, type RefObject } from "react";

// The one way a web panel that sits over the page behaves as a dialog
// (WCAG 2.4.3, 2.1.2): opening it moves focus inside, Tab and Shift+Tab
// wrap within it, Escape closes it, and closing hands focus back to the
// control that opened it. A modal one also makes the rest of the page
// `inert`, so neither Tab nor a screen reader's reading cursor can wander
// behind the scrim. Used by the step-up sheet, the merchant panel and the
// phone-width nav drawer. See specs/accessibility.md.

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])", "textarea:not([disabled])", "summary",
  "[tabindex]:not([tabindex='-1'])", "[contenteditable='true']",
].join(",");

/** The elements inside `root` that Tab can stop on, in order. A control
 *  that is not drawn (display:none, a closed panel) has no size. */
export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
    .filter((el) => !el.closest("[inert]") && (el.offsetWidth > 0 || el.offsetHeight > 0));
}

// Live regions (toasts, the sync chip) keep speaking while a dialog is
// open, and an element marked data-dialog-keep (the drawer's scrim, which
// closes it on a tap) keeps taking clicks.
const keep = (el: Element) => el.hasAttribute("data-dialog-keep")
  || el.hasAttribute("aria-live")
  || ["status", "alert"].includes(el.getAttribute("role") ?? "");

/** Make every element outside `root` inert; returns the undo. */
function inertOutside(root: HTMLElement): () => void {
  const made: HTMLElement[] = [];
  for (let n: HTMLElement | null = root; n && n !== document.body; n = n.parentElement) {
    const parent: HTMLElement | null = n.parentElement;
    if (!parent) break;
    for (const sib of Array.from(parent.children) as Element[]) {
      if (sib === n || !(sib instanceof HTMLElement) || sib.inert || keep(sib)) continue;
      sib.inert = true;
      made.push(sib);
    }
  }
  return () => { for (const el of made) el.inert = false; };
}

// Dialogs open over one another (the step-up sheet over the merchant
// panel): only the top one answers Tab and Escape.
const stack: object[] = [];

export function useDialog(ref: RefObject<HTMLElement | null>, opts: {
  open?: boolean;
  onClose: () => void;
  /** what takes focus on open; by default the first form field, else the
   *  dialog itself (it carries tabIndex={-1} and a name) */
  initialFocus?: RefObject<HTMLElement | null>;
  /** where focus goes on close; by default the control that had it */
  returnFocus?: RefObject<HTMLElement | null>;
  modal?: boolean;
}) {
  const { open = true, modal = false } = opts;
  const latest = useRef(opts);
  latest.current = opts;
  useEffect(() => {
    const root = ref.current;
    if (!open || !root) return;
    const me = {};
    stack.push(me);
    const opener = document.activeElement as HTMLElement | null;
    const undoInert = modal ? inertOutside(root) : () => {};
    const first = latest.current.initialFocus?.current
      ?? root.querySelector<HTMLElement>(
        "input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled])")
      ?? root;
    first.focus();
    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== me || e.defaultPrevented) return;
      if (e.key === "Escape") {
        latest.current.onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const stops = focusables(root);
      if (!stops.length) { e.preventDefault(); root.focus(); return; }
      const at = document.activeElement as HTMLElement | null;
      const i = at ? stops.indexOf(at) : -1;
      if (i < 0 || (e.shiftKey && i === 0) || (!e.shiftKey && i === stops.length - 1)) {
        e.preventDefault();
        (e.shiftKey ? stops[stops.length - 1] : stops[0]).focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      stack.splice(stack.indexOf(me), 1);
      undoInert();
      // back to the opener — unless focus has already been put somewhere
      // else on purpose, outside the dialog
      const at = document.activeElement;
      const back = latest.current.returnFocus?.current ?? opener;
      if (back && back.isConnected
          && (!at || at === document.body || root.contains(at)))
        back.focus();
    };
  }, [open, modal, ref]);
}
