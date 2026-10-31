// Hand-rolled SVG chart primitives of the 数据分析 page (EPIC 2026-10-31 §8.2, OD5: no chart library),
// ported from the approved preview. Every chart: role="img" + aria-label, one focusable item in the tab
// order with arrow / Home / End roving, and one shared tooltip that follows the pointer and the focus
// (Escape hides it). LaneChart (C5) reuses the same chartKit plumbing (useChartWidth, useRoving,
// tipHandlers, niceTicks, the path helpers).
import type { SvgIconComponent } from "@mui/icons-material";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { tickFmt } from "./analyticsFormat";
import type { Stats } from "./analyticsModel";
import {
  barPath, colPath, linePath, niceTicks, thinTicks, tipHandlers, TipContext, useChartTip, useChartWidth, useRoving, type ChartTip,
  type TipContent,
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

// ---- HBarChart: labelled horizontal bars, optional stacking, value text and a leading icon ----
export interface HBarSeg { v: number; color: string }
export interface HBarRow {
  key: string;
  label: string;
  segs: HBarSeg[];
  value?: string | null; // text after the bar
  tip?: () => TipContent;
  icon?: SvgIconComponent; // e.g. the fail / warn icon, always beside the text label (never colour alone)
  iconColor?: string;
}
export interface HBarChartProps {
  rows: HBarRow[];
  label: string;
  max?: number; // the scale's top (100 for shares); the largest row total by default
  labelWidth?: number;
  valueWidth?: number;
  rowHeight?: number;
  thickness?: number;
}
const hbarHeight = (rows: number, rowHeight = 30) => rows * rowHeight + 8;
export function HBarChart(props: HBarChartProps) {
  const [ref, width] = useChartWidth();
  const height = hbarHeight(props.rows.length, props.rowHeight);
  return <div className="an-chart" ref={ref} style={{ minHeight: height }}>{width >= 10 && <HBarSvg {...props} W={width} H={height} />}</div>;
}

function HBarSvg({ rows, label, max, labelWidth = 110, valueWidth = 64, rowHeight = 30, thickness = 14, W, H }: HBarChartProps & { W: number; H: number }) {
  const tipApi = useChartTip();
  const rove = useRoving(rows.length);
  const lw = Math.min(labelWidth, Math.round(W * 0.42)), pw = Math.max(30, W - lw - valueWidth), top = 4;
  const total = (r: HBarRow) => r.segs.reduce((a, s) => a + s.v, 0);
  const mx = max ?? Math.max(1e-9, ...rows.map(total));
  const th = Math.min(thickness, rowHeight - 10);
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {rows.map((r, ri) => {
        const cy = top + ri * rowHeight + rowHeight / 2;
        const segs = r.segs.filter((s) => s.v > 0);
        const RowIcon = r.icon;
        let acc = 0;
        return (
          <g key={r.key} {...rove(ri)} {...(r.tip ? tipHandlers(tipApi, r.tip) : {})}>
            <rect x={0} y={cy - rowHeight / 2} width={W} height={rowHeight} className="hit" />
            {RowIcon && <RowIcon x={0} y={cy - 7} width={14} height={14} style={{ width: 14, height: 14, color: r.iconColor ?? "currentColor" }} aria-hidden />}
            <text x={RowIcon ? 20 : 0} y={cy + 4} className="rlabel">{r.label}</text>
            {segs.map((s, k) => {
              let x = lw + (acc / mx) * pw, w = (s.v / mx) * pw;
              acc += s.v;
              if (k > 0) { x += 2; w -= 2; }
              w = Math.max(1, w);
              return k === segs.length - 1
                ? <path key={k} d={barPath(x, cy - th / 2, w, th)} className="mk" style={{ fill: s.color }} />
                : <rect key={k} x={x} y={cy - th / 2} width={w} height={th} className="mk" style={{ fill: s.color }} />;
            })}
            {r.value != null && <text x={lw + (acc / mx) * pw + 6} y={cy + 4} className="dlabel">{r.value}</text>}
          </g>
        );
      })}
    </svg>
  );
}

