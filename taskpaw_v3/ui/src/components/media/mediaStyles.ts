// 媒体处理中心 V3 tokens and styles (build spec §2), scoped to `.mw` so the global TaskPaw theme
// stays untouched. Dialogs and the drawer render in portals and carry the `mw` class themselves.
// The values are the prototype's (design-system/taskpaw-v3/preview/media-workflow-v3*.html).

export const mediaTokens = {
  bg: "#0B1120",
  surface: "#111A2E",
  low: "#0D1626",
  line: "#263650",
  text: "#F8FAFC",
  muted: "#A8B5C7",
  green: "#22C55E",
  greenLight: "#97E8B4",
  blue: "#75D2FA",
  amber: "#FBC76A",
  red: "#FDA4AF",
  onGreen: "#06210F",
  sans: '"Fira Sans","Noto Sans SC",system-ui,"Microsoft YaHei",sans-serif',
  mono: '"Fira Code",Consolas,"Microsoft YaHei",monospace',
} as const;

const T = mediaTokens;

export const MEDIA_CSS = `
.mw{--bg:${T.bg};--surface:${T.surface};--low:${T.low};--line:${T.line};--text:${T.text};--muted:${T.muted};
--green:${T.green};--green-light:${T.greenLight};--blue:${T.blue};--amber:${T.amber};--red:${T.red};--on-green:${T.onGreen};
--sans:${T.sans};--mono:${T.mono};color:var(--text);font:14px/1.6 var(--sans);color-scheme:dark}
.mw *,.mw *::before,.mw *::after{box-sizing:border-box}
.mw button,.mw input,.mw select{font:inherit}
.mw button{cursor:pointer;color:inherit}
.mw button:disabled{cursor:not-allowed;opacity:.52}
.mw button,.mw a,.mw input,.mw select,.mw summary{outline-offset:4px;-webkit-tap-highlight-color:transparent}
.mw button:focus-visible,.mw a:focus-visible,.mw input:focus-visible,.mw select:focus-visible,.mw summary:focus-visible,.mw [tabindex]:focus-visible{outline:2px solid var(--blue)}
.mw h1,.mw h2,.mw h3,.mw p{margin:0}
.mw h2{font-size:17px;font-weight:600}
.mw h3{font-size:15px;font-weight:600}
.mw .muted{color:var(--muted)}
.mw .small{font-size:12px}
.mw .mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.mw .tabular{font-variant-numeric:tabular-nums}
.mw .t-green{color:var(--green-light)}.mw .t-amber{color:var(--amber)}.mw .t-blue{color:var(--blue)}.mw .t-red{color:var(--red)}
.mw .t-restore{color:#91bdff}
.mw .wrap{overflow-wrap:anywhere}
.mw .icon{width:19px;height:19px;flex:none;vertical-align:middle}
.mw .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.mw .spacer-top{margin-top:20px}
.mw .skip-link{position:absolute;top:-100px;left:12px;z-index:20;background:var(--green);color:var(--on-green);padding:12px}
.mw .skip-link:focus{top:12px}

/* shell */
.mw-root{position:relative;background:radial-gradient(1000px 650px at 80% -200px,#38bdf80b,transparent),var(--bg);border:1px solid var(--line);border-radius:12px;overflow:hidden;min-width:0}
.mw .scope-banner{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 25px;background:#22c55e06;border-bottom:1px solid #223c33;font-size:11px;color:#b5cdbf;flex-wrap:wrap}
.mw .scope-banner .btn{min-height:36px;font-size:10px;padding:5px 9px}
.mw .scope-left{display:flex;align-items:center;gap:10px;min-width:0}
.mw .health{display:flex;gap:6px;flex-wrap:wrap}
.mw .workspace-shell{display:grid;grid-template-columns:205px minmax(0,1fr);min-height:calc(100dvh - 190px)}
.mw .workspace-rail{padding:22px 13px;background:#0e1727;border-right:1px solid var(--line)}
.mw .workspace-heading{padding:0 13px 18px;margin-bottom:11px;border-bottom:1px solid var(--line)}
.mw .workspace-heading strong{display:block;font-size:14px}
.mw .nav-group{display:flex;flex-direction:column;gap:5px}
.mw .nav-item{min-height:46px;display:flex;align-items:center;gap:12px;color:var(--muted);padding:10px 13px;border:1px solid transparent;border-radius:8px;font-size:13px;background:transparent;text-align:left;width:100%;transition:background .16s,color .16s}
.mw .nav-item:hover{background:#ffffff05;color:var(--text)}
.mw .nav-item[aria-current=page]{background:#22c55e10;border-color:#22c55e36;color:#9be7b5}
.mw .nav-item .icon{width:19px;height:19px}
.mw .nav-count{margin-left:auto;min-width:22px;text-align:center;background:#263650;border-radius:5px;font:11px/20px var(--mono);color:#cedbea;padding:0 4px}
.mw .nav-item[aria-current=page] .nav-count{background:#22c55e19;color:#a8edbe}
.mw .nav-later{margin-left:auto;font-size:9px;line-height:1.6;padding:1px 6px;border:1px solid #344861;border-radius:4px;color:#97a9bf}
.mw .content{padding:27px;max-width:1650px;width:100%;margin:0 auto;min-width:0}
.mw .content:focus{outline:none}
.mw .mobile-menu{display:none}

/* page title row (operator decision 7: no slogan headings) */
.mw .page-title{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px;flex-wrap:wrap}
.mw .page-title h1{font-size:20px;line-height:1.4;font-weight:600;letter-spacing:-.2px}
.mw .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mw .page-title-main{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;min-width:0}
.mw .page-sub{font-size:12px;color:var(--muted)}

/* buttons */
.mw .btn{min-height:44px;padding:9px 14px;display:inline-flex;justify-content:center;align-items:center;gap:8px;border:1px solid #344861;border-radius:8px;background:#17243a;color:#e1eaf5;font-size:12px;font-weight:500;line-height:1.5;transition:background .16s,border-color .16s;text-decoration:none}
.mw .btn:hover:not(:disabled){background:#20324b;border-color:#4c6584}
.mw .btn.primary{background:var(--green);color:var(--on-green);border-color:var(--green);font-weight:650}
.mw .btn.primary:hover:not(:disabled){background:#4bd37e}
.mw .btn.ghost{background:transparent}
.mw .btn.danger{color:#fecdd3;border-color:#8c4052;background:#4c213233}
.mw .btn.small{font-size:11px;padding:8px 11px}
.mw .icon-btn{width:44px;height:44px;padding:0;flex:none}
.mw .link-btn{min-height:44px;padding:6px 3px;border:0;background:none;display:inline-flex;align-items:center;gap:6px;color:var(--blue);font-size:12px}

/* chips */
.mw .chip{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border:1px solid #344861;border-radius:5px;background:#1b2940;color:#c8d7e9;font-size:10px;line-height:1.6;white-space:nowrap}
.mw .chip.green{background:#22c55e0e;border-color:#22c55e42;color:var(--green-light)}
.mw .chip.blue{background:#38bdf80c;border-color:#38bdf83d;color:var(--blue)}
.mw .chip.amber{background:#f59e0b0d;border-color:#f59e0b42;color:var(--amber)}
.mw .chip.red{background:#fb71850d;border-color:#fb718548;color:var(--red)}
.mw .chips{display:flex;gap:6px;flex-wrap:wrap}

/* panels, notices */
.mw .panel{border:1px solid var(--line);border-radius:12px;background:linear-gradient(170deg,var(--surface),var(--low));min-width:0;overflow:hidden}
.mw .panel-head{padding:18px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px;border-bottom:1px solid var(--line)}
.mw .panel-head p{font-size:11px;color:var(--muted);margin-top:3px}
.mw .panel-body{padding:20px}
.mw .notice{padding:13px 16px;border:1px solid #3d536d;background:#1a2b414d;border-radius:8px;color:#c0d4e9;font-size:12px;display:flex;gap:10px;align-items:flex-start;margin-bottom:20px}
.mw .notice .icon{margin-top:1px}
.mw .notice.amber{color:#f9d492;border-color:#6a5229;background:#60451c20}
.mw .notice.green{color:#a7e9bc;border-color:#315641;background:#1b542420}
.mw .notice.red{color:#fec0c9;border-color:#6e3544;background:#59233120}
.mw .notice p+p{margin-top:5px}
.mw .empty{padding:60px 24px;text-align:center;color:var(--muted)}
.mw .empty .icon{width:36px;height:36px;color:#7fb69b;margin-bottom:14px}
.mw .empty h2{color:var(--text);margin-bottom:7px}

/* overview */
.mw .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:22px}
.mw .stat{padding:18px 20px;position:relative;min-width:0}
.mw .stat .label{font-size:11px;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
.mw .stat .label .icon{color:#8eabc2;width:16px}
.mw .stat strong{display:block;font:500 31px/1.6 var(--mono);letter-spacing:-1px}
.mw .stat strong em{font:12px var(--sans);color:var(--muted);margin-left:8px;font-style:normal;letter-spacing:0}
.mw .stat .note{font-size:10px;color:var(--muted)}
.mw .two-col{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(285px,1fr);gap:20px;align-items:start}
.mw .stack{display:flex;flex-direction:column;gap:20px;min-width:0}
.mw .flow-strip{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:22px}
.mw .flow-stage{background:#101a2c;min-height:84px;border:0;padding:12px 7px;text-align:center;position:relative}
.mw .flow-stage:hover{background:#1b2a40}
.mw .flow-stage span{display:block;font-size:10px;color:var(--muted)}
.mw .flow-stage b{display:block;font:500 22px/1.6 var(--mono);color:#d9e8f6}
.mw .flow-stage.attention b{color:var(--amber)}
.mw .film-list{display:flex;flex-direction:column}
.mw .film-row{padding:17px 20px;border-bottom:1px solid var(--line);display:flex;gap:15px;align-items:center;min-width:0}
.mw .film-row:last-child{border-bottom:0}
.mw .queue-summary{width:100%;min-height:44px;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px;color:var(--muted);font-size:11px;text-align:left;background:none;border:0;border-top:1px solid var(--line)}
.mw .queue-summary:hover{color:var(--text)}
.mw .steps-list{display:flex;gap:8px;flex-wrap:wrap}
.mw .step{border:1px solid #33465f;padding:7px 10px;border-radius:6px;font-size:10px;color:var(--muted)}
.mw .review-peek{padding:18px 20px;border-bottom:1px solid var(--line)}
.mw .review-peek:last-child{border:0}
.mw .review-peek .top{display:flex;gap:13px;align-items:center}
.mw .review-peek p{font-size:11px;color:var(--muted);margin:4px 0}
.mw .review-peek .actions{margin-top:12px}
.mw .device-mini{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 20px}
.mw .device-mini .name{font-size:12px}
.mw .device-mini .desc{font-size:10px;color:var(--muted);margin-top:2px}
.mw .device-icon{width:32px;height:32px;border:1px solid #324862;border-radius:8px;display:grid;place-items:center;color:#91b2ce;flex:none}
.mw .device-meta{display:flex;gap:11px;align-items:center;min-width:0}

/* covers */
.mw .cover{width:60px;height:80px;flex:none;border-radius:7px;overflow:hidden;background:#1c2b43;position:relative;display:grid;place-items:center;color:#8fa3bb;font-size:10px;border:1px solid #2b3d57}
.mw .cover img{width:100%;height:100%;object-fit:cover;display:block}
.mw .cover.card{width:61px;height:82px}
.mw .cover.peek{width:48px;height:64px}
.mw .cover.review{width:88px;height:117px;border-radius:8px}
.mw .cover.large{width:116px;height:154px;border-radius:9px}
.mw .cover.confirm{width:108px;height:144px;border-radius:9px}
/* U1b: a landscape jacket's front cover is its right part; the cover opens the lightbox */
.mw .cover img.landscape{object-position:right center}
.mw .cover-btn{padding:0;font:inherit;cursor:zoom-in;appearance:none;transition:border-color .15s}
.mw .cover-btn:hover{border-color:var(--blue)}
.mw .cover-btn:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.mw-lightbox .lightbox-close{position:fixed;top:12px;right:12px;width:44px;height:44px;display:grid;place-items:center;border-radius:50%;border:1px solid #ffffff40;background:#0b1120cc;color:#f8fafc;cursor:pointer}
.mw-lightbox .lightbox-close:hover{background:#1c2b43}
.mw-lightbox .lightbox-close:focus-visible{outline:2px solid ${T.blue};outline-offset:2px}

/* film body, stage block */
.mw .film-body{flex:1;min-width:0}
.mw .film-title{display:flex;gap:8px;align-items:center;flex-wrap:wrap;min-width:0}
.mw .film-title h3{font-size:14px;overflow-wrap:anywhere}
.mw .film-body .meta{font-size:10px;color:var(--muted);margin-top:4px;display:flex;gap:10px;flex-wrap:wrap}
.mw .film-stage{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-top:10px;font-size:11px}
.mw .film-stage .mono{font-size:12px}
.mw .progress{height:5px;background:#27364b;border-radius:5px;overflow:hidden;margin-top:7px}
.mw .progress span{height:100%;background:#91bdff;display:block;border-radius:5px}
.mw .stage-detail{display:flex;justify-content:space-between;gap:10px;font-size:10px;color:var(--muted);margin-top:5px;flex-wrap:wrap}
.mw .step-line{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;row-gap:2px;font-size:10px;line-height:1.8;color:#8395ab}
.mw .step-line li+li::before{content:"›";margin:0 5px;color:#4f627b}
.mw .step-line li.done{color:#b9c8da}
.mw .step-line li.current{color:#91bdff;font-weight:600}
.mw .step-line li.current[data-step=asr]{color:#c7a7ff}.mw .step-line li.current[data-step=translate]{color:#f6aad3}
.mw .step-line li.current[data-step=return]{color:#7edce9}.mw .step-line li.current[data-step=scrape]{color:#b0b7ff}
.mw .step-line li.current[data-step=archive]{color:#70dfc6}.mw .step-line li.current[data-step=cleanup]{color:#efb88b}
.mw .step-line li.current[data-state=waiting],.mw .step-line li.current[data-state=pending]{color:#b6c3d4}
.mw .step-line li.current[data-state=done]{color:#99e5a3}
.mw .step-line li.failed{color:var(--red);font-weight:600}
.mw .step-line li{display:flex;align-items:flex-start}
.mw .step-line .step-label{display:inline-flex;flex-direction:column;align-items:center}
.mw .step-line .step-time{font-size:9px;line-height:1.3;color:var(--muted);font-weight:400;font-variant-numeric:tabular-nums}
.mw .timing-panel{padding:18px 20px}
.mw .timing-panel h3{margin:0;font-size:14px}
.mw .timing-sub{display:block;margin-top:2px}
.mw .film-card .run-total{color:var(--text)}
.mw .detail-steps{margin:-6px 0 22px;padding:12px 14px;border:1px solid #2d405a;border-radius:8px;background:#0c1729}

/* toolbar, filters, inputs */
.mw .toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.mw .filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mw .input,.mw select{background:#0d1728;border:1px solid #3b4d66;border-radius:8px;min-height:44px;color:#e0e9f5;padding:9px 12px;max-width:100%;font-size:12px}
.mw .input::placeholder{color:#99abc1}
.mw .search-wrap{position:relative}
.mw .search-wrap .icon{position:absolute;left:12px;top:13px;width:17px;height:17px;color:var(--muted)}
.mw .search{width:245px;padding-left:38px}
.mw .filter-pill{min-height:44px;padding:7px 13px;border:1px solid #33455d;border-radius:7px;background:transparent;color:var(--muted);font-size:11px}
.mw .filter-pill[aria-pressed=true]{border-color:#48805d;background:#22c55e0d;color:#a0e8b9}

/* queue */
.mw .queue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.mw .queue-worker p{margin-top:9px;overflow-wrap:anywhere}.mw .queue-worker-title{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.mw .queue-machine{display:flex;flex-direction:column;align-items:flex-start;gap:5px}
.mw .dispatch-table{table-layout:fixed}.mw .dispatch-table th:nth-child(1){width:25%}.mw .dispatch-table th:nth-child(2){width:17%}
.mw .dispatch-table th:nth-child(3){width:27%}.mw .dispatch-table td{padding:12px 8px;font-size:12px}
.mw .film-card{padding:17px;display:flex;flex-direction:column;gap:15px}
.mw .film-card .top{display:flex;gap:13px}
.mw .film-card .card-foot{border-top:1px solid var(--line);padding-top:12px;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted);margin-top:auto}
.mw .film-card .film-stage{margin-top:0}

/* review, pending */
.mw .review-grid,.mw .pending-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.mw .review-card{display:flex;gap:18px;padding:20px}
.mw .review-card .film-body{display:flex;flex-direction:column;gap:7px}
.mw .review-card p{font-size:11px;color:var(--muted)}
.mw .review-card .actions{margin-top:4px}
.mw .keyline{display:flex;gap:8px;align-items:center;font-size:11px;color:#c9d7e9}
.mw .keyline .icon{width:15px;height:15px}
.mw .pending-card{display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-areas:"cover info" "cover plan" "foot foot";grid-template-rows:auto 1fr auto;gap:14px 18px;align-items:start}
.mw .pending-card>.cover{grid-area:cover}
.mw .pending-card>.film-body{grid-area:info}
.mw .pending-card>.plan{grid-area:plan}
.mw .pending-card>.pending-foot{grid-area:foot}
.mw .pending-card .notice{margin:3px 0 0;padding:10px 12px;font-size:11px}
.mw .plan{display:flex;flex-direction:column;gap:14px;padding:14px;border:1px solid #2d405a;border-radius:9px;background:#0c1729;min-width:0}
.mw .plan-field{min-width:0}
.mw .plan-label{display:block;font-size:12px;color:#c9d6e7;margin-bottom:7px}
.mw .plan-field select{width:100%}
.mw .review-card .plan-reason,.mw .plan-reason{font-size:11px;color:var(--muted);margin-top:6px}
.mw .seg{display:flex;flex-wrap:wrap;gap:4px;padding:3px;border:1px solid #33455d;border-radius:9px;background:#0d1728}
.mw .seg-opt{flex:1 1 72px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:6px 10px;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--muted);font-size:12px;line-height:1.4;transition:background .16s,border-color .16s,color .16s}
.mw .seg-opt:hover{background:#ffffff08;color:var(--text)}
.mw .seg-opt:focus-visible{outline-offset:2px}
.mw .seg-opt[aria-checked=true]{border-color:#48805d;background:#22c55e14;color:#a0e8b9;font-weight:500}
.mw .rec-tag{font-size:9px;line-height:1.5;padding:1px 5px;border-radius:4px;border:1px solid #22c55e42;background:#22c55e0e;color:var(--green-light);font-weight:500;letter-spacing:.3px}
.mw .pending-foot{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px}

/* exceptions */
.mw .exception-card{padding:20px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:center;margin-bottom:14px}
.mw .exception-card .summary{display:flex;gap:14px;min-width:0}
.mw .exception-symbol{width:39px;height:39px;border-radius:10px;background:#f59e0b12;border:1px solid #f59e0b40;color:var(--amber);display:grid;place-items:center;flex:none}
.mw .exception-card p{font-size:12px;color:var(--muted);margin:6px 0}

/* local, later pages */
.mw .device-card{padding:21px}
.mw .device-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:20px}
.mw .device-name{display:flex;gap:12px}
.mw .device-name .device-icon{width:43px;height:43px}
.mw .device-name p{color:var(--muted);font-size:11px;margin-top:4px}
.mw .device-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:18px}
.mw .device-list .facts{margin-top:4px}
.mw .field{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
.mw .field .input{width:100%;font-size:13px}
.mw .code-box{padding:14px;border:1px solid #3b4d66;border-radius:8px;background:#0d1728;font-size:13px;overflow-wrap:anywhere;user-select:all}
.mw .switch-row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.mw .switch{display:inline-flex;align-items:center;gap:10px;min-height:44px;font-size:13px;cursor:pointer}
.mw .switch input{width:20px;height:20px;accent-color:var(--green)}
.mw .probe-table .changed td:first-of-type{color:var(--amber)}
.mw .probe-table .chip{margin-left:8px}
.mw .metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.mw .metric-grid label{font-size:10px;color:var(--muted);display:block}
.mw .metric-grid strong{display:block;font:500 20px/1.8 var(--mono);white-space:nowrap}
.mw .metric-grid .text-value{font-family:var(--sans);font-size:13px;line-height:2.7}
.mw .divider{height:1px;background:var(--line);margin:22px 0}
.mw .facts{display:grid;grid-template-columns:125px minmax(0,1fr);gap:11px 16px;font-size:12px;margin:0}
.mw .facts dt{color:var(--muted)}
.mw .facts dd{margin:0;overflow-wrap:anywhere}
.mw .facts dd.amber,.mw .small.amber{color:var(--amber)}
.mw .facts dd.green{color:var(--green-light)}

/* P2-4 (#295): multi-machine — 设备 rows, the drawer's 设备 line, 设备失联 rows */
.mw .device-state{display:flex;flex-direction:column;align-items:flex-end;gap:6px;min-width:0}
.mw .device-state p{text-align:right}
.mw .tier-chips{align-items:center}
.mw .device-assignments{margin-top:16px;padding:14px;border:1px solid #6e3544;border-radius:9px;background:#59233114}
.mw .device-assignments h4,.mw .device-more h4{margin:0 0 8px;font-size:12px;font-weight:600;color:#c9d6e7}
.mw .device-assignments ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.mw .device-assignments li{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;font-size:12px}
.mw .device-more{margin-top:16px;padding-top:16px;border-top:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:20px}
.mw .eligibility-table td,.mw .eligibility-table th{text-align:center}
.mw .eligibility-table th[scope=row]{text-align:left}
.mw .device-line{padding:16px 18px}
.mw .device-line-name{font-weight:600}
.mw .device-line-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}
.mw .device-line-action{display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap;min-width:0}
.mw .machine-name{color:#c9d6e7}
.mw .lost-row .t-red{margin-top:4px}

/* #361 片库与规则 and 设置 · 片库 (preview media-library-v1.html) */
.mw .tiny{font-size:11px}
.mw .subtabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:20px;overflow-x:auto}
.mw .subtab{min-height:44px;padding:8px 16px;border:0;border-bottom:2px solid transparent;background:none;color:var(--muted);font-size:13px;display:inline-flex;gap:8px;align-items:center;white-space:nowrap;margin-bottom:-1px}
.mw .subtab:hover{color:var(--text)}
.mw .subtab[aria-selected=true]{color:#a7ecbf;border-bottom-color:var(--green)}
.mw .stack>.stats,.mw .stack>.toolbar{margin-bottom:0}
.mw .lib-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.mw .lib-card{padding:20px;display:flex;flex-direction:column;gap:16px}
.mw .lib-card.offline{border-color:#5b3442}
.mw .lib-card .notice{margin-bottom:0}
.mw .lib-name{display:flex;gap:12px;min-width:0}
.mw .lib-icon{width:42px;height:42px;border:1px solid #324862;border-radius:9px;display:grid;place-items:center;color:#91b2ce;flex:none;font:600 14px var(--mono)}
.mw .lib-card.offline .lib-icon{color:var(--red);border-color:#6e3544}
.mw .path{font:12px/1.6 var(--mono);color:#b9cbe0;overflow-wrap:anywhere}
.mw .path.tiny{font-size:11px}
.mw .lib-card .facts{grid-template-columns:96px minmax(0,1fr);gap:9px 14px}
.mw .lib-foot{display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px;margin-top:auto}
.mw .scan-line{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:6px}
.mw .progress.indeterminate span{width:35%;animation:mw-scan 1.2s ease-in-out infinite}
@keyframes mw-scan{from{transform:translateX(-100%)}to{transform:translateX(290%)}}
.mw .add-card{border:1px dashed #3b4f6a;border-radius:12px;background:transparent;min-height:240px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--muted);font-size:13px;transition:border-color .16s,color .16s}
.mw .add-card:hover{border-color:#4c8a63;color:var(--green-light)}
.mw .add-card .icon{width:28px;height:28px}
.mw .empty .actions{justify-content:center;margin-top:16px}
.mw .filter-pill .n{font-family:var(--mono);margin-left:4px;opacity:.8}
.mw .table-wrap{overflow-x:auto}
.mw table.grid{width:100%;border-collapse:collapse;font-size:12px;min-width:720px}
.mw table.grid th{font-weight:500;color:var(--muted);text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.3px;white-space:nowrap}
.mw table.grid td{padding:12px 14px;border-bottom:1px solid #26365080;vertical-align:middle}
.mw table.grid tbody tr[tabindex]{cursor:pointer;transition:background .14s}
.mw table.grid tbody tr[tabindex]:hover{background:#ffffff05}
.mw table.grid tbody tr:focus-visible{outline:2px solid var(--blue);outline-offset:-2px}
.mw table.grid tr:last-child td{border-bottom:0}
.mw .who{display:flex;align-items:center;gap:12px;min-width:0}
.mw .avatar{width:54px;height:72px;border-radius:7px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;flex:none;overflow:hidden;border:1px solid #ffffff1a;text-align:center;background:#1c2b43}
.mw .avatar img{width:100%;height:100%;object-fit:cover;display:block}
.mw .avatar.lg{width:144px;height:192px;border-radius:12px}
.mw .avatar.none{color:#8fa3bb;border:1px dashed #3b4f6a}
.mw .avatar span{display:none;font:500 11px/1.4 var(--sans);padding:0 8px}
.mw .avatar.lg span{display:block}
.mw .aliases{font-size:11px;color:var(--muted);margin-top:2px}
.mw .loc-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:5px;font-size:11px;border:1px solid #344861;background:#16233a;color:#c8d7e9;white-space:nowrap}
.mw .loc-chip b{font:600 11px var(--mono);color:#e6eef8}
.mw .loc-chip.off{border-color:#6e354480;color:#d8a9b3}
.mw .loc-chip.miss{border-style:dashed;color:var(--amber);border-color:#6a5229}
.mw .pager{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);gap:12px;flex-wrap:wrap}
.mw .rules-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:18px;align-items:start}
.mw .rule-row{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid #26365099}
.mw .rule-row:last-child{border-bottom:0;padding-bottom:0}
.mw .rule-row:first-child{padding-top:0}
.mw .rule-row h3{font-size:13px;font-weight:500}
.mw .rule-row p{font-size:11px;color:var(--muted);margin-top:3px}
.mw .toggle{position:relative;width:44px;height:26px;flex:none;border-radius:13px;border:1px solid #3b4d66;background:#0d1728;transition:background .16s,border-color .16s;padding:0}
.mw .toggle::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#8fa2b9;transition:transform .16s,background .16s}
.mw .toggle[aria-checked=true]{background:#22c55e30;border-color:#3c8a59}
.mw .toggle[aria-checked=true]::after{transform:translateX(18px);background:var(--green-light)}
.mw .prio{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
.mw .prio li{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid #2d405a;border-radius:9px;background:#0c1729}
.mw .prio .rank{font:600 13px var(--mono);color:var(--green-light);width:18px;text-align:center}
.mw .prio .grow{flex:1;min-width:0}
.mw .icon-btn.small{width:36px;height:36px;min-height:36px;padding:0}
.mw .prio li.ro{opacity:.7}
.mw .prio li.ro .rank{color:var(--muted)}
.mw .fixed{display:flex;flex-direction:column;gap:10px}
.mw .fixed-rule{display:flex;gap:12px;padding:13px 14px;border:1px solid #2a3b54;border-radius:9px;background:#0e1829}
.mw .fixed-rule .icon{width:16px;height:16px;color:#8fa9c4;margin-top:2px}
.mw .fixed-rule h3{font-size:12px}
.mw .fixed-rule p{font-size:11px;color:var(--muted);margin-top:3px}
.mw .form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 18px}
.mw .field .hint{font-size:11px;color:var(--muted)}
.mw .field select{width:100%}
.mw .path-field{display:flex;gap:8px}
.mw .path-field .input{flex:1;min-width:0;font-family:var(--mono);font-size:12px}
.mw .example{margin-top:10px;padding:10px 12px;border-radius:8px;background:#0c1729;border:1px solid #2d405a;font-size:11px;color:var(--muted)}
.mw .example .path{color:#a7e8bc;font-size:11px}
.mw .state{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
.mw .state::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.mw .import-line{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding:14px;border:1px solid #2d405a;border-radius:9px;background:#0c1729}
.mw .section-h{font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px}
.mw .alias-edit{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.mw .alias-chip{display:inline-flex;align-items:center;gap:2px;padding:2px 2px 2px 9px;border:1px solid #3c506a;border-radius:6px;background:#16233a;font-size:12px}
.mw .alias-chip button{width:26px;height:26px;border:0;background:none;color:var(--muted);display:grid;place-items:center;border-radius:5px;padding:0}
.mw .alias-chip button:hover{background:#ffffff10;color:var(--text)}
.mw .alias-chip .icon{width:13px;height:13px}
.mw .alias-add{display:flex;gap:8px;margin-top:10px}
.mw .alias-add .input{flex:1;min-width:0}
.mw .loc-list{display:flex;flex-direction:column;gap:8px}
.mw .loc-item{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px;border:1px solid #2d405a;border-radius:9px;background:#0c1729;flex-wrap:wrap}
.mw .drawer-stack{display:flex;flex-direction:column;gap:18px}
.mw .drawer-stack .notice{margin-bottom:0}
.mw .check-list,.mw .run-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.mw .check-list li{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid #2d405a;border-radius:9px;background:#0c1729;font-size:12px}
.mw .check-list .grow,.mw .run-list .grow{flex:1;min-width:0}
.mw .run-list{gap:12px}
.mw .run-list li{display:flex;gap:12px;align-items:flex-start;font-size:12px}
.mw .run-list .mark{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;flex:none;border:1px solid #3b4d66;color:var(--muted);font-size:11px}
.mw .run-list .mark.done{background:#22c55e22;border-color:#3c8a59;color:var(--green-light)}
.mw .run-list .mark.run{border-color:var(--blue);color:var(--blue)}
.mw .run-list .mark .icon{width:13px;height:13px}
.mw.mw-modal.narrow{width:min(560px,calc(100% - 32px))}
@media(max-width:1250px){.mw .lib-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mw .rules-grid{grid-template-columns:1fr}}
@media(max-width:600px){.mw .lib-grid,.mw .form-grid{grid-template-columns:1fr}}

@media(min-width:1600px){.mw .content{padding:36px}.mw .queue-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:1200px){.mw .two-col{grid-template-columns:minmax(0,1.4fr) minmax(270px,1fr);gap:16px}.mw .queue-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mw .review-card{padding:17px;gap:13px}.mw .cover.review{width:66px;height:88px}.mw .stat{padding:15px}.mw .film-row{padding:16px}.mw .panel-head{padding:16px}.mw .review-peek{padding:16px}.mw .pending-card{column-gap:13px}}
@media(max-width:1080px){.mw .workspace-shell{grid-template-columns:180px minmax(0,1fr)}}
@media(max-width:900px){.mw .workspace-shell{display:block}.mw .workspace-rail{display:none}.mw .mobile-menu{display:inline-flex}.mw .content{padding:24px}.mw .two-col{grid-template-columns:1fr}.mw .review-grid,.mw .pending-grid{grid-template-columns:1fr;gap:15px}.mw .exception-card{grid-template-columns:1fr}}
@media(max-width:600px){.mw{font-size:13px}.mw .scope-banner{padding:10px 15px;font-size:10px}.mw .content{padding:22px 15px}.mw .page-title{display:block}.mw .page-title .actions{margin-top:12px}.mw .stats{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px}.mw .stat{padding:14px}.mw .stat strong{font-size:27px}.mw .flow-strip{grid-template-columns:repeat(3,1fr);margin-bottom:18px}.mw .flow-strip .flow-stage:last-child{grid-column:1/-1}.mw .flow-stage{min-height:71px;padding:9px 7px}.mw .flow-stage b{font-size:20px}.mw .panel-head{padding:15px}.mw .panel-head h2{font-size:15px}.mw .panel-body{padding:16px}.mw .film-row{padding:14px;gap:11px;align-items:flex-start}.mw .film-row .cover{width:48px;height:64px}.mw .film-title h3{font-size:12px}.mw .queue-grid,.mw .review-grid,.mw .pending-grid{grid-template-columns:1fr}.mw .cover.review{width:77px;height:103px}.mw .search-wrap,.mw .search{width:100%}.mw .filter-pill{padding:7px 11px;font-size:10px}.mw .filters select{max-width:175px}.mw .notice{font-size:11px;padding:13px;margin-bottom:17px}.mw .exception-card{padding:16px}.mw .facts{grid-template-columns:90px minmax(0,1fr);font-size:11px;gap:10px}.mw .pending-card{grid-template-areas:"cover info" "plan plan" "foot foot";row-gap:13px}.mw .plan{padding:12px}.mw .pending-foot .btn{flex:1}.mw .pending-toolbar .btn{width:100%}.mw .device-top{flex-direction:column}.mw .device-state{align-items:flex-start}.mw .device-state p{text-align:left}.mw .device-more{grid-template-columns:1fr}.mw .device-actions .btn{flex:1 1 auto}.mw .device-line-action{width:100%}.mw .device-line-action .btn{flex:1}.mw .lost-row{flex-wrap:wrap}.mw .lost-row .btn{width:100%}.mw .device-mini{padding:12px 15px}.mw .queue-summary{padding:12px 15px}}
@media(prefers-reduced-motion:reduce){.mw *,.mw *::before,.mw *::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

/* modal + drawer (portal content; the Paper carries .mw) */
.mw.mw-modal{border:1px solid #3b506c;border-radius:14px;background:#101a2c;color:var(--text);box-shadow:0 25px 100px #000a;display:flex;flex-direction:column;overflow:hidden;margin:20px;max-height:calc(100dvh - 40px);width:min(760px,calc(100% - 32px));max-width:none}
.mw.mw-drawer{border:1px solid #3b506c;border-right:0;border-radius:14px 0 0 14px;background:#101a2c;color:var(--text);width:min(660px,100%);height:100dvh;display:flex;flex-direction:column;overflow:hidden}
.mw .modal-head{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:20px 24px;border-bottom:1px solid var(--line);background:#101a2c;flex:none}
.mw .modal-head h2:focus{outline:none}
.mw .modal-head p{font-size:11px;color:var(--muted);margin-top:3px}
.mw .modal-body{padding:24px;overflow:auto;min-height:0;flex:1}
.mw .modal-foot{display:flex;gap:10px;justify-content:space-between;align-items:center;padding:17px 24px;border-top:1px solid var(--line);background:#0e1727;flex-wrap:wrap;flex:none}
.mw .modal-foot>p{font-size:10px;color:var(--muted);max-width:310px}
.mw .detail-hero{display:flex;gap:22px;align-items:center;margin-bottom:24px}
.mw .detail-hero h2{font-size:24px;margin-bottom:8px;overflow-wrap:anywhere}
.mw .detail-hero p{font-size:12px;color:var(--muted);margin-top:8px}
.mw .history-hero .cover.large{width:120px;height:160px}
.mw .history-hero>div:last-child{min-width:0;flex:1}
.mw .detail-progress{padding:18px;background:#0b1726;border:1px solid #2e465e;border-radius:10px}
.mw .final-panel{padding:20px}
.mw .confirmation-layout{display:grid;grid-template-columns:108px minmax(0,1fr);gap:20px;margin-bottom:20px}
.mw .confirmation-layout h3{font-size:20px;margin-bottom:6px;overflow-wrap:anywhere}
.mw .confirmation-layout p{font-size:11px;color:var(--muted);margin-top:7px}
.mw .eyebrow-id{font-size:10px;letter-spacing:1.4px;color:var(--muted);margin-bottom:7px}
.mw .target-box{background:#0b1727;border:1px solid #345c47;border-radius:9px;padding:17px;margin:16px 0}
.mw .target-box h3{font-size:12px;color:var(--green-light);margin-bottom:12px;display:flex;gap:6px;align-items:center}
.mw .target-path{padding:11px;background:#142b24;border:1px solid #284c3b;border-radius:6px;font:11px/1.7 var(--mono);color:#a7e8bc;margin-top:12px;overflow-wrap:anywhere}
.mw .package-list{list-style:none;padding:0;margin:10px 0;display:flex;flex-direction:column;gap:7px}
.mw .package-list li{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:#c2d3e5;padding:7px 0;border-bottom:1px solid #2636509c;overflow-wrap:anywhere}
.mw .package-list li:last-child{border:0}
.mw .source-box{background:#32271736;border:1px solid #675133;border-radius:9px;padding:17px;margin:16px 0}
.mw .source-box p{font-size:11px;color:#d6c6ae}
.mw .candidate-list{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:12px 0}
.mw .candidate{padding:12px;border:1px solid #3c506a;background:#0d1728;border-radius:8px;text-align:left;font-size:11px;min-height:75px;display:flex;gap:10px;align-items:flex-start}
.mw .candidate[aria-pressed=true]{border-color:#45885d;background:#22c55e0a}
.mw .candidate:disabled{opacity:.6;cursor:default}
.mw .candidate strong{display:block;font-size:12px}
.mw .candidate span{display:block;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.mw .candidate .avatar{width:48px;height:64px}
.mw .candidate .avatar span{display:none}
.mw .candidate .kicon{width:48px;height:64px;border-radius:7px;display:grid;place-items:center;flex:none;border:1px solid #38bdf83d;background:#38bdf80c;color:var(--blue)}
.mw .candidate.new{border-style:dashed}
.mw .candidate.new .avatar{background:#1b2a40;color:var(--green-light);border:1px dashed #3c8a59}
.mw .new-form{padding:16px;border:1px solid #345c47;border-radius:9px;background:#0b1727;display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px}
.mw .new-form .wide{grid-column:1/-1}
.mw .second-add{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:center;padding:14px 16px;border:1px dashed #3b4f6a;border-radius:9px;margin:16px 0}
.mw .target-box.second{border-color:#38bdf85a}
.mw .target-box.second h3{color:var(--blue);margin-bottom:0}
.mw .target-box.second .target-path{background:#0f2433;border-color:#24506a;color:#a5ddf7}
.mw .receipt{border:1px solid #2d6241;border-radius:9px;padding:18px;background:#22c55e09;margin-bottom:18px}
.mw .receipt h3{color:var(--green-light);margin-bottom:8px}
.mw .receipt p{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.mw .files-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:10px}
.mw .files-table th,.mw .files-table td{text-align:left;padding:7px 6px;border-bottom:1px solid #2636509c;vertical-align:top;overflow-wrap:anywhere}
.mw .files-table th{color:var(--muted);font-weight:500}
.mw .mobile-nav-head h2:focus{outline:none}
.mw .mobile-nav-head{display:flex;justify-content:space-between;align-items:center;padding:4px 8px 12px}
.mw.mw-nav{width:min(380px,calc(100% - 24px));padding:14px;border:1px solid #415875;border-radius:12px;background:#101a2c;color:var(--text);margin:12px;max-width:none}
.mw.mw-nav .nav-item{min-height:49px}
@media(max-width:600px){.mw.mw-modal{max-height:calc(100dvh - 16px);width:calc(100% - 16px);border-radius:11px;margin:8px}.mw .modal-head{padding:15px 16px}.mw .modal-head h2{font-size:16px}.mw .modal-body{padding:17px}.mw .modal-foot{padding:14px 17px;gap:12px}.mw .modal-foot .actions{width:100%;justify-content:flex-end}.mw .modal-foot .actions .primary{flex:1}.mw .modal-foot>p{max-width:none}.mw.mw-drawer{border-radius:0;width:100%}.mw .detail-hero{gap:16px}.mw .cover.large{width:86px;height:115px}.mw .detail-hero h2{font-size:20px}.mw .confirmation-layout{grid-template-columns:80px minmax(0,1fr);gap:15px}.mw .cover.confirm{width:80px;height:107px}.mw .confirmation-layout h3{font-size:18px}.mw .candidate-list,.mw .new-form{grid-template-columns:1fr}.mw .source-box,.mw .target-box{padding:14px}}
`;
