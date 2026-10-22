// PWR-3: 「功耗分布」 — every machine's average power stacked over the chosen range (inline SVG). A
// point without samples is a gap: never drawn as zero power, shown as a dashed segment on the axis.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { PowerRange } from "../../api";
import { RANGE, type ChartSeries } from "./powerView";

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => { if (el.clientWidth > 0) setWidth(el.clientWidth); };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

export function PowerChart({ series, times, range, onToast }: {
  series: ChartSeries[]; times: number[]; range: PowerRange; onToast: (text: string) => void;
}) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<number | null>(null);
  const [announce, setAnnounce] = useState("");
  const [ref, measured] = useWidth();
  const R = RANGE[range];
  const vis = series.filter((s) => !hidden.has(s.id));
  const n = times.length;
  const W = Math.max(280, measured);
  const H = W < 600 ? 230 : 300;
  const M = { l: 44, r: 10, t: 22, b: 26 };

  const geo = useMemo(() => {
    let prev = new Array<number>(n).fill(0);
    const layers = vis.map((s) => {
      const hi = prev.map((p, i) => p + (s.values[i] ?? 0));
      const layer = { s, lo: prev, hi };
      prev = hi;
      return layer;
    });
    const top = prev;
    const gaps = Array.from({ length: n }, (_, i) => vis.every((s) => s.values[i] == null));
    const maxRaw = Math.max(...top, 1);
    const step = maxRaw > 800 ? 200 : maxRaw > 300 ? 100 : 50;
    return { layers, top, gaps, maxRaw, step, maxY: Math.ceil(maxRaw / step) * step };
  }, [vis, n]);
  const x = (i: number) => M.l + ((W - M.l - M.r) * i) / Math.max(1, n - 1);
  const y = (v: number) => M.t + (H - M.t - M.b) * (1 - v / geo.maxY);

  const toggle = (id: string) => {
    if (!hidden.has(id) && hidden.size === series.length - 1) { onToast("至少保留一台机器。"); return; }
    setHidden((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  };
  const timeLabel = (i: number) => (i === n - 1 ? "现在" : R.fmt(times[i]));
  const tipRows = (i: number) => vis.map((s) => ({ s, v: s.values[i] })).reverse();
  const total = (i: number) => vis.reduce((a, s) => a + (s.values[i] ?? 0), 0);
  const show = (i: number, speak: boolean) => {
    setHover(i);
    if (speak) {
      setAnnounce(`${timeLabel(i)}，合计约 ${Math.round(total(i))} 瓦；`
        + tipRows(i).map((r) => `${r.s.name} ${r.v == null ? "无数据" : `${Math.round(r.v)} 瓦`}`).join("，"));
    }
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!n) return;
    const cur = hover ?? n - 1;
    const k = ({ ArrowRight: 1, ArrowLeft: -1 } as Record<string, number>)[e.key];
    let i: number;
    if (k) i = Math.max(0, Math.min(n - 1, (hover === null ? n - 1 : cur) + k * (e.shiftKey ? 10 : 1)));
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = n - 1;
    else if (e.key === "Escape") { setHover(null); return; }
    else return;
    e.preventDefault();
    show(i, true);
  };

  const grid = [];
  for (let v = 0; v <= geo.maxY; v += geo.step) {
    grid.push(<g key={`g${v}`}><line x1={M.l} x2={W - M.r} y1={y(v)} y2={y(v)} stroke={v === 0 ? "#3b4d66" : "#22324a"} strokeWidth={1} />
      <text x={M.l - 6} y={y(v) + 3} textAnchor="end">{v}</text></g>);
  }
  const every = W < 600 ? R.tickEvery * 2 : R.tickEvery;
  const ticks = [];
  for (let i = n - 1; i >= 0; i -= every) {
    ticks.push(<g key={`t${i}`}><line x1={x(i)} x2={x(i)} y1={H - M.b} y2={H - M.b + 4} stroke="#3b4d66" />
      <text x={x(i)} y={H - 8} textAnchor={i === n - 1 ? "end" : "middle"}>{i === n - 1 ? "现在" : R.tick(times[i])}</text></g>);
  }
  // each machine's area over its own sampled points; a gap breaks the area
  const areas = geo.layers.flatMap(({ s, lo, hi }) => {
    const out = [];
    let start: number | null = null;
    for (let i = 0; i <= n; i++) {
      const has = i < n && s.values[i] != null;
      if (has && start === null) start = i;
      if (!has && start !== null) {
        const end = i - 1;
        let d = `M${x(start).toFixed(1)},${y(hi[start]).toFixed(1)}`;
        for (let j = start + 1; j <= end; j++) d += `L${x(j).toFixed(1)},${y(hi[j]).toFixed(1)}`;
        if (end === start) d += `L${(x(start) + 1).toFixed(1)},${y(hi[start]).toFixed(1)}L${(x(start) + 1).toFixed(1)},${y(lo[start]).toFixed(1)}`;
        for (let j = end; j >= start; j--) d += `L${x(j).toFixed(1)},${y(lo[j]).toFixed(1)}`;
        out.push(<path key={`${s.id}-${start}`} d={`${d}Z`} fill={s.color} fillOpacity={0.88} stroke="#0d1626" strokeWidth={1.5} strokeLinejoin="round" />);
        start = null;
      }
    }
    return out;
  });
  const gapLines = [];
  for (let i = 0; i < n; i++) {
    if (!geo.gaps[i]) continue;
    let j = i;
    while (j + 1 < n && geo.gaps[j + 1]) j++;
    gapLines.push(<line key={`gap${i}`} data-gap="" x1={x(Math.max(0, i - 1))} x2={x(Math.min(n - 1, j + 1))} y1={y(0) - 1} y2={y(0) - 1}
      stroke="#8fa1b8" strokeWidth={1.5} strokeDasharray="4 4" />);
    i = j;
  }
  const last = geo.top[n - 1] ?? 0;
  const tipLeft = hover === null ? 0 : (() => { const px = x(hover) + 8; let left = px + 14; if (left + 190 > W - 6) left = px - 200; return Math.max(6, left); })();

  return (
    <>
      <div className="legend" role="group" aria-label="显示或隐藏机器">
        {series.map((s) => (
          <button key={s.id} type="button" aria-pressed={!hidden.has(s.id)} onClick={() => toggle(s.id)}>
            <i style={{ background: s.color }} />{s.name}
          </button>
        ))}
      </div>
      <div className="chart-wrap">
        <div className="chart" ref={ref} tabIndex={0} aria-describedby="pw-chart-help" onKeyDown={onKey}
          onFocus={() => n && show(hover ?? n - 1, true)} onBlur={() => setHover(null)} data-testid="power-chart">
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
            aria-label={`${R.label}功耗按机器堆叠面积图，当前合计约 ${Math.round(last)} 瓦，峰值约 ${Math.round(geo.maxRaw)} 瓦`}>
            {grid}
            <text x={M.l - 6} y={9} textAnchor="end" style={{ fontSize: 9 }}>W</text>
            {areas}
            {gapLines}
            {ticks}
            {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={M.t} y2={H - M.b} stroke="#e1eaf5" strokeWidth={1} opacity={0.55} />}
            <rect x={M.l} y={M.t} width={Math.max(0, W - M.l - M.r)} height={H - M.t - M.b} fill="transparent"
              onPointerMove={(e) => {
                const r = (e.currentTarget.ownerSVGElement ?? e.currentTarget).getBoundingClientRect();
                const i = Math.round(((e.clientX - r.left - M.l) / (W - M.l - M.r)) * (n - 1));
                show(Math.max(0, Math.min(n - 1, i)), false);
              }}
              onPointerLeave={() => setHover(null)} />
          </svg>
        </div>
        {hover !== null && n > 0 && (
          <div className="chart-tip" aria-hidden="true" style={{ left: tipLeft }}>
            <div className="t">{timeLabel(hover)}</div>
            {tipRows(hover).map((r) => (
              <div key={r.s.id} className="r"><span><i style={{ background: r.s.color }} />{r.s.name}</span>
                <b>{r.v == null ? "无数据" : `${Math.round(r.v)} W`}</b></div>
            ))}
            <div className="r tot"><span>合计（估算）</span><b>{Math.round(total(hover))} W</b></div>
          </div>
        )}
      </div>
      <div className="chart-foot">
        <span id="pw-chart-help">按机器堆叠；悬停或用 ← → 键逐点查看，Home / End 跳到首尾。虚线 = 没有采样（机器离线或未运行）。</span>
        <span>显示 {vis.length} / {series.length} 台</span>
      </div>
      <p className="sr-only" aria-live="polite">{announce}</p>
    </>
  );
}

