// Small V3 building blocks of the 媒体处理中心 (build spec §2–§3). Styled by mediaStyles.ts.
import type { SvgIconComponent } from "@mui/icons-material";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import { CircularProgress, Dialog } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useContext, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { api, type StepKey, type WorkflowStep } from "../../api";
import {
  currentStep, fmtDuration, heartbeatText, isRestoring, lifecycle, ltxOf, paramSummary, restoreLive, restoreStep, SPLIT_LABEL,
  STATUS_META, STEP_LABEL,
  stageLine, type ChipTone, type RestoreLive, type StatusKey, type WorkItem,
} from "./mediaModel";
import { useNow } from "./useNow";
import { mediaSourceKey, WorkspaceContext } from "./workspace";

export function Icon({ of: C }: { of: SvgIconComponent }) {
  return <C className="icon" aria-hidden />;
}

export function Chip({ tone = "neutral", children }: { tone?: ChipTone; children: ReactNode }) {
  return <span className={`chip${tone === "neutral" ? "" : ` ${tone}`}`}>{children}</span>;
}

export function StatusChip({ status }: { status: StatusKey }) {
  const [label, tone] = STATUS_META[status];
  return <Chip tone={tone}>{label}</Chip>;
}

export function Notice({ tone, icon, children, id }: {
  tone?: "amber" | "green" | "red"; icon?: SvgIconComponent; children: ReactNode; id?: string;
}) {
  return (
    <div className={`notice${tone ? ` ${tone}` : ""}`} id={id}>
      {icon && <Icon of={icon} />}
      <div>{children}</div>
    </div>
  );
}

// The compact title row of every page (operator decision 7): page name, actions on the right.
// `summary`: one short count line beside the title (「工作机 4 · 可领取 2」), never a slogan.
export function PageTitle({ title, actions, summary }: { title: string; actions?: ReactNode; summary?: string | null }) {
  return (
    <div className="page-title">
      <div className="page-title-main"><h1>{title}</h1>{summary && <p className="page-sub">{summary}</p>}</div>
      {actions && <div className="actions">{actions}</div>}
    </div>
  );
}

export function EmptyState({ title, description, children }: { title: string; description: string; children?: ReactNode }) {
  return (
    <div className="panel empty">
      <Icon of={CheckIcon} />
      <h2>{title}</h2>
      <p>{description}</p>
      {children}
    </div>
  );
}

export type CoverSize = "row" | "card" | "peek" | "review" | "large" | "confirm";

// The one cover of every page: the real poster (an authenticated image as a data: URL), or
// 「无封面」. A landscape image is a whole jacket whose front cover is its right part, so the box
// shows that part (U1b). Clicking the cover opens the lightbox (「查看大图」).
export function Cover({ filmId, hasPoster, source, hasCover = false, coverSource, size = "row", label }: {
  filmId: string | null; hasPoster: boolean; source?: string | null;
  hasCover?: boolean; coverSource?: string | null; size?: CoverSize; label: string;
}) {
  // Cover also has standalone Agent callers outside the workspace.
  const serverId = useContext(WorkspaceContext)?.server?.id;
  const poster = useQuery({
    queryKey: mediaSourceKey(["mediaPoster", filmId, source ?? null], serverId),
    queryFn: () => serverId === undefined ? api.image(api.posterPath(filmId as string))
      : api.image(api.hubWorkflowImagePath(serverId, filmId as string, "poster"), "hub"),
    enabled: hasPoster && filmId !== null, staleTime: Infinity, retry: false,
  });
  const [landscape, setLandscape] = useState(false);
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);  // the lightbox lives only from open to its exit
  const button = useRef<HTMLButtonElement>(null);
  const cls = `cover${size === "row" ? "" : ` ${size}`}`;
  if (!hasPoster || !poster.data || filmId === null) {
    return <div className={cls} role="img" aria-label={`${label}：无封面`}><span aria-hidden>无封面</span></div>;
  }
  const onLoad = (e: SyntheticEvent<HTMLImageElement>) =>
    setLandscape(e.currentTarget.naturalWidth > e.currentTarget.naturalHeight);
  return (
    <>
      <button type="button" ref={button} className={`${cls} cover-btn`} aria-label={`查看大图：${label}`}
        onClick={() => { setMounted(true); setOpen(true); }}>
        <img src={poster.data} alt={`${label} 封面`} className={landscape ? "landscape" : undefined} onLoad={onLoad} />
      </button>
      {mounted && (
        <CoverLightbox open={open} onClose={() => setOpen(false)}
          onExited={() => { setMounted(false); button.current?.focus(); }}
          filmId={filmId} hasCover={hasCover} coverSource={coverSource ?? null} poster={poster.data} label={label} />
      )}
    </>
  );
}

const LIGHTBOX_BACKDROP = { sx: { backgroundColor: "#020611EB" } };
const COVER_GC_MS = 60_000;
const LIGHTBOX_PLACEHOLDER = {
  width: "min(92vw, 960px)", maxHeight: "88vh", aspectRatio: "3 / 2", background: "#0b1120", color: "#cbd5e1",
  display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, borderRadius: 8,
} as const;
const LIGHTBOX_IMAGE = { display: "block", maxWidth: "92vw", maxHeight: "88vh", objectFit: "contain" } as const;

