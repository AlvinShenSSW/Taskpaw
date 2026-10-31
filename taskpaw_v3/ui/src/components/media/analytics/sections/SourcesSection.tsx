// 片源与片库 (EPIC 2026-10-31 §8.4 item 9, D9; C5 #392 AC4, AC5): the INBOX card (current, not range-filtered:
// a meter of 待处理 / 其他 / 可用, 平均每天腾出, 现在还可放入约 N 部 「估算」 — free space is 「可用」, never 「剩余」,
// and there is no forecast of when more fits, OD1/D9), the input-size and length histograms, the 8 actors
// with most archived films, and the share of films carrying each mark.
import StorageIcon from "@mui/icons-material/Storage";
import { useId } from "react";
import type { WorkflowAnalytics } from "../../../../api";
import { Icon } from "../../parts";
import { AnalyticsPanel, Block, DataTable, Legend } from "../AnalyticsParts";
import { fGB, fPct, fTB, n1 } from "../analyticsFormat";
import { GB, inboxFigures, sourceFigures, type Analysis, type Histogram } from "../analyticsModel";
import { ColumnChart, HBarChart, type HBarRow } from "../charts";
import { tipHandlers, useChartTip } from "../chartKit";

const DESC = "收进来的片子是什么样，以及归档进了哪些演员目录";
const SEG = { pending: "var(--c1)", other: "var(--c-queue)", free: "var(--meter-track)" };

function InboxCard({ data }: { data: WorkflowAnalytics }) {
  const tip = useChartTip();
  const headId = useId();
  const x = inboxFigures(data.inbox);
  const head = (
    <h3 id={headId}><Icon of={StorageIcon} />INBOX 空间<span className="chip tag">当前 · 不随时段筛选</span></h3>
  );
  if (!x) return <div className="inbox" role="group" aria-labelledby={headId}><div>{head}<p className="small muted">没有配置 INBOX 目录，无法读取空间</p></div></div>;
  const pct = (b: number) => `${x.total > 0 ? (b / x.total) * 100 : 0}%`;
  const total = fTB(x.total);
  return (
    <div className="inbox" role="group" aria-labelledby={headId}>
      <div>
        {head}
        <div className="meter" role="meter" aria-label="INBOX 已用空间" tabIndex={0} aria-valuemin={0}
          aria-valuemax={Math.round(x.total / GB)} aria-valuenow={Math.round(x.used / GB)}
          aria-valuetext={`已用 ${fGB(x.used)} GB，可用 ${fGB(x.free)} GB，共 ${total}`}
          {...tipHandlers(tip, () => ({
            title: `INBOX ${total}`,
            rows: [
              { color: SEG.pending, value: `${fGB(x.pending)} GB`, label: "待处理影片" }, { color: SEG.other, value: `${fGB(x.other)} GB`, label: "其他文件" },
              { value: `${fGB(x.used)} GB`, label: "已用" }, { value: `${fGB(x.free)} GB`, label: "可用" },
            ],
            note: "其他文件包括这块盘上的其他所有内容",
          }))}>
          <span style={{ width: pct(x.pending), background: SEG.pending }} />
          <span style={{ width: pct(x.other), background: SEG.other }} />
        </div>
        <div className="sub">{`已用 ${fGB(x.used)} GB · 可用 ${fGB(x.free)} GB（共 ${total}）`}</div>
        <Legend items={[{ label: "待处理影片", color: SEG.pending }, { label: "其他文件", color: SEG.other }, { label: "可用", color: SEG.free }]} />
      </div>
      <div>
        <div className="sub">待处理影片占用</div>
        <div className="fig">{fGB(x.pending)}<em>GB</em></div>
        <div className="sub">{`${x.pendingFilms} 部 · 已入库、原片还在 INBOX`}</div>
      </div>
      <div>
        <div className="sub">平均每天腾出</div>
        <div className="fig">{fGB(x.freedPerDay)}<em>GB</em></div>
        <div className="sub">近 7 天平均，原片归档后删除</div>
      </div>
      <div>
        <div className="sub">现在还可放入</div>
        <div className="fig">{x.canNow === null ? "—" : `约 ${x.canNow}`}{x.canNow !== null && <em>部</em>}</div>
        <div className="sub">
          <span className="chip tag" title="按可用空间和近 7 天平均每部大小估算">估算</span>
          {x.canNow === null ? " 近 7 天没有归档，无法估算" : ` 按近 7 天平均每部 ${n1(x.avgInput / GB)} GB`}
        </div>
      </div>
    </div>
  );
}

