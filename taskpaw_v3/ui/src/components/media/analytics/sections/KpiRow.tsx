// KPI row (EPIC 2026-10-31 §8.4 item 1): 完成影片 · 平均全程时长 · 平均修复时长 · 总用电「估算」 · 失败率 ·
// GPU 在线 N / M. Every tile shows its delta vs the previous period, or the preview's flat text when there
// is none to compare (approved preview `dlt`); 对比上一周期 only adds the chart overlays.
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import type { AnalyticsMachine, AnalyticsMachineState, WorkflowAnalytics } from "../../../../api";
import { Icon } from "../../parts";
import { EstimateChip, KpiTile } from "../AnalyticsParts";
import { fH, fKwh, HOUR, n1, fInt } from "../analyticsFormat";
import {
  amax, amin, avg, delta, energyReading, failureRate, failureTime, filmsIn, isFail, isNewMachine, kpiFigures, leadHours, recordedPresence,
  restoreHours, sliceLabels, sliceValues, type Analysis,
} from "../analyticsModel";

const STATE_TEXT: Record<AnalyticsMachineState, string> = { online: "在线", paused: "停领", offline: "离线", unknown: "确认中", off: "未启用" };

function FlatLine({ icon, text }: { icon: typeof InfoOutlinedIcon; text: string }) {
  return <div className="delta flat"><Icon of={icon} /><span>{text}</span></div>;
}

export function KpiRow({ data, A }: { data: WorkflowAnalytics; A: Analysis }) {
  const k = kpiFigures(A);
  const labels = sliceLabels(A);
  const inSlice = (s: number, e: number) => filmsIn(A.cur, s, e);
  const d = (cur: number | null, prev: number | null, good: "up" | "down" | "none", pp = false) => delta(A, cur, prev, good, pp);
  const spark = (fn: (s: number, e: number) => number | null, fmt: (v: number) => string) => ({ vals: sliceValues(A, fn), labels, fmt });
  const leadAvg = avg(k.lead), restoreAvg = avg(k.restore);
  const fails = A.failures.filter(isFail);
  const incomplete = k.energyMissing + k.energyPartial; // films without, or with only part of, a reading
  return (
    <div className="kpis">
      <KpiTile label="完成影片" value={fInt(k.films)} unit="部" delta={d(k.films, k.prevFilms, "up")}
        note={`4K ${k.films4k} · 1080P ${k.films1080}`} spark={spark((s, e) => inSlice(s, e).length, (v) => `${v} 部`)} />
      <KpiTile label="平均全程时长" value={n1(leadAvg)} unit="小时" delta={d(leadAvg, k.prevLeadAvg, "down")}
        note={`入库→归档 · 最短 ${fH(amin(k.lead))} · 最长 ${fH(amax(k.lead))}`}
        spark={spark((s, e) => avg(leadHours(inSlice(s, e))), fH)} />
      {A.ctrl && !k.ctrlRestored
        ? <KpiTile label="平均修复时长" value="—" delta={<FlatLine icon={InfoOutlinedIcon} text="本机不做修复" />} note="修复只在 GPU 机器上运行" />
        : <KpiTile label="平均修复时长" value={n1(restoreAvg)} unit="小时" delta={d(restoreAvg, k.prevRestoreAvg, "down")}
          note={`4K ${fH(avg(k.restore4k))} · 1080P ${fH(avg(k.restore1080))}`}
          spark={spark((s, e) => avg(restoreHours(A, inSlice(s, e))), fH)} />}
      <KpiTile label="总用电" tag={<><EstimateChip />{incomplete > 0 && k.energyMeasured > 0 && <span className="chip tag amber" title="部分影片没有或缺少用电记录，只合计已有的记录">部分</span>}</>}
        value={k.energyMeasured ? fKwh(k.energy) : "—"} unit={k.energyMeasured ? "kWh" : undefined}
        delta={d(k.energyMeasured ? k.energy : null, k.prevEnergy, "none")}
        note={[k.energyMeasured ? `每片平均 ${fKwh(k.energyPerFilm)} kWh` : null,
          k.energyPartial ? `${incomplete} 部用电记录不全` : k.energyMissing ? `${k.energyMissing} 部无用电记录` : null].filter(Boolean).join(" · ")}
        spark={spark((s, e) => energyReading(inSlice(s, e), A.ctrl).kwh, (v) => `${fKwh(v)} kWh`)} />
      <KpiTile label="失败率" value={k.failRate === null ? "—" : n1(k.failRate)} unit={k.failRate === null ? undefined : "%"}
        delta={d(k.failRate, k.prevFailRate, "down", true)} note={`失败 ${k.fails} 次 · 警告 ${k.warns} 次`}
        spark={spark((s, e) => failureRate(fails.filter((f) => failureTime(f) >= s && failureTime(f) < e).length, inSlice(s, e).length),
          (v) => `${n1(v)}%`)} />
      <GpuTile data={data} A={A} />
    </div>
  );
}

function GpuTile({ data, A }: { data: WorkflowAnalytics; A: Analysis }) {
  if (A.ctrl) {
    const c = A.controller!;
    return (
      <KpiTile label="本机状态" value={STATE_TEXT[c.state_now]}
        delta={<FlatLine icon={CheckCircleOutlineIcon} text={c.state_now === "online" ? "主控正常" : c.state_now === "paused" ? "新领取已暂停" : STATE_TEXT[c.state_now]} />}
        note={`主控 · ${c.model ?? "型号未知"}`} />
    );
  }
  const live = A.fleet;
  const present = live.filter((m) => m.state_now === "online" || m.state_now === "paused");
  const available = live.filter((m) => m.state_now === "online");
  const hoursSince = (m: AnalyticsMachine) => (m.state_since ? ` ${Math.max(1, Math.round((A.now - Date.parse(m.state_since)) / HOUR))} 小时` : "");
  const notOnline = live.filter((m) => m.state_now !== "online").map((m) => `${m.name} ${STATE_TEXT[m.state_now]}${hoursSince(m)}`);
  const joined = live.filter((m) => isNewMachine(m, A.R, A.now)).map((m) => m.name);
  const note = notOnline.join(" · ") || (joined.length ? `${joined.join("、")} 新加入` : "全部在线");
  // the spark counts machines a device span shows online or 停领, only where device history covers
  const devices = data.coverage.devices ? Date.parse(data.coverage.devices) : null;
  const ids = new Set(live.map((m) => m.id));
  // unrecorded instants are null (无记录), never 0; the latest point is the live snapshot (state_now),
  // since the last recorded tick is always a little older than the page's clock
  const vals = A.slices.map(([, e], i) => (i === A.slices.length - 1 ? present.length
    : !data.devices.available || devices === null || e <= devices ? null : recordedPresence(data.devices.spans, ids, e - 1)));
  return (
    <KpiTile label="GPU 在线" value={live.length === 1 ? STATE_TEXT[live[0].state_now] : `${present.length} / ${live.length}`}
      delta={<FlatLine icon={CheckCircleOutlineIcon} text={`可领取 ${available.length} 台`} />} note={note}
      spark={{ vals, labels: sliceLabels(A), fmt: (v) => `${v} 台在线` }} />
  );
}
