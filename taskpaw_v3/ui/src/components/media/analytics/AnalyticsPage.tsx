// 数据分析 (EPIC 2026-10-31 §8): what the media pipeline has done — never progress against a total,
// a remaining count or a forecast (OD1). One query per range; machine / resolution / compare recompute
// locally over the cached response (D2). Filters live in page state only (D15). Sections follow the
// preview order and read the same `Analysis`.
import AutorenewIcon from "@mui/icons-material/Autorenew";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { GlobalStyles } from "@mui/material";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useState } from "react";
import { api, HttpError } from "../../../api";
import { Chip, Icon, Notice, PageTitle } from "../parts";
import { errorText } from "../workspace";
import { fHMS, fMD, fYMD } from "./analyticsFormat";
import {
  ALL_MACHINES, compute, controllerLabel, controllerOf, machineGroups, machineName, querySince, rangeDef, RANGES, RESOLUTIONS,
  type RangeKey, type ResFilter,
} from "./analyticsModel";
import { ANALYTICS_CSS } from "./analyticsStyles";
import { ChartTooltipProvider } from "./charts";
import { AlertsSection } from "./sections/AlertsSection";
import { ControllerSection } from "./sections/ControllerSection";
import { DurationsSection } from "./sections/DurationsSection";
import { EnergySection } from "./sections/EnergySection";
import { KpiRow } from "./sections/KpiRow";
import { MachinesSection } from "./sections/MachinesSection";
import { ProductionSection } from "./sections/ProductionSection";
import { SourcesSection } from "./sections/SourcesSection";
import { UptimeSection } from "./sections/UptimeSection";

// OD6 / D3: no global refetchInterval (#379); archives land ~16/day, so one cheap incremental read a
// minute keeps 「最后刷新」 honest. TanStack pauses it while the document is hidden and refetches on focus.
export const ANALYTICS_REFETCH_MS = 60_000;

function Seg<K extends string>({ name, options, value, onChange }: {
  name: string; options: ReadonlyArray<readonly [K, string]>; value: K; onChange: (k: K) => void;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={name}>
      {options.map(([k, label]) => (
        <button key={k} type="button" className="seg-opt" role="radio" aria-checked={value === k} onClick={() => onChange(k)}>{label}</button>
      ))}
    </div>
  );
}

