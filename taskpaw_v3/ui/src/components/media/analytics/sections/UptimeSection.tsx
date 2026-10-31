// 设备在线 (EPIC 2026-10-31 §8.4 item 7, §8.6; C5 #392 AC2): one lane per machine in range plus 本机, each
// split into 在线 / 停领 (with its reason) / 离线 / 无记录. 在线率 = (在线 + 停领) ÷ recorded time; unrecorded
// time is listed apart, never counted as offline. A 新加入 machine's lane starts at first_seen (D14).
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import PauseCircleOutlineIcon from "@mui/icons-material/PauseCircleOutline";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import WifiOffIcon from "@mui/icons-material/WifiOff";
import type { WorkflowAnalytics } from "../../../../api";
import { Notice } from "../../parts";
import { AnalyticsPanel, DataTable, Legend } from "../AnalyticsParts";
import { fMDHM, fPct, fSpan } from "../analyticsFormat";
import {
  bucketShort, PAUSE_REASON_TEXT, uptimeLanes, type Analysis, type LaneSegment, type LaneState, type UptimeLane,
} from "../analyticsModel";
import { LaneChart, type Lane } from "../charts";

const STATE: Record<LaneState, { text: string; color: string; icon: typeof WifiOffIcon; iconColor: string }> = {
  online: { text: "在线", color: "var(--lane-on)", icon: CheckCircleOutlineIcon, iconColor: "var(--green-light)" },
  paused: { text: "停领", color: "var(--st-amber)", icon: PauseCircleOutlineIcon, iconColor: "var(--amber)" },
  offline: { text: "离线", color: "var(--st-red)", icon: WifiOffIcon, iconColor: "var(--red)" },
  none: { text: "无记录", color: "var(--lane-none)", icon: HelpOutlineIcon, iconColor: "var(--muted)" },
};
const NO_RECORD = "主控未运行或未收到心跳，无记录";
const DESC = "每台机器在所选时段内的在线、停领与离线 · 在线率 = （在线 + 停领）÷ 有记录的时间，无记录的时间单独列出";

const nameOf = (l: UptimeLane) => (l.m.kind === "controller" ? "本机" : l.m.name);
const stateText = (z: LaneSegment) => (z.state === "paused" && z.reason ? `停领（${PAUSE_REASON_TEXT[z.reason]}）` : STATE[z.state].text);
// an open span is still going: its end is the last recorded tick, after which the lane is 无记录
const until = (z: LaneSegment) => (z.open ? `${fMDHM(z.e)}（仍在继续）` : fMDHM(z.e));
const rateText = (l: UptimeLane) => (l.rate === null ? "—" : fPct(l.rate));
const joinedNote = (l: UptimeLane) => (l.isNew && l.m.first_seen ? `${fMDHM(Date.parse(l.m.first_seen))} 加入，之前没有记录` : null);

/** The coverage notice of §8.6: history unavailable, not recorded yet, or recorded from inside the range. */
function coverageNotice(data: WorkflowAnalytics, A: Analysis): string | null {
  if (!data.devices.available) return `设备记录不可用：${data.devices.reason || "原因未知"}`;
  if (data.coverage.devices === null) return "还没有设备在线记录：主控开始记录后从那一刻起显示，之前的时间显示为「无记录」。";
  const from = Date.parse(data.coverage.devices);
  return from > A.R.start ? `设备在线记录从 ${fMDHM(from)} 开始，之前的时间显示为「无记录」。` : null;
}

export function UptimeSection({ data, A }: { data: WorkflowAnalytics; A: Analysis }) {
  const R = A.R, lanes = uptimeLanes(data, A), notice = coverageNotice(data, A);
  const noticeEl = notice && (
    <Notice tone="amber" icon={data.devices.available ? InfoOutlinedIcon : WarningAmberIcon}>{notice}</Notice>
  );
  const chartLanes: Lane[] = lanes.map((l) => ({
    key: l.m.id, label: nameOf(l), from: l.from, to: l.to, value: l.rate === null ? "—" : `在线 ${fPct(l.rate)}`,
    empty: l.m.first_seen ? `${fMDHM(Date.parse(l.m.first_seen))} 加入` : undefined,
    tip: () => ({
      title: `${nameOf(l)}${l.m.model ? ` · ${l.m.model}` : ""}`,
      rows: [
        { color: "var(--lane-on)", value: rateText(l), label: "在线率（含停领）" }, { value: fSpan(l.offline), label: "离线合计" },
        { value: fSpan(l.paused), label: "停领合计" }, { value: fSpan(l.none), label: "无记录合计" },
      ],
      note: joinedNote(l),
    }),
    segs: l.segs.map((z, i) => ({
      key: `${i}`, s: z.s, e: z.e, color: STATE[z.state].color,
      tip: () => ({
        title: nameOf(l),
        rows: z.state === "none"
          ? [{ color: STATE.none.color, value: "无记录", label: NO_RECORD }, { value: `${fMDHM(z.s)} – ${until(z)}` }]
          : [
            { color: STATE[z.state].color, value: STATE[z.state].text, label: z.state === "paused" ? PAUSE_REASON_TEXT[z.reason] : "" },
            { value: `${fMDHM(z.s)} – ${until(z)}` }, { value: fSpan(z.e - z.s), label: "时长" },
          ],
      }),
    })),
  }));

  const chart = () => (
    <div>
      {noticeEl}
      <Legend items={(["online", "paused", "offline", "none"] as const).map((k) => ({
        label: STATE[k].text, color: STATE[k].color, icon: STATE[k].icon, iconColor: STATE[k].iconColor,
      }))} />
      <LaneChart start={R.start} end={R.end} lanes={chartLanes} label="设备在线时间轴"
        ticks={R.edges.slice(0, -1).map((t, i) => ({ t, label: bucketShort(R, i) }))} />
    </div>
  );

  const table = () => (
    <div>
      {noticeEl}
      <DataTable caption="在线率"
        cols={[{ label: "机器" }, { label: "在线率", num: true }, { label: "在线", num: true }, { label: "停领", num: true },
          { label: "离线", num: true }, { label: "无记录", num: true }]}
        rows={lanes.map((l) => [nameOf(l), rateText(l), fSpan(l.online), fSpan(l.paused), fSpan(l.offline), fSpan(l.none)])} />
      <div className="divider" />
      <DataTable caption="离线与停领记录"
        cols={[{ label: "机器" }, { label: "状态" }, { label: "开始" }, { label: "结束" }, { label: "时长", num: true }]}
        rows={lanes.flatMap((l) => l.segs.filter((z) => z.state === "paused" || z.state === "offline")
          .map((z) => [nameOf(l), stateText(z), fMDHM(z.s), until(z), fSpan(z.e - z.s)]))} />
    </div>
  );

  return <AnalyticsPanel id="uptime" title="设备在线" desc={DESC} chart={chart} table={table} />;
}
