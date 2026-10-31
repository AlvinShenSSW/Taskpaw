// 异常与报警 (EPIC 2026-10-31 §8.4 item 8, §7.2, §7.3, D5, D6, D16; C5 #392 AC3): counts by type (失败 red /
// 警告 amber, icon + text), failures per machine (失败 only), and one newest-first list of failures and
// warnings, offline spans and translation outages with their outcome. No CPU alerts and no CPU toggle (D6).
// A failure burst (≥ 3 machine faults on one machine within 1 h) always highlights its rows; the red notice
// shows only for a burst that ended in the last 24 h or still has a row that needs action (D16).
import AutorenewIcon from "@mui/icons-material/Autorenew";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import HourglassEmptyIcon from "@mui/icons-material/HourglassEmpty";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import WifiOffIcon from "@mui/icons-material/WifiOff";
import { useState } from "react";
import type { WorkflowAnalytics } from "../../../../api";
import { Chip, Icon, Notice } from "../../parts";
import { AnalyticsPanel, Block, DataTable } from "../AnalyticsParts";
import { fHM, fMD, fMDHM } from "../analyticsFormat";
import { alertFigures, machineName, type AlertEvent, type Analysis, type Burst, type OutcomeTone, type Severity } from "../analyticsModel";
import { HBarChart, type HBarRow } from "../charts";

const PAGE = 14;
const SEV = {
  fail: { text: "失败", color: "var(--st-red)", icon: ErrorOutlineIcon, iconColor: "var(--red)", tone: "red" },
  warn: { text: "警告", color: "var(--st-amber)", icon: WarningAmberIcon, iconColor: "var(--amber)", tone: "amber" },
} as const satisfies Record<Severity, unknown>;
const TONE_ICON: Record<OutcomeTone, typeof ErrorOutlineIcon> = {
  green: CheckCircleOutlineIcon, blue: AutorenewIcon, amber: WarningAmberIcon, red: WifiOffIcon, neutral: CheckCircleOutlineIcon,
};

function BurstNotice({ data, b }: { data: WorkflowAnalytics; b: Burst }) {
  const name = machineName(data, b.machine);
  const sameDay = fMD(b.start) === fMD(b.end);
  const when = sameDay ? `${fMD(b.start)} ${fHM(b.start)}–${fHM(b.end)}` : `${fMDHM(b.start)}–${fMDHM(b.end)}`;
  const moved = b.rows.filter((r) => r.outcome === "reassigned").length, needs = b.rows.filter((r) => r.outcome === "needs_action").length;
  const done = [moved ? `已自动改派 ${moved} 部` : null, needs ? `${needs} 部需要处理` : null].filter(Boolean).join("，");
  return (
    <Notice tone="red" icon={WarningAmberIcon}>
      <strong>{`${name} ${when} 出现 ${b.rows.length} 次「应用程序控制拦截」`}</strong>
      <div>{`${done ? `${done}。` : ""}需要在 ${name} 上检查应用程序控制设置，处理后在「设备」里恢复领取。`}</div>
    </Notice>
  );
}

