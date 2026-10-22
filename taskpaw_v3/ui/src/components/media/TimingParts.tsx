// U1e: how long a film took. 「LTX 合计用时」 reads like the LtxProgress metrics (shared progress
// parts); 「用时」 lists every finished stage, the 修复 breakdown and 修复→翻译.
import { Box, Typography } from "@mui/material";
import type { FilmTiming, RestoreTiming } from "../../api";
import { DATA, ProgressFrame } from "../progressParts";
import { fmtDuration, hasLtxTime, ltxStageTimes, TIMING_STAGES } from "./mediaModel";

function TimeCell({ label, sub, value, testId }: { label: string; sub?: string; value: number; testId?: string }) {
  return (
    <Box data-timing={testId} sx={{ minWidth: 0, px: 1.5, py: 1, border: "1px solid", borderColor: "divider", borderRadius: 1.5,
      bgcolor: "background.default" }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", overflowWrap: "anywhere" }}>
        <Box component="span" sx={DATA}>{label}</Box>{sub && <> · {sub}</>}
      </Typography>
      <Typography sx={{ ...DATA, mt: 0.25, fontSize: 17, lineHeight: 1.4 }}>{fmtDuration(value)}</Typography>
    </Box>
  );
}

// 「LTX 合计用时 h:mm:ss」 with the four stage times and 测速 when one ran: live on the running 修复
// step, from the restore's breakdown once it finished.
export function LtxTimingCard({ timing, live = false }: { timing: RestoreTiming | null; live?: boolean }) {
  if (!hasLtxTime(timing)) return null;
  const runs = timing.ltx_runs > 1 ? `${timing.ltx_runs} 段 LTX` : null;
  return (
    <ProgressFrame testId="ltx-timing" label="LTX 合计用时"
      title={<>LTX 合计用时 <Box component="span" sx={{ ...DATA, ml: 1, color: "success.light" }}>{fmtDuration(timing.ltx)}</Box></>}
      position={[live ? "至今" : null, runs].filter(Boolean).join(" · ") || undefined}>
      <Box sx={{ px: { xs: 2, sm: 3 }, py: 2, display: "grid", gap: 1,
        gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(auto-fit, minmax(118px, 1fr))" } }}>
        {ltxStageTimes(timing).map(([name, zh, value]) => <TimeCell key={name} label={name} sub={zh || undefined} value={value} testId={name} />)}
        {timing.speed_test > 0 && <TimeCell label="测速" value={timing.speed_test} testId="speed_test" />}
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: { xs: 2, sm: 3 }, pb: 2 }}>
        Taskpaw 看到每个阶段为当前阶段的时间，累计所有 LTX 段与分卷；合计含测速。
      </Typography>
    </ProgressFrame>
  );
}

// 「用时」: every finished stage's time, 修复's breakdown, 修复→翻译 and, apart, the queue wait.
export function TimingBlock({ timing }: { timing: FilmTiming | null }) {
  if (!timing) return null;
  const rows = TIMING_STAGES.filter(([key]) => timing.stages[key] !== undefined);
  const b = timing.restore_breakdown;
  if (!rows.length && timing.queue_wait === null) return null;
  const parts = b ? [
    b.scan > 0 ? `扫描马赛克 ${fmtDuration(b.scan)}` : null,
    b.basicvsr > 0 ? `BasicVSR++ ${fmtDuration(b.basicvsr)}` : null,
    b.ltx > 0 ? `LTX ${fmtDuration(b.ltx)}` : null,
  ].filter(Boolean).join(" · ") : "";
  return (
    <section className="panel timing-panel spacer-top" aria-label="用时" data-testid="film-timing">
      <h3>用时</h3>
      <dl className="facts spacer-top">
        {rows.flatMap(([key, label]) => [
          <dt key={`${key}-t`}>{label}</dt>,
          <dd key={`${key}-d`}>
            <span className="mono">{fmtDuration(timing.stages[key])}</span>
            {key === "restore" && parts && <span className="muted small timing-sub">{parts}</span>}
          </dd>,
        ])}
        {timing.total_restore_to_translate !== null && (
          <><dt>修复→翻译合计</dt><dd><strong className="mono t-green">{fmtDuration(timing.total_restore_to_translate)}</strong></dd></>
        )}
        {timing.queue_wait !== null && (
          <><dt>排队等待</dt><dd><span className="mono">{fmtDuration(timing.queue_wait)}</span><span className="muted small timing-sub">确认安排到开始修复，不计入合计</span></dd></>
        )}
      </dl>
      {hasLtxTime(b) && <div className="spacer-top"><LtxTimingCard timing={b} /></div>}
    </section>
  );
}
