// PWR-3 「功耗」 (approved preview design-system/taskpaw-v3/preview/power-v3.html): this machine and its
// peers — live estimate, today / week / month, the stacked chart, energy per machine and activity,
// and every film's energy from the media workflow. Every figure is an estimate (估算); the smart plug
// is the measurement. Phase 1 mounts it as an AgentConsole tab; it does not depend on the console.
import CalendarMonthIcon from "@mui/icons-material/CalendarMonth";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import MemoryIcon from "@mui/icons-material/Memory";
import MovieIcon from "@mui/icons-material/Movie";
import TuneIcon from "@mui/icons-material/Tune";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { GlobalStyles, Snackbar } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, HttpError, type EnergyRate, type FilmRow, type PowerRange, type PowerSettings } from "../../api";
import { MEDIA_CSS } from "../media/mediaStyles";
import { Chip, Icon, Notice, PageTitle } from "../media/parts";
import { readFilmTiming } from "../media/mediaModel";
import { PowerChart, Sparkline } from "./PowerChart";
import { PowerParams } from "./PowerParams";
import { Bolt, Est, SplitBar, SplitLegend } from "./PowerParts";
import {
  ACTIVITY_LABEL, ACTIVITY_ORDER, activityNow, clock, clockHM, fmtKwh, fmtKwhCell, fmtKwhShort, fmtW, kwh, restoreShare,
  splitOf, sumSplits, TIER_SHORT_ALL,
} from "./powerModel";
import { chartSeries, machinesOf, peakOf, rangeAgg, rangeLabel, stateChip, type Machine } from "./powerView";
import { POWER_CSS } from "./powerStyles";

const RANGES: PowerRange[] = ["24h", "7d", "30d"];
const CALENDAR: Record<PowerRange, ["today_wh" | "week_wh" | "month_wh", string]> = {
  "24h": ["today_wh", "今日"], "7d": ["week_wh", "本周"], "30d": ["month_wh", "本月"],
};
const POLL_MS = 5000;

function completedFilms(rows: FilmRow[] | undefined): FilmRow[] {
  return (rows ?? []).filter((r) => r.energy && !r.energy.running && readFilmTiming(r.timing)?.total_restore_to_translate != null)
    .slice(-8).reverse();
}

