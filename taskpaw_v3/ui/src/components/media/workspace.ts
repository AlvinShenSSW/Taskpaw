// Shared state of the 媒体处理中心: the polled data and what the pages can ask the shell to do.
import { useQueries, useQuery } from "@tanstack/react-query";
import { createContext, useContext, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { api, type PackageRow, type TargetPreflight, type WorkflowFilms, type WorkflowPlans } from "../../api";
import { ACTIVE_STATES, EXCEPTION_STATES, REVIEW_STATES, workItems, type StageFilter, type WorkItem } from "./mediaModel";

export const MEDIA_FILMS_KEY = ["mediaFilms"] as const;
export const MEDIA_PLANS_KEY = ["mediaPlans"] as const;
export const mediaFilmKey = (filmId: string) => ["mediaFilm", filmId] as const;
export const mediaTargetKey = (filmId: string) => ["mediaTarget", filmId] as const;
const POLL_MS = 5000;
const PREFLIGHT_MS = 30000;

export type PageKey =
  | "overview" | "pending" | "queue" | "review" | "exceptions" | "devices" | "local" | "libraries" | "bridge" | "settings";

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
// preflight, which says whether they are ready, ambiguous, unmatched or blocked (U1 §7.3).
export function useMediaData(): MediaData {
  const films = useQuery({ queryKey: MEDIA_FILMS_KEY, queryFn: api.workflowFilms, refetchInterval: POLL_MS });
  const running = films.data?.state === "running";
  const plans = useQuery({
    queryKey: MEDIA_PLANS_KEY, queryFn: api.workflowPlans, refetchInterval: POLL_MS, enabled: running, retry: false,
  });
  const confirmIds = useMemo(
    () => (films.data?.films ?? []).filter((f) => f.group === "confirm").map((f) => f.film_id),
    [films.data],
  );
  const targetList = useQueries({
    queries: confirmIds.map((id) => ({
      queryKey: mediaTargetKey(id), queryFn: () => api.workflowTarget(id),
      refetchInterval: PREFLIGHT_MS, retry: false, enabled: running,
    })),
    combine: (results) => results.map((r) => r.data ?? null),
  });
  const preflights = useMemo(() => {
    const out: Record<string, TargetPreflight | null> = {};
    confirmIds.forEach((id, i) => { out[id] = targetList[i] ?? null; });
    return out;
  }, [confirmIds, targetList]);
  return useMemo(() => {
    const items = workItems(films.data, preflights);
    const filmItems = items.filter((i) => i.status !== "pending");
    // The rail badge counts from /films (U1 V'): 待处理 = the `planning` group.
    return {
      films, plans, running, items, filmItems, preflights,
      active: items.filter((i) => ACTIVE_STATES.includes(i.status)),
      review: items.filter((i) => REVIEW_STATES.includes(i.status)),
      exceptions: items.filter((i) => EXCEPTION_STATES.includes(i.status)),
      pendingCount: films.data?.counts.planning ?? 0,
    };
  }, [films, plans, running, preflights]);
}

export interface WorkspaceApi {
  data: MediaData;
  navigate: (page: PageKey, opts?: { stage?: StageFilter }) => void;
  queueStage: StageFilter;
  setQueueStage: (stage: StageFilter) => void;
  openDetail: (filmId: string) => void;
  openReview: (filmId: string) => void;
  openExplain: (kind: "reconcile" | "cleanup", filmId: string) => void;
  toast: (message: string) => void;
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

