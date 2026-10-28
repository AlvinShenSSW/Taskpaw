import { Box, Chip, LinearProgress, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { StatusDot } from "./StatusDot";
import { knownLtxStage, readLtxProgress } from "./ltxProgress.helpers";
import type {
  MachineMedia, MediaDispatch, MediaSource, MediaSummary, MediaTask, MediaWorker, SelfReport,
} from "../views/hubMedia.helpers";

// #359: the 机群 row's read-only media-center work — the machine's running
// task with its Jasna/LTX progress, its GPU node state, and on the controller's
// row the dispatch, counts and every GPU node (also those the Hub does not poll).

type T = (key: string, options?: Record<string, unknown>) => string;
const DATA = { fontFamily: '"Fira Code","Noto Sans SC",ui-monospace,monospace',
  fontVariantNumeric: "tabular-nums", overflowWrap: "anywhere" } as const;
const NODE_DOT: Record<string, string> = { online: "ok", lost: "error", waiting: "starting" };
const gb = (mib: number) => (mib / 1024).toFixed(1);

// One running task: film + stage, then LTX stage/percent/frames/rate/ETAs, or
// the controller's progress text (older controllers send only that).
export function MediaTaskLine({ task, machine }: { task: MediaTask; machine?: string }) {
  const { t } = useTranslation();
  const ltx = task.ltx ? readLtxProgress({ ltx_progress: task.ltx }) : null;
  const known = ltx ? knownLtxStage(ltx) : undefined;
  const unit = known === "denoise" ? t("jasna.countSteps") : known ? t("jasna.countFrames") : t("jasna.countNeutral");
  const stage = ltx && t("hub.media.ltxStage", { index: ltx.stage_index, total: ltx.stage_total, name: ltx.stage_name });
  const facts = ltx ? [
    ltx.current && ltx.total ? `${ltx.current}/${ltx.total} ${unit}` : undefined,
    ltx.rate_value && ltx.rate_unit ? `${ltx.rate_value} ${ltx.rate_unit}` : undefined,
    ltx.elapsed ? t("hub.media.stageElapsed", { time: ltx.elapsed }) : undefined,
    ltx.eta ? t("hub.media.stageEta", { time: ltx.eta }) : undefined,
    ltx.run_eta ? t("hub.media.runEta", { time: ltx.run_eta }) : undefined,
  ].filter(Boolean) : [];
  return (
    <Box data-testid="hub-media-task" sx={{ minWidth: 0 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: "wrap", rowGap: 0.5 }}>
        <StatusDot state="running" />
        {machine && <Typography variant="body2" fontWeight={600} sx={{ overflowWrap: "anywhere" }}>{machine}</Typography>}
        {task.film && <Typography variant="body2" sx={DATA}>{task.film}</Typography>}
        {task.stage && <Chip size="small" variant="outlined"
          label={t(`media.stage.${task.stage}`, { defaultValue: task.stage })} />}
        {stage && <Typography variant="caption" color="text.secondary" sx={DATA}>
          {stage}{known ? ` · ${t(`jasna.${known}`)}` : ""}
        </Typography>}
        <Box sx={{ flex: 1 }} />
        {ltx?.percent_text !== undefined && <Typography sx={{ ...DATA, color: "success.light" }}>{ltx.percent_text}%</Typography>}
      </Stack>
      {ltx && <LinearProgress variant={ltx.percent === undefined ? "indeterminate" : "determinate"} value={ltx.percent}
        aria-label={stage ?? undefined}
        sx={{ mt: 0.75, height: 6, borderRadius: 3, bgcolor: theme => alpha(theme.palette.text.secondary, 0.15),
          "& .MuiLinearProgress-bar": { borderRadius: 3 },
          "@media (prefers-reduced-motion: reduce)": { "&, & *": { animation: "none", transition: "none" } } }} />}
      {facts.length > 0 && <Typography variant="caption" color="text.secondary" sx={{ ...DATA, display: "block", mt: 0.5 }}>
        {facts.join(" · ")}
      </Typography>}
      {task.progress && !ltx?.run_eta && <Typography variant="caption" color="text.secondary"
        sx={{ display: "block", mt: 0.5, overflowWrap: "anywhere" }}>{task.progress}</Typography>}
    </Box>
  );
}

function workerFacts(row: MediaWorker, t: T): string[] {
  const parts = [t(`hub.media.node.${row.state}`, { defaultValue: row.state })];
  if (row.state !== "online" && row.heartbeat_age_s !== undefined) parts.push(t("hub.media.heartbeat", { s: row.heartbeat_age_s }));
  if (row.paused) parts.push(t("hub.media.paused"));
  if (row.state === "online" && row.gpu) {
    // a lost node's last GPU sample is not current
    if (row.gpu.util !== undefined) parts.push(t("hub.media.gpu", { util: Math.round(row.gpu.util) }));
    if (row.gpu.used_mib !== undefined && row.gpu.vram_mib !== undefined) {
      parts.push(t("hub.media.vram", { used: gb(row.gpu.used_mib), total: gb(row.gpu.vram_mib) }));
    }
    if (row.gpu.power_w !== undefined) parts.push(`${Math.round(row.gpu.power_w)}W`);
  }
  if (row.staged) parts.push(t("hub.media.staged", { count: row.staged }));
  for (const tr of row.transfers) {
    parts.push(t(tr.dir === "up" ? "hub.media.upload" : "hub.media.download",
      { film: tr.film, percent: tr.percent ?? "—" }));
  }
  return parts;
}

function NodeRow({ row }: { row: MediaWorker }) {
  const { t } = useTranslation();
  return (
    <Box data-testid="hub-media-node" sx={{ minWidth: 0 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: "wrap", rowGap: 0.5 }}>
        <StatusDot state={NODE_DOT[row.state] ?? "unknown"} live={false} />
        <Typography variant="body2" fontWeight={600} sx={{ overflowWrap: "anywhere" }}>{row.name}</Typography>
        <Typography variant="caption" color="text.secondary" sx={DATA}>{workerFacts(row, t).join(" · ")}</Typography>
      </Stack>
      {row.state === "online" && row.current && <Box sx={{ mt: 0.75, pl: 2 }}><MediaTaskLine task={row.current} /></Box>}
    </Box>
  );
}

function dispatchText(d: MediaDispatch | undefined, t: T): string {
  if (!d?.state) return t("hub.media.dispatchUnknown");
  return [t("hub.media.dispatch", { state: d.state }), d.paused ? t("hub.media.paused") : "", d.reason ?? ""]
    .filter(Boolean).join(" · ");
}

const COUNT_LABEL: Record<string, string> = {
  planning: "hub.media.planning", active: "media.groups.active", confirm: "media.groups.confirm",
  attention: "media.groups.attention", archiving: "media.groups.archiving", done: "media.groups.done",
};

// The controller's own row: dispatch, counts and the GPU nodes. A controller
// without `workers` (before 3.11.2) lists the remote running tasks instead.
function ControllerPart({ summary }: { summary: MediaSummary }) {
  const { t } = useTranslation();
  const remote = summary.tasks.filter(item => item.machine && item.machine !== "本机");
  return (
    <Stack spacing={1}>
      <Typography variant="caption" sx={DATA}>{t("hub.media.controllerLine", { dispatch: dispatchText(summary.dispatch, t) })}</Typography>
      {Object.keys(summary.counts).length > 0 && (
        <Stack direction="row" spacing={2} sx={{ flexWrap: "wrap", rowGap: 0.5 }} aria-label={t("hub.media.counts")}>
          {Object.entries(summary.counts).map(([key, n]) => (
            <Typography key={key} variant="caption">
              <Box component="span" sx={{ ...DATA, fontWeight: 700 }}>{n}</Box> {t(COUNT_LABEL[key] ?? key)}
            </Typography>
          ))}
        </Stack>
      )}
      {summary.workers ? (
        <Box>
          <Typography variant="caption" color="text.secondary">
            {t("hub.media.nodes", { shown: summary.workers.length, total: summary.workersTotal })}
            {summary.workersMore > 0 ? ` · ${t("hub.media.more", { count: summary.workersMore })}` : ""}
          </Typography>
          <Stack spacing={1} sx={{ mt: 0.75 }}>
            {summary.workers.map(row => <NodeRow key={row.name} row={row} />)}
          </Stack>
        </Box>
      ) : remote.length > 0 && (
        <Stack spacing={1}>
          {remote.map((item, i) => <MediaTaskLine key={`${item.machine}-${i}`} task={item} machine={item.machine} />)}
        </Stack>
      )}
      {summary.tasksMore > 0 && <Typography variant="caption" color="text.secondary">
        {t("hub.media.moreTasks", { count: summary.tasksMore })}</Typography>}
    </Stack>
  );
}

// One machine's 媒体处理 block in its fleet row.
export function MachineMediaBlock({ media, self, source }: {
  media: MachineMedia | null; self: SelfReport | null; source?: MediaSource;
}) {
  const { t } = useTranslation();
  let body: ReactNode = null;
  if (source?.unavailable) {
    body = <Typography variant="body2" color="warning.main">{t(`hub.media.unavailable.${source.unavailable.code}`)}</Typography>;
  } else if (media) {
    const row = media.worker;
    body = <Stack spacing={1}>
      {media.tasks.map((item, i) => <MediaTaskLine key={`${item.film}-${i}`} task={item} />)}
      {!media.tasks.length && <Typography variant="body2" color="text.secondary">
        {row && row.state !== "online" ? workerFacts(row, t).slice(0, 2).join(" · ") : t("hub.media.idle")}
      </Typography>}
      {row && <Typography variant="caption" color="text.secondary" sx={DATA}>
        {t("hub.media.nodeLine", { facts: workerFacts(row, t).join(" · ") })}</Typography>}
      {media.controller && source?.summary && <ControllerPart summary={source.summary} />}
    </Stack>;
  } else if (self) {
    const parts = [t("hub.media.selfReport"), t(self.connected ? "hub.media.connected" : "hub.media.disconnected")];
    if (self.paused) parts.push(t("hub.media.paused"));
    parts.push(self.current
      ? [self.current.stage && t(`media.stage.${self.current.stage}`, { defaultValue: self.current.stage }), self.current.phase]
        .filter(Boolean).join(" ")
      : t("hub.media.noTasks"));
    body = <Typography variant="body2" color="text.secondary" sx={{ overflowWrap: "anywhere" }}>
      {t(`hub.media.selfState.${self.state}`, { defaultValue: self.state })} · {parts.join(" · ")}
    </Typography>;
  }
  if (!body) return null;
  return (
    <Box data-testid="hub-machine-media" sx={{ mt: 1.5, pt: 1.25, borderTop: "1px solid", borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>{t("hub.media.title")}</Typography>
      {body}
    </Box>
  );
}