export function PowerPage({ onOpenMedia }: { onOpenMedia?: () => void } = {}) {
  const [range, setRange] = useState<PowerRange>("24h");
  const [params, setParams] = useState(false);
  const [toast, setToast] = useState<{ text: string; key: number } | null>(null);
  const day = useQuery({ queryKey: ["power", "24h"], queryFn: () => api.power("24h"), refetchInterval: POLL_MS, retry: false });
  const ranged = useQuery({
    queryKey: ["power", range], queryFn: () => api.power(range), refetchInterval: 60000, enabled: range !== "24h", retry: false,
  });
  const films = useQuery({ queryKey: ["power", "films"], queryFn: api.workflowFilms, refetchInterval: 30000, retry: false });
  const config = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const view = range === "24h" ? day.data : ranged.data;
  const machines = useMemo(() => machinesOf(day.data, view), [day.data, view]);
  const say = (text: string) => setToast({ text, key: Date.now() });
  const settings = config.data?.power as PowerSettings | undefined;

  let body;
  if (day.isLoading) body = <div className="panel empty"><p>正在读取功耗估算…</p></div>;
  else if (day.error) {
    body = day.error instanceof HttpError && day.error.status === 404
      ? <Notice tone="amber" icon={WarningAmberIcon}>本机没有运行功耗估算（agent.yaml 中 power.enabled 关闭，或这版 TaskPaw 不支持）。</Notice>
      : <Notice tone="red" icon={WarningAmberIcon}>无法读取功耗估算，请重试。</Notice>;
  } else {
    body = <PowerBody machines={machines} range={range} rows={films.data?.state === "running" ? films.data.films : undefined}
      rates={films.data?.energy_rates} workflowOff={films.data ? films.data.state !== "running" : films.isError}
      onOpenMedia={onOpenMedia} say={say} />;
  }

  return (
    <>
      <GlobalStyles styles={MEDIA_CSS} />
      <GlobalStyles styles={POWER_CSS} />
      <div className="mw mw-root" data-testid="power-page">
        <main className="pw-page" aria-label="功耗">
          <PageTitle title="功耗" actions={<>
            <div className="range" role="group" aria-label="时间范围">
              {RANGES.map((r) => (
                <button key={r} type="button" className="filter-pill" aria-pressed={range === r} onClick={() => setRange(r)}>{rangeLabel(r)}</button>
              ))}
            </div>
            <button type="button" className="btn ghost" aria-haspopup="dialog" disabled={!config.data} onClick={() => setParams(true)}>
              <Icon of={TuneIcon} />估算参数
            </button>
          </>} />
          <Notice icon={InfoOutlinedIcon}>整机实测请以智能插座为准；此处为估算，用于看分布和每片能耗。</Notice>
          {body}
        </main>
        <Snackbar key={toast?.key} open={toast !== null} autoHideDuration={4800} onClose={() => setToast(null)}
          anchorOrigin={{ vertical: "bottom", horizontal: "center" }}>
          <div role="status" aria-live="polite" style={{
            padding: "13px 18px", background: "#22364A", border: "1px solid #4D6A81", borderRadius: 9,
            color: "#ECF6FF", fontSize: 12, boxShadow: "0 7px 30px #0008", maxWidth: "min(620px, calc(100vw - 28px))",
          }}>{toast?.text}</div>
        </Snackbar>
      </div>
      <PowerParams open={params} onClose={() => setParams(false)} settings={settings} live={day.data?.power ?? null}
        machineName={day.data?.machine?.name || "本机"} os={day.data?.machine?.os} onSaved={say} />
    </>
  );
}

