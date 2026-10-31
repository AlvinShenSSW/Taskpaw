// 各 GPU 机器 (EPIC 2026-10-31 §8.4 item 3; C4 #391 AC1, AC2): a model-grouped, sortable table (state chip
// with note, 4K/1080P bar with the same-model average tick, restore times, 平均功率 from restore_wh, 用电,
// 失败, 修复效率), 各机型实测产能, and small multiples on one shared y-axis. A row click filters the page to
// that machine. 本机 joins only in a range where it restored (D11).
import ArrowDropDownIcon from "@mui/icons-material/ArrowDropDown";
import ArrowDropUpIcon from "@mui/icons-material/ArrowDropUp";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import UnfoldMoreIcon from "@mui/icons-material/UnfoldMore";
import { Fragment, useState, type KeyboardEvent } from "react";
import type { WorkflowAnalytics } from "../../../../api";
import { Icon, Notice } from "../../parts";
import { AnalyticsPanel, Block, DataTable, EstimateChip, Legend, MachineStateChip } from "../AnalyticsParts";
import { fH, fInt, fKwh, fMDHM, n1 } from "../analyticsFormat";
import {
  ALL_MACHINES, bucketize, bucketShort, bucketTitle, devicesCoverRange, MACHINE_STATE_TEXT, machineRows, modelAverage, modelGroups,
  modelThroughput, PAUSE_REASON_TEXT, shownResSeries, sortMachineRows, sum, type Analysis, type MachineRow, type MachineSort,
  type MachineSortKey, type ResFilter,
} from "../analyticsModel";
import { ColumnChart } from "../charts";
import { tipHandlers, useChartTip } from "../chartKit";

const SORT_COLS: ReadonlyArray<readonly [MachineSortKey, string, boolean]> = [
  ["name", "机器", false], ["st", "状态", false], ["total", "修复影片", false], ["a4", "平均修复 4K", true], ["a1", "平均修复 1080P", true],
  ["min", "最短 / 最长", true], ["power", "平均功率", true], ["kwh", "用电", true], ["fails", "失败", true], ["eff", "修复效率", true],
];
const ESTIMATED = new Set<MachineSortKey>(["power", "kwh"]); // OD4: energy figures are estimates, labelled 「估算」
const DESC = "按归档完成时间统计每台机器修复的影片，同型号放在一起比较 · 点击一行只看这台机器";

const nameOf = (r: MachineRow) => (r.m.kind === "controller" ? "本机" : r.m.name);
const at = (iso: string | null) => (iso ? fMDHM(Date.parse(iso)) : null);
/** The line under a state chip: why and since when (停领 names its reason). */
function stateNote(r: MachineRow): string | null {
  const since = at(r.m.state_since);
  if (r.state === "new") return r.m.first_seen ? `${at(r.m.first_seen)} 加入` : null;
  if (r.state === "paused") return [PAUSE_REASON_TEXT[r.m.state_reason], since && `${since} 起`].filter(Boolean).join(" · ");
  if (r.state === "offline") return since && `${since} 起`;
  if (r.state === "off") return "远程工作机未启用";
  return null;
}
const power = (r: MachineRow) => (r.power === null ? "—" : `${fInt(r.power)} W`);
// C3's energy rule: no reading is 「—」, never 0 kWh; restores that lacked energy mark the sum 「部分」
const PARTIAL = "（部分）";
const kwhText = (r: MachineRow, unit: boolean) =>
  `${r.kwh === null ? "—" : `${fKwh(r.kwh)}${unit ? " kWh" : ""}`}${r.kwhPartial ? PARTIAL : ""}`;
const PARTIAL_TITLE = "部分修复没有用电记录，只合计已有的记录";
const eff = (r: MachineRow) => (r.eff === null ? "—" : `${n1(r.eff)} 小时`);

