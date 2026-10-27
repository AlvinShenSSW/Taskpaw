// #336: one machine's sensor line (approved preview power-sensors-preview.html). The age is the
// controller's `sensors_age_s` plus the seconds since this browser received it; no wall clock is compared.
// #345: without LHM the line ends with a link that opens the install guide as the row's next grid item.
import { useState } from "react";
import type { MachineSensors } from "../../api";
import { LhmGuide } from "./LhmGuide";

const STALE_S = 30;
const SOURCE = { lhm: "LHM", "nvidia-smi": "nvidia-smi" } as const;

export function SensorLine({ machine, sensors, ageS, receivedMs, nowMs, failed }: {
  machine: string; sensors: MachineSensors | null | undefined; ageS: number | null | undefined;
  receivedMs: number | undefined; nowMs: number; failed: boolean;
}) {
  const [guide, setGuide] = useState(false);
  if (!sensors) return <div className="m-sensors"><span className="unavailable">传感器未提供 · <b>需升级 TaskPaw</b></span></div>;
  const age = ageS == null || receivedMs === undefined ? null : Math.round(ageS + Math.max(0, nowMs - receivedMs) / 1000);
  const stale = failed || (age !== null && age > STALE_S);
  const when = age === null ? "采样时间未知" : `${age} 秒前`;
  const temp = (label: string, c: number | null) => c !== null && <span className="sensor">{label} <b>{Math.round(c)}<small>°C</small></b></span>;
  const temps = sensors.cpu_c !== null || sensors.gpu_c !== null;
  const noLhm = sensors.source !== "lhm";
  const rest = noLhm || sensors.fans.length > 0;
  return (<>
    <div className={`m-sensors${stale ? " stale" : ""}`}>
      {temp("CPU", sensors.cpu_c)}{temp("GPU", sensors.gpu_c)}
      {temps && rest && <span className="sep">·</span>}
      {sensors.source === "lhm" ? sensors.fans.length > 0 && <span className="fans">风扇
        {sensors.fans.map((f, i) => <span key={`${i}-${f.name}`} className="fan">
          {i > 0 && <span className="sep">/</span>}{f.name} <b>{Math.round(f.rpm)}</b><small>RPM</small></span>)}
      </span> : <span className="unavailable">CPU / 风扇：<b>LHM 未运行</b></span>}
      {noLhm && <button type="button" className="guide-link" aria-expanded={guide} onClick={() => setGuide(!guide)}>
        {guide ? "收起安装步骤" : "安装步骤"}</button>}
      <span className="meta">{stale ? `已过期 · ${age === null ? when : `最后读数 ${when}`}`
        : [sensors.source && SOURCE[sensors.source], when].filter(Boolean).join(" · ")}</span>
    </div>
    {noLhm && guide && <LhmGuide machine={machine} />}
  </>);
}