// A machine's 24 h sparkline; gaps break the line.
export function Sparkline({ values, color, name }: { values: Array<number | null>; color: string; name: string }) {
  const nums = values.filter((v): v is number => v != null);
  if (!nums.length) return <span className="k"><span>24 小时</span><span>暂无记录</span></span>;
  const W = 150, H = 36;
  const max = Math.max(...nums), min = Math.min(...nums);
  const sc = (v: number) => H - 3 - ((H - 6) * (v - min)) / Math.max(1, max - min);
  const segments: string[] = [];
  let cur: string[] = [];
  values.forEach((v, i) => {
    if (v == null) { if (cur.length) segments.push(cur.join(" ")); cur = []; return; }
    cur.push(`${((i / Math.max(1, values.length - 1)) * W).toFixed(1)},${sc(v).toFixed(1)}`);
  });
  if (cur.length) segments.push(cur.join(" "));
  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {segments.map((pts) => <polyline key={pts} points={pts} fill="none" stroke={color} strokeWidth={1.6}
          vectorEffect="non-scaling-stroke" strokeLinejoin="round" />)}
      </svg>
      <span className="k"><span>24 小时</span><span className="mono">{Math.round(min)}–{Math.round(max)} W</span></span>
      <span className="sr-only">{name} 24 小时功耗 {Math.round(min)} 到 {Math.round(max)} 瓦</span>
    </>
  );
}
