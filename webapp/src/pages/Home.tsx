// Home: one page, three timeframe lenses — Today | Month | Year —
// with a ‹ › stepper and month/year dropdowns. There is deliberately no
// Week lens — the weekly EMAIL digest covers that, built straight from
// lenses.week_summary. Deep-linkable:
//   /app/?lens=today&date=YYYY-MM-DD    (a past day; clean URL = live today)
//   /app/?lens=month&y=2026&m=6
//   /app/?lens=year&y=2025
// Today is the default lens (clean URL); the omicron logo in the header is
// the home button (the Today nav tab is gone).
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { api, mmdd } from "../api/client";
import Today from "./Today";
import MonthLens from "./MonthLens";
import YearLens from "./YearLens";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];

// local-date helpers (no UTC round-trips — the user's wall calendar rules)
const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-` +
  `${String(d.getDate()).padStart(2, "0")}`;
const addDays = (iso: string, n: number) => {
  const [y, m, d] = iso.split("-").map(Number);
  return isoOf(new Date(y, m - 1, d + n));
};

export default function Home() {
  const [sp, setSp] = useSearchParams();
  const now = new Date();
  const cy = now.getFullYear(), cm = now.getMonth() + 1;

  const lens = (["today", "month", "year"] as const)
    .find((l) => l === sp.get("lens")) ?? "today";
  // today lens: an optional past day (?date=); anything else — invalid,
  // today itself, future — is the live view (clean URL semantics). The
  // server clamps again to [first transaction, real today].
  const todayISO = isoOf(now);
  const rawDate = sp.get("date") ?? "";
  const date = /^\d{4}-\d{2}-\d{2}$/.test(rawDate) && rawDate < todayISO
    ? rawDate : todayISO;
  const backDate = date === todayISO ? null : date;  // null = live today
  const y = Math.min(Math.max(Number(sp.get("y")) || cy, 1900), 2100);
  const m = Math.min(Math.max(Number(sp.get("m")) || cm, 1), 12);

  // prime/reuse the child's query (same key → one fetch) to learn the
  // data's first year for the dropdown ranges / the ‹ button's floor
  const todayQ = useQuery({ queryKey: ["today", backDate ?? "now"],
                            queryFn: () => api.todayFull(backDate ?? undefined),
                            enabled: lens === "today" });
  const monthQ = useQuery({ queryKey: ["lens-month", y, m],
                            queryFn: () => api.lensMonth(y, m),
                            enabled: lens === "month" });
  const yearQ = useQuery({ queryKey: ["lens-year", y],
                           queryFn: () => api.lensYear(y),
                           enabled: lens === "year" });
  const firstYear = (lens === "month" ? monthQ.data?.first_year
                     : yearQ.data?.first_year) ?? cy;
  const years: number[] = [];
  for (let yy = cy; yy >= Math.min(firstYear, cy); yy--) years.push(yy);

  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const title = lens === "today"
    ? (backDate
        ? `${DOW[new Date(backDate + "T00:00").getDay()]} ${mmdd(backDate)}`
        : "Today")
    : lens === "month" ? `${MONTHS[m - 1]} ${y}`
    : String(y);
  useEffect(() => { document.title = `Oikonome — ${title}`; }, [title]);

  const go = (params: Record<string, string>) => setSp(params);
  const setLens = (l: string) => {
    if (l === "today") go({});
    else if (l === "month") go({ lens: "month", y: String(cy), m: String(cm) });
    else go({ lens: "year", y: String(cy) });
  };
  const goMonth = (yy: number, mm: number) =>
    go({ lens: "month", y: String(yy), m: String(mm) });

  const step = (dir: 1 | -1) => {
    if (lens === "today") {
      // stepping forward onto (or past) the real today = the clean URL
      const nd = addDays(date, dir);
      if (nd >= todayISO) go({});
      else go({ lens: "today", date: nd });
    } else if (lens === "month") {
      const mm = m + dir;
      goMonth(y + (mm === 0 ? -1 : mm === 13 ? 1 : 0),
              mm === 0 ? 12 : mm === 13 ? 1 : mm);
    } else if (lens === "year") go({ lens: "year", y: String(y + dir) });
  };
  const atCurrent = lens === "today" ? !backDate
    : lens === "month" ? y > cy || (y === cy && m >= cm)
    : y >= cy;
  // the ‹ floor for the today lens: the ledger's first date (no data =
  // nowhere to go). Unknown while the payload loads → allow the click;
  // the server clamps.
  const minDate = todayQ.data?.min_date;
  const atFloor = lens === "today" && !!todayQ.data
    && (!minDate || date <= minDate);

  return (
    <>
      <div className="lenshead">
        <h1>{title}</h1>
        <span className="lensctl">
          {/* Today steps a day at a time like every other lens (no
              dropdowns — ‹ › and the URL's ?date= carry the state) */}
          <button onClick={() => step(-1)} disabled={atFloor}
                  title="previous">‹</button>
          {lens !== "today" && (
            <>
              {lens === "month" && (
                <select value={m} aria-label="month"
                        onChange={(e) => goMonth(y, Number(e.target.value))}>
                  {MONTHS.map((name, i) => (
                    <option key={name} value={i + 1}
                            disabled={y === cy && i + 1 > cm}>{name}</option>
                  ))}
                </select>
              )}
              {(lens === "month" || lens === "year") && (
                <select value={y} aria-label="year"
                        onChange={(e) => lens === "month"
                          ? goMonth(Number(e.target.value),
                                    Number(e.target.value) === cy
                                      ? Math.min(m, cm) : m)
                          : go({ lens: "year", y: e.target.value })}>
                  {years.map((yy) => <option key={yy} value={yy}>{yy}</option>)}
                </select>
              )}
            </>
          )}
          <button onClick={() => step(1)} disabled={atCurrent} title="next">›</button>
          <span className="lenspick">
            {(["today", "month", "year"] as const).map((l) => (
              <button key={l} className={l === lens ? "on" : ""}
                      onClick={() => setLens(l)}>
                {l[0].toUpperCase() + l.slice(1)}</button>
            ))}
          </span>
        </span>
      </div>
      {lens === "today" && <Today date={backDate} />}
      {lens === "month" && <MonthLens y={y} m={m} />}
      {lens === "year" && <YearLens y={y} onMonth={(mm) => goMonth(y, mm)} />}
    </>
  );
}