function PowerBody({ machines, range, rows, rates, workflowOff, onOpenMedia, say }: {
  machines: Machine[]; range: PowerRange; rows: FilmRow[] | undefined; rates: EnergyRate[] | undefined;
  workflowOff: boolean; onOpenMedia?: () => void; say: (text: string) => void;
}) {
  const online = machines.filter((m) => m.live);
  const all = sumSplits(online.map((m) => splitOf(m.live)));
  const gpuMachines = online.filter((m) => m.live?.gpu_w != null);
  const jasna = sumSplits(gpuMachines.map((m) => splitOf(m.live)));
  const working = gpuMachines.filter((m) => m.live?.activity === "restore" || m.live?.activity === "asr").length;
  const cal = (key: "today_wh" | "week_wh" | "month_wh") => online.reduce((a, m) => a + (m.live?.[key] ?? 0), 0);
  const done = completedFilms(rows);
  const whole = done.filter((r) => !r.energy?.partial);
  const perFilm = whole.length ? whole.reduce((a, r) => a + (r.energy?.total_wh ?? 0), 0) / whole.length : null;
  const known = (rates ?? []).filter((r) => r.wh_per_hour != null);
  const { series, times } = chartSeries(machines);
  const monthStart = new Date();

  return (
    <>
      <div className="stats">
        <div className="panel stat" data-testid="stat-all">
          <div className="label">全部机器实时 <Est /><Bolt /></div>
          <strong>{fmtW(all.total)}<em>W · {online.length} 台</em></strong>
          <SplitBar split={all} /><SplitLegend split={all} />
        </div>
        <div className="panel stat" data-testid="stat-energy">
          <div className="label">耗电 <Est /><Icon of={CalendarMonthIcon} /></div>
          <strong>{fmtKwhShort(cal("today_wh"))}<em>度 · 今日</em></strong>
          <div className="note">本周约 <span className="mono">{fmtKwhShort(cal("week_wh"))}</span> 度 · 本月约 <span className="mono">{fmtKwhShort(cal("month_wh"))}</span> 度</div>
          <div className="note">今日 00:00 起 · 本周自周一 · 本月自 {monthStart.getMonth() + 1}/1</div>
        </div>
        <div className="panel stat" data-testid="stat-jasna">
          <div className="label">Jasna 集群实时 <Est /><Icon of={MemoryIcon} /></div>
          <strong>{fmtW(jasna.total)}<em>W · GPU 机器</em></strong>
          <SplitBar split={jasna} />
          <div className="note">{gpuMachines.length} 台 GPU 机器 · {working} 台在工作{all.total > 0 ? ` · 占全部 ${Math.round((jasna.total / all.total) * 100)}%` : ""}</div>
        </div>
        <div className="panel stat" data-testid="stat-film">
          <div className="label">每片能耗 <Est /><Icon of={MovieIcon} /></div>
          <strong>{perFilm === null ? "—" : fmtKwhShort(perFilm)}<em>度 / 部</em></strong>
          <div className="note">
            {whole.length ? `最近 ${whole.length} 部完整记录` : "还没有完整记录的影片"}
            {known.slice(0, 2).map((r) => ` · ${r.resolution} ${TIER_SHORT_ALL[r.tier]}约 ${fmtKwhShort((r.wh_per_hour ?? 0))} 度/片长小时`).join("")}
          </div>
        </div>
      </div>

      <div className="stack">
        <section className="panel" aria-labelledby="pw-machines-title">
          <div className="panel-head">
            <div><h2 id="pw-machines-title">机器</h2><p>整机 = GPU 实测 + CPU + 其他基线 · 每 5 秒刷新 · 全部为估算</p></div>
            <Chip>{machines.length} 台 · {online.length} 台有读数</Chip>
          </div>
          <ul className="machines">{machines.map((m) => <MachineRow key={m.id} m={m} />)}</ul>
          <p className="section-note">其他机器在「估算参数」中按名称与地址添加；只运行 Hub 的 Mac 没有 TaskPaw Agent，暂不计入。</p>
        </section>

        <section className="panel" aria-labelledby="pw-chart-title">
          <div className="panel-head">
            <div><h2 id="pw-chart-title">功耗分布</h2><p>{rangeLabel(range)} · 按机器堆叠 · {rangeAgg(range)} · 估算</p></div>
            <Chip>峰值约 {Math.round(peakOf(series, times.length))} W</Chip>
          </div>
          {series.length ? <PowerChart series={series} times={times} range={range} onToast={say} />
            : <div className="empty"><p>正在读取{rangeLabel(range)}的记录…</p></div>}
        </section>

        <div className="pw-two-col">
          <EnergyTable machines={machines} range={range} />
          <Uses machines={machines} range={range} />
        </div>

        <section className="panel" aria-labelledby="pw-films-title">
          <div className="panel-head">
            <div><h2 id="pw-films-title">每片能耗</h2><p>最近完成的 {done.length} 部 · 修复 + 识别 + 翻译 + 回传与刮削</p></div>
            {onOpenMedia && <button type="button" className="btn ghost small" onClick={onOpenMedia}>在影片队列中查看</button>}
          </div>
          <div className="hist">
            <span>历史平均 · 度 / 片长小时：</span>
            {known.length ? known.map((r) => (
              <Chip key={`${r.machine}-${r.resolution}-${r.tier}`}>{r.resolution} {TIER_SHORT_ALL[r.tier]} <span className="mono">{fmtKwhCell(r.wh_per_hour)}</span></Chip>
            )) : <Chip>历史不足（每组至少 3 部）</Chip>}
          </div>
          {workflowOff ? <div className="empty"><p>媒体流水线未运行，暂无每片能耗。</p></div>
            : done.length ? <FilmsTable films={done} /> : <div className="empty"><p>还没有完成的影片能耗记录。</p></div>}
          <p className="section-note">每片能耗 = 各阶段运行期间本机的估算耗电之和（失败的尝试也计入；被中断的尝试没有读数，标「部分」）。「度 / 片长小时」用于预计：待处理页按同设备、同分辨率、同画质最近 10 部的中位数推算。</p>
        </section>
      </div>
    </>
  );
}

