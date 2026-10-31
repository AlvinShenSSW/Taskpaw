// 耗时分布 (EPIC 2026-10-31 §8.4 item 5; C4 #391 AC4): range strips in hours (排队等待, 修复 4K / 1080P, 全程)
// and minutes (ASR, 翻译, 刮削, 归档), the time-share bar and a full stats table. Definitions follow D4.
import type { ReactNode } from "react";
import { AnalyticsPanel, Block, DataTable, Legend } from "../AnalyticsParts";
import { fAutoH, fH, fMin, fPct } from "../analyticsFormat";
import { durationFigures, sum, type Analysis, type DurationRow } from "../analyticsModel";
import { RangeChart, ShareBar } from "../charts";

// Stage colours of the approved preview (STAGE): fixed, never cycled
const COLOR: Record<string, string> = {
  queue: "var(--c-queue)", restore: "var(--c1)", restore4k: "var(--c1)", restore1080: "var(--c1)", asr: "var(--c3)",
  translate: "var(--c4)", scrape: "var(--c5)", archive: "var(--c6)", lead: "var(--c-total)",
};
const LEGEND = [
  ["queue", "排队等待"], ["restore", "修复（LTX）"], ["asr", "ASR"], ["translate", "翻译"], ["scrape", "刮削"], ["archive", "归档"], ["lead", "全程"],
] as const;
const DESC = "每个阶段最短到最长（细线）、中间一半的影片（粗段）和平均值（圆点）";

export function DurationsSection({ A }: { A: Analysis }) {
  if (A.cover === null) {
    const none = () => <p className="an-none">还没有归档完成的影片</p>;
    return <AnalyticsPanel id="dur" title="耗时分布" desc={DESC} chart={none} table={none} />;
  }
  const f = durationFigures(A);
  const range = (rows: DurationRow[]) => rows.map((r) => ({ ...r, color: COLOR[r.key] }));
  const total = sum(f.share.map((s) => s.hours));
  const share = f.share.map((s) => {
    const pct = total ? (s.hours / total) * 100 : 0;
    return {
      key: s.key, label: s.label, v: s.hours, color: COLOR[s.key], pct,
      tip: () => ({ title: "平均每部片", rows: [{ color: COLOR[s.key], value: fPct(pct), label: s.label }, { value: fAutoH(s.hours), label: "平均耗时" }] }),
    };
  });
  const statRows = (rows: DurationRow[], fmt: (v: number) => string): ReactNode[][] => rows.map((r) => [
    r.label, r.st?.n ?? 0, ...(r.st ? [r.st.min, r.st.p25, r.st.p50, r.st.p75, r.st.max, r.st.avg].map(fmt) : Array<string>(6).fill("—")),
  ]);

  const chart = () => (
    <div>
      <Legend items={LEGEND.map(([k, label]) => ({ label, color: COLOR[k] }))} />
      <div className="grid2">
        <Block title="以小时计" desc="排队等待 = 确认修复计划到开始修复；全程 = 入库到归档完成">
          <RangeChart rows={range(f.hours)} fmt={fH} label="按小时计的阶段耗时分布" />
        </Block>
        <Block title="以分钟计" desc="本机上的后处理环节，翻译只统计实际翻译的影片">
          <RangeChart rows={range(f.minutes)} fmt={fMin} label="按分钟计的阶段耗时分布" />
        </Block>
      </div>
      <div className="divider" />
      <Block title="一部片的时间都花在哪" desc="各阶段平均耗时占比；不含文件传输与回传">
        <ShareBar segs={share} label="各阶段平均耗时占比" />
        <div className="share-lg">
          {share.map((s) => (
            <span className="lgd-i" key={s.key}>
              <span className="lgd-k rect" style={{ background: s.color }} />{s.label}<b>{fPct(s.pct)}</b><span className="m">{fAutoH(s.v)}</span>
            </span>
          ))}
        </div>
      </Block>
    </div>
  );

  const table = () => (
    <div>
      <DataTable caption="耗时分布"
        cols={[{ label: "阶段" }, { label: "影片", num: true }, { label: "最短", num: true }, { label: "p25", num: true }, { label: "中位数", num: true },
          { label: "p75", num: true }, { label: "最长", num: true }, { label: "平均", num: true }]}
        rows={[...statRows(f.hours, fH), ...statRows(f.minutes, fMin)]} />
      <div className="divider" />
      <DataTable caption="时间占比" cols={[{ label: "阶段" }, { label: "平均耗时", num: true }, { label: "占比", num: true }]}
        rows={share.map((s) => [s.label, fAutoH(s.v), fPct(s.pct)])} />
    </div>
  );

  return <AnalyticsPanel id="dur" title="耗时分布" desc={DESC} chart={chart} table={table} />;
}
