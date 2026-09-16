// demo instances show blocked features disabled-in-place, because
// discoverable beats hidden. The server 403s every guarded route
// regardless — this is presentation, not enforcement.
import type { ReactNode } from "react";

export default function DemoLock({ on, children }: {
  on: boolean | undefined; children: ReactNode;
}) {
  if (!on) return <>{children}</>;
  return (
    <div>
      <div className="sub" style={{ margin: "1rem 0 -.5rem",
            color: "var(--amber)" }}>
        Not available on the demo instance — synthetic data only.
      </div>
      {/* fieldset[disabled] switches off every input/button inside */}
      <fieldset disabled style={{ border: 0, margin: 0, padding: 0,
                                  opacity: 0.55 }}>
        {children}
      </fieldset>
    </div>
  );
}
