import type { MachineTemperatures, TemperatureSensor } from "../../api";
import { temperatureAge, TEMPERATURE_STALE_S } from "./temperatureView";

const REASON: Record<string, string> = {
  pending: "正在读取传感器", unsupported_platform: "当前平台不支持", provider_missing: "未找到温度读取器",
  permission_denied: "读取权限不足", timeout: "读取超时", query_failed: "读取失败", invalid_reading: "传感器读数无效",
  no_sensor: "没有可读传感器", no_reader: "当前未提供传感器读数",
};
const KINDS = ["cpu", "gpu", "board"] as const;
const DEFAULT_LABEL = { cpu: "CPU", gpu: "NVIDIA GPU 核心最高", board: "主板 / 机箱" };
const WIRE_LABEL = { cpu: "CPU", gpu: "NVIDIA GPU core (highest readable)", board: "Board / chassis" };
const isCelsius = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v >= -50 && v <= 200;
const sensorLabel = (kind: typeof KINDS[number], sensor: TemperatureSensor | undefined) =>
  typeof sensor?.label === "string" && sensor.label.trim() && sensor.label !== WIRE_LABEL[kind]
    ? sensor.label : DEFAULT_LABEL[kind];

export function TemperatureStrip({ value, cacheS, failed }: {
  value: MachineTemperatures | null | undefined; cacheS: number | null; failed: boolean;
}) {
  if (!value) return <div className="m-temperatures" aria-label="当前温度"><span className="temperature-empty">温度未提供 · 需升级 TaskPaw</span></div>;
  const age = temperatureAge(value, cacheS);
  const stale = failed || value.stale !== false || age === null || age >= TEMPERATURE_STALE_S
    || typeof value.sampled_at !== "number" || !Number.isFinite(value.sampled_at) || value.sampled_at < 0;
  const gpu = value.gpu;
  const coverage = Number.isInteger(gpu?.readable) && Number.isInteger(gpu?.total)
    && gpu.readable >= 0 && gpu.total >= gpu.readable;
  return (
    <div className="m-temperatures" aria-label="当前温度">
      {KINDS.map((kind) => {
        const sensor = value[kind];
        const readable = isCelsius(sensor?.celsius) && typeof sensor?.source === "string" && sensor.source.trim().length > 0
          && !sensor?.reason && (kind !== "gpu" || (coverage && gpu.readable > 0));
        const partial = kind === "gpu" && coverage && gpu.readable > 0 && gpu.readable < gpu.total;
        const reason = readable ? "" : REASON[sensor?.reason ?? "invalid_reading"] ?? "传感器不可用";
        return (
          <div key={kind} className={`temperature-sensor${readable && stale ? " stale" : ""}`} data-sensor={kind}>
            <span className="temperature-label">{sensorLabel(kind, sensor)}{partial && "（可读部分）"}</span>
            <span className={`temperature-reading${!readable ? " unavailable" : ""}`}>
              {readable ? `${Number(sensor.celsius!.toFixed(1))} °C${stale ? " · 已过期" : ""}` : "—"}
            </span>
            <span className="temperature-detail">
              {!readable && `不可用 · ${reason}`}
              {readable && stale && "最后读数 · "}
              {kind === "gpu" && coverage && gpu.total > 0 && `${!readable ? " · " : ""}${gpu.readable} / ${gpu.total} NVIDIA GPU 可读${readable ? " · " : ""}`}
              {readable && typeof sensor.source === "string" && `${sensor.source} · `}
              {readable && (failed ? "连接中断 · " : "")}
              {readable && (age === null ? "采样时间未知" : `${Math.floor(age)} 秒前`)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
