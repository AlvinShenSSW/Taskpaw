// Panel building blocks of the 数据分析 page (EPIC 2026-10-31 §8.2): a panel with the 图表/表格
// toggle (every chart has a table view, §8.5), the data table, the KPI tile, the 「估算」 chip, the machine
// state chip, the small stat tile and the titled block.
import type { SvgIconComponent } from "@mui/icons-material";
import AddIcon from "@mui/icons-material/Add";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import BarChartIcon from "@mui/icons-material/BarChart";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import Inventory2OutlinedIcon from "@mui/icons-material/Inventory2Outlined";
import PauseCircleOutlineIcon from "@mui/icons-material/PauseCircleOutline";
import PowerOffIcon from "@mui/icons-material/PowerOff";
import RemoveIcon from "@mui/icons-material/Remove";
import TableRowsIcon from "@mui/icons-material/TableRows";
import WifiOffIcon from "@mui/icons-material/WifiOff";
import { useId, useState, type ReactNode } from "react";
import type { ChipTone } from "../mediaModel";
import { Chip, Icon } from "../parts";
import { MACHINE_STATE_TEXT, type Delta, type MachineStateKey } from "./analyticsModel";
import { Sparkline, type SparklineProps } from "./charts";

export type PanelView = "chart" | "table";

export function AnalyticsPanel({ id, title, desc, badges, actions, chart, table }: {
  id: string; title: string; desc: ReactNode; badges?: ReactNode; actions?: ReactNode;
  chart: () => ReactNode; table: () => ReactNode;
}) {
  const [view, setView] = useState<PanelView>("chart");
  const heading = `an-h-${id}`;
  return (
    <section className="panel an-panel" id={`an-sec-${id}`} aria-labelledby={heading}>
      <div className="panel-head">
        <div>
          <h2 id={heading}>{title}{badges}</h2>
          <p>{desc}</p>
        </div>
        <div className="ph-actions">
          {actions}
          <div className="seg an-vt" role="radiogroup" aria-label={`${title} 显示方式`}>
            {([["chart", "图表", BarChartIcon], ["table", "表格", TableRowsIcon]] as const).map(([key, label, icon]) => (
              <button key={key} type="button" className="seg-opt" role="radio" aria-checked={view === key} onClick={() => setView(key)}>
                <Icon of={icon} />{label}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="panel-body">{view === "chart" ? chart() : table()}</div>
    </section>
  );
}

export interface Column { label: string; num?: boolean; tag?: ReactNode } // tag: e.g. 「估算」 beside the header
export function DataTable({ cols, rows, caption }: { cols: Column[]; rows: ReactNode[][]; caption: string }) {
  return (
    <div className="table-wrap">
      <table className="grid fit">
        <caption className="sr-only">{caption}</caption>
        <thead><tr>{cols.map((c) => <th key={c.label} scope="col" className={c.num ? "num" : undefined}>{c.label}{c.tag}</th>)}</tr></thead>
        <tbody>
          {rows.length
            ? rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={cols[j]?.num ? "num" : undefined}>{v}</td>)}</tr>)
            : <tr><td colSpan={cols.length} className="muted">这个时段没有数据</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** A chart legend: colour keys with text (identity is never colour alone). */
export function Legend({ items }: { items: Array<{ label: string; color: string; kind?: "rect" | "line" | "tick" }> }) {
  return (
    <div className="lgd">
      {items.map((it) => (
        <span className="lgd-i" key={it.label}><span className={`lgd-k ${it.kind ?? "rect"}`} style={{ background: it.color }} />{it.label}</span>
      ))}
    </div>
  );
}

export function EstimateChip() {
  return <span className="chip tag" title="用电为估算值：按运行期间的功率采样计算">估算</span>;
}

const DELTA_ICON = { up: ArrowUpwardIcon, down: ArrowDownwardIcon, flat: RemoveIcon } as const;
export function DeltaLine({ delta }: { delta: Delta }) {
  return (
    <div className={`delta ${delta.tone}`}>
      <Icon of={DELTA_ICON[delta.dir]} /><span>{delta.text}</span>{delta.vs && <span className="vs">较上一周期</span>}
    </div>
  );
}

/** A KPI tile: label (+ tag), value + unit, delta (or a custom line), note, optional sparkline. */
export function KpiTile({ label, tag, value, unit, delta, note, spark }: {
  label: string; tag?: ReactNode; value: string; unit?: string; delta?: Delta | ReactNode; note: ReactNode; spark?: SparklineProps | null;
}) {
  const labelId = useId();
  const isDelta = (d: unknown): d is Delta => typeof d === "object" && d !== null && "tone" in d && "text" in d;
  return (
    <div className="panel kpi" role="group" aria-labelledby={labelId}>
      <div className="label" id={labelId}>{label}{tag}</div>
      <strong className="kv">{value}{unit && <em>{unit}</em>}</strong>
      {isDelta(delta) ? <DeltaLine delta={delta} /> : delta}
      <div className="note">{note}</div>
      {spark && <Sparkline {...spark} />}
    </div>
  );
}

const MACHINE_STATE: Record<MachineStateKey, { tone: ChipTone; icon: SvgIconComponent }> = {
  online: { tone: "green", icon: CheckCircleOutlineIcon },
  paused: { tone: "amber", icon: PauseCircleOutlineIcon },
  offline: { tone: "red", icon: WifiOffIcon },
  new: { tone: "blue", icon: AddIcon },
  retired: { tone: "neutral", icon: Inventory2OutlinedIcon },
  unknown: { tone: "neutral", icon: HelpOutlineIcon },
  off: { tone: "neutral", icon: PowerOffIcon },
};
/** 在线 / 停领 / 离线 / 新加入 / 已退休 (+ 确认中 / 未启用): text and icon, never colour alone (OD3). */
export function MachineStateChip({ state }: { state: MachineStateKey }) {
  const s = MACHINE_STATE[state];
  return <Chip tone={s.tone}><Icon of={s.icon} />{MACHINE_STATE_TEXT[state]}</Chip>;
}

/** A small stat tile: label, value + unit, a note line and optional extras (chips). */
export function MiniStat({ label, value, unit, note, children }: { label: string; value: string; unit?: string; note: ReactNode; children?: ReactNode }) {
  const labelId = useId();
  return (
    <div className="mstat" role="group" aria-labelledby={labelId}>
      <div className="label" id={labelId}>{label}</div>
      <strong>{value}{unit && <em>{unit}</em>}</strong>
      <div className="note">{note}</div>
      {children}
    </div>
  );
}

/** A titled block inside a panel: heading, optional description, content. */
export function Block({ title, desc, children }: { title: ReactNode; desc?: ReactNode; children?: ReactNode }) {
  return (
    <div className="block">
      <h3>{title}</h3>
      {desc && <p className="bdesc">{desc}</p>}
      {children}
    </div>
  );
}
