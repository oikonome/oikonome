// The buttons under a secret shown once (recovery codes, a one-time link).
// A copy the browser refuses must leave the secret on screen — it can still
// be selected by hand — and a plain dismiss is always there so a refused
// clipboard never traps the page. (The phone app's SecretCopyButtons does
// the same.)

/** Puts `text` on the clipboard; false when the browser has no clipboard
 *  or refuses the write (an insecure context, a denied permission). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function RecoverySheetButtons({ codes, onDone, onErr,
                                       copyLabel = "Copy & dismiss",
                                       doneLabel = "Dismiss" }: {
  codes: string[]; onDone: () => void; onErr: (text: string) => void;
  copyLabel?: string; doneLabel?: string;
}) {
  return (
    <div style={{ display: "flex", gap: ".5rem", marginTop: ".4rem" }}>
      <button onClick={async () => {
        if (!(await copyText(codes.join("\n")))) {
          onErr("Couldn't copy — select the codes and copy them by hand, "
                + `then press ${doneLabel}.`);
          return;
        }
        onDone();
      }}>{copyLabel}</button>
      <button onClick={onDone}>{doneLabel}</button>
    </div>
  );
}
