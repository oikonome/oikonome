// "How has the forecast done?" and "Why the forecast missed"
// (specs/forecast-grading.md): opened from the track-record line under the
// cash chart. Every sentence and figure is the server's
// (forecast_score.panel / forecast_score.miss); this draws them.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errText, mmdd, money } from "../api/client";
import AreaChart from "./AreaChart";
import Disclosure from "./Disclosure";

export default function ForecastRecord({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [day, setDay] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["forecast-score"], queryFn: api.forecastScore,
                       enabled: open });
  const p = q.data;
  return (
    <Disclosure open={open} onToggle={setOpen} style={{ marginTop: ".3rem" }}
      summary={<span className="sub" style={{ fontSize: 12 }}>{text}</span>}>
      <div style={{ padding: ".4rem .2rem" }}>
        <div className="sub" style={{ fontWeight: 600 }}>How has the forecast done?</div>
        {q.isError && <div className="sub">{errText(q.error)}</div>}
        {p && p.empty_text && <div className="sub">{p.empty_text}</div>}
        {p && p.history.length > 1 && (
          <AreaChart variant="static" height={160}
            subject="Real checking balance against the 30-day forecast"
            points={p.history.map((h) => [h.day, h.actual] as [string, number])}
            second={p.history.map((h) => [h.day, h.predicted] as [string, number])}
            labels={["real balance", "forecast 30 days earlier"]} />
        )}
        {p?.coverage_text && <div className="sub" style={{ fontSize: 12 }}>{p.coverage_text}</div>}
        {p?.card_paying_text && <div className="sub" style={{ fontSize: 12 }}>{p.card_paying_text}</div>}
        {p?.skips_text.map((t) => <div key={t} className="sub" style={{ fontSize: 12 }}>{t}</div>)}
        {p && p.history.length > 0 && (
          <table style={{ marginTop: ".4rem" }}>
            <thead><tr><th>Day</th><th className="num">Forecast</th>
              <th className="num">Real</th><th className="num">Miss</th><th aria-label="Why it missed" /></tr></thead>
            <tbody>
              {[...p.history].reverse().slice(0, 30).map((h) => (
                <tr key={h.day}>
                  <td className="mut">{mmdd(h.day)}</td>
                  <td className="num">{money(h.predicted)}</td>
                  <td className="num">{money(h.actual)}</td>
                  <td className="num">{money(h.error)}</td>
                  <td><button type="button" className="link"
                    onClick={() => setDay(day === h.day ? null : h.day)}>
                    {day === h.day ? "Hide" : "Why"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {day && <WhyItMissed day={day} />}
      </div>
    </Disclosure>
  );
}

function WhyItMissed({ day }: { day: string }) {
  const q = useQuery({ queryKey: ["forecast-miss", day],
                       queryFn: () => api.forecastMiss(day, 30) });
  const m = q.data;
  if (q.isLoadingError) return <div className="sub">{errText(q.error)}</div>;
  if (!m) return null;
  return (
    <div style={{ marginTop: ".5rem" }}>
      <div className="sub" style={{ fontWeight: 600 }}>Why the forecast missed — {mmdd(m.day)}</div>
      <table>
        <thead><tr><th aria-label="Part" /><th className="num">Forecast</th>
          <th className="num">Real</th><th className="num">Difference</th></tr></thead>
        <tbody>
          {m.parts.map((x) => (
            <tr key={x.kind}><td>{x.label}</td>
              <td className="num">{money(x.predicted)}</td>
              <td className="num">{money(x.actual)}</td>
              <td className="num">{money(x.diff)}</td></tr>
          ))}
          <tr><td><b>Miss</b></td><td className="num">{money(m.predicted)}</td>
            <td className="num">{money(m.actual)}</td>
            <td className="num"><b>{money(m.error)}</b></td></tr>
        </tbody>
      </table>
      {m.items.map((it) => <div key={it.text} className="sub" style={{ fontSize: 12 }}>{it.text}</div>)}
    </div>
  );
}
