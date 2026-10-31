// Hand-rolled SVG chart primitives of the 数据分析 page (EPIC 2026-10-31 §8.2, OD5: no chart library),
// ported from the approved preview. Every chart: role="img" + aria-label, one focusable item in the tab
// order with arrow / Home / End roving, and one shared tooltip that follows the pointer and the focus
// (Escape hides it). C4/C5 add HBarChart, RangeChart, ShareBar, StripChart and LaneChart beside these,
// reusing chartKit (useChartWidth, useRoving, tipHandlers, niceTicks, the path helpers).
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { tickFmt } from "./analyticsFormat";
import {
  colPath, linePath, niceTicks, tipHandlers, TipContext, useChartTip, useChartWidth, useRoving, type ChartTip, type TipContent,
} from "./chartKit";

export type { TipContent, TipRow } from "./chartKit";
type TipState = { content: TipContent; x: number; y: number; above: boolean } | null;
const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

/** One tooltip for every chart below it; fixed-position, never covering the pointer. */
export function ChartTooltipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<TipState>(null);
  const box = useRef<HTMLDivElement>(null);
  const api = useMemo<ChartTip>(() => ({
    show: (content, x, y, above = false) => setTip({ content, x, y, above }),
    move: (x, y) => setTip((t) => (t ? { ...t, x, y } : t)),
    hide: () => setTip(null),
  }), []);
  const open = tip !== null;
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setTip(null); };
    // a pointer tooltip goes away on scroll; a focus tooltip stays with its focused item
    const onScroll = () => { if (!(document.activeElement instanceof Element && document.activeElement.matches("[data-tip]"))) setTip(null); };
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => { document.removeEventListener("keydown", onKey); window.removeEventListener("scroll", onScroll); };
  }, [open]);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !tip) return;
    const w = el.offsetWidth, h = el.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
    let l = tip.x + 14, t = tip.above ? tip.y - h - 10 : tip.y + 14;
    if (l + w > vw - 8) l = tip.x - w - 14;
    if (l < 8) l = 8;
    if (t + h > vh - 8) t = tip.y - h - 14;
    if (t < 8) t = tip.above ? tip.y + 14 : 8;
    el.style.transform = `translate(${Math.round(l)}px,${Math.round(t)}px)`;
  }, [tip]);
  return (
    <TipContext.Provider value={api}>
      {children}
      {tip && (
        <div className="an-tt" role="tooltip" ref={box}>
          {tip.content.title && <div className="tt-title">{tip.content.title}</div>}
          {tip.content.rows.map((r, i) => (
            <div className="tt-row" key={i}>
              <span className={`tt-key${r.color ? "" : " none"}`} style={r.color ? { background: r.color } : undefined} />
              <b>{r.value}</b><span>{r.label ?? ""}</span>
            </div>
          ))}
          {tip.content.note && <div className="tt-note">{tip.content.note}</div>}
        </div>
      )}
    </TipContext.Provider>
  );
}

// ---- ColumnChart: stacked columns, previous-period line, uncovered band, max label ----
export interface ColumnSeries { key: string; label: string; color: string; vals: number[] }
export interface ColumnChartProps {
  n: number;
  series: ColumnSeries[];
  label: string;
  prev?: Array<number | null> | null;
  tick?: (i: number) => string;
  tickAll?: boolean;
  tip?: (i: number) => TipContent;
  labelMax?: boolean;
  fmt?: (v: number) => string;
  uncov?: number; // fraction of the width before the first record
  height?: number;
  mini?: boolean;
  yMax?: number;
}
export function ColumnChart(props: ColumnChartProps) {
  const [ref, width] = useChartWidth();
  const height = props.height ?? (props.mini ? 96 : 232);
  return <div className="an-chart" ref={ref} style={{ minHeight: height }}>{width >= 10 && <ColumnSvg {...props} W={width} H={height} />}</div>;
}

