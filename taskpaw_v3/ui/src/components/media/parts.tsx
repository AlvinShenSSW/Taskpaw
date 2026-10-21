// Small V3 building blocks of the 媒体处理中心 (build spec §2–§3). Styled by mediaStyles.ts.
import type { SvgIconComponent } from "@mui/icons-material";
import CheckIcon from "@mui/icons-material/Check";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type WorkflowStep } from "../../api";
import {
  currentStep, lifecycle, ltxOf, STATUS_META, STEP_LABEL, stageLine, type ChipTone, type StatusKey, type WorkItem,
} from "./mediaModel";

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
export function PageTitle({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="page-title">
      <h1>{title}</h1>
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

// The real poster (an authenticated image as a data: URL), or 「无封面」.
export function Cover({ filmId, hasPoster, source, size = "row", label }: {
  filmId: string | null; hasPoster: boolean; source?: string | null; size?: CoverSize; label: string;
}) {
  const poster = useQuery({
    queryKey: ["mediaPoster", filmId, source ?? null],
    queryFn: () => api.image(api.posterPath(filmId as string)),
    enabled: hasPoster && filmId !== null, staleTime: Infinity, retry: false,
  });
  const cls = `cover${size === "row" ? "" : ` ${size}`}`;
  return hasPoster && poster.data ? (
    <div className={cls}><img src={poster.data} alt={`${label} 封面`} /></div>
  ) : (
    <div className={cls} role="img" aria-label={`${label}：无封面`}><span aria-hidden>无封面</span></div>
  );
}

export function ItemCover({ item, size }: { item: WorkItem; size?: CoverSize }) {
  const title = item.kind === "film" ? item.row.title || item.row.name : item.pkg.name;
  return item.kind === "film"
    ? <Cover filmId={item.row.film_id} hasPoster={item.row.has_poster} source={item.row.poster_source}
      size={size} label={title} />
    : <Cover filmId={null} hasPoster={false} size={size} label={title} />;
}

// The stage block (§3): LTX shows only this stage's percent, counts and rate; others one line.
export function StageBlock({ item }: { item: WorkItem }) {
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

// The lifecycle step line (operator decision 4; U1 V3 adds 待处理).
export function StepLine({ steps, title }: { steps?: WorkflowStep[] | null; title: string }) {
  const line = lifecycle(steps);
  const cur = currentStep(line);
  return (
    <ol className="step-line" aria-label={`${title} 生命周期`}>
      {line.map((s, i) => {
        const failed = s.state === "failed";
        if (i === cur) {
          return (
            <li key={s.key} className={failed ? "failed" : "current"} aria-current="step" data-step={s.key}>
              {STEP_LABEL[s.key]}{failed && <span className="sr-only">（失败）</span>}
            </li>
          );
        }
        return s.state === "done"
          ? <li key={s.key} className="done" data-step={s.key}>{STEP_LABEL[s.key]}<span className="sr-only">（已完成）</span></li>
          : <li key={s.key} data-step={s.key}>{STEP_LABEL[s.key]}</li>;
      })}
    </ol>
  );
}