function MachineRow({ m }: { m: Machine }) {
  const split = splitOf(m.live);
  const [stateText, tone] = stateChip(m);
  const act = activityNow(m.live?.activity);
  return (
    <li className="machine" data-machine={m.id}>
      <div className="m-id">
        <span className="swatch" style={{ background: m.color }} aria-hidden="true" />
        <div>
          <h3>{m.name}</h3>
          <div className="hw">{m.hw}</div>
          <div className="chips">
            <Chip tone={m.local ? "green" : "neutral"}>{m.local ? "本机" : "对端"}</Chip>
            <Chip tone={tone}>{tone === "green" && <span className="dot" aria-hidden="true" />}{stateText}</Chip>
          </div>
        </div>
      </div>
      <div className="m-act">
        <span className="k">当前</span>
        {!m.live ? <span className="idle">—</span> : act && m.live.activity !== "idle" ? <strong>{act}</strong> : <span className="idle">空闲</span>}
      </div>
      <div className="m-pow">
        {m.live && split ? (
          <><div className="w">{fmtW(m.live.w)}<small>W · 估算</small></div><SplitBar split={split} /><SplitLegend split={split} /></>
        ) : <span className="none">{stateText}</span>}
      </div>
      <div className="m-spark"><Sparkline values={m.day?.avg_w ?? []} color={m.color} name={m.name} /></div>
      <div className="m-today"><span className="k">今日</span><strong>{m.live ? fmtKwh(m.live.today_wh) : "—"}<small>度</small></strong></div>
    </li>
  );
}

function EnergyTable({ machines, range }: { machines: Machine[]; range: PowerRange }) {
  const cols: Array<["today_wh" | "week_wh" | "month_wh", string]> = [["today_wh", "今日"], ["week_wh", "本周"], ["month_wh", "本月"]];
  const [sel, selLabel] = CALENDAR[range];
  const value = (m: Machine, k: (typeof cols)[number][0]) => m.live?.[k] ?? null;
  const total = (k: (typeof cols)[number][0]) => machines.reduce((a, m) => a + (value(m, k) ?? 0), 0);
  const selTotal = total(sel);
  return (
    <section className="panel" aria-labelledby="pw-energy-title">
      <div className="panel-head"><div><h2 id="pw-energy-title">耗电 · 按机器</h2><p>单位：度（kWh）· 日历口径 · 估算</p></div></div>
      <div className="scroller" role="region" aria-labelledby="pw-energy-title" tabIndex={0}>
        <table className="pw-table">
          <thead><tr>
            <th scope="col">机器</th>
            {cols.map(([k, l]) => <th key={k} scope="col" className={`num${k === sel ? " sel" : ""}`}>{l}</th>)}
            <th scope="col" className="num">占比（{selLabel}）</th>
          </tr></thead>
          <tbody>
            {machines.map((m) => {
              const share = selTotal > 0 ? (value(m, sel) ?? 0) / selTotal : 0;
              return (
                <tr key={m.id}>
                  <th scope="row"><span className="ename"><span className="swatch" style={{ background: m.color }} aria-hidden="true" />{m.name}</span></th>
                  {cols.map(([k]) => <td key={k} className={`num${k === sel ? " sel" : ""}`}>{fmtKwhCell(value(m, k))}</td>)}
                  <td><span className="share"><span className="bar" aria-hidden="true"><span style={{ width: `${share * 100}%` }} /></span><span className="mono">{Math.round(share * 100)}%</span></span></td>
                </tr>
              );
            })}
          </tbody>
          <tfoot><tr>
            <td>合计（估算）</td>
            {cols.map(([k]) => <td key={k} className={`num${k === sel ? " sel" : ""}`}>{fmtKwhCell(total(k))}</td>)}
            <td className="num">100%</td>
          </tr></tfoot>
        </table>
      </div>
    </section>
  );
}

