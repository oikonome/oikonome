// viewers see owner chrome disabled-in-place where hiding
// it would also hide read value (same show-disabled doctrine as DemoLock);
// pure write chrome is hidden outright instead. The server 403s every
// viewer write regardless — this is presentation, not enforcement.
import type { ReactNode } from "react";

export default function ViewerLock({ on, children }: {
  on: boolean | undefined; children: ReactNode;
}) {
  if (!on) return <>{children}</>;
  return (
    <div>
      <div className="sub" style={{ margin: "1rem 0 -.5rem" }}>
        View-only access — the instance owner manages this.
      </div>
      {/* fieldset[disabled] switches off every input/button inside */}
      <fieldset disabled style={{ border: 0, margin: 0, padding: 0,
                                  opacity: 0.55 }}>
        {children}
      </fieldset>
    </div>
  );
}
