// 本机（主控）处理 (EPIC 2026-10-31 §8.4 item 4; C4 #391 AC3): ASR / 翻译 / 刮削 / 归档 tiles, 归档去向 bars and
// the 翻译服务中断 strip. 翻译 shows the films actually translated, the line total (counted from coverage.cues,
// D8) and two chips from the ASR decisions: 「N 片已有中文字幕」 and 「N 片无语音」 — 无需翻译 is never shown as
// 无语音 (§7.1). No model-share chip (D8). Outages come from the task log and are covered from
// coverage.outages only (D7); they belong to 本机, so a GPU-machine filter shows a note instead.
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import type { WorkflowAnalytics } from "../../../../api";
import { Chip, Icon } from "../../parts";
import { AnalyticsPanel, Block, DataTable, MiniStat } from "../AnalyticsParts";
import { fInt, fMD, fMDHM, fMin, fSpan, n1 } from "../analyticsFormat";
import {
  ALL_MACHINES, bucketShort, controllerFigures, controllerLabel, machineName, outageFigures, type Analysis, type ControllerFigures,
  type OutageItem, type StageFigure,
} from "../analyticsModel";
import { HBarChart, StripChart, type HBarRow } from "../charts";

const TARGETS: ReadonlyArray<readonly [keyof ControllerFigures["targets"], string]> = [
  ["performer", "主片库（演员目录）"], ["category", "分类目录"], ["new_actor", "新建演员"], ["manual", "手动目录"], ["second", "第二目标（额外复制）"],
];
const GPU_NOTE = "翻译服务属于本机；选择「全部 GPU 机器」或「本机」时显示。";
const stageNote = (s: StageFigure) => `平均 ${fMin(s.avg)} · 最长 ${fMin(s.max)}`;
const lineText = (c: ControllerFigures) => {
  if (c.cues === null) return "还没有字幕行数记录";
  const total = c.cues >= 10_000 ? `共 ${n1(c.cues / 10_000)} 万行字幕` : `共 ${fInt(c.cues)} 行字幕`;
  return c.cuesSince === null ? total : `${total}（自 ${fMD(c.cuesSince)} 起统计行数）`;
};
const outageKind = (o: OutageItem) => (o.paused ? "翻译暂停" : "翻译服务中断");
const outageEnd = (o: OutageItem) => (o.open ? "尚未恢复" : fMDHM(o.start + o.ms));

