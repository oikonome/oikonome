// The zone this browser reports, or "" when it cannot say. Read both by
// the Settings zone picker and by App's first-load adoption of the device
// zone, so it lives outside the Settings page rather than dragging that
// page into the boot bundle.
export function deviceZone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }
  catch { return ""; }
}
