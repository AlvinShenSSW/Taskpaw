import { Fragment } from "react";
import { Box, Chip, LinearProgress, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import HourglassEmptyIcon from "@mui/icons-material/HourglassEmpty";
import MemoryIcon from "@mui/icons-material/Memory";
import RemoveIcon from "@mui/icons-material/Remove";
import ScheduleIcon from "@mui/icons-material/Schedule";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { TINT } from "./monitorMetrics.helpers";
import {
  BOX, MONO, SLATE_WASH, type Pipeline, type Step, type StepState,
  clock, durationText, etaText, fin, focusStep, isTerminal, stepIndex, stepLabel, str,
} from "./pipelineProgress.helpers";
import { Tile } from "./Tile";
import { FilmList } from "./FilmList";
import { PagedFilmList } from "./PagedFilmList";
import { RunFilmsCard } from "./RunFilmsCard";
import { type Tone, toneSx } from "./filmList.helpers";

// #189 AV 翻译 progress: one film's 修复 → 识别 → 翻译 (Jasna) or 识别 → 翻译
// (avsubs) stepper, the active step's own bar + tiles, the queue card and the
// batch list. MonitorMetrics renders it INSTEAD of the now-processing banner, the
// queue bar and the fps/ETA tiles whenever `metrics.steps` is usable (D13).
// Design: docs/specs/2026-09-25-189-progress-redesign-design.md → "UI (D8/D13)";
// every state is spelled out in text next to its icon (never colour alone).

const SLATE_TRACK = "rgba(148,163,184,0.15)";

type Metrics = Record<string, unknown>;

// ── stepper ─────────────────────────────────────────────────────────────────
function StepDot({ state, index }: { state: StepState; index: number }) {
  const base = {
    width: 30, height: 30, borderRadius: "50%", boxSizing: "border-box", flex: "0 0 auto",
    display: "flex", alignItems: "center", justifyContent: "center",
    transition: "all 200ms ease",
  } as const;
  const ring = (color: string, style = "solid") => ({ ...base, border: `2px ${style} ${color}` });
  switch (state) {
    case "done":
      return (
        <Box aria-hidden sx={{ ...base, bgcolor: TINT.ok }}>
          <CheckIcon data-testid="step-done-icon" sx={{ fontSize: 18, color: "background.default" }} />
        </Box>
      );
    case "failed":
      return (
        <Box aria-hidden sx={{ ...base, bgcolor: TINT.crit }}>
          <CloseIcon data-testid="step-failed-icon" sx={{ fontSize: 18, color: "background.default" }} />
        </Box>
      );
    case "active":
      return (
        <Box aria-hidden data-testid="step-active-icon"
          sx={{ ...ring(TINT.ok), boxShadow: `0 0 10px ${alpha(TINT.ok, 0.45)}` }}>
          <Box sx={{ width: 12, height: 12, borderRadius: "50%", bgcolor: TINT.ok }} />
        </Box>
      );
    case "waiting_gpu":
      return (
        <Box aria-hidden sx={ring(TINT.warn, "dashed")}>
          <ScheduleIcon data-testid="step-waiting-icon" sx={{ fontSize: 16, color: TINT.warn }} />
        </Box>
      );
    case "queued":
      return (
        <Box aria-hidden sx={ring(TINT.idle)}>
          <HourglassEmptyIcon data-testid="step-queued-icon" sx={{ fontSize: 16, color: "text.secondary" }} />
        </Box>
      );
    case "skipped":
      return (
        <Box aria-hidden sx={ring(TINT.idle)}>
          <RemoveIcon data-testid="step-skipped-icon" sx={{ fontSize: 16, color: "text.secondary" }} />
        </Box>
      );
    default: // pending
      return (
        <Box aria-hidden sx={ring(TINT.idle)}>
          <Typography sx={{ fontFamily: MONO, fontSize: 13, color: "text.secondary", lineHeight: 1 }}>
            {index}
          </Typography>
        </Box>
      );
  }
}

// Percent + ETA for a running step; elapsed when there is no percent yet
// (non-qwen engines, pre-pipeline start).
function activeText(s: Step, t: TFunction): string {
  const bits: string[] = [];
  if (s.percent !== undefined) bits.push(`${Math.round(s.percent)}%`);
  if (s.eta_s !== undefined) bits.push(t("pipeline.left", { d: etaText(s.eta_s, t) }));
  if (bits.length === 0 && s.elapsed_s !== undefined) {
    bits.push(t("pipeline.elapsed", { t: clock(s.elapsed_s) }));
  }
  return bits.length > 0 ? bits.join(" · ") : t("pipeline.running");
}

function waitText(s: Step, t: TFunction): string {
  const head = s.holder ? t("pipeline.waitGpuHeld", { holder: s.holder }) : t("pipeline.waitGpu");
  if (s.waited_s === undefined) return head;
  // A full-width "）" already carries its own gap — no extra space before the dot.
  const sep = head.endsWith("）") ? "· " : " · ";
  return `${head}${sep}${t("pipeline.waited", { t: clock(s.waited_s) })}`;
}

function stepSubText(s: Step, prev: Step | undefined, t: TFunction): string {
  switch (s.state) {
    case "done":
      return s.duration_s !== undefined
        ? t("pipeline.doneIn", { d: durationText(s.duration_s, t) })
        : t("pipeline.done");
    case "failed":
      return t("pipeline.failed");
    case "skipped":
      return t("pipeline.skipped");
    case "queued":
      return t("pipeline.queued");
    case "waiting_gpu":
      return waitText(s, t);
    case "active":
      return activeText(s, t);
    default: // pending: waits for the step before it, unless that one is finished
      return prev && !isTerminal(prev.state)
        ? t("pipeline.after", { step: stepLabel(prev.key, t) })
        : t("pipeline.notStarted");
  }
}

const SUB_COLOR: Partial<Record<StepState, string>> = {
  active: "success.main", waiting_gpu: "warning.main", failed: "error.main",
};

function Stepper({ steps }: { steps: Step[] }) {
  const { t } = useTranslation();
  return (
    <Stack direction="row" alignItems="center" data-testid="pipeline-stepper"
      sx={{ flexWrap: "wrap", columnGap: 1.5, rowGap: 1.5, mt: 2 }}>
      {steps.map((s, i) => (
        <Fragment key={`${s.key}-${i}`}>
          {i > 0 && (
            <Box aria-hidden sx={{
              flex: "1 1 24px", minWidth: 16, height: 2, borderRadius: 1,
              bgcolor: steps[i - 1].state === "done" ? TINT.ok : SLATE_TRACK,
              transition: "background-color 200ms ease",
            }} />
          )}
          <Stack direction="row" alignItems="center" spacing={1.25} sx={{ minWidth: 0 }}>
            <StepDot state={s.state} index={i + 1} />
            <Box sx={{ minWidth: 0 }}>
              <Typography sx={{ fontWeight: 600, fontSize: 15, lineHeight: 1.3,
                color: s.state === "pending" || s.state === "skipped" ? "text.secondary" : "text.primary" }}>
                {stepLabel(s.key, t)}
              </Typography>
              <Typography variant="caption" sx={{ display: "block", fontVariantNumeric: "tabular-nums",
                color: SUB_COLOR[s.state] ?? "text.secondary" }}>
                {stepSubText(s, i > 0 ? steps[i - 1] : undefined, t)}
              </Typography>
            </Box>
          </Stack>
        </Fragment>
      ))}
    </Stack>
  );
}

// ── active-step panel ───────────────────────────────────────────────────────
function panelTiles(s: Step, m: Metrics, t: TFunction) {
  const tiles: { label: string; value: string }[] = [];
  const add = (label: string, value: string | undefined) => {
    if (value) tiles.push({ label, value });
  };
  const elapsed = s.elapsed_s !== undefined ? clock(s.elapsed_s) : undefined;
  const eta = s.eta_s !== undefined ? etaText(s.eta_s, t) : undefined;
  if (s.key === "restore") {
    const fps = fin(m.fps);
    const frames = fin(m.processed_frames);
    const rest = fin(m.remaining_frames);
    add(t("pipeline.tile.speed"), fps !== undefined ? `${fps.toFixed(fps < 10 ? 1 : 0)} fps` : undefined);
    add(t("pipeline.tile.frames"), frames === undefined ? undefined
      : rest !== undefined ? `${frames} / ${frames + rest}` : String(frames));
    add(t("pipeline.tile.elapsed"), elapsed ?? str(m.elapsed));
    add(t("pipeline.tile.eta"), eta ?? str(m.eta));
    return tiles;
  }
  if (s.key === "asr") {
    if (s.scene !== undefined && s.scenes !== undefined && s.scene >= 1 && s.scenes >= 1) {
      add(t("pipeline.tile.scene"), `${s.scene} / ${s.scenes}`);
    } else if (s.phase !== undefined && s.phase >= 1) {
      add(t("pipeline.tile.phase"), s.phase_n ? `${s.phase} / ${s.phase_n}` : String(s.phase));
    }
  } else if (s.key === "translate") {
    if (s.batches_total !== undefined) {
      add(t("pipeline.tile.batches"), `${s.batches_done ?? 0} / ${s.batches_total}`);
    }
    if (s.cues_total !== undefined) {
      add(t("pipeline.tile.cues"), t("pipeline.cues", { done: s.cues_done ?? 0, total: s.cues_total }));
    }
    // #192: resumed / fallback / kept-Japanese counts, each only once it is > 0.
    const count = (label: string, n: number | undefined) =>
      add(label, n !== undefined && n > 0 ? String(n) : undefined);
    count(t("pipeline.tile.resumed"), s.cues_resumed);
    count(t("pipeline.tile.fallback"), s.cues_fallback);
    count(t("pipeline.tile.keptJa"), s.cues_kept_ja);
  }
  add(t("pipeline.tile.elapsed"), elapsed);
  add(t("pipeline.tile.eta"), eta);
  return tiles;
}

function panelTitle(s: Step, p: Pipeline, t: TFunction): string {
  if (s.key === "restore") return t("pipeline.panel.restore");
  if (s.key === "asr") return t("pipeline.panel.asr");
  if (s.key === "translate") {
    const model = s.model ?? p.model;
    return model ? `${t("pipeline.panel.translate")} · ${model}` : t("pipeline.panel.translate");
  }
  return stepLabel(s.key, t);
}

function StepPanel({ step, pipeline, metrics }: { step: Step; pipeline: Pipeline; metrics: Metrics }) {
  const { t } = useTranslation();
  if (step.state === "waiting_gpu") {
    return (
      <Box data-testid="pipeline-panel" sx={{
        mt: 2, p: 2, borderRadius: 2, display: "flex", alignItems: "center", gap: 2,
        bgcolor: alpha(TINT.warn, 0.07), border: `1px solid ${alpha(TINT.warn, 0.35)}`,
      }}>
        <MemoryIcon aria-hidden sx={{ color: "warning.main", fontSize: 22 }} />
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontWeight: 600, fontSize: 15, color: "warning.main" }}>
            {step.holder ? t("pipeline.gpuHeld", { holder: step.holder }) : t("pipeline.waitGpu")}
          </Typography>
          <Typography sx={{ fontSize: 13, color: "text.secondary" }}>
            {step.holder ? t("pipeline.gpuHeldHint") : t("pipeline.gpuFreeHint")}
          </Typography>
        </Box>
      </Box>
    );
  }
  if (step.state === "queued") {
    return (
      <Box data-testid="pipeline-panel" sx={{ mt: 2, p: 2, ...BOX, bgcolor: SLATE_WASH }}>
        <Typography sx={{ fontSize: 14, color: "text.secondary" }}>{t("pipeline.translateQueued")}</Typography>
      </Box>
    );
  }
  // active
  const title = panelTitle(step, pipeline, t);
  const pct = step.percent ?? (step.key === "restore" ? fin(metrics.percent) : undefined);
  const tiles = panelTiles(step, metrics, t);
  // #192: a translation waiting for a provider to come back stays `active` (C8) and
  // says so in text + icon (never colour alone); the count names every film on hold.
  const paused = step.key === "translate" && step.paused === true;
  const onHold = step.deferred !== undefined && step.deferred > 1 ? step.deferred : undefined;
  return (
    <Box data-testid="pipeline-panel" sx={{ mt: 2, p: 2, borderRadius: 2,
      bgcolor: "rgba(34,197,94,0.06)", border: "1px solid", borderColor: "rgba(34,197,94,0.25)" }}>
      <Stack direction="row" justifyContent="space-between" alignItems="baseline" spacing={1}>
        <Typography sx={{ fontSize: 14, fontWeight: 500, minWidth: 0, wordBreak: "break-word" }}>{title}</Typography>
        {pct !== undefined && (
          <Typography sx={{ fontFamily: MONO, fontWeight: 600, fontSize: 18,
            fontVariantNumeric: "tabular-nums" }}>{Math.round(pct)}%</Typography>
        )}
      </Stack>
      {pct !== undefined && (
        <LinearProgress variant="determinate" value={Math.max(0, Math.min(100, pct))} aria-label={title}
          sx={{ mt: 1, height: 10, borderRadius: 5,
                "& .MuiLinearProgress-bar": { bgcolor: TINT.ok, borderRadius: 5 },
                bgcolor: SLATE_TRACK }} />
      )}
      {paused && (
        <Chip size="small" data-testid="translate-paused" icon={<ScheduleIcon />}
          label={onHold !== undefined ? t("pipeline.pausedN", { n: onHold }) : t("pipeline.paused")}
          sx={{ mt: 1.5, ...toneSx("warn"), "& .MuiChip-icon": { color: "inherit", fontSize: 16 } }} />
      )}
      {tiles.length > 0 && (
        <Stack direction="row" sx={{ flexWrap: "wrap", gap: 1.5, mt: 1.5 }}>
          {tiles.map((tile) => <Tile key={tile.label} label={tile.label} value={tile.value} />)}
        </Stack>
      )}
    </Box>
  );
}