export function SourcesSection({ data, A }: { data: WorkflowAnalytics; A: Analysis }) {
  if (A.cover === null) {
    const none = () => <div><InboxCard data={data} /><div className="divider" /><p className="an-none">还没有归档完成的影片</p></div>;
    return <AnalyticsPanel id="src" title="片源与片库" desc={DESC} chart={none} table={none} />;
  }
  const s = sourceFigures(A);
  const hist = (h: Histogram, unit: string, label: string, gone: string) => (
    <>
      <ColumnChart n={h.counts.length} series={[{ key: "n", label: "影片", color: "var(--c1)", vals: h.counts }]} tick={(i) => h.labels[i]}
        tickAll={h.counts.length <= 6} height={190} labelMax label={label}
        tip={(i) => ({ title: `${h.labels[i]} ${unit}`, rows: [{ color: "var(--c1)", value: `${h.counts[i]} 部`, label: "影片" }] })} />
      {h.missing > 0 && <p className="tiny muted an-after">{`${h.missing} 部${gone}`}</p>}
    </>
  );
  const actorRows: HBarRow[] = s.actors.map(([name, n]) => ({
    key: name, label: name, segs: [{ v: n, color: "var(--c1)" }], value: `${n} 部`,
    tip: () => ({ title: name, rows: [{ color: "var(--c1)", value: `${n} 部`, label: "归档影片" }] }),
  }));
  const share = (n: number) => (s.total ? (n / s.total) * 100 : 0);
  const markRows: HBarRow[] = s.marks.map((m) => ({
    key: m.key, label: m.label, segs: [{ v: share(m.n), color: "var(--c1)" }], value: fPct(share(m.n)),
    tip: () => ({ title: `标识：${m.label}`, rows: [{ color: "var(--c1)", value: fPct(share(m.n)), label: "的影片带有这个标识" }, { value: `${m.n} 部` }] }),
  }));

  const chart = () => (
    <div>
      <InboxCard data={data} />
      <div className="divider" />
      <div className="grid2">
        <Block title="输入大小" desc="原片大小分布，单位 GB">{hist(s.size, "GB", "输入大小分布", "没有大小记录")}</Block>
        <Block title="片长" desc="视频时长分布，单位小时">{hist(s.duration, "小时", "片长分布", "没有片长记录")}</Block>
        <Block title="归档最多的演员（前 8）" desc="只统计进演员目录的影片（主片库演员目录和新建演员）">
          {actorRows.length ? <HBarChart rows={actorRows} labelWidth={110} valueWidth={52} label="归档最多的演员" /> : <p className="small muted">这个时段没有数据</p>}
        </Block>
        <Block title="标识占比" desc="一部片可以同时有多个标识">
          <HBarChart rows={markRows} labelWidth={90} valueWidth={52} max={100} label="标识占比" />
        </Block>
      </div>
    </div>
  );

  const table = () => (
    <div>
      <InboxCard data={data} />
      <div className="divider" />
      <div className="grid2">
        <DataTable caption="输入大小" cols={[{ label: "输入大小（GB）" }, { label: "影片", num: true }]} rows={s.size.labels.map((l, i) => [l, s.size.counts[i]])} />
        <DataTable caption="片长" cols={[{ label: "片长（小时）" }, { label: "影片", num: true }]} rows={s.duration.labels.map((l, i) => [l, s.duration.counts[i]])} />
        <DataTable caption="归档最多的演员" cols={[{ label: "演员" }, { label: "影片", num: true }]} rows={s.actors.map(([n, v]) => [n, v])} />
        <DataTable caption="标识占比" cols={[{ label: "标识" }, { label: "影片", num: true }, { label: "占比", num: true }]}
          rows={s.marks.map((m) => [m.label, m.n, fPct(share(m.n))])} />
      </div>
    </div>
  );

  return <AnalyticsPanel id="src" title="片源与片库" desc={DESC} chart={chart} table={table} />;
}
