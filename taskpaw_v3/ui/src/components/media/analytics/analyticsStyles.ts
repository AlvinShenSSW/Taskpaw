// 数据分析 styles (EPIC 2026-10-31 §8.2), scoped under `.mw .an`. Shell values come from mediaTokens
// (through the `.mw` custom properties); chart colours are the approved preview's validated palette
// (design-system/taskpaw-v3/preview/media-analytics-v1.html): 4K = --c1 blue, 1080P = --c2 orange,
// fixed order, never cycled. Breakpoints as the preview: KPI 6 → 3 → 2 columns, filter bar static ≤ 900 px.
import { mediaTokens as T } from "../mediaStyles";

export const ANALYTICS_CSS = `
.mw .an{--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#9085e9;
--c-queue:#5b6b82;--c-total:#8b9bb3;--c-prev:#6b7d96;--c-spark:#5f7190;
--st-green:${T.green};--st-amber:${T.amber};--st-red:${T.red};--lane-on:#22c55e8c;
--grid:#1f2d45;--axis:#33465f;--uncov:#ffffff09;--tt-bg:#0A1222}
.mw .an .tiny{font-size:11px}
.mw .an .refresh-meta{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.mw .an .chip .icon{width:12px;height:12px}
.mw .an .chip.tag{padding:0 6px;font-size:10px;line-height:1.7}

/* filter bar: one row above everything it scopes; sticks below the app's sticky dense AppBar (48 px),
   at 64 px as the 设置 nav does (views/Settings.tsx) */
.mw .an .filterbar{display:flex;flex-wrap:wrap;gap:10px 20px;align-items:center;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#0e1727f0;margin-bottom:16px;position:sticky;top:64px;z-index:5}
.mw .an .fgroup{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);flex-wrap:wrap}
.mw .an .fgroup .seg-opt{flex:0 0 auto;padding:6px 12px}
.mw .an .switch-l{display:inline-flex;gap:10px;align-items:center;font-size:12px;color:var(--text)}
.mw .an .switch-l .toggle{min-height:26px}

/* sections */
.mw .an .an-sections{display:flex;flex-direction:column;gap:18px;transition:opacity .18s ease}
.mw .an .an-sections.busy{opacity:.45}
.mw .an .panel-head{flex-wrap:wrap}
.mw .an .panel-head h2{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.mw .an .ph-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mw .an .an-vt .seg-opt{flex:0 0 auto;font-size:11px;padding:4px 12px}
.mw .an .an-vt .icon{width:14px;height:14px}
.mw .an .an-none{padding:28px 12px;text-align:center;color:var(--muted);font-size:12px}
.mw .an .an-sub{font-size:11px;color:var(--muted);margin:-4px 0 8px}

/* KPI tiles */
.mw .an .kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px}
.mw .an .kpi{padding:14px 16px 10px;display:flex;flex-direction:column;gap:2px;min-width:0}
.mw .an .kpi .label{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.mw .an .kv{display:block;font:600 28px/1.3 var(--sans);letter-spacing:-.3px;color:var(--text)}
.mw .an .kv em{font:400 12px var(--sans);color:var(--muted);margin-left:5px;font-style:normal;letter-spacing:0}
.mw .an .kpi .note{font-size:11px;color:var(--muted)}
.mw .an .delta{display:flex;align-items:center;gap:4px;font-size:11px;flex-wrap:wrap}
.mw .an .delta .icon{width:13px;height:13px}
.mw .an .delta.good{color:var(--green-light)}.mw .an .delta.bad{color:var(--red)}.mw .an .delta.flat{color:var(--muted)}
.mw .an .delta .vs{color:var(--muted)}
.mw .an .spark-box{margin-top:6px}
.mw .an .spark-empty{height:34px;line-height:34px}

/* charts */
.mw .an .an-chart{position:relative;width:100%;min-width:0}
.mw .an .chart-svg{display:block;width:100%;height:auto;overflow:visible}
.mw .an .chart-svg text{font-family:var(--sans);font-size:11px;fill:var(--muted)}
.mw .an .chart-svg .tick{font-variant-numeric:tabular-nums}
.mw .an .chart-svg .grid{stroke:var(--grid);stroke-width:1;shape-rendering:crispEdges}
.mw .an .chart-svg .axis{stroke:var(--axis);stroke-width:1;shape-rendering:crispEdges}
.mw .an .chart-svg .hit{fill:transparent;stroke:none;transition:fill .15s}
.mw .an .chart-svg [data-rove]{cursor:pointer;outline:none}
.mw .an .chart-svg [data-rove]:hover .hit{fill:#ffffff07}
.mw .an .chart-svg [data-rove]:focus-visible .hit{stroke:var(--blue);stroke-width:2}
.mw .an .chart-svg .mk{transition:filter .15s}
.mw .an .chart-svg [data-rove]:hover .mk,.mw .an .chart-svg [data-rove]:focus-visible .mk{filter:brightness(1.25)}
.mw .an .chart-svg .dlabel{fill:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.mw .an .chart-svg .uncov{fill:var(--uncov)}
.mw .an .chart-svg .uncov-t{fill:var(--muted)}
.mw .an .chart-svg .prev-line{fill:none;stroke:var(--c-prev);stroke-width:2;stroke-linejoin:round;stroke-linecap:round;pointer-events:none}
.mw .an .chart-svg .spark-line{fill:none;stroke:var(--c-spark);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.mw .an .chart-svg .spark-dot{fill:var(--c1);stroke:var(--surface);stroke-width:2}
.mw .an .chart-svg .spark-hit{fill:transparent;cursor:default}
.mw .an .lgd{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--text);margin-bottom:10px;align-items:center}
.mw .an .lgd-i{display:inline-flex;align-items:center;gap:7px}
.mw .an .lgd-k{display:inline-block;flex:none}
.mw .an .lgd-k.rect{width:10px;height:10px;border-radius:2px}
.mw .an .lgd-k.line{width:14px;height:2px;border-radius:1px}

/* the shared tooltip */
.mw .an .an-tt{position:fixed;z-index:50;pointer-events:none;background:var(--tt-bg);border:1px solid #3b4d66;border-radius:8px;padding:8px 11px;font-size:12px;box-shadow:0 10px 30px #000a;min-width:130px;max-width:280px;left:0;top:0}
.mw .an .tt-title{color:var(--muted);font-size:11px;margin-bottom:4px}
.mw .an .tt-row{display:flex;align-items:center;gap:8px;line-height:1.75}
.mw .an .tt-row b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.mw .an .tt-row span:last-child{color:var(--muted)}
.mw .an .tt-key{width:12px;height:2px;border-radius:1px;flex:none}
.mw .an .tt-key.none{background:transparent}
.mw .an .tt-note{color:var(--muted);font-size:11px;margin-top:4px;border-top:1px solid var(--line);padding-top:4px}

/* tables */
.mw .an .table-wrap{overflow-x:auto}
.mw .an table.grid.fit{min-width:0}
.mw .an table.grid th.num,.mw .an table.grid td.num{text-align:right;font-variant-numeric:tabular-nums}

@media(max-width:1250px){.mw .an .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.mw .an .filterbar{position:static}}
@media(max-width:640px){.mw .an .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mw .an .kpi{padding:12px 12px 8px}.mw .an .kv{font-size:24px}.mw .an .filterbar{gap:10px}}
`;
