// PWR-2/PWR-3 styles over the V3 media tokens (mediaStyles.ts), scoped to `.mw`. The values are the
// approved previews' (design-system/taskpaw-v3/preview/media-workflow-v3-power.html and power-v3.html).
export const POWER_CSS = `
.mw{--pw-gpu:#1c9a52;--pw-cpu:#86dca8;--pw-other:#5b6b85}
/* shared: the GPU / CPU / 其他 split and the 估算 tag */
.mw .pw-bar{display:flex;gap:2px;height:8px;border-radius:4px;overflow:hidden;background:#27364b;margin:5px 0 7px}
.mw .pw-bar span{display:block;height:100%;min-width:2px}
.mw .pw-bar .g{background:var(--pw-gpu)}.mw .pw-bar .c{background:var(--pw-cpu)}.mw .pw-bar .o{background:var(--pw-other)}
.mw .pw-legend{display:flex;flex-wrap:wrap;gap:2px 10px;font-size:10px;color:#c3d0e0;line-height:1.7}
.mw .pw-legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;vertical-align:0}
.mw .pw-legend i.g{background:var(--pw-gpu)}.mw .pw-legend i.c{background:var(--pw-cpu)}.mw .pw-legend i.o{background:var(--pw-other)}
.mw .pw-est{display:inline-flex;align-items:center;font:9px/1.5 var(--sans);letter-spacing:0;padding:0 5px;border:1px solid #3d536d;border-radius:4px;color:#c0d4e9;background:#1a2b414d;vertical-align:middle}
.mw .pw-chip{display:inline-flex;align-items:center;gap:3px;font-family:var(--mono);color:#c8d7e9}
.mw .pw-chip .icon{width:12px;height:12px;color:#8eabc2}
/* 概览: 集群功耗 */
.mw .stats.pw-stats{grid-template-columns:repeat(5,minmax(0,1fr))}
.mw .pw-stat .label{gap:6px}
.mw .pw-stat .label .pw-est{margin-right:auto}
.mw .pw-stat strong em{font:11px var(--sans)}
.mw .pw-stat .note{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}
.mw .pw-stat .note .link-btn{min-height:24px;font-size:10px;padding:0;gap:3px}
.mw .pw-stat .note .link-btn .icon{width:12px;height:12px}
/* drawer: the live row on a running GPU step and the 能耗 block */
.mw .pw-live{display:flex;justify-content:space-between;align-items:center;gap:8px 16px;flex-wrap:wrap;padding:12px 14px;margin:0 0 14px;border:1px solid #2e465e;border-radius:10px;background:#0b1726;font-size:11px;color:#c9d7e9}
.mw .pw-now{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.mw .pw-now .icon{width:14px;height:14px;color:#8eabc2}
.mw .pw-now strong{font-family:var(--mono);font-weight:500;color:var(--text)}
.mw .pw-now .pw-bar{width:84px;margin:0;height:6px}
.mw .energy-panel{padding:18px 20px}
.mw .energy-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.mw .energy-head h3{font-size:14px}
.mw .energy-head p{font-size:10px;color:var(--muted);margin-top:3px}
.mw .energy-total{text-align:right}
.mw .energy-total .lab{font-size:11px;color:var(--muted);margin-right:6px}
.mw .energy-total strong{font:500 24px/1.3 var(--mono);color:var(--green-light)}
.mw .energy-total .u{font-size:12px;color:var(--muted);margin-left:4px}
.mw .energy-total .sub{display:block;font-size:10px;color:var(--muted)}
.mw .energy-rows{list-style:none;margin:6px 0 0;padding:0}
.mw .energy-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 12px;padding:11px 0;border-bottom:1px solid #22324a}
.mw .energy-row .name{font-size:12px;color:#d8e5f6;display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px}
.mw .energy-row .name small{font-size:10px;color:var(--muted)}
.mw .energy-row .tag{font-size:9px;padding:1px 6px;border-radius:4px;border:1px solid #22c55e42;background:#22c55e0e;color:#97e8b4}
.mw .energy-row .tag.amber{border-color:#f59e0b42;background:#f59e0b0d;color:var(--amber)}
.mw .energy-row .kwh{font:12px var(--mono);text-align:right;color:var(--text)}
.mw .energy-row .sub{grid-column:1/-1;font-size:10px;color:var(--muted);display:flex;flex-wrap:wrap;gap:1px 12px}
.mw .energy-row .sub .mono{font-size:10px;color:#c3d0e0}
.mw .energy-row .share{grid-column:1/-1;height:4px;border-radius:2px;background:#27364b;overflow:hidden}
.mw .energy-row .share span{display:block;height:100%;background:var(--pw-gpu);border-radius:2px}
.mw .energy-row.todo .name,.mw .energy-row.todo .kwh{color:var(--muted)}
.mw .energy-tiers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px}
.mw .energy-tiers div{border:1px solid #2d405a;border-radius:8px;padding:9px 11px;background:#0d1728;font-size:10px;color:var(--muted)}
.mw .energy-tiers div.on{border-color:#48805d;background:#22c55e0d;color:#a0e8b9}
.mw .energy-tiers strong{display:block;font:500 15px/1.6 var(--mono);color:var(--text)}
.mw .energy-tiers div.on strong{color:#a0e8b9}
.mw .energy-tiers strong.none{font:12px/2 var(--sans);color:var(--muted)}
.mw .energy-note{font-size:10px;color:var(--muted);margin-top:12px;line-height:1.75}
/* 待处理 and the queue foot */
.mw .pw-predict{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.7}
.mw .pw-predict .t{white-space:nowrap}
.mw .pw-predict .t.on{color:var(--green-light);font-weight:600}
.mw .pw-predict .t.on::before{content:"▸";margin-right:2px}
.mw .card-foot .pw-foot{font-family:var(--mono);color:#c3d0e0}
.mw .card-foot .pw-foot strong{font-weight:500;color:var(--text)}

/* ===== 功耗 tab ===== */
.mw .pw-page{padding:27px;max-width:1650px;margin:0 auto;min-width:0}
.mw .pw-page .stat strong em{font-size:12px}
.mw .range{display:flex;gap:6px;flex-wrap:wrap}
.mw .split-note{font-size:10px;color:var(--muted)}
.mw .machines{list-style:none;margin:0;padding:0}
.mw .machine{display:grid;grid-template-columns:minmax(210px,1.25fr) minmax(190px,1.15fr) minmax(210px,1.1fr) 150px 92px;grid-template-areas:"id act pow spark today";gap:10px 22px;align-items:center;padding:16px 20px;border-bottom:1px solid #22324a99}
.mw .machine:last-child{border-bottom:0}
.mw .m-id{grid-area:id;display:flex;gap:12px;align-items:flex-start;min-width:0}
.mw .swatch{width:10px;height:10px;border-radius:3px;flex:none;margin-top:6px}
.mw .m-id h3{font-size:14px;font-weight:600;overflow-wrap:anywhere}
.mw .m-id .hw{font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.mw .m-id .chips{margin-top:6px}
.mw .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green)}
.mw .m-act{grid-area:act;min-width:0;font-size:12px;color:#d8e5f6}
.mw .m-act .k,.mw .m-today .k{display:block;font-size:10px;color:var(--muted)}
.mw .m-act .idle{color:var(--muted)}
.mw .m-pow{grid-area:pow;min-width:0}
.mw .m-pow .w{font:500 21px/1.3 var(--mono);letter-spacing:-.5px}
.mw .m-pow .w small{font:11px var(--sans);color:var(--muted);margin-left:5px;letter-spacing:0}
.mw .m-pow .none{font-size:12px;color:var(--muted)}
.mw .m-spark{grid-area:spark;min-width:0}
.mw .m-spark svg{display:block;width:100%;height:36px}
.mw .m-spark .k{display:flex;justify-content:space-between;font-size:10px;color:var(--muted)}
.mw .m-today{grid-area:today;text-align:right}
.mw .m-today strong{font:500 16px/1.5 var(--mono)}
.mw .m-today strong small{font:10px var(--sans);color:var(--muted);margin-left:3px}
.mw .m-sensors{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px;border-top:1px solid #34485b;padding-top:10px;margin-top:2px;min-width:0;font-size:12px;color:#b2c3d5}
.mw .m-sensors .sensor{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.mw .m-sensors .sensor b{font:500 15px/1.4 var(--mono);color:#eaf2fc;font-variant-numeric:tabular-nums}
.mw .m-sensors .sensor b small{font:10px var(--sans);color:#b2c3d5;margin-left:2px}
.mw .m-sensors .fans{display:inline-flex;align-items:baseline;gap:6px 10px;flex-wrap:wrap}
.mw .m-sensors .fan{display:inline-flex;align-items:baseline;gap:4px;white-space:nowrap}
.mw .m-sensors .fan b{font:500 13px/1.4 var(--mono);color:#eaf2fc;font-variant-numeric:tabular-nums}
.mw .m-sensors .fan small{font-size:10px;color:var(--muted)}
.mw .m-sensors .meta{font-size:10px;color:var(--muted);margin-left:auto;white-space:nowrap}
.mw .m-sensors .sep{color:#34485b}
.mw .m-sensors.stale .sensor b,.mw .m-sensors.stale .fan b,.mw .m-sensors.stale .meta{color:#f0c57a}
.mw .m-sensors .unavailable{color:var(--muted)}
.mw .m-sensors .unavailable b{color:#b2c3d5;font:500 12px var(--sans)}
.mw .section-note{font-size:10px;color:var(--muted);padding:12px 20px 16px;line-height:1.75}
.mw .legend{display:flex;gap:6px;flex-wrap:wrap;padding:14px 20px 0}
.mw .legend button{min-height:44px;display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid #33455d;border-radius:7px;background:transparent;color:#c3d0e0;font-size:11px;transition:background .16s,border-color .16s,opacity .16s}
.mw .legend button i{width:10px;height:10px;border-radius:3px}
.mw .legend button[aria-pressed=false]{opacity:.62;color:var(--muted);border-style:dashed}
.mw .legend button[aria-pressed=false] i{background:transparent!important;border:1.5px solid currentColor}
.mw .chart-wrap{position:relative;padding:8px 12px 4px 8px;min-width:0}
.mw .chart{position:relative;border-radius:8px;outline-offset:2px}
.mw .chart svg{display:block;width:100%}
.mw .chart text{fill:#8fa1b8;font:10px var(--mono)}
.mw .chart-tip{position:absolute;top:10px;pointer-events:none;min-width:178px;padding:10px 12px;background:#101a2cf2;border:1px solid #3b506c;border-radius:9px;box-shadow:0 10px 30px #0009;font-size:11px;color:#d8e5f6;z-index:3}
.mw .chart-tip .t{font:10px var(--mono);color:var(--muted);margin-bottom:5px}
.mw .chart-tip .r{display:flex;justify-content:space-between;gap:14px;line-height:1.75}
.mw .chart-tip .r i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.mw .chart-tip .r b{font:500 11px var(--mono)}
.mw .chart-tip .tot{border-top:1px solid var(--line);margin-top:4px;padding-top:4px}
.mw .chart-foot{display:flex;justify-content:space-between;gap:8px 16px;flex-wrap:wrap;padding:6px 20px 16px;font-size:10px;color:var(--muted)}
.mw .pw-two-col{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(300px,1fr);gap:20px;align-items:start}
.mw .scroller{overflow-x:auto;min-width:0;-webkit-overflow-scrolling:touch}
.mw table.pw-table{width:100%;border-collapse:collapse;font-size:12px}
.mw .pw-table th,.mw .pw-table td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
.mw .pw-table th{font-size:11px;color:var(--muted);font-weight:500}
.mw .pw-table td.num,.mw .pw-table th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.mw .pw-table th.num{font-family:var(--sans)}
.mw .pw-table .sel{background:#22c55e0a}
.mw .pw-table th.sel{color:#a0e8b9}
.mw .pw-table tfoot td{color:var(--text);font-weight:500;border-bottom:0}
.mw .pw-table tbody tr:last-child td,.mw .pw-table tbody tr:last-child th{border-bottom-color:#344861}
.mw .pw-table th[scope=row]{font-weight:400;color:#d8e5f6}
.mw .ename{display:flex;align-items:center;gap:8px}
.mw .ename .swatch{margin:0}
.mw .share{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.mw .share .bar,.mw .kwh-cell .bar{width:70px;height:5px;border-radius:3px;background:#27364b;overflow:hidden}
.mw .share .bar span,.mw .kwh-cell .bar span,.mw .use .bar span{display:block;height:100%;background:linear-gradient(90deg,#169a4c,#39d373);border-radius:3px}
.mw .kwh-cell{display:flex;align-items:center;justify-content:flex-end;gap:8px}
.mw .kwh-cell .bar{width:56px}
.mw .uses{list-style:none;margin:0;padding:6px 20px 10px}
.mw .use{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;padding:12px 0;border-bottom:1px solid #22324a}
.mw .use:last-child{border-bottom:0}
.mw .use .n{font-size:12px;color:#d8e5f6}
.mw .use .n small{display:block;font-size:10px;color:var(--muted)}
.mw .use .v{text-align:right;font:12px var(--mono)}
.mw .use .v small{display:block;font:10px var(--mono);color:var(--muted)}
.mw .use .bar{grid-column:1/-1;height:6px;border-radius:3px;background:#27364b;overflow:hidden}
.mw .hist{display:flex;gap:8px;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid var(--line);font-size:11px;color:var(--muted);align-items:center}
.mw .hist .chip .mono{font-size:10px;color:var(--text)}
.mw .pw-film{display:flex;flex-direction:column}
.mw .pw-film .code{font:12px var(--mono);color:#d8e5f6}
.mw .pw-film .res{font-size:10px;color:var(--muted)}
/* 估算参数 dialog */
.mw .param{border:1px solid #2d405a;border-radius:10px;padding:14px 16px 16px;margin:0 0 12px;background:#0c1626;min-width:0}
.mw .param legend{padding:0 6px;font-size:13px;font-weight:600;display:flex;align-items:center;gap:4px 8px;flex-wrap:wrap}
.mw .param legend small{font-size:10px;font-weight:400;color:var(--muted)}
.mw .param legend .swatch{margin:0}
.mw .param-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.mw .field{display:flex;flex-direction:column;gap:5px;min-width:0}
.mw .field label{font-size:11px;color:#c9d6e7}
.mw .field .hint{font-size:10px;color:var(--muted);line-height:1.5}
.mw .field .input,.mw .field select{width:100%}
.mw .unit-input{position:relative}
.mw .unit-input .input{padding-right:34px;width:100%}
.mw .unit-input span{position:absolute;right:12px;top:50%;transform:translateY(-50%);font-size:11px;color:var(--muted);pointer-events:none}
.mw .param .src{font-size:10px;color:var(--muted);margin-top:10px}
.mw .param.spacer-top{margin-top:16px}
.mw .calib{border:1px solid #345c47;border-radius:10px;padding:14px 16px;background:#0b1727;margin-top:16px}
.mw .calib h3{font-size:13px;margin-bottom:4px}
.mw .calib p{font-size:11px;color:var(--muted);line-height:1.7}
.mw .calib-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) auto;gap:12px;align-items:end;margin-top:12px}
.mw .calib-out{margin-top:10px;font-size:12px;color:#a7e8bc;min-height:20px}
.mw .peer-list{list-style:none;margin:10px 0 0;padding:0}
.mw .peer-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr) auto;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid #22324a;font-size:12px}
.mw .peer-row .mono{font-size:11px;color:#c3d0e0;overflow-wrap:anywhere}
.mw .peer-add{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr) auto;gap:10px;align-items:end;margin-top:12px}
.mw .form-error{font-size:11px;color:var(--red);margin-top:8px}

@media(min-width:1600px){.mw .pw-page{padding:36px}}
@media(max-width:1300px){.mw .machine{grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) 140px;grid-template-areas:"id act today" "pow spark spark";row-gap:12px}.mw .m-spark svg{height:40px}}
@media(max-width:1200px){.mw .stats.pw-stats{grid-template-columns:repeat(4,minmax(0,1fr))}.mw .stats.pw-stats .pw-stat{grid-column:1/-1;display:grid;grid-template-columns:minmax(160px,auto) minmax(0,1fr);column-gap:28px;align-items:center}.mw .stats.pw-stats .pw-stat .label{grid-column:1/-1}.mw .pw-stat .pw-split{min-width:0}
  .mw .pw-two-col{grid-template-columns:1fr}.mw .param-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.mw .pw-page{padding:24px}.mw .machine{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"id today" "act act" "pow pow" "spark spark"}}
@media(max-width:600px){.mw .stats.pw-stats{grid-template-columns:1fr 1fr}.mw .stats.pw-stats .pw-stat{display:block}.mw .energy-panel{padding:15px}.mw .energy-total{text-align:left}.mw .energy-tiers{gap:6px}.mw .energy-tiers div{padding:8px}.mw .energy-tiers strong{font-size:13px}.mw .pw-now .pw-bar{width:64px}
  .mw .m-sensors .meta{margin-left:0;flex-basis:100%}
  .mw .pw-page{padding:22px 15px}.mw .pw-page .page-title .actions{width:100%}.mw .range{flex:1}.mw .range .filter-pill{flex:1;padding:7px 8px}
  .mw .pw-page .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mw .pw-page .stat{padding:14px}.mw .pw-page .stat strong{font-size:24px}.mw .pw-page .stat strong em{margin-left:5px;font-size:11px}
  .mw .pw-page .panel-head{padding:15px}.mw .pw-page .panel-head h2{font-size:15px}.mw .machine{padding:14px 15px;gap:8px 14px}.mw .legend{padding:12px 15px 0}.mw .chart-wrap{padding:6px 6px 2px 2px}.mw .chart-foot{padding:6px 15px 14px}.mw .uses{padding:4px 15px 8px}.mw .hist{padding:12px 15px}.mw .section-note{padding:12px 15px 14px}
  .mw .pw-table th,.mw .pw-table td{padding:10px 10px;font-size:11px}
  .mw .param{padding:12px}.mw .param legend small{flex-basis:100%}.mw .param-grid{grid-template-columns:1fr 1fr;gap:10px}.mw .calib-grid,.mw .peer-add{grid-template-columns:1fr 1fr}.mw .calib-grid .btn,.mw .peer-add .btn{grid-column:1/-1}.mw .peer-row{grid-template-columns:minmax(0,1fr) auto}.mw .peer-row .mono{grid-column:1/2}}
@media(forced-colors:active){.mw .pw-bar span,.mw .share .bar span,.mw .use .bar span{forced-color-adjust:none}}
`;
