import { useTranslation } from "react-i18next";
import type { FilmGroup, FilmRow, WorkflowCurrent } from "../api";

// Shared by the 「媒体入库」 list and the film drawer (T06-P1).
export const MEDIA_FILMS_KEY = ["mediaFilms"] as const;
export const mediaFilmKey = (filmId: string) => ["mediaFilm", filmId] as const;
export const MEDIA_GROUPS: FilmGroup[] = ["confirm", "active", "attention", "archiving", "done"];
// Groups whose films wait behind the dispatcher/finalizer, so a missing note means "queued" (D06).
const QUEUED_GROUPS = new Set<FilmGroup>(["active", "archiving"]);

export function useMediaLabels() {
  const { t } = useTranslation();
  return {
    stage: (stage: string) => t(`media.stage.${stage}`, { defaultValue: stage }),
    status: (status: string) => t(`media.status.${status}`, { defaultValue: status }),
    // why a cleanup kept a member (WorkflowCleanupItem.status)
    kept: (status: string) => t(`media.keptStatus.${status}`, { defaultValue: status }),
  };
}

// The row's waiting reason, or 「排队中（当前：…）」 when it has none and another film runs.
export function rowNote(row: FilmRow, current: WorkflowCurrent | null, queued: (c: WorkflowCurrent) => string) {
  if (row.note) return row.note;
  if (current && current.film_id !== row.film_id && QUEUED_GROUPS.has(row.group)) return queued(current);
  return null;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}
