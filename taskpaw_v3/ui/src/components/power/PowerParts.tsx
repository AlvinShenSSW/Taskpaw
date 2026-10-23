// PWR-2: the power additions inside 媒体处理中心 (approved preview media-workflow-v3-power.html): the
// GPU / CPU / 其他 split, the drawer's live row and 能耗 block, and 待处理's 预计耗电 line.
import BoltIcon from "@mui/icons-material/Bolt";
import type { FilmEnergy, LivePower, LtxTier, PlanPrediction } from "../../api";
import { Icon } from "../media/parts";
import {
  clock, clockHM, ENERGY_STAGES, fmtKwh, fmtKwhShort, fmtW, restoreShare, splitOf, TIER_SHORT_ALL, type Split,
} from "./powerModel";

export const Est = () => <span className="pw-est">估算</span>;
export const Bolt = () => <Icon of={BoltIcon} />;

export function SplitBar({ split }: { split: Split | null }) {
  if (!split || split.total <= 0) return <div className="pw-bar" aria-hidden="true" />;
  const pct = (v: number) => `${(v / split.total) * 100}%`;
  return (
    <div className="pw-bar" aria-hidden="true">
      <span className="g" style={{ width: pct(split.gpu) }} /><span className="c" style={{ width: pct(split.cpu) }} />
      <span className="o" style={{ width: pct(split.other) }} />
    </div>
  );
}

export function SplitLegend({ split, noCpu = false }: { split: Split | null; noCpu?: boolean }) {
  if (!split) return null;
  return (
    <span className="pw-legend">
      <span><i className="g" />GPU <span className="mono">{split.noGpu ? "—" : fmtW(split.gpu)}</span></span>
      <span><i className="c" />CPU <span className="mono">{noCpu ? "—" : fmtW(split.cpu)}</span></span>
      <span><i className="o" />其他 <span className="mono">{fmtW(split.other)}</span></span>
    </span>
  );
}

// The running GPU step: 「本机 286 W（GPU 210 · CPU 46 · 其他 30）· 估算」 (PW12: GPU-owning film only).
export function LivePowerRow({ power, label = "本机" }: { power: LivePower | null | undefined; label?: string }) {
  const split = splitOf(power);
  if (!power || !split) return null;
  return (
    <div className="pw-live" data-testid="pw-live">
      <span className="pw-now">
        <Bolt />
        <span>{label} <strong>{fmtW(power.w)} W</strong>（GPU {split.noGpu ? "—" : fmtW(split.gpu)} · CPU {fmtW(split.cpu)} · 其他 {fmtW(split.other)}）</span>
        <SplitBar split={split} /><Est />
      </span>
    </div>
  );
}

// 「预计耗电：标准 ~1.4 度 · 良好 ~2.3 · ▸高 ~5.3（按本机历史）」; 「历史不足」 below three films (PW13).
export function PredictLine({ prediction, tier, needsGpu, id }: {
  prediction: PlanPrediction | null | undefined; tier: LtxTier | null; needsGpu: boolean; id: string;
}) {
  if (!prediction) return null;
  if (!needsGpu) {
    const v = prediction.none;
    return (
      <p className="pw-predict" id={id}>
        预计耗电：{v == null ? "历史不足" : `约 ${fmtKwh(v)} 度`}（仅识别与翻译 · 估算）
      </p>
    );
  }
  const tiers: LtxTier[] = ["standard", "good", "high"];
  return (
    <p className="pw-predict" id={id}>
      预计耗电：
      {tiers.map((t, i) => {
        const v = prediction[t];
        const on = tier === t;
        return (
          <span key={t}>
            {i > 0 && " · "}
            <span className={`t${on ? " on" : ""}`} data-t={t}>
              {TIER_SHORT_ALL[t]} {v == null ? "历史不足" : `~${fmtKwhShort(v)}${i === 0 ? " 度" : ""}`}
              {on && <span className="sr-only">（已选）</span>}
            </span>
          </span>
        );
      })}
      （按本机历史）
    </p>
  );
}

const NOTE = "GPU 阶段（修复、识别）独占整机，按整机估算全额计入；翻译、回传与刮削计入其运行期间的整机估算。整机功耗 = GPU 实测（nvidia-smi）+ CPU（按占用估算）+ 其他基线。整机实测请以智能插座为准。";