export function MachinesSection({ data, A, res, onMachine }: {
  data: WorkflowAnalytics; A: Analysis; res: ResFilter; onMachine: (id: string) => void;
}) {
  const [sort, setSort] = useState<MachineSort>({ key: "model", dir: 1 });
  const tip = useChartTip();
  const back = A.machine !== ALL_MACHINES
    ? <button type="button" className="link-btn" onClick={() => onMachine(ALL_MACHINES)}>查看全部 GPU 机器</button> : null;
  if (A.cover === null) {
    const none = () => <p className="an-none">还没有归档完成的影片</p>;
    return <AnalyticsPanel id="machines" title="各 GPU 机器" desc={DESC} chart={none} table={none} />;
  }
  const rows = machineRows(data, A);
  if (A.ctrl && !rows.length) {
    const notice = () => (
      <Notice icon={InfoOutlinedIcon}>已筛选「本机」。主控只做 ASR、翻译、刮削和归档，不做修复，这里没有 GPU 数据。{back}</Notice>
    );
    return <AnalyticsPanel id="machines" title="各 GPU 机器" desc={DESC} chart={notice} table={notice} />;
  }

  const groups = modelGroups(rows);
  const ordered = groups.flatMap((g) => sortMachineRows(g.rows, sort).map((r) => ({ r, ma: modelAverage(g) })));
  const series = shownResSeries(res, A.cur);
  const maxT = Math.max(1, ...rows.map((r) => r.total));
  const covered = devicesCoverRange(data, A);
  const tp = modelThroughput(groups);
  const pickRow = (id: string) => onMachine(A.machine === id ? ALL_MACHINES : id);
  const onRowKey = (id: string) => (e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickRow(id); } };
  const toggleSort = (k: MachineSortKey) => setSort((s) => (s.key === k ? { key: k, dir: s.dir === 1 ? -1 : 1 } : { key: k, dir: k === "name" || k === "st" ? 1 : -1 }));
  const countOf = (r: MachineRow, key: string) => (key === "4K" ? r.n4 : key === "1080p" ? r.n1 : r.nNone);

  const throughput = () => (
    <Block title="各机型实测产能"
      desc={covered ? "每台机器每个在线日完成的影片数（按在线时长折算，含停领）" : "每台机器每天完成的影片数（按在所选时段内出现的日历天折算，未扣除离线时间）"}>
      <DataTable caption="各机型实测产能"
        cols={[{ label: "机型" }, { label: "台数", num: true }, { label: "每台每天 4K", num: true }, { label: "每台每天 1080P", num: true },
          { label: "每台每天合计", num: true }, { label: "平均修复 4K", num: true }, { label: "平均修复 1080P", num: true }]}
        rows={tp.map((t) => [t.model, t.newCount ? `${t.count}（含 ${t.newCount} 台新加入）` : t.count, res === "1080p" ? "—" : n1(t.p4),
          res === "4K" ? "—" : n1(t.p1), n1(t.pt), fH(t.a4), fH(t.a1)])} />
    </Block>
  );

  const per = ordered.map(({ r }) => ({ r, series: series.map((s) => ({ ...s, vals: bucketize(A, r.films.filter((f) => f.res === s.res)) })) }));
  const yMax = Math.max(1, ...per.flatMap((p) => Array.from({ length: A.R.n }, (_, i) => sum(p.series.map((s) => s.vals[i])))));
  const multiples = () => (
    <Block title="每台机器每个时段的修复量" desc={`${A.R.unit}一格，所有小图用同一个纵轴（最高 ${yMax} 部）`}>
      <Legend items={series.map((s) => ({ label: s.label, color: s.color }))} />
      <div className="multiples">
        {per.map(({ r, series: ss }) => (
          <div className="mini" key={r.m.id}>
            <div className="mini-h"><strong>{nameOf(r)}</strong><span className="muted">{r.total} 部</span></div>
            <ColumnChart n={A.R.n} series={ss} mini height={96} yMax={yMax} tick={(i) => bucketShort(A.R, i)} uncov={A.uncov}
              label={`${nameOf(r)} 每个时段修复量`}
              tip={(i) => ({ title: `${nameOf(r)} · ${bucketTitle(A.R, i, A.now)}`, rows: ss.map((s) => ({ color: s.color, value: `${s.vals[i]} 部`, label: s.label })) })} />
          </div>
        ))}
      </div>
    </Block>
  );

  const chart = () => (
    <div>
      <Legend items={[...series.map((s) => ({ label: s.label, color: s.color })), { label: "同型号平均（每台）", color: "var(--text)", kind: "tick" as const }]} />
      <div className="table-wrap">
        <table className="grid an-machines">
          <caption className="sr-only">各 GPU 机器</caption>
          <thead>
            <tr>
              {SORT_COLS.map(([k, label, num]) => {
                const on = sort.key === k;
                return (
                  <th key={k} scope="col" className={num ? "num" : undefined} aria-sort={on ? (sort.dir > 0 ? "ascending" : "descending") : undefined}
                    title={k === "eff" ? "每 1 小时片长需要的修复时间" : undefined}>
                    <button type="button" className="th-btn" onClick={() => toggleSort(k)}>
                      {label}<Icon of={on ? (sort.dir > 0 ? ArrowDropUpIcon : ArrowDropDownIcon) : UnfoldMoreIcon} />
                    </button>
                    {ESTIMATED.has(k) && <EstimateChip />}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const ma = modelAverage(g);
              return (
                <Fragment key={g.model}>
                  <tr className="grp">
                    <th colSpan={SORT_COLS.length} scope="colgroup">
                      {`${g.model} × ${g.rows.length}`}
                      <span>{ma !== null ? `同型号平均每台 ${n1(ma)} 部` : g.rows.length === 1 ? "只有一台，无同型号比较" : "新加入的机器不计入，同型号可比机器不足两台"}</span>
                    </th>
                  </tr>
                  {sortMachineRows(g.rows, sort).map((r) => {
                    const note = stateNote(r);
                    const segs = series.map((s) => ({ s, n: countOf(r, s.key) })).filter((z) => z.n > 0);
                    return (
                      <tr key={r.m.id} className={A.machine === r.m.id ? "sel" : undefined} tabIndex={0} aria-label={`只看 ${nameOf(r)}`}
                        onClick={() => pickRow(r.m.id)} onKeyDown={onRowKey(r.m.id)}>
                        <td><div className="mname"><strong>{nameOf(r)}</strong><span>{r.m.model ?? "未识别型号"}</span></div></td>
                        <td><MachineStateChip state={r.state} />{note && <span className="state-note">{note}</span>}</td>
                        <td>
                          <div className="sbar-cell">
                            <div className="sbar-track">
                              <div className="sbar" style={{ width: `${(r.total / maxT) * 100}%` }}>
                                {segs.map(({ s, n }) => (
                                  <span key={s.key} style={{ flex: `${n} 1 0`, background: s.color }}
                                    {...tipHandlers(tip, () => ({ title: nameOf(r), rows: [{ color: s.color, value: `${n} 部`, label: s.label }, { value: `${r.total} 部`, label: "合计" }] }))} />
                                ))}
                              </div>
                              {ma !== null && (
                                <span className="avg-mk" style={{ left: `${(ma / maxT) * 100}%` }}
                                  {...tipHandlers(tip, () => ({ title: `${g.model} 同型号平均`, rows: [{ value: `${n1(ma)} 部`, label: "每台平均" }] }))} />
                              )}
                            </div>
                            <b>{r.total}</b>
                          </div>
                        </td>
                        <td className="num">{fH(r.a4)}</td>
                        <td className="num">{fH(r.a1)}</td>
                        <td className="num">{`${fH(r.min)} / ${fH(r.max)}`}</td>
                        <td className="num">{power(r)}</td>
                        <td className="num" title={r.kwhPartial ? PARTIAL_TITLE : undefined}>{kwhText(r, true)}</td>
                        <td className="num">{r.fails}</td>
                        <td className="num">{eff(r)}</td>
                      </tr>
                    );
                  })}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="divider" />
      {throughput()}
      <div className="divider" />
      {multiples()}
    </div>
  );

  const unknown = rows.some((r) => r.nNone > 0);
  const table = () => (
    <div>
      <DataTable caption="各 GPU 机器"
        cols={[{ label: "机型" }, { label: "机器" }, { label: "状态" }, { label: "4K", num: true }, { label: "1080P", num: true },
          ...(unknown ? [{ label: "未知分辨率", num: true }] : []), { label: "合计", num: true }, { label: "同型号平均", num: true },
          { label: "平均修复 4K", num: true }, { label: "平均修复 1080P", num: true }, { label: "最短", num: true }, { label: "最长", num: true },
          { label: "平均功率", num: true, tag: <EstimateChip /> }, { label: "用电 kWh", num: true, tag: <EstimateChip /> }, { label: "失败", num: true }, { label: "修复效率", num: true }]}
        rows={ordered.map(({ r, ma }) => {
          const note = stateNote(r);
          return [r.m.model ?? "未识别型号", nameOf(r), `${MACHINE_STATE_TEXT[r.state]}${note ? `（${note}）` : ""}`, r.n4, r.n1,
            ...(unknown ? [r.nNone] : []), r.total, ma === null ? "—" : n1(ma), fH(r.a4), fH(r.a1), fH(r.min), fH(r.max), power(r),
            kwhText(r, false), r.fails, eff(r)];
        })} />
      <div className="divider" />
      {throughput()}
      <div className="divider" />
      <Block title="每台机器每个时段的修复量">
        <DataTable caption="每台机器每个时段的修复量" cols={[{ label: "时段" }, ...per.map(({ r }) => ({ label: nameOf(r), num: true }))]}
          rows={Array.from({ length: A.R.n }, (_, i) => [bucketTitle(A.R, i, A.now), ...per.map(({ series: ss }) => sum(ss.map((s) => s.vals[i])))])} />
      </Block>
    </div>
  );

  return <AnalyticsPanel id="machines" title="各 GPU 机器" desc={DESC} actions={back} chart={chart} table={table} />;
}
