// demo instances show blocked features disabled-in-place, because
// discoverable beats hidden. The server 403s every guarded route
// regardless — this is presentation, not enforcement.
//
// The same treatment serves the shared demonstration login (an owner whose
// password is handed out), which the server refuses
// every change to the login itself and every grant that would outlive it.
// `shared` is the sentence that refusal carries (sharedLock in ../sharedlogin), or
// null when the control is live for this login.
import type { ReactNode } from "react";

export default function DemoLock({ on, shared, children }: {
  on: boolean | undefined; shared?: string | null; children: ReactNode;
}) {
  if (!on && !shared) return <>{children}</>;
  return (
    <div>
      <div className="sub" style={{ margin: "1rem 0 -.5rem",
            color: "var(--amber)" }}>
        {on ? "Not available on the demo instance — synthetic data only."
            : shared}
      </div>
      {/* fieldset[disabled] switches off every input/button inside */}
      <fieldset disabled style={{ border: 0, margin: 0, padding: 0,
                                  opacity: 0.55 }}>
        {children}
      </fieldset>
    </div>
  );
}
