// 片库与规则 and 设置 · 片库 (#361 §11): the library queries the pages share, and small helpers.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, type KeyboardEvent } from "react";
import { api, type Library, type LibraryRules, type ScanStatus } from "../../api";
import { errorText, useWorkspace } from "./workspace";

export const LIBRARIES_KEY = ["libraryLibraries"] as const;
export const SCAN_KEY = ["libraryScanStatus"] as const;
export const RULES_KEY = ["libraryRules"] as const;
export const PERFORMERS_KEY = ["libraryPerformers"] as const;
export const PERFORMER_KEY = ["libraryPerformer"] as const;
export const HISTORY_KEY = ["libraryHistory"] as const;
const SCAN_POLL_MS = 1500;
// idle, the scanner is still asked: a scheduled or recovery scan starts without the UI (§6)
export const SCAN_IDLE_MS = 30_000;

export const SCAN_EVERY: Array<[LibraryRules["scan_every_hours"], string]> = [
  [0, "关闭"], [2, "每 2 小时"], [6, "每 6 小时"], [12, "每 12 小时"], [24, "每天"],
];
export const scanEveryText = (hours: number) => (hours === 0 ? "已关闭" : hours === 24 ? "每天" : `每 ${hours} 小时`);

export function useLibraries(enabled = true) {
  return useQuery({ queryKey: LIBRARIES_KEY, queryFn: api.libraries, retry: false, enabled });
}

export function useRules() {
  return useQuery({ queryKey: RULES_KEY, queryFn: api.libraryRules, retry: false });
}

export const scanBusy = (s: ScanStatus | undefined) => Boolean(s && (s.running || s.queued.length));

// The scanner's state, polled fast while a scan runs or waits and slowly otherwise; once it settles
// the counts are read again.
export function useScanStatus() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: SCAN_KEY, queryFn: api.libraryScanStatus, retry: false,
    refetchInterval: (q) => (scanBusy(q.state.data) ? SCAN_POLL_MS : SCAN_IDLE_MS),
  });
  const busy = scanBusy(status.data);
  const was = useRef(busy);
  useEffect(() => {
    if (was.current && !busy) {
      void qc.invalidateQueries({ queryKey: LIBRARIES_KEY });
      void qc.invalidateQueries({ queryKey: PERFORMERS_KEY });
    }
    was.current = busy;
  }, [busy, qc]);
  return status;
}

// Every library change: the list, the scanner (a new root is scanned next) and the actors.
export function useLibraryRefresh() {
  const qc = useQueryClient();
  return () => {
    for (const key of [LIBRARIES_KEY, SCAN_KEY, PERFORMERS_KEY, RULES_KEY]) void qc.invalidateQueries({ queryKey: key });
  };
}

// One rule (or the scan schedule) saved on its own, as the preview does: 「规则已保存」.
export function useSaveRules(message = "规则已保存") {
  const qc = useQueryClient();
  const ws = useWorkspace();
  return useMutation({
    mutationFn: (changes: Partial<LibraryRules>) => api.librarySaveRules(changes),
    onSuccess: (rules) => { qc.setQueryData(RULES_KEY, rules); ws.toast(message); },
    onError: (e) => ws.toast(errorText(e)),
  });
}

// 打开 / 打开目录: Explorer on the controller; a refusal is told in the toast.
export function useOpenPath() {
  const ws = useWorkspace();
  return useMutation({ mutationFn: (path: string) => api.libraryOpen(path), onError: (e) => ws.toast(`无法打开目录：${errorText(e)}`) });
}

export const actorLibraries = (items: Library[] | undefined) => (items ?? []).filter((l) => l.kind === "actor");
export const categories = (items: Library[] | undefined) => (items ?? []).filter((l) => l.kind === "category");

// Actor libraries are lettered by priority (A, B, …) as in the preview; the letter names a location.
export function letters(items: Library[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  actorLibraries(items).forEach((l, i) => { out[l.id] = i < 26 ? String.fromCharCode(65 + i) : String(i + 1); });
  return out;
}

export const offline = (l: Library) => l.last_scan_ok === false;

// The library whose root holds `path` (a receipt's target), by the backend's casefold prefix rule.
export function libraryOf(items: Library[] | undefined, path: string | null): Library | undefined {
  if (!path) return undefined;
  const key = path.toLowerCase();
  return (items ?? []).find((l) => key.startsWith(l.path.toLowerCase()));
}

// The folder that holds a film folder: the actor directory of an actor receipt.
export function parentName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 2] : "";
}

export const shortHash = (sha: string) => (sha.length > 12 ? `${sha.slice(0, 4)}…${sha.slice(-4)}` : sha);

// Enter / Space on a focused table row opens it, as a click does (not when a control inside has the focus).
export const rowKeys = (open: () => void) => (e: KeyboardEvent<HTMLTableRowElement>) => {
  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); }
};