export function ControllerSection({ data, A }: { data: WorkflowAnalytics; A: Analysis }) {
  const gpuSel = A.machine !== ALL_MACHINES && !A.ctrl;
  const desc = gpuSel
    ? `${machineName(data, A.machine)} 修复的影片在本机完成的后处理`
    : `ASR、翻译、刮削、归档都在${A.controller ? controllerLabel(A.controller) : "本机"}上运行`;
  if (A.cover === null) {
    const none = () => <p className="an-none">还没有归档完成的影片</p>;
    return <AnalyticsPanel id="ctrl" title="本机（主控）处理" desc={desc} chart={none} table={none} />;
  }
  const c = controllerFigures(data, A), o = outageFigures(data, A), R = A.R;
  const n = A.cur.length;
  const targetRows: HBarRow[] = TARGETS.map(([key, label]) => {
    const v = c.targets[key];
    return {
      key, label, segs: [{ v, color: "var(--c1)" }], value: `${v} 部`,
      tip: () => ({ title: label, rows: [{ color: "var(--c1)", value: `${v} 部`, label: n ? `${Math.round((v / n) * 100)}% 的完成影片` : "" }] }),
    };
  });
  const coverNote = o.since !== null
    ? <p className="tiny muted an-after">{`翻译服务中断记录从 ${fMD(o.since)} 起`}</p>
    : o.uncovUntil !== null ? <p className="tiny muted an-after">还没有可读取的翻译服务中断记录</p> : null;
  const outageDesc = `中断 ${o.items.length} 次 · 共 ${fSpan(o.totalMs)}${o.items.length ? ` · 最长 ${fSpan(o.longestMs)}` : ""} · 期间影片在本机排队，恢复后自动继续`;
  const title = <><span className="t-amber an-ic"><Icon of={WarningAmberIcon} /></span>翻译服务中断</>;

  const chart = () => (
    <div>
      <div className="ministats">
        <MiniStat label="ASR" value={fInt(c.asr.n)} unit="片" note={stageNote(c.asr)} />
        <MiniStat label="翻译" value={fInt(c.translated)} unit="片" note={`${lineText(c)} · 平均 ${fMin(c.translate.avg)}`}>
          {(c.reusedZh > 0 || c.noSpeech > 0) && (
            <div className="chips">
              {c.reusedZh > 0 && <Chip>{`${c.reusedZh} 片已有中文字幕`}</Chip>}
              {c.noSpeech > 0 && <Chip>{`${c.noSpeech} 片无语音`}</Chip>}
            </div>
          )}
        </MiniStat>
        <MiniStat label="刮削" value={fInt(c.scrape.n)} unit="片" note={stageNote(c.scrape)} />
        <MiniStat label="归档" value={fInt(c.archive.n)} unit="片" note={stageNote(c.archive)} />
      </div>
      <div className="divider" />
      <div className="grid2">
        <Block title="归档去向" desc="一部片可以同时进主片库并复制到第二目标">
          <HBarChart rows={targetRows} labelWidth={150} label="归档去向" />
        </Block>
        <Block title={title} desc={gpuSel ? GPU_NOTE : outageDesc}>
          {!gpuSel && (
            <>
              <StripChart start={R.start} end={R.end} label="翻译服务中断时间轴"
                ticks={R.edges.slice(0, -1).map((t, i) => ({ t, label: bucketShort(R, i) }))}
                uncovUntil={o.uncovUntil} trackFrom={o.uncovUntil ?? R.start} trackTo={Math.min(R.end, A.now)}
                items={o.items.map((it, i) => ({
                  key: `${it.start}-${i}`, s: it.s, e: it.e,
                  tip: () => ({
                    title: outageKind(it),
                    rows: [{ color: "var(--st-amber)", value: fSpan(it.ms), label: "中断时长" }, { value: `${fMDHM(it.start)} – ${outageEnd(it)}` }],
                  }),
                }))} />
              {coverNote}
            </>
          )}
        </Block>
      </div>
    </div>
  );

  const stageRow = (label: string, s: StageFigure) => [label, s.n, fMin(s.avg), fMin(s.max)];
  const table = () => (
    <div>
      <DataTable caption="本机处理" cols={[{ label: "环节" }, { label: "影片", num: true }, { label: "平均", num: true }, { label: "最长", num: true }]}
        rows={[stageRow("ASR", c.asr), stageRow("翻译", c.translate), stageRow("刮削", c.scrape), stageRow("归档", c.archive)]} />
      <div className="divider" />
      <DataTable caption="字幕" cols={[{ label: "项目" }, { label: "数值", num: true }]}
        rows={[["翻译字幕行数", lineText(c)], ["已有中文字幕（无需翻译）", `${c.reusedZh} 片`], ["无语音", `${c.noSpeech} 片`]]} />
      <div className="divider" />
      <DataTable caption="归档去向" cols={[{ label: "归档去向" }, { label: "影片", num: true }]} rows={TARGETS.map(([key, label]) => [label, c.targets[key]])} />
      <div className="divider" />
      {gpuSel ? <p className="small muted">{GPU_NOTE}</p> : (
        <>
          <DataTable caption="翻译服务中断"
            cols={[{ label: "开始" }, { label: "恢复" }, { label: "类型" }, { label: "时长", num: true }]}
            rows={o.items.map((it) => [fMDHM(it.start), outageEnd(it), outageKind(it), fSpan(it.ms)])} />
          {coverNote}
        </>
      )}
    </div>
  );

  return <AnalyticsPanel id="ctrl" title="本机（主控）处理" desc={desc} chart={chart} table={table} />;
}
