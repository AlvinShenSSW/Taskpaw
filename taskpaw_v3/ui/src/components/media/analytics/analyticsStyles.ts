// 数据分析 styles (EPIC 2026-10-31 §8.2), scoped under `.mw .an`. Shell values come from mediaTokens
// (through the `.mw` custom properties); chart colours are the approved preview's validated palette
// (design-system/taskpaw-v3/preview/media-analytics-v1.html): 4K = --c1 blue, 1080P = --c2 orange,
// fixed order, never cycled. Breakpoints as the preview: KPI 6 → 3 → 2 columns, filter bar static ≤ 900 px.
import { mediaTokens as T } from "../mediaStyles";

export const ANALYTICS_CSS = `
.mw .an{--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#9085e9;
--c-queue:#5b6b82;--c-total:#8b9bb3;--c-prev:#6b7d96;--c-spark:#5f7190;
--st-green:${T.green};--st-amber:${T.amber};--st-red:${T.red};--lane-on:#22c55e8c;
--grid:#1f2d45;--axis:#33465f;--uncov:#ffffff09;--tt-bg:#0A1222;--track:#1b2a40;--lane-none:#3b4d6699;--meter-track:#1d3352}
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
.mw .an .lgd-k.tick{width:2px;height:12px;border-radius:1px}
.mw .an .chart-svg .rlabel{fill:var(--text);font-size:12px}
.mw .an .chart-svg .vt1{fill:var(--text);font-size:11px}
.mw .an .chart-svg .vt2{fill:var(--muted);font-size:10.5px}
.mw .an .chart-svg .range-line{stroke-width:1;opacity:.85}
.mw .an .chart-svg .range-dot{fill:var(--surface);stroke-width:2}
.mw .an .chart-svg .strip-track{fill:var(--track)}
.mw .an .chart-svg .strip-mk{fill:var(--st-amber)}

/* section layout (各 GPU 机器, 本机（主控）处理, 耗时分布) */
.mw .an .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px 28px}
.mw .an .block{min-width:0}
.mw .an .block>h3{font-size:13px;font-weight:600;margin-bottom:2px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.mw .an .block>h3 .icon{width:15px;height:15px}
.mw .an .an-ic{display:inline-flex}
.mw .an .bdesc{font-size:11px;color:var(--muted);margin-bottom:10px}
.mw .an .an-after{margin-top:6px}
.mw .an .multiples{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.mw .an .mini{border:1px solid #22324a;border-radius:9px;padding:10px 12px 6px;background:#0c1626;min-width:0}
.mw .an .mini-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:12px;margin-bottom:4px}
.mw .an .mini-h strong{font-weight:600}
.mw .an .ministats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.mw .an .mstat{border:1px solid #22324a;border-radius:9px;padding:12px 14px;background:#0c1626;min-width:0}
.mw .an .mstat .label{font-size:11px;color:var(--muted);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.mw .an .mstat strong{display:block;font:600 22px/1.35 var(--sans)}
.mw .an .mstat strong em{font:400 12px var(--sans);color:var(--muted);margin-left:4px;font-style:normal}
.mw .an .mstat .note{font-size:11px;color:var(--muted)}
.mw .an .mstat .chips{margin-top:6px}
.mw .an .share-lg{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:6px 18px;margin-top:12px;font-size:12px;color:var(--text)}
.mw .an .share-lg .lgd-i b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.mw .an .share-lg .lgd-i span.m{color:var(--muted)}

/* the machine table: model group rows, sortable headers, the 4K/1080P bar with the same-model tick */
.mw .an table.grid tr.grp th{background:#0c1626;color:var(--text);font-size:11px;padding:8px 12px;border-bottom:1px solid var(--line)}
.mw .an table.grid tr.grp th span{color:var(--muted);font-weight:400;margin-left:8px}
.mw .an table.grid tr.sel td{background:#22c55e0a}
.mw .an .th-btn{background:none;border:0;color:inherit;font:inherit;display:inline-flex;align-items:center;gap:4px;padding:2px 0;min-height:30px;letter-spacing:inherit;transition:color .15s}
.mw .an .th-btn:hover{color:var(--text)}
.mw .an .th-btn .icon{width:14px;height:14px;opacity:.5}
.mw .an th[aria-sort] .th-btn{color:var(--text)}
.mw .an th[aria-sort] .th-btn .icon{opacity:1}
.mw .an table.grid th .chip.tag{margin-left:5px;vertical-align:middle}
.mw .an .mname strong{display:block;font-weight:600}
.mw .an .mname span{font-size:11px;color:var(--muted)}
.mw .an .sbar-cell{display:flex;align-items:center;gap:8px;min-width:190px}
.mw .an .sbar-track{position:relative;flex:1;height:14px;display:flex;align-items:center}
.mw .an .sbar{display:flex;gap:2px;height:12px}
.mw .an .sbar span{display:block;height:100%;min-width:2px;transition:filter .15s}
.mw .an .sbar span:hover{filter:brightness(1.25)}
.mw .an .sbar span:last-child{border-radius:0 4px 4px 0}
.mw .an .avg-mk{position:absolute;top:-2px;width:2px;height:18px;margin-left:-1px;background:var(--text);border-radius:1px;opacity:.85}
.mw .an .sbar-cell b{font-weight:600;font-variant-numeric:tabular-nums;min-width:22px;text-align:right}
.mw .an .state-note{display:block;font-size:10.5px;color:var(--muted);margin-top:3px;white-space:nowrap}

/* 用电, 设备在线, 异常与报警, 片源与片库 (C5) */
.mw .an .lgd-ic{display:inline-flex}
.mw .an .lgd-ic .icon{width:14px;height:14px}
.mw .an .egrid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:22px 28px}
.mw .an .estats{display:grid;grid-template-columns:1fr;gap:10px;align-content:start}
.mw .an #an-sec-uptime .notice,.mw .an #an-sec-alerts .notice{margin-bottom:14px}
.mw .an table.grid tr.hl td{background:#fb71850b}
.mw .an table.grid tr.hl td:first-of-type{box-shadow:inset 3px 0 0 var(--st-red)}
.mw .an .more{margin-top:6px}
.mw .an .inbox{display:grid;grid-template-columns:minmax(0,1.3fr) repeat(3,minmax(0,1fr));gap:12px 20px;align-items:start;padding:14px 16px;border:1px solid #22324a;border-radius:10px;background:#0c1626}
.mw .an .inbox h3{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;font-weight:600}
.mw .an .inbox h3 .icon{width:15px;height:15px;color:var(--muted)}
.mw .an .meter{height:10px;border-radius:5px;background:var(--meter-track);overflow:hidden;margin:10px 0 6px;display:flex;gap:2px;cursor:default;outline:none}
.mw .an .meter:focus-visible{box-shadow:0 0 0 2px var(--blue)}
.mw .an .meter span{display:block;height:100%}
.mw .an .meter span:last-child{border-radius:0 5px 5px 0}
.mw .an .inbox .fig{font:600 18px/1.4 var(--sans)}
.mw .an .inbox .fig em{font:400 11px var(--sans);color:var(--muted);font-style:normal;margin-left:4px}
.mw .an .inbox .sub{font-size:11px;color:var(--muted)}
.mw .an .inbox .lgd{margin:4px 0 0}

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

@media(max-width:1250px){.mw .an .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.mw .an .inbox{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:1080px){.mw .an .grid2,.mw .an .egrid{grid-template-columns:1fr}.mw .an .ministats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.mw .an .filterbar{position:static}}
@media(max-width:640px){.mw .an .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mw .an .kpi{padding:12px 12px 8px}.mw .an .kv{font-size:24px}.mw .an .filterbar{gap:10px}.mw .an .ministats{gap:10px}.mw .an .inbox{grid-template-columns:1fr}}
`;
