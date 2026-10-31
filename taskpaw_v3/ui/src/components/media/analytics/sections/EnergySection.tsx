// 用电 「估算」 (EPIC 2026-10-31 §8.4 item 6; C5 #392 AC1): estimated kWh per bucket (+ the previous period),
// 每片平均 / 最多 / 最少, per resolution, per machine — restore energy attributed to the machine that ran each
// attempt (restore_wh, failed attempts on films finished elsewhere included) plus 本机's post-processing
// (controller_wh) — and the top 5 films. C3's honesty rule: a film without a reading is missing, never 0 kWh;
// an incomplete total says 「部分」, and no previous-period comparison unless both periods are fully measured.
import type { AnalyticsFilm, WorkflowAnalytics } from "../../../../api";
import { AnalyticsPanel, Block, DataTable, EstimateChip, Legend, MiniStat } from "../AnalyticsParts";
import { fH, fKwh } from "../analyticsFormat";
import {
  bucketShort, bucketTitle, energyFigures, filmKwh, machineName, type Analysis, type EnergyGroup, type ResFilter,
} from "../analyticsModel";
import { ColumnChart, HBarChart, type HBarRow, type TipContent } from "../charts";

const PARTIAL = "（部分）";
const PARTIAL_TITLE = "部分影片没有或缺少用电记录，只合计已有的记录";
const resText = (f: AnalyticsFilm) => (f.res === "4K" ? "4K" : f.res === "1080p" ? "1080P" : "未知分辨率");