// The full, uncropped image: the landscape jacket when the film has one (`/cover`), else the
// poster. Esc, a backdrop click or 「关闭大图」 closes it; the cover gets the focus back.
function CoverLightbox({ open, onClose, onExited, filmId, hasCover, coverSource, poster, label }: {
  open: boolean; onClose: () => void; onExited: () => void; filmId: string; hasCover: boolean;
  coverSource: string | null; poster: string; label: string;
}) {
  const serverId = useContext(WorkspaceContext)?.server?.id;
  const full = useQuery({
    queryKey: mediaSourceKey(["mediaCover", filmId, coverSource], serverId),
    queryFn: () => serverId === undefined ? api.image(api.coverPath(filmId))
      : api.image(api.hubWorkflowImagePath(serverId, filmId, "cover"), "hub"),
    enabled: open && hasCover, staleTime: Infinity, retry: false,
    gcTime: COVER_GC_MS,  // an opened jacket is megabytes: do not keep it for the card's lifetime
  });
  const [broken, setBroken] = useState(false);  // the jacket arrived but the <img> cannot decode it
  const loading = hasCover && full.isPending;
  const src = hasCover && full.data && !broken ? full.data : poster;
  return (
    <Dialog open={open} onClose={onClose} maxWidth={false} disableRestoreFocus className="mw-lightbox"
      slotProps={{
        backdrop: LIGHTBOX_BACKDROP,
        transition: { onExited },
        paper: {
          "aria-label": `${label} 大图`,
          sx: { m: 0, maxWidth: "92vw", maxHeight: "88vh", overflow: "visible", background: "none", boxShadow: "none" },
        } as object,
      }}>
      {loading
        ? (
          <div role="status" style={LIGHTBOX_PLACEHOLDER}>
            <CircularProgress size={28} aria-hidden />
            <span>正在加载大图…</span>
          </div>
        )
        : <img src={src} alt={`${label} 大图`} style={LIGHTBOX_IMAGE} onError={() => setBroken(true)} />}
      <button type="button" className="lightbox-close" aria-label="关闭大图" onClick={onClose}>
        <CloseIcon aria-hidden />
      </button>
    </Dialog>
  );
}

export function ItemCover({ item, size }: { item: WorkItem; size?: CoverSize }) {
  const title = item.kind === "film" ? item.row.title || item.row.name : item.pkg.name;
  return item.kind === "film"
    ? <Cover filmId={item.row.film_id} hasPoster={item.row.has_poster} source={item.row.poster_source}
      hasCover={item.row.has_cover} coverSource={item.row.cover_source} size={size} label={title} />
    : <Cover filmId={null} hasPoster={false} size={size} label={title} />;
}

const ELLIPSIS = { minWidth: 0, maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } as const;