/** `resetKey` names the filters: a new one starts the event list at 14 rows again, keeping the panel's view. */
export function AlertsSection({ data, A, resetKey }: { data: WorkflowAnalytics; A: Analysis; resetKey: string }) {
  const [page, setPage] = useState({ key: resetKey, shown: PAGE });
  const shown = page.key === resetKey ? page.shown : PAGE;
  const a = alertFigures(data, A);
  const desc = `失败（红）需要重试或改派，警告（琥珀）多为自动恢复 · ${A.R.name}失败 ${a.fails} 次、警告 ${a.warns} 次`
    + `${a.offline ? ` · 设备离线 ${a.offline} 次` : ""}${a.outages ? ` · 翻译服务中断 ${a.outages} 次` : ""}`;
  const banners = a.bursts.filter((b) => b.banner).map((b) => <BurstNotice key={`${b.machine}-${b.start}`} data={data} b={b} />);
  const typeRows: HBarRow[] = a.types.map((t) => {
    const s = SEV[t.severity];
    return {
      key: t.key, label: t.label, segs: [{ v: t.n, color: s.color }], value: String(t.n), icon: s.icon, iconColor: s.iconColor,
      tip: () => ({ title: t.label, rows: [{ color: s.color, value: `${t.n} 次`, label: s.text }], note: t.hint }),
    };
  });
  const machineRows: HBarRow[] = a.perMachine.map((m) => ({
    key: m.id, label: m.label, segs: [{ v: m.n, color: "var(--st-red)" }], value: String(m.n),
    tip: () => ({ title: m.label, rows: [{ color: "var(--st-red)", value: `${m.n} 次`, label: "失败" }] }),
  }));
  const list = a.events.slice(0, shown);
  const needs = a.events.filter((e) => e.outcome === "需要处理").length;
  const typeChip = (e: AlertEvent) => <Chip tone={SEV[e.severity].tone}><Icon of={SEV[e.severity].icon} />{e.label}</Chip>;
  const outcomeChip = (e: AlertEvent) => (
    <Chip tone={e.tone}><Icon of={e.outcome === "重试中" ? HourglassEmptyIcon : e.outcome === "记录中断" ? HelpOutlineIcon : TONE_ICON[e.tone]} />{e.outcome}</Chip>
  );
  const recent = () => (
    <Block title="最近的异常与报警" desc={`显示 ${list.length} 条，共 ${a.events.length} 条${needs ? ` · ${needs} 条需要处理` : ""}`}>
      <div className="table-wrap">
        <table className="grid">
          <caption className="sr-only">最近的异常与报警</caption>
          <thead><tr>{["时间", "机器", "类型", "影片", "处理结果", "说明"].map((l) => <th key={l} scope="col">{l}</th>)}</tr></thead>
          <tbody>
            {list.length ? list.map((e) => (
              <tr key={e.key} className={e.burst ? "hl" : undefined}>
                <td className="mono">{fMDHM(e.at)}</td>
                <td>{machineName(data, e.machine)}</td>
                <td>{typeChip(e)}</td>
                <td className="mono">{e.code ?? "—"}</td>
                <td>{outcomeChip(e)}</td>
                <td className="muted">{e.note}</td>
              </tr>
            )) : <tr><td colSpan={6} className="muted">这个时段没有异常</td></tr>}
          </tbody>
        </table>
      </div>
      {shown < a.events.length && (
        <button type="button" className="link-btn more" onClick={() => setPage({ key: resetKey, shown: shown + PAGE })}>显示更多</button>
      )}
    </Block>
  );

  const chart = () => (
    <div>
      {banners}
      <div className="grid2">
        <Block title="按类型">
          {typeRows.length ? <HBarChart rows={typeRows} labelWidth={180} valueWidth={44} label="按类型的异常次数" /> : <p className="small muted">这个时段没有异常</p>}
        </Block>
        <Block title={<><span className="t-red an-ic"><Icon of={ErrorOutlineIcon} /></span>各机器失败次数</>}>
          <HBarChart rows={machineRows} labelWidth={120} valueWidth={44} label="各机器失败次数" />
        </Block>
      </div>
      <div className="divider" />
      {recent()}
    </div>
  );

  const table = () => (
    <div>
      {banners}
      <DataTable caption="按类型" cols={[{ label: "类型" }, { label: "严重程度" }, { label: "次数", num: true }]}
        rows={a.types.map((t) => [t.label, SEV[t.severity].text, t.n])} />
      <div className="divider" />
      <DataTable caption="各机器失败次数" cols={[{ label: "机器" }, { label: "失败", num: true }]} rows={a.perMachine.map((m) => [m.label, m.n])} />
      <div className="divider" />
      {recent()}
    </div>
  );

  return <AnalyticsPanel id="alerts" title="异常与报警" desc={desc} chart={chart} table={table} />;
}
