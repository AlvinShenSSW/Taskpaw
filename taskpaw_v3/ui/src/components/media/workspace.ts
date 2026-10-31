// Shared state of the 媒体处理中心: the polled data and what the pages can ask the shell to do.
import { useQueries, useQuery } from "@tanstack/react-query";
import { createContext, useContext, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  api, type FilmDetail, type FilmRevisions, type HubServer, type PackageRow, type TargetPreflight, type WorkflowFilms, type WorkflowPlans,
} from "../../api";
import { ACTIVE_STATES, EXCEPTION_STATES, REVIEW_STATES, workItems, type StageFilter, type WorkItem } from "./mediaModel";

export const MEDIA_FILMS_KEY = ["mediaFilms"] as const;
export const MEDIA_PLANS_KEY = ["mediaPlans"] as const;
export const DEVICES_KEY = ["workflowDevices"] as const;
export const mediaFilmKey = (filmId: string) => ["mediaFilm", filmId] as const;
// Preserve the Agent's existing keys; a remote source never shares its cached rows or images.
export const mediaSourceKey = (key: readonly unknown[], serverId?: number) =>
  serverId === undefined ? key : [...key, "hub", serverId];
// The target preflight of a film's current final revision (U1 Codex r2 F3): a new revision is a new
// query, so its answer is never an older revision's. The list and the 图文确认 dialog share it.
export const finalRevision = (revisions: FilmRevisions | null | undefined) =>
  (revisions?.final ? `${revisions.final.revision}:${revisions.final.hash}` : null);
export const mediaTargetKey = (filmId: string, final: string | null) => ["mediaTarget", filmId, final] as const;
const POLL_MS = 5000;
// A dialog title's ref: focus it once when it mounts (IR01). A module-level function keeps one identity,
// so React does not call it again on every render — an inline `ref={(el) => el?.focus()}` would pull the
// focus back to the title on each keystroke and each poll.
export const focusOnMount = (el: HTMLElement | null) => { el?.focus({ preventScroll: true }); };
export const PREFLIGHT_MS = 30000;
// Each preflight searches the library and stats the NAS, so at most this many films per poll cycle (IR05).
export const PREFLIGHT_MAX = 10;

export type PageKey =
  | "overview" | "pending" | "queue" | "review" | "exceptions" | "devices" | "local" | "libraries" | "analytics" | "bridge" | "settings";

export interface MediaData {
  films: ReturnType<typeof useQuery<WorkflowFilms>>;
  plans: ReturnType<typeof useQuery<WorkflowPlans>>;
  running: boolean;
  items: WorkItem[];
  filmItems: WorkItem[];
  active: WorkItem[];
  review: WorkItem[];
  exceptions: WorkItem[];
  pendingCount: number;
  preflights: Record<string, TargetPreflight | null>;
}

// Polls like the rest of the console (5 s). Films waiting for 确认入库 also get the target
// preflight, which says whether they are ready, ambiguous, unmatched or blocked (U1 §7.3; with
// exactly one candidate the backend checks its target in the same call, F4) — only
// while a view that shows it is mounted (`preflight`: 概览, 待确认, 异常, 图文确认) and only for the top
// PREFLIGHT_MAX of them; the others keep their last answer, or read 「检查中」 (IR05).
export function useMediaData(preflight = true, serverId?: number, readOnly = false): MediaData {
  const films = useQuery({
    queryKey: mediaSourceKey(MEDIA_FILMS_KEY, serverId),
    queryFn: () => serverId === undefined ? api.workflowFilms() : api.hubWorkflowFilms(serverId),
    refetchInterval: POLL_MS,
  });
  const running = films.data?.state === "running";
  const plans = useQuery({
    queryKey: mediaSourceKey(MEDIA_PLANS_KEY, serverId),
    queryFn: () => serverId === undefined ? api.workflowPlans() : api.hubWorkflowPlans(serverId),
    refetchInterval: POLL_MS, enabled: running, retry: false,
  });
  const confirmRows = useMemo(() => (films.data?.films ?? []).filter((f) => f.group === "confirm"), [films.data]);
  const confirmIds = useMemo(() => confirmRows.map((f) => f.film_id), [confirmRows]);
  const targetList = useQueries({
    queries: (readOnly ? [] : confirmRows).map((f, i) => ({
      queryKey: mediaTargetKey(f.film_id, finalRevision(f.revisions)), queryFn: () => api.workflowTarget(f.film_id),
      refetchInterval: PREFLIGHT_MS, retry: false, enabled: running && preflight && i < PREFLIGHT_MAX,
    })),
    combine: (results) => results.map((r) => r.data ?? null),
  });
  const preflights = useMemo(() => {
    const out: Record<string, TargetPreflight | null> = {};
    confirmIds.forEach((id, i) => { out[id] = targetList[i] ?? null; });
    return out;
  }, [confirmIds, targetList]);
  return useMemo(() => {
    const items = workItems(films.data, preflights, readOnly);
    const filmItems = items.filter((i) => i.status !== "pending");
    // The rail badge counts from /films (U1 V'): 待处理 = the `planning` group.
    return {
      films, plans, running, items, filmItems, preflights,
      active: items.filter((i) => ACTIVE_STATES.includes(i.status)),
      review: items.filter((i) => REVIEW_STATES.includes(i.status)),
      exceptions: items.filter((i) => EXCEPTION_STATES.includes(i.status)),
      pendingCount: films.data?.counts.planning ?? 0,
    };
  }, [films, plans, running, preflights, readOnly]);
}

export interface WorkspaceApi {
  readOnly: boolean;
  server?: HubServer;
  readFilm: (filmId: string) => Promise<FilmDetail>;
  filmKey: (filmId: string) => readonly unknown[];
  data: MediaData;
  navigate: (page: PageKey, opts?: { stage?: StageFilter }) => void;
  queueStage: StageFilter;
  setQueueStage: (stage: StageFilter) => void;
  openDetail: (filmId: string) => void;
  openReview: (filmId: string) => void;
  openExplain: (kind: "reconcile" | "cleanup", filmId: string) => void;
  toast: (message: string) => void;
  // PWR-2: 「功耗详情」 switches the console to the 功耗 tab (absent where there is none)
  openPower?: () => void;
  // P2-4: 设备's 「主控设置」 opens 设置 · 媒体工作流 (absent where there is none)
  openSettings?: () => void;
}

export const WorkspaceContext = createContext<WorkspaceApi | null>(null);
export function useWorkspace(): WorkspaceApi {
  const ws = useContext(WorkspaceContext);
  if (!ws) throw new Error("useWorkspace outside <MediaWorkspace/>");
  return ws;
}

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Whether the dispatcher stops new claims (U1 §7.5); in-flight work continues.
export function isPaused(data: MediaData): boolean {
  return Boolean(data.films.data?.dispatch?.paused);
}

// The intake reason labels and file names of a package that needs the operator (decision 5).
export function useIntakeReasons() {
  const { t } = useTranslation();
  return (pkg: PackageRow) => {
    const label = (code: string) => t(`media.intakeReason.${code}`, { defaultValue: t(`media.intakeState.${code}`, { defaultValue: code }) });
    const reasons = [...(pkg.grouping_reasons ?? []), ...(pkg.reason && !(pkg.grouping_reasons ?? []).length ? [pkg.reason] : [])].map(label);
    const files = (pkg.files ?? []).join("、");
    return [reasons.join("；"), files].filter(Boolean).join("：");
  };
}

