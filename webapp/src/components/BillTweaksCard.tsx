import { Link } from "react-router";
import { type Settings as SettingsData } from "../api/client";

// This was a full card holding a read-only three-row table that
// restated things set on the Bills page — a whole card to tell you what
// another page owns. One line now, and it says nothing at all when there is
// nothing to say.
export default function BillTweaksCard({ data }: { data: SettingsData }) {
  const caps = Object.entries(data.occurrence_caps ?? {});
  const hints = Object.keys(data.dismissed_hints ?? {});
  const disabled = data.disabled_bills ?? [];
  const parts = [
    disabled.length && `${disabled.length} bill${disabled.length > 1 ? "s" : ""} disabled`,
    caps.length && `${caps.length} occurrence cap${caps.length > 1 ? "s" : ""}`,
    hints.length && `${hints.length} hint${hints.length > 1 ? "s" : ""} dismissed`,
  ].filter(Boolean) as string[];
  if (!parts.length) return null;
  return (
    <p className="sub" style={{ margin: ".7rem .2rem 0" }}>
      Bill tweaks: {parts.join(" · ")} — all set on{" "}
      <Link to="/bills">Bills ›</Link>
    </p>
  );
}