// ---- RangeChart: per row the min–max hairline, the p25–p75 band and the average dot, on one shared axis ----
export interface RangeRow { key: string; label: string; color: string; st: Stats | null }
export interface RangeChartProps { rows: RangeRow[]; fmt: (v: number) => string; label: string }
const rangeHeight = (rows: number) => 6 + rows * 42 + 22;
export function RangeChart(props: RangeChartProps) {
  const [ref, width] = useChartWidth();
  const height = rangeHeight(props.rows.length);
  return <div className="an-chart" ref={ref} style={{ minHeight: height }}>{width >= 10 && <RangeSvg {...props} W={width} H={height} />}</div>;
}

function RangeSvg({ rows, fmt, label, W, H }: RangeChartProps & { W: number; H: number }) {
  const tipApi = useChartTip();
  const rove = useRoving(rows.length);
  const narrow = W < 480, lw = narrow ? 70 : 92, vw = narrow ? 92 : 118, rowH = 42, top = 6;
  const pw = Math.max(40, W - lw - vw - 8);
  const mx = Math.max(1e-9, ...rows.map((r) => (r.st ? r.st.max : 0)));
  const ticks = niceTicks(mx, narrow ? 3 : 4), tv = ticks[ticks.length - 1];
  const x = (v: number) => lw + (v / tv) * pw, yb = top + rows.length * rowH;
  const tip = (r: RangeRow): TipContent => {
    const st = r.st;
    return {
      title: `${r.label}${st ? ` · ${st.n} 部` : ""}`,
      rows: st
        ? [
          { color: r.color, value: fmt(st.avg), label: "平均" }, { value: fmt(st.p50), label: "中位数" },
          { value: `${fmt(st.p25)} – ${fmt(st.p75)}`, label: "中间一半（p25–p75）" }, { value: fmt(st.min), label: "最短" },
          { value: fmt(st.max), label: "最长" },
        ]
        : [{ value: "—", label: "这个时段没有数据" }],
    };
  };
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={top} y2={yb} className={t === 0 ? "axis" : "grid"} />
          <text x={x(t)} y={H - 6} textAnchor="middle" className="tick">{tickFmt(t)}</text>
        </g>
      ))}
      {rows.map((r, ri) => {
        const cy = top + ri * rowH + rowH / 2, st = r.st;
        return (
          <g key={r.key} {...rove(ri)} {...tipHandlers(tipApi, () => tip(r))}>
            <rect x={0} y={cy - rowH / 2} width={W} height={rowH} className="hit" />
            <text x={0} y={cy + 4} className="rlabel">{r.label}</text>
            {st ? (
              <>
                <line x1={x(st.min)} x2={x(st.max)} y1={cy} y2={cy} className="mk range-line" style={{ stroke: r.color }} />
                <rect x={x(st.p25)} y={cy - 4} width={Math.max(2, x(st.p75) - x(st.p25))} height={8} rx={2} className="mk" style={{ fill: r.color }} />
                <circle cx={x(st.avg)} cy={cy} r={4.5} className="mk range-dot" style={{ stroke: r.color }} />
                <text x={W - vw + 6} y={cy - 2}>
                  <tspan className="vt1">平均 {fmt(st.avg)}</tspan>
                  <tspan x={W - vw + 6} dy={14} className="vt2">最长 {fmt(st.max)}</tspan>
                </text>
              </>
            ) : <text x={lw} y={cy + 4} className="tick">没有数据</text>}
          </g>
        );
      })}
    </svg>
  );
}

// ---- ShareBar: one 100 % bar split by share; stages without time are left out ----
export interface ShareSeg { key: string; label: string; v: number; color: string; tip?: () => TipContent }
export function ShareBar({ segs, label }: { segs: ShareSeg[]; label: string }) {
  const [ref, width] = useChartWidth();
  const shown = segs.filter((s) => s.v > 0);
  return (
    <div className="an-chart" ref={ref} style={{ minHeight: 34 }}>
      {!shown.length ? <p className="small muted">这个时段没有数据</p> : width >= 10 && <ShareSvg segs={shown} label={label} W={width} />}
    </div>
  );
}

