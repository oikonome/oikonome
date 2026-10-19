import { useEffect, useState } from "react";
import { LINK_DONE_CHANNEL } from "../components/plaidlinks";

// Where the bank tab lands once Plaid's Hosted Link finishes, when the
// instance asks Plaid to send it here (OIKONOME_PLAID_COMPLETION_REDIRECT).
// It tells the tab that started the link to poll now — the row there
// flips to the bank within a second — and closes itself; a browser that
// will not close a tab shows the one line instead.
export default function PlaidDone() {
  const [stays, setStays] = useState(false);
  useEffect(() => {
    try {
      const ch = new BroadcastChannel(LINK_DONE_CHANNEL);
      ch.postMessage("done");
      ch.close();
    } catch { /* the other tab's focus wake-up still serves */ }
    const t = setTimeout(() => {
      try { window.close(); } catch { /* not ours to close */ }
      setStays(true);
    }, 300);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: 420 }}>
        <h2>Bank sign-in done</h2>
        <p className="mut">
          {stays ? "You can close this tab and go back to Oikonome — the "
                   + "connection appears there on its own."
                 : "Closing this tab…"}</p>
      </div>
    </div>
  );
}