// The drawer's 「能耗」 block: the total, each stage's energy with 用时 / 平均 / 峰值 / 设备 (PWR-2).
// `predicted`: the whole run on this machine's history (Wh), shown while the film is still running.
export function EnergyBlock({ filmId, energy, machine, predicted, runSeconds }: {
  filmId: string; energy: FilmEnergy; machine: string; predicted: number | null; runSeconds: number | null;
}) {
  const tid = `energy-title-${filmId}`;
  const rows = ENERGY_STAGES.filter(([key]) => energy.stages[key] || ["restore", "asr", "translate"].includes(key));
  const max = Math.max(...rows.map(([key]) => energy.stages[key]?.wh ?? 0), 0.0001);
  const share = restoreShare(energy);
  const done = !energy.running;
  const multi = Object.values(energy.stages).some((s) => s?.devices?.some((d) => d.name !== "本机"));
  return (
    <section className="panel energy-panel spacer-top" aria-labelledby={tid} data-testid="film-energy">
      <div className="energy-head">
        <div><h3 id={tid}>能耗</h3><p>估算 · 每阶段 = 其运行期间{multi ? "所在机器" : "本机"}的估算耗电</p></div>
        <div className="energy-total">
          <span className="lab">{done ? "本片约" : "已用约"}</span><strong>{fmtKwh(energy.total_wh)}</strong><span className="u">度</span>
          <span className="sub">
            {done
              ? [runSeconds != null ? `全程 ${clockHM(runSeconds)}` : null, share != null ? `修复占 ${share}%` : null,
                energy.partial ? "部分（有尝试被中断，未计入）" : null].filter(Boolean).join(" · ")
              : predicted != null ? `预计全程约 ${fmtKwhShort(predicted)} 度（按本机历史）` : "进行中"}
          </span>
        </div>
      </div>
      <ul className="energy-rows">
        {rows.map(([key, label, note]) => {
          const s = energy.stages[key];
          if (!s) {
            return (
              <li key={key} className="energy-row todo">
                <span className="name">{label}<small>{note}</small></span><span className="kwh">—</span>
                <span className="sub"><span>未开始</span></span>
              </li>
            );
          }
          return (
            <li key={key} className={`energy-row${s.running ? " run" : ""}`} data-energy={key}>
              <span className="name">{label}<small>{note}</small>
                {s.running && <span className="tag">进行中</span>}
                {s.partial && <span className="tag amber">部分</span>}
                {s.attempts > 1 && <small>{s.attempts} 次尝试</small>}
              </span>
              <span className="kwh">{fmtKwh(s.wh)} 度</span>
              <span className="sub">
                <span>用时 <span className="mono">{clock(s.seconds)}</span></span>
                <span>平均 <span className="mono">{fmtW(s.avg_w)} W</span></span>
                <span>峰值 <span className="mono">{fmtW(s.peak_w)} W</span></span>
                {/* P2-4 R10: the machines the stage's attempts ran on */}
                <span data-testid="energy-devices">{s.devices?.length ? s.devices.map((d) => (d.name === "本机" ? machine : d.name)).join("、") : machine}</span>
              </span>
              <span className="share" aria-hidden="true"><span style={{ width: `${(s.wh / max) * 100}%` }} /></span>
            </li>
          );
        })}
      </ul>
      <p className="energy-note">{NOTE}</p>
    </section>
  );
}

// The drawer of a 待处理 film: the prediction per tier on the recommended machine.
export function PredictBlock({ filmId, prediction, tier, needsGpu, machine }: {
  filmId: string; prediction: PlanPrediction; tier: LtxTier | null; needsGpu: boolean; machine: string;
}) {
  const tid = `energy-title-${filmId}`;
  const chosen = needsGpu ? prediction[tier ?? "standard"] : prediction.none;
  const tiers: LtxTier[] = ["standard", "good", "high"];
  return (
    <section className="panel energy-panel spacer-top" aria-labelledby={tid} data-testid="film-energy">
      <div className="energy-head">
        <div><h3 id={tid}>能耗</h3><p>尚未开始 · 按本机历史预计（每片长小时的平均耗电）</p></div>
        <div className="energy-total">
          <span className="lab">预计约</span><strong>{chosen == null ? "—" : fmtKwhShort(chosen)}</strong><span className="u">度</span>
          <span className="sub">{chosen == null ? "历史不足" : needsGpu ? `${TIER_SHORT_ALL[tier ?? "standard"]}画质 · ${machine}` : "无需修复 · 仅识别与翻译"}</span>
        </div>
      </div>
      {needsGpu && (
        <div className="energy-tiers">
          {tiers.map((t) => {
            const v = prediction[t];
            return (
              <div key={t} className={t === tier ? "on" : ""}>
                {TIER_SHORT_ALL[t]}{t === tier ? "（已选）" : ""}
                {v == null ? <strong className="none">历史不足</strong> : <strong>~{fmtKwhShort(v)} 度</strong>}
              </div>
            );
          })}
        </div>
      )}
      <p className="energy-note">至少 {prediction.min_films} 部同分辨率、同画质的完整影片后才给出预计。</p>
    </section>
  );
}