function ShareSvg({ segs, label, W }: { segs: ShareSeg[]; label: string; W: number }) {
  const tipApi = useChartTip();
  const rove = useRoving(segs.length);
  const H = 34, bh = 24, y0 = 5, tot = segs.reduce((a, s) => a + s.v, 0);
  const avail = W - 2 * (segs.length - 1);
  const ws = segs.map((s) => Math.max(3, (s.v / tot) * avail));
  const big = ws.indexOf(Math.max(...ws));
  ws[big] -= ws.reduce((a, w) => a + w, 0) - avail; // the minimum widths come out of the largest share
  let x = 0;
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {segs.map((s, k) => {
        const x0 = x;
        x += ws[k] + 2;
        return (
          <g key={s.key} {...rove(k)} {...(s.tip ? tipHandlers(tipApi, s.tip) : {})}>
            <rect x={x0} y={0} width={ws[k] + 2} height={H} className="hit" />
            {k === segs.length - 1
              ? <path d={barPath(x0, y0, ws[k], bh)} className="mk" style={{ fill: s.color }} />
              : <rect x={x0} y={y0} width={ws[k]} height={bh} className="mk" style={{ fill: s.color }} />}
          </g>
        );
      })}
    </svg>
  );
}

// ---- StripChart: events on a time axis over a track, with the uncovered band before the first record ----
export interface StripItem { key: string; s: number; e: number; tip?: () => TipContent }
export interface StripChartProps {
  start: number;
  end: number; // the time domain (epoch ms)
  items: StripItem[];
  ticks: Array<{ t: number; label: string }>; // candidate labels; thinned to ≥ 58 px apart
  label: string;
  uncovUntil?: number | null; // grey band from `start` to here
  trackFrom?: number; // the recorded track; the whole domain by default
  trackTo?: number;
}
const STRIP_HEIGHT = 56;
export function StripChart(props: StripChartProps) {
  const [ref, width] = useChartWidth();
  return <div className="an-chart" ref={ref} style={{ minHeight: STRIP_HEIGHT }}>{width >= 10 && <StripSvg {...props} W={width} />}</div>;
}

function StripSvg({ start, end, items, ticks, label, uncovUntil, trackFrom = start, trackTo = end, W }: StripChartProps & { W: number }) {
  const tipApi = useChartTip();
  const rove = useRoving(items.length);
  const H = STRIP_HEIGHT, pw = W - 4, y0 = 10, bh = 14;
  const x = (t: number) => ((t - start) / (end - start || 1)) * pw;
  const shown = thinTicks(ticks.map((t) => ({ ...t, px: x(t.t) })));
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {uncovUntil != null && uncovUntil > start && <rect x={0} y={y0 - 4} width={x(Math.min(uncovUntil, end))} height={bh + 8} className="uncov" />}
      <rect x={x(trackFrom)} y={y0} width={Math.max(0, x(trackTo) - x(trackFrom))} height={bh} rx={2} className="strip-track" />
      {shown.map((t) => (
        <g key={t.t}>
          <line x1={t.px} x2={t.px} y1={y0 + bh + 2} y2={y0 + bh + 7} className="axis" />
          <text x={clamp(t.px, 16, W - 18)} y={H - 6} textAnchor="middle" className="tick">{t.label}</text>
        </g>
      ))}
      {items.map((it, i) => {
        const xa = x(it.s), w = Math.max(3, x(it.e) - xa), hw = Math.max(16, w);
        return (
          <g key={it.key} {...rove(i)} {...(it.tip ? tipHandlers(tipApi, it.tip) : {})}>
            <rect x={xa + w / 2 - hw / 2} y={0} width={hw} height={y0 + bh + 8} className="hit" />
            <rect x={xa} y={y0} width={w} height={bh} rx={1} className="mk strip-mk" />
          </g>
        );
      })}
    </svg>
  );
}