export function EnergySection({ data, A, res, compare }: { data: WorkflowAnalytics; A: Analysis; res: ResFilter; compare: boolean }) {
  const scope = A.ctrl ? "本机后处理" : "GPU 修复 + 本机后处理";
  const badges = <EstimateChip />;
  if (A.cover === null) {
    const none = () => <p className="an-none">还没有归档完成的影片</p>;
    return <AnalyticsPanel id="energy" title="用电" desc={`${scope}的估算用电`} badges={badges} chart={none} table={none} />;
  }
  const R = A.R, e = energyFigures(data, A, res), r = e.reading;
  const kwhOf = (f: AnalyticsFilm) => filmKwh(f, A.ctrl);
  const incomplete = r.missing + r.partial;
  const prev = compare && A.prevOk ? e.prev : null;
  const vals = e.buckets.map((b) => b.kwh);
  const filmLine = (f: AnalyticsFilm) => [f.code, f.worker ? machineName(data, f.worker) : null, resText(f), f.restore_s != null ? `修复 ${fH(f.restore_s / 3600)}` : null]
    .filter(Boolean).join(" · ");
  const tip = (i: number): TipContent => {
    const b = e.buckets[i];
    return {
      title: bucketTitle(R, i, A.now),
      rows: [
        { color: "var(--c1)", value: `${fKwh(b.kwh)} kWh`, label: "用电（估算）" },
        { value: `${b.films} 部`, label: "影片" },
        ...(prev ? [{ color: "var(--c-prev)", value: `${fKwh(prev[i])} kWh`, label: "上一周期同位置" }] : []),
      ],
      note: [b.missing ? `${b.missing} 部无用电记录` : null, b.partial ? `${b.partial} 部用电记录不全` : null,
        A.cover !== null && R.edges[i + 1] <= A.cover ? "早于数据记录起点，没有记录" : null].filter(Boolean).join(" · ") || null,
    };
  };
  const value = (v: number | null, partial: boolean) => `${v === null ? "—" : fKwh(v)}${partial ? PARTIAL : ""}`;
  const resRows: HBarRow[] = e.byRes.map((x) => ({
    key: x.series.key, label: `${x.series.label} 每片平均`, segs: [{ v: x.avg ?? 0, color: x.series.color }],
    value: x.avg === null ? "—" : `${fKwh(x.avg)} kWh`,
    tip: () => ({ title: x.series.label, rows: [{ color: x.series.color, value: x.avg === null ? "—" : `${fKwh(x.avg)} kWh`, label: "每片平均" }, { value: `${x.measured} 部`, label: "有用电记录" }, { value: `${x.films} 部`, label: "影片" }] }),
  }));
  const machineRow = (g: EnergyGroup): HBarRow => ({
    key: g.key, label: g.label, segs: [{ v: g.kwh ?? 0, color: "var(--c1)" }], value: value(g.kwh, g.partial),
    tip: () => ({ title: g.label, rows: [{ color: "var(--c1)", value: g.kwh === null ? "没有用电记录" : `${fKwh(g.kwh)} kWh`, label: "估算用电" }], note: g.partial ? PARTIAL_TITLE : null }),
  });
  const mRows = e.byMachine.map(machineRow);
  const total = r.kwh === null ? "—" : fKwh(r.kwh);
  const desc = `${scope}的估算用电，按归档完成时间计入 · ${R.name}共 ${total} kWh`;
  const partialBadge = incomplete > 0 && r.measured > 0 ? <span className="chip tag amber" title={PARTIAL_TITLE}>部分</span> : null;
  const note = incomplete > 0 ? <p className="tiny muted an-after">{`${incomplete} 部用电记录不全，只合计已有的记录`}</p> : null;
  const compareNote = compare && !A.prevOk ? "上一周期没有完整记录，暂时无法对比" : compare && !prev ? "用电记录不全，暂时无法对比" : null;
  const topTable = () => (
    <Block title="用电最多的 5 部影片">
      <DataTable caption="用电最多的影片"
        cols={[{ label: "影片" }, { label: "修复机器" }, { label: "分辨率" }, { label: "修复时长", num: true }, { label: "片长", num: true }, { label: "用电 kWh", num: true }]}
        rows={e.top.map((f) => [`${f.code}${f.energy_partial ? PARTIAL : ""}`, f.worker ? machineName(data, f.worker) : "—", resText(f),
          fH(f.restore_s == null ? null : f.restore_s / 3600), fH(f.duration_s == null ? null : f.duration_s / 3600), fKwh(kwhOf(f))])} />
    </Block>
  );

  const chart = () => (
    <div>
      <div className="egrid">
        <Block title={`${R.unit}用电`} desc={prev ? null : "单一序列，柱高 = kWh"}>
          {prev && <Legend items={[{ label: "本周期", color: "var(--c1)" }, { label: "上一周期", color: "var(--c-prev)", kind: "line" }]} />}
          {compareNote && <p className="an-sub">{compareNote}</p>}
          <ColumnChart n={R.n} series={[{ key: "kwh", label: "用电", color: "var(--c1)", vals }]} prev={prev} tick={(i) => bucketShort(R, i)}
            tip={tip} labelMax fmt={fKwh} uncov={A.uncov} height={210} label="每个时段的估算用电" />
          {note}
        </Block>
        <div className="estats">
          <MiniStat label="每片平均" value={fKwh(e.perFilm)} unit="kWh" note={`${r.measured} 部有用电记录${r.missing ? ` · ${r.missing} 部没有` : ""}`} />
          <MiniStat label="最多" value={e.most ? fKwh(kwhOf(e.most)) : "—"} unit="kWh" note={e.most ? filmLine(e.most) : "—"} />
          <MiniStat label="最少" value={e.least ? fKwh(kwhOf(e.least)) : "—"} unit="kWh" note={e.least ? filmLine(e.least) : "没有完整的用电记录"} />
        </div>
      </div>
      <div className="divider" />
      <div className="grid2">
        <div>
          <Block title="按分辨率"><HBarChart rows={resRows} labelWidth={120} valueWidth={78} label="按分辨率的每片平均用电" /></Block>
          <div style={{ height: 14 }} />
          <Block title="按机器" desc={A.ctrl ? "本机的后处理（ASR、翻译、回传、刮削）" : "GPU 修复用电记在运行它的机器上（含在其他影片上失败的尝试），加上本机的后处理"}>
            <HBarChart rows={mRows} labelWidth={120} valueWidth={84} label="按机器的估算用电" />
          </Block>
        </div>
        {topTable()}
      </div>
    </div>
  );

  const table = () => (
    <div>
      <DataTable caption="用电"
        cols={[{ label: "时段" }, { label: "用电 kWh", num: true }, { label: "影片", num: true }, { label: "无用电记录", num: true },
          ...(prev ? [{ label: "上一周期 kWh", num: true }] : [])]}
        rows={e.buckets.map((b, i) => [bucketTitle(R, i, A.now), `${fKwh(b.kwh)}${b.partial ? PARTIAL : ""}`, b.films, b.missing, ...(prev ? [fKwh(prev[i])] : [])])} />
      {note}
      <div className="divider" />
      <DataTable caption="用电分组" cols={[{ label: "分组" }, { label: "kWh", num: true }]}
        rows={[...e.byRes.map((x) => [`${x.series.label} 每片平均`, x.avg === null ? "—" : fKwh(x.avg)]),
          ...e.byMachine.map((g) => [g.label, value(g.kwh, g.partial)])]} />
      <div className="divider" />
      {topTable()}
    </div>
  );

  return <AnalyticsPanel id="energy" title="用电" desc={desc} badges={<>{badges}{partialBadge}</>} chart={chart} table={table} />;
}