// ── queue card ──────────────────────────────────────────────────────────────
const SUBS_BUSY: ReadonlySet<StepState> = new Set<StepState>(["active", "queued", "waiting_gpu"]);

function QueueCard({ pipeline, metrics }: { pipeline: Pipeline; metrics: Metrics }) {
  const { t } = useTranslation();
  const total = fin(metrics.queue_total);
  const done = fin(metrics.queue_completed);
  if (total === undefined || total <= 0 || done === undefined) return null;
  const rem = fin(metrics.queue_remaining);
  const failed = fin(metrics.queue_failed) ?? 0;
  const translating = fin(metrics.subs_translating) ?? 0;
  const segs: { id: string; value: number; color: string }[] = [
    { id: "seg-done", value: done, color: TINT.ok },
  ];
  const chips: { label: string; tone: Tone }[] = [];
  let legend = false;

  if (pipeline.kind === "jasna") {
    // Jasna with AV 翻译: queue_completed = fully done (restored AND subtitles
    // settled); queue_restored − that = films still in progress (light green).
    const restored = fin(metrics.queue_restored);
    if (restored !== undefined) {
      const inProgress = Math.max(0, restored - done);
      segs.push({ id: "seg-progress", value: inProgress, color: alpha(TINT.ok, 0.4) });
      legend = inProgress > 0;
      chips.push({ label: t("pipeline.q.restored", { a: restored, b: total }),
                   tone: restored >= total ? "ok" : "idle" });
    }
    const subsTotal = fin(metrics.subs_total);
    if (subsTotal !== undefined) {
      const sDone = fin(metrics.subs_completed) ?? 0;
      const sFailed = fin(metrics.subs_failed) ?? 0;
      const sSkipped = fin(metrics.subs_skipped) ?? 0;
      const busy = translating > 0
        || pipeline.steps.some((s) => s.key !== "restore" && SUBS_BUSY.has(s.state));
      let label = t("pipeline.q.subs", { a: sDone, b: subsTotal });
      if (busy) label += t("pipeline.q.inProgress");
      if (sFailed > 0) label += t("pipeline.q.nFailed", { n: sFailed });
      if (sSkipped > 0) label += t("pipeline.q.nSkipped", { n: sSkipped });
      chips.push({ label, tone: subsTotal > 0 && sDone >= subsTotal ? "ok" : "idle" });
    }
    if (failed > 0) chips.push({ label: t("pipeline.q.failed", { n: failed }), tone: "crit" });
  } else {
    // avsubs: queue_completed includes the films that already had subtitles at
    // scan (queue_pre_done, the 已有字幕 chip); 排队 = remaining − translating.
    const pre = fin(metrics.queue_pre_done);
    const skipped = fin(metrics.queue_skipped) ?? 0;
    segs.push({ id: "seg-failed", value: failed, color: TINT.crit });
    segs.push({ id: "seg-progress", value: translating, color: alpha(TINT.ok, 0.45) });
    chips.push(
      { label: t("pipeline.q.done", { n: Math.max(0, done - (pre ?? 0)) }), tone: "ok" },
      { label: t("pipeline.q.translating", { n: translating }), tone: "soft" },
      { label: t("pipeline.q.failed", { n: failed }), tone: failed > 0 ? "crit" : "idle" },
      { label: t("pipeline.q.skipped", { n: skipped }), tone: "idle" },
      { label: t("pipeline.q.queued", { n: Math.max(0, (rem ?? 0) - translating) }), tone: "idle" },
    );
    if (pre !== undefined) chips.push({ label: t("pipeline.q.preDone", { n: pre }), tone: "idle" });
  }

  const head = `${t("events.queueDone", { done, total })}${rem ? t("events.queueLeft", { n: rem }) : ""}`;
  let used = 0;
  const bars = segs.filter((s) => s.value > 0).map((s) => {
    const width = Math.max(0, Math.min((s.value / total) * 100, 100 - used));
    used += width;
    return { ...s, width };
  });
  return (
    <Box data-testid="pipeline-queue">
      <Stack direction="row" justifyContent="space-between" alignItems="baseline" sx={{ mb: 0.5 }}>
        <Typography variant="overline" color="text.secondary">{t("events.queue")}</Typography>
        <Typography sx={{ fontFamily: MONO, fontSize: 13, fontVariantNumeric: "tabular-nums" }}>{head}</Typography>
      </Stack>
      <Box role="img" aria-label={head} sx={{ display: "flex", height: 10, borderRadius: 5,
        overflow: "hidden", bgcolor: SLATE_TRACK }}>
        {bars.map((b) => (
          <Box key={b.id} data-testid={b.id} style={{ width: `${b.width}%` }}
            sx={{ height: "100%", bgcolor: b.color, transition: "width 300ms ease" }} />
        ))}
      </Box>
      {chips.length > 0 && (
        <Stack direction="row" sx={{ flexWrap: "wrap", gap: 1, mt: 1 }}>
          {chips.map((c, i) => <Chip key={i} size="small" label={c.label} sx={toneSx(c.tone)} />)}
        </Stack>
      )}
      {legend && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.75 }}>
          {t("pipeline.q.legend")}
        </Typography>
      )}
    </Box>
  );
}