// ---- LaneChart: one row per lane, coloured state segments on a shared time axis (设备在线) ----
export interface LaneSeg { key: string; s: number; e: number; color: string; tip?: () => TipContent }
export interface Lane {
  key: string;
  label: string;
  from: number;
  to: number; // the lane's own span inside the domain (a machine that joined later starts later)
  segs: LaneSeg[];
  value?: string; // text right of the lane (「在线 97%」)
  tip?: () => TipContent; // the lane summary, on its label
  empty?: string; // shown when the lane has no span (from ≥ to)
}
export interface LaneChartProps {
  start: number;
  end: number; // the time domain (epoch ms)
  lanes: Lane[];
  ticks: Array<{ t: number; label: string }>; // candidate labels; thinned to ≥ 58 px apart
  label: string;
}
const laneHeight = (n: number) => 6 + n * 34 + 22;
export function LaneChart(props: LaneChartProps) {
  const [ref, width] = useChartWidth();
  const height = laneHeight(props.lanes.length);
  return <div className="an-chart" ref={ref} style={{ minHeight: height }}>{width >= 10 && <LaneSvg {...props} W={width} H={height} />}</div>;
}

function LaneSvg({ start, end, lanes, ticks, label, W, H }: LaneChartProps & { W: number; H: number }) {
  const tipApi = useChartTip();
  const live = (l: Lane) => l.to > l.from;
  const rove = useRoving(lanes.reduce((a, l) => a + (live(l) ? 1 + l.segs.length : 0), 0));
  const narrow = W < 520, lw = narrow ? 96 : 132, rw = narrow ? 64 : 84, rowH = 34, top = 6, bh = 14;
  const pw = Math.max(40, W - lw - rw), yb = top + lanes.length * rowH;
  const x = (t: number) => lw + ((t - start) / (end - start || 1)) * pw;
  const shown = thinTicks(ticks.map((t) => ({ ...t, px: x(t.t) })));
  let k = 0; // the running roving index: each lane's summary, then its segments
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      {shown.map((t) => (
        <g key={t.t}>
          <line x1={t.px} x2={t.px} y1={top} y2={yb} className="grid" />
          <text x={clamp(t.px, lw + 14, W - rw - 6)} y={H - 6} textAnchor="middle" className="tick">{t.label}</text>
        </g>
      ))}
      {lanes.map((ln, li) => {
        const cy = top + li * rowH + rowH / 2;
        if (!live(ln)) {
          return (
            <g key={ln.key} className="lane">
              <text x={0} y={cy + 4} className="rlabel">{ln.label}</text>
              <text x={lw + 6} y={cy + 4} className="tick">{ln.empty ?? "没有记录"}</text>
            </g>
          );
        }
        return (
          <g key={ln.key} className="lane">
            <g {...rove(k++)} {...(ln.tip ? tipHandlers(tipApi, ln.tip) : {})}>
              <rect x={0} y={cy - rowH / 2 + 2} width={lw - 4} height={rowH - 4} className="hit" />
              <text x={0} y={cy + 4} className="rlabel">{ln.label}</text>
              <rect x={x(ln.from)} y={cy - bh / 2} width={Math.max(0, x(ln.to) - x(ln.from))} height={bh} rx={2} className="strip-track" />
            </g>
            {ln.segs.map((z) => {
              const xa = x(Math.max(z.s, ln.from)), w = Math.max(2, x(Math.min(z.e, ln.to)) - xa), hw = Math.max(12, w);
              return (
                <g key={z.key} {...rove(k++)} {...(z.tip ? tipHandlers(tipApi, z.tip) : {})}>
                  <rect x={xa + w / 2 - hw / 2} y={cy - rowH / 2 + 2} width={hw} height={rowH - 4} className="hit" />
                  <rect x={xa} y={cy - bh / 2} width={w} height={bh} rx={1} className="mk" style={{ fill: z.color }} />
                </g>
              );
            })}
            {ln.value && <text x={W - rw + 10} y={cy + 4} className="vt1">{ln.value}</text>}
          </g>
        );
      })}
    </svg>
  );
}
