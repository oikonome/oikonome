// The zone this phone keeps, and the zones it can name — the web's
// deviceZone/knownZones/zoneLabel (webapp/src/pages/Settings.tsx), kept in
// step. Hermes may lack Intl.supportedValuesOf; then the list is the
// phone's zone and the instance's, which is still the choice that matters.

export function deviceZone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }
  catch { return ""; }
}

// Hermes (the phone) has no Intl.supportedValuesOf, so without a list of
// its own the picker held exactly one zone — the phone's — and was a dead
// end for choosing any other. The browser's full IANA list is ~420 names;
// these are the ones a household is actually in, one per distinct
// offset/rule per populated region. A zone missing here can still be set
// from the web, and whatever the server reports back is always shown.
const COMMON_ZONES = [
  "Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles",
  "America/Vancouver", "America/Phoenix", "America/Denver",
  "America/Edmonton", "America/Boise", "America/Chicago",
  "America/Winnipeg", "America/Mexico_City", "America/New_York",
  "America/Toronto", "America/Detroit", "America/Indiana/Indianapolis",
  "America/Halifax", "America/St_Johns", "America/Puerto_Rico",
  "America/Bogota", "America/Lima", "America/Caracas", "America/Santiago",
  "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
  "Atlantic/Reykjavik", "Europe/London", "Europe/Dublin", "Europe/Lisbon",
  "Europe/Madrid", "Europe/Paris", "Europe/Brussels", "Europe/Amsterdam",
  "Europe/Berlin", "Europe/Zurich", "Europe/Rome", "Europe/Vienna",
  "Europe/Prague", "Europe/Warsaw", "Europe/Stockholm", "Europe/Oslo",
  "Europe/Copenhagen", "Europe/Helsinki", "Europe/Athens", "Europe/Kyiv",
  "Europe/Istanbul", "Europe/Moscow", "Africa/Cairo", "Africa/Lagos",
  "Africa/Johannesburg", "Africa/Nairobi", "Asia/Jerusalem", "Asia/Dubai",
  "Asia/Karachi", "Asia/Kolkata", "Asia/Dhaka", "Asia/Bangkok",
  "Asia/Jakarta", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai",
  "Asia/Manila", "Asia/Taipei", "Asia/Seoul", "Asia/Tokyo",
  "Australia/Perth", "Australia/Adelaide", "Australia/Brisbane",
  "Australia/Sydney", "Australia/Melbourne", "Pacific/Auckland",
  "Etc/UTC",
];

export function knownZones(extra: string[]): string[] {
  let all: string[] = [];
  try {
    const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf;
    if (f) all = f.call(Intl, "timeZone");
  } catch { /* fall through */ }
  return Array.from(new Set([...extra.filter(Boolean), ...COMMON_ZONES, ...all]))
    .sort();
}

/** "America/Los_Angeles" → "Los Angeles (America)" — same as the web */
export function zoneLabel(z: string): string {
  const i = z.lastIndexOf("/");
  if (i < 0) return z;
  return `${z.slice(i + 1).split("_").join(" ")} (${z.slice(0, i).split("_").join(" ")})`;
}
