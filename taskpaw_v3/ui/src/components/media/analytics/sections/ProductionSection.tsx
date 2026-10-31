// 产量趋势 (EPIC 2026-10-31 §8.4 item 2): films archived per bucket, stacked by resolution, with the
// previous-period line (对比上一周期), the max label, the uncovered band, a tooltip and a table view.
import type { AnalyticsFilm } from "../../../../api";
import { AnalyticsPanel, DataTable, Legend } from "../AnalyticsParts";
import { bucketize, bucketShort, bucketTitle, type Analysis, type ResFilter } from "../analyticsModel";
import { ColumnChart, type ColumnSeries, type TipContent } from "../charts";

const RES_SERIES: Array<{ key: string; label: string; color: string; res: AnalyticsFilm["res"] }> = [
  { key: "4K", label: "4K", color: "var(--c1)", res: "4K" },
  { key: "1080p", label: "1080P", color: "var(--c2)", res: "1080p" },
  // films whose input probes gave no resolution (§7 `res: null`); only shown when there are some
  { key: "none", label: "未知分辨率", color: "var(--c-queue)", res: null },
];

export function ProductionSection({ A, res, compare }: { A: Analysis; res: ResFilter; compare: boolean }) {
  const R = A.R;
  const series: ColumnSeries[] = RES_SERIES
    .filter((s) => (res === "all" ? s.res !== null || A.cur.some((f) => f.res === null) : s.res === res))
    .map((s) => ({ key: s.key, label: s.label, color: s.color, vals: bucketize(A, A.cur.filter((f) => f.res === s.res)) }));
  const prev = compare && A.prevOk && A.prev ? bucketize(A, A.prev, undefined, true) : null;
  const total = (i: number) => series.reduce((a, s) => a + s.vals[i], 0);
  const tip = (i: number): TipContent => ({
    title: bucketTitle(R, i, A.now),
    rows: [
      ...series.map((s) => ({ color: s.color, value: `${s.vals[i]} 部`, label: s.label })),
      { value: `${total(i)} 部`, label: "合计" },
      ...(prev ? [{ color: "var(--c-prev)", value: `${prev[i]} 部`, label: "上一周期同位置" }] : []),
    ],
    note: A.cover !== null && R.edges[i + 1] <= A.cover ? "早于数据记录起点，没有记录" : null,
  });
  const none = A.cover === null ? <p className="an-none">还没有归档完成的影片</p> : null;
  return (
    <AnalyticsPanel id="prod" title="产量趋势" desc={`${R.unit}完成归档的影片数，按分辨率分层 · ${R.name}共 ${A.cur.length} 部`}
      chart={() => none ?? (
        <div>
          <Legend items={[...series.map((s) => ({ label: s.label, color: s.color })), ...(prev ? [{ label: "上一周期", color: "var(--c-prev)", kind: "line" as const }] : [])]} />
          {compare && !A.prevOk && <p className="an-sub">上一周期没有完整记录，暂时无法对比。</p>}
          <ColumnChart n={R.n} series={series} prev={prev} tick={(i) => bucketShort(R, i)} tip={tip} labelMax uncov={A.uncov}
            label="产量趋势：每个时段完成的影片数" />
        </div>
      )}
      table={() => none ?? (
        <DataTable caption="产量趋势"
          cols={[{ label: "时段" }, ...series.map((s) => ({ label: s.label, num: true })), { label: "合计", num: true }, ...(prev ? [{ label: "上一周期", num: true }] : [])]}
          rows={Array.from({ length: R.n }, (_, i) => [bucketTitle(R, i, A.now), ...series.map((s) => s.vals[i]), total(i), ...(prev ? [prev[i]] : [])])} />
      )} />
  );
}