function Uses({ machines, range }: { machines: Machine[]; range: PowerRange }) {
  const sums = Object.fromEntries(ACTIVITY_ORDER.map((k) => [k, machines.reduce((a, m) => a + (m.hist?.totals.activity_wh[k] ?? 0), 0)])) as Record<string, number>;
  const all = Object.values(sums).reduce((a, b) => a + b, 0);
  const max = Math.max(...Object.values(sums), 0.0001);
  return (
    <section className="panel" aria-labelledby="pw-uses-title">
      <div className="panel-head"><div><h2 id="pw-uses-title">按用途</h2><p>最近 {rangeLabel(range)} · 共约 {fmtKwhCell(all)} 度 · 估算</p></div></div>
      <ul className="uses">
        {ACTIVITY_ORDER.map((k) => (
          <li key={k} className="use" data-use={k}>
            <span className="n">{ACTIVITY_LABEL[k][0]}<small>{ACTIVITY_LABEL[k][1]}</small></span>
            <span className="v">{fmtKwhCell(sums[k])} 度<small>{all > 0 ? Math.round((sums[k] / all) * 100) : 0}%</small></span>
            <span className="bar" aria-hidden="true"><span style={{ width: `${(sums[k] / max) * 100}%` }} /></span>
          </li>
        ))}
      </ul>
      <p className="section-note">空闲 = 机器在线但没有任务；其他 = 主控编排、回传与整理。GPU 任务独占整机，整机功耗全部计入该用途。</p>
    </section>
  );
}

function FilmsTable({ films }: { films: FilmRow[] }) {
  const max = Math.max(...films.map((f) => f.energy?.total_wh ?? 0), 0.0001);
  return (
    <div className="scroller" role="region" aria-labelledby="pw-films-title" tabIndex={0}>
      <table className="pw-table" data-testid="films-energy">
        <thead><tr>
          <th scope="col">影片</th><th scope="col">画质</th><th scope="col" className="num">片长</th><th scope="col" className="num">全程</th>
          <th scope="col" className="num">能耗</th><th scope="col" className="num">度 / 片长小时</th><th scope="col" className="num">修复占比</th>
          <th scope="col">修复设备</th>
        </tr></thead>
        <tbody>
          {films.map((f) => {
            const e = f.energy!;
            const timing = readFilmTiming(f.timing);
            const hours = f.duration_ms ? f.duration_ms / 3_600_000 : null;
            const tier = f.plan ? f.plan.ltx_model ?? "none" : null;
            const share = restoreShare(e);
            return (
              <tr key={f.film_id}>
                <th scope="row"><span className="pw-film"><span className="code">{f.number || f.title || f.name}</span><span className="res">{f.resolution ?? "—"}</span></span></th>
                <td>{tier ? <Chip tone={tier === "high" ? "blue" : tier === "none" ? "green" : "neutral"}>{TIER_SHORT_ALL[tier]}</Chip> : "—"}</td>
                <td className="num">{clock(f.duration_ms == null ? null : f.duration_ms / 1000)}</td>
                <td className="num">{clockHM(timing?.total_restore_to_translate)}</td>
                <td><span className="kwh-cell"><span className="bar" aria-hidden="true"><span style={{ width: `${(e.total_wh / max) * 100}%` }} /></span>
                  <span className="mono">约 {fmtKwh(e.total_wh)} 度{e.partial ? "（部分）" : ""}</span></span></td>
                <td className="num">{hours ? ((kwh(e.total_wh) ?? 0) / hours).toFixed(2) : "—"}</td>
                <td className="num">{share != null && e.stages.restore ? `${share}%` : "—"}</td>
                <td>本机</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