// ── the view ────────────────────────────────────────────────────────────────
export function PipelineProgress({ pipeline, metrics, taskName }: { pipeline: Pipeline; metrics: Metrics; taskName?: string }) {
  const { t } = useTranslation();
  const { steps } = pipeline;
  const focus = focusStep(steps);
  const heading = steps.some((s) => s.state === "active") ? t("events.nowProcessing")
    : steps.every((s) => isTerminal(s.state)) ? t("pipeline.lastFinished") : t("pipeline.upNext");
  return (
    <Stack spacing={2} data-testid="pipeline-progress">
      <Box sx={{ ...BOX, p: 2 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-end"
          sx={{ flexWrap: "wrap", columnGap: 2, rowGap: 0.5 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="caption" sx={{ color: "text.secondary", textTransform: "uppercase",
                                                letterSpacing: 0.6, fontSize: 10 }}>
              {heading}
            </Typography>
            {pipeline.film && (
              <Typography sx={{ fontFamily: MONO, fontWeight: 600, fontSize: 16,
                                wordBreak: "break-all", mt: 0.25 }}>{pipeline.film}</Typography>
            )}
          </Box>
          <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: "tabular-nums" }}>
            {t("pipeline.stepOf", { k: stepIndex(steps), n: steps.length })}
          </Typography>
        </Stack>
        <Stepper steps={steps} />
        {focus && <StepPanel step={focus} pipeline={pipeline} metrics={metrics} />}
      </Box>
      <QueueCard pipeline={pipeline} metrics={metrics} />
      {taskName !== undefined ? (steps.some(s => s.key === "restore")
        ? <RunFilmsCard key={taskName} name={taskName} fallback={pipeline} />
        : <PagedFilmList key={taskName} name={taskName} fallback={pipeline} />)
        : <FilmList films={pipeline.films} filmsMore={pipeline.filmsMore} focus={pipeline.film} />}
    </Stack>
  );
}