export function AnalyticsPage() {
  const [range, setRange] = useState<RangeKey>("7d");
  const [machine, setMachine] = useState<string>(ALL_MACHINES);
  const [res, setRes] = useState<ResFilter>("all");
  const [compareOn, setCompareOn] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const compareId = useId();

  // `since` is the bucket-aligned start of the previous period, so the key is stable within a bucket
  const since = querySince(rangeDef(range, now, null));
  const query = useQuery({
    queryKey: ["mediaAnalytics", since ?? null],
    queryFn: () => api.workflowAnalytics(since),
    refetchInterval: ANALYTICS_REFETCH_MS,
    placeholderData: keepPreviousData,
    retry: false, // 409/503 are answers, not glitches; the interval asks again in a minute
  });
  const { data, isPlaceholderData, dataUpdatedAt, error } = query;
  useEffect(() => { if (dataUpdatedAt) setNow(Date.now()); }, [dataUpdatedAt]);
  // While a new range loads, the previous render stays (dimmed) with the range it was computed for.
  const [settled, setSettled] = useState<RangeKey>(range);
  useEffect(() => { if (data && !isPlaceholderData) setSettled(range); }, [data, isPlaceholderData, range]);
  const viewRange = isPlaceholderData ? settled : range;

  const cover = data?.coverage.archives ? Date.parse(data.coverage.archives) : null;
  const R = useMemo(() => rangeDef(viewRange, now, cover), [viewRange, now, cover]);
  const groups = useMemo(() => (data ? machineGroups(data, R, now) : []), [data, R, now]);
  const controller = data ? controllerOf(data) : null;
  const known = machine === ALL_MACHINES || machine === controller?.id || groups.some((g) => g.options.some((o) => o.id === machine));
  const machineNow = known ? machine : ALL_MACHINES;
  const hasPrev = range !== "all";
  const compare = compareOn && hasPrev && R.hasPrev;
  const A = useMemo(() => (data ? compute(data, R, { machine: machineNow, res, compare }, now) : null), [data, R, machineNow, res, compare, now]);

  const updated = dataUpdatedAt ? `${fMD(dataUpdatedAt)} ${fHMS(dataUpdatedAt)}` : "—";
  const head = (
    <PageTitle title="数据分析" summary={cover !== null ? `处理量、耗时、用电与设备健康 · 数据自 ${fYMD(cover)} 起` : null}
      actions={(
        <div className="refresh-meta">
          <span>最后刷新 <span className="mono">{updated}</span></span>
          <Chip tone="green"><Icon of={AutorenewIcon} />每分钟自动刷新</Chip>
        </div>
      )} />
  );

  // The header and the filter bar always render, so a range that fails (503, 409, other) can be left by
  // picking another one; a cached range shows its data at once. Only the panels give way to the notice.
  const filterBar = (
    <div className="filterbar" role="group" aria-label="筛选（作用于下方全部内容）">
      <div className="fgroup">
        <span>时段 · 过去</span>
        <Seg name="时段" options={RANGES} value={range} onChange={(k) => { setRange(k); if (k === "all") setCompareOn(false); }} />
      </div>
      <label className="fgroup">
        <span>机器</span>
        <select aria-label="机器" value={machineNow} onChange={(e) => setMachine(e.target.value)}>
          <option value={ALL_MACHINES}>全部 GPU 机器</option>
          {groups.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </optgroup>
          ))}
          {controller && <optgroup label="主控"><option value={controller.id}>{controllerLabel(controller)}</option></optgroup>}
        </select>
      </label>
      <div className="fgroup"><span>分辨率</span><Seg name="分辨率" options={RESOLUTIONS} value={res} onChange={setRes} /></div>
      <div className="switch-l">
        <button type="button" className="toggle" role="switch" aria-checked={compare} aria-labelledby={compareId}
          disabled={!hasPrev} title={hasPrev ? undefined : "「全部」没有上一周期"} onClick={() => setCompareOn((v) => !v)} />
        <span id={compareId}>对比上一周期</span>
      </div>
    </div>
  );

  let body;
  if (!data || !A) {
    const status = error instanceof HttpError ? error.status : null;
    body = query.isLoading ? <div className="panel empty"><p>正在读取数据分析…</p></div>
      : status === 503 ? <Notice icon={WarningAmberIcon}>媒体流水线未运行：{errorText(error)}</Notice>
        : status === 409 ? <Notice icon={WarningAmberIcon}>数据分析只在主控上</Notice>
          : <Notice tone="red" icon={WarningAmberIcon}>无法读取数据分析，请重试。{error ? `（${errorText(error)}）` : ""}</Notice>;
  } else {
    const resLabel = res === "all" ? "全部分辨率" : res === "4K" ? "4K" : "1080P";
    body = (
      <>
        {error && <Notice tone="amber" icon={WarningAmberIcon}>最近一次刷新失败，显示的是上次读取的数据。（{errorText(error)}）</Notice>}
        {A.uncov > 0 && A.cover !== null && (
          <Notice tone="amber" icon={WarningAmberIcon}>
            {`所选时段早于数据记录起点（${fMD(A.cover)}），只统计 ${A.coverDays} 天。图中灰色区域没有记录。`}
          </Notice>
        )}
        <div className={`an-sections${isPlaceholderData ? " busy" : ""}`}>
          <KpiRow data={data} A={A} />
          <ProductionSection A={A} res={res} compare={compare} />
          <MachinesSection data={data} A={A} res={res} onMachine={setMachine} />
          <ControllerSection data={data} A={A} />
          <DurationsSection A={A} />
          <EnergySection data={data} A={A} res={res} compare={compare} />
          <UptimeSection data={data} A={A} />
          <AlertsSection data={data} A={A} resetKey={`${viewRange}|${machineNow}|${res}`} />
          <SourcesSection data={data} A={A} />
        </div>
        <div className="sr-only" aria-live="polite">
          {`已更新：${R.name} · ${machineName(data, machineNow)} · ${resLabel} · 完成 ${A.cur.length} 部`}
        </div>
      </>
    );
  }

  return (
    <div className="an">
      <GlobalStyles styles={ANALYTICS_CSS} />
      <ChartTooltipProvider>
        {head}
        {filterBar}
        {body}
      </ChartTooltipProvider>
    </div>
  );
}