// U1c: the running 修复 step before LTX — Jasna loading, the auto split's mosaic scan, BasicVSR++.
function RestorePhaseBlock({ title, live, beat }: { title: string; live: RestoreLive; beat: string }) {
  const heartbeat = <><Heartbeat beat={beat} /><ParamsLine live={live} /></>;
  if (live.phase === "scan" && live.scan) {
    const scan = live.scan;
    return (
      <>
        <div className="film-stage">
          <strong className="t-green">扫描马赛克</strong>
          <span className="mono">{scan.percent_text === undefined ? "—" : `${scan.percent_text}%`} <span className="muted small">本阶段</span></span>
        </div>
        <div className="progress" role="progressbar" aria-label={`${title} 扫描马赛克 本阶段进度`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={scan.percent ?? undefined}>
          <span style={{ width: `${scan.percent ?? 0}%` }} />
        </div>
        <div className="stage-detail">
          <span className="mono">{scan.current ?? "—"} / {scan.total ?? "—"} 采样</span>
          <span className="mono">剩余 {scan.eta ?? "—"}</span>
        </div>
        {heartbeat}
      </>
    );
  }
  if (live.phase === "basic" && live.basic) {
    const basic = live.basic;
    const total = basic.processed !== undefined && basic.remaining !== undefined ? basic.processed + basic.remaining : undefined;
    return (
      <>
        <div className="film-stage">
          <strong className="t-green">BasicVSR++ 修复</strong>
          <span className="mono">{Math.round(basic.percent)}% <span className="muted small">本片段</span></span>
        </div>
        <div className="progress" role="progressbar" aria-label={`${title} BasicVSR++ 修复 本片段进度`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(basic.percent)}>
          <span style={{ width: `${basic.percent}%` }} />
        </div>
        <div className="stage-detail">
          <span className="mono">{basic.processed ?? "—"} / {total ?? "—"} 帧</span>
          <span className="mono">{basic.fps === undefined ? "—" : `${basic.fps.toFixed(1)} fps`}</span>
        </div>
        {heartbeat}
      </>
    );
  }
  const detail = live.split ? SPLIT_LABEL[live.split.kind] : live.console ?? "等待 Jasna 的第一行输出";
  return (
    <>
      <div className="film-stage"><strong className="t-green" data-testid="restore-heartbeat">{beat}</strong></div>
      <div className="stage-detail"><span className={live.split ? undefined : "mono"} style={ELLIPSIS} title={detail}>{detail}</span></div>
      <ParamsLine live={live} />
    </>
  );
}

const Heartbeat = ({ beat }: { beat: string }) =>
  <div className="stage-detail" data-testid="restore-heartbeat"><span className="mono">{beat}</span></div>;

// 「标准 · nvfp4 · 上限 12.2 Mbps」: this film's launch in one line (U1c).
function ParamsLine({ live }: { live: RestoreLive | null }) {
  if (!live?.params) return null;
  const line = paramSummary(live.params);
  return <div className="stage-detail" data-testid="restore-params"><span style={ELLIPSIS} title={line}>{line}</span></div>;
}

// The stage block (§3): LTX shows only this stage's percent, counts and rate; others one line.
// U1c: a running 修复 always shows it is alive (已用) and its phase before LTX.
export function StageBlock({ item }: { item: WorkItem }) {
  const restoring = item.kind === "film" && isRestoring(item.status);
  const live = restoring && item.kind === "film" ? restoreLive(restoreStep(item.row)) : null;
  const now = useNow(live !== null && live.startedAt !== null);
  const beat = live ? heartbeatText(live, now) : null;
  if (item.kind === "film" && restoring && live && beat && item.status === "restore") {
    return <RestorePhaseBlock title={item.row.title || item.row.name} live={live} beat={beat} />;
  }
  const ltx = item.kind === "film" && (item.status === "encode" || item.status === "denoise") ? ltxOf(item.row) : null;
  if (item.kind === "film" && (item.status === "encode" || item.status === "denoise")) {
    const title = item.row.title || item.row.name;
    const stageLabel = ltx?.stage_name === "denoise" ? "Denoise / 去噪" : ltx?.stage_name === "scan" ? "Scan / 扫描"
      : ltx?.stage_name === "compose" ? "Compose / 合成" : "Encode / 编码";
    const unit = ltx?.stage_name === "denoise" ? "步" : "帧";
    const percent = ltx?.percent;
    return (
      <>
        <div className="film-stage">
          <strong className="t-green">{stageLabel}</strong>
          <span className="mono">{percent === undefined ? "—" : `${ltx?.percent_text ?? percent}%`} <span className="muted small">本阶段</span></span>
        </div>
        <div className="progress" role="progressbar" aria-label={`${title} ${stageLabel} 本阶段进度`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}>
          <span style={{ width: `${percent ?? 0}%` }} />
        </div>
        <div className="stage-detail">
          <span className="mono">{ltx?.current ?? "—"} / {ltx?.total ?? "—"} {unit}</span>
          <span className="mono">{ltx?.rate_value ? `${ltx.rate_value} ${ltx.rate_unit ?? ""}` : "—"}</span>
        </div>
        {beat && <Heartbeat beat={beat} />}
        <ParamsLine live={live} />
      </>
    );
  }
  return (
    <>
      <div className="film-stage"><span>{stageLine(item)}</span></div>
      <div className="stage-detail"><span>此阶段不提供影片总体百分比</span></div>
    </>
  );
}

// The lifecycle step line (operator decision 4; U1 V3 adds 待处理). U1e: a finished step with a
// time shows it under its label, small and muted.
export function StepLine({ steps, title, durations }: {
  steps?: WorkflowStep[] | null; title: string; durations?: Partial<Record<StepKey, number>>;
}) {
  const line = lifecycle(steps);
  const cur = currentStep(line);
  const label = (key: StepKey, done: boolean) => {
    const seconds = done ? durations?.[key] : undefined;
    if (seconds === undefined) return STEP_LABEL[key];
    return (
      <span className="step-label">{STEP_LABEL[key]}<span className="step-time"><span className="sr-only">用时 </span>{fmtDuration(seconds)}</span></span>
    );
  };
  return (
    <ol className="step-line" aria-label={`${title} 生命周期`}>
      {line.map((s, i) => {
        const failed = s.state === "failed";
        if (i === cur) {
          return (
            <li key={s.key} className={failed ? "failed" : "current"} aria-current="step" data-step={s.key}>
              {s.machine ? (
                // P2-3 R15: the stage runs on a worker — its 设备 under the step
                <span className="step-label">{STEP_LABEL[s.key]}<span className="step-time"><span className="sr-only">设备 </span>{s.machine.name}</span></span>
              ) : STEP_LABEL[s.key]}
              {failed && <span className="sr-only">（失败）</span>}
            </li>
          );
        }
        return s.state === "done"
          ? <li key={s.key} className="done" data-step={s.key}>{label(s.key, true)}<span className="sr-only">（已完成）</span></li>
          : <li key={s.key} data-step={s.key}>{STEP_LABEL[s.key]}</li>;
      })}
    </ol>
  );
}