function ColumnSvg({ n, series, label, prev, tick, tickAll, tip, labelMax, fmt = String, uncov = 0, mini, yMax, W, H }: ColumnChartProps & { W: number; H: number }) {
  const tipApi = useChartTip();
  const rove = useRoving(n);
  const padL = mini ? 22 : 34, padR = 6, padT = mini ? 8 : 18, padB = mini ? 18 : 24;
  const pw = Math.max(40, W - padL - padR), ph = H - padT - padB;
  const tot = (i: number) => series.reduce((a, s) => a + (s.vals[i] || 0), 0);
  const totals = Array.from({ length: n }, (_, i) => tot(i));
  const mx = yMax ?? Math.max(0, ...totals, ...(prev ?? []).map((v) => v || 0));
  const ticks = niceTicks(mx || 1, mini ? 2 : 4), top = ticks[ticks.length - 1];
  const y = (v: number) => padT + ph - (v / top) * ph;
  const band = pw / n, bw = Math.max(1.5, Math.min(24, band * 0.68, band - 2));
  const every = tickAll ? 1 : Math.max(1, Math.ceil((mini ? 42 : 52) / band));
  const uw = pw * Math.min(1, uncov);
  let maxI = -1, maxV = 0;
  totals.forEach((v, i) => { if (v > maxV) { maxV = v; maxI = i; } });
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {uncov > 0 && <rect x={padL} y={padT} width={uw} height={ph} className="uncov" />}
      {uncov > 0 && uw > 48 && !mini && <text x={padL + uw / 2} y={padT + ph / 2} textAnchor="middle" className="uncov-t">无记录</text>}
      {ticks.map((t) => (
        <g key={t}>
          <line x1={padL} x2={padL + pw} y1={y(t)} y2={y(t)} className={t === 0 ? "axis" : "grid"} />
          <text x={padL - 6} y={y(t) + 3.5} textAnchor="end" className="tick">{tickFmt(t)}</text>
        </g>
      ))}
      {tick && Array.from({ length: n }, (_, i) => i).filter((i) => i % every === 0).map((i) => {
        const lb = tick(i);
        return lb ? <text key={`t${i}`} x={clamp(padL + band * i + band / 2, padL + 14, W - 16)} y={H - 6} textAnchor="middle" className="tick">{lb}</text> : null;
      })}
      {totals.map((_, i) => {
        const x = padL + band * i + (band - bw) / 2;
        const segs = series.map((s) => ({ s, v: s.vals[i] || 0 })).filter((z) => z.v > 0);
        let acc = 0;
        return (
          <g key={i} {...rove(i)} {...(tip ? tipHandlers(tipApi, () => tip(i)) : {})}>
            <rect x={padL + band * i} y={padT} width={band} height={ph} className="hit" />
            {segs.map((z, k) => {
              const y0 = y(acc), y1 = y(acc + z.v);
              acc += z.v;
              const h = Math.max(1, y0 - y1 - (k > 0 ? 2 : 0));
              return k === segs.length - 1
                ? <path key={z.s.key} d={colPath(x, y1, bw, h)} className="mk" style={{ fill: z.s.color }} />
                : <rect key={z.s.key} x={x} y={y1} width={bw} height={h} className="mk" style={{ fill: z.s.color }} />;
            })}
          </g>
        );
      })}
      {prev && <path d={linePath(prev.map((v, i) => (v == null ? null : [padL + band * i + band / 2, y(v)])))} className="prev-line" />}
      {labelMax && maxI >= 0 && (
        <text x={clamp(padL + band * maxI + band / 2, padL + 10, W - 14)} y={y(maxV) - 6} textAnchor="middle" className="dlabel">{fmt(maxV)}</text>
      )}
    </svg>
  );
}

// ---- Sparkline: de-emphasised line, the latest point in the accent, a pointer tooltip per slice ----
export interface SparklineProps { vals: Array<number | null>; labels: string[]; fmt: (v: number) => string; height?: number }
export function Sparkline({ vals, labels, fmt, height = 34 }: SparklineProps) {
  const [ref, width] = useChartWidth();
  const tip = useChartTip();
  const present = vals.filter((v): v is number => v != null);
  const W = width, p = 4, nn = vals.length;
  const lo = Math.min(0, ...present), hi = Math.max(...present) || 1;
  const x = (i: number) => p + (i * (W - 2 * p)) / Math.max(1, nn - 1);
  const y = (v: number) => height - p - ((v - lo) / (hi - lo || 1)) * (height - 2 * p);
  let last = -1;
  vals.forEach((v, i) => { if (v != null) last = i; });
  return (
    <div className="an-chart spark-box" ref={ref} style={{ minHeight: height }}>
      {!present.length ? <div className="tiny muted spark-empty">没有足够记录</div> : W >= 10 && (
        <svg className="chart-svg" viewBox={`0 0 ${W} ${height}`} width={W} height={height} role="img" aria-label="近期趋势">
          <path d={linePath(vals.map((v, i) => (v == null ? null : [x(i), y(v)])))} className="spark-line" />
          {last >= 0 && <circle cx={x(last)} cy={y(vals[last]!)} r={3.5} className="spark-dot" />}
          {vals.map((v, i) => {
            const content = () => ({ title: labels[i], rows: [{ color: i === last ? "var(--c1)" : "var(--c-spark)", value: v == null ? "无记录" : fmt(v) }] });
            return (
              <rect key={i} x={x(i) - W / nn / 2} y={0} width={W / nn} height={height} className="spark-hit" data-tip=""
                onPointerEnter={(e) => tip.show(content(), e.clientX, e.clientY)} onPointerMove={(e) => tip.move(e.clientX, e.clientY)}
                onPointerLeave={() => tip.hide()} />
            );
          })}
        </svg>
      )}
    </div>
  );
}
