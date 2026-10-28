import type { HubServer } from "../api";
import { hubServerHealth } from "../hubHealth";

// #359: the media-center view of the Hub dashboard, read from the controller's
// `/status.workflow` summary the Hub already polls (#351/#355). The rules
// follow status.md's (hub/server/status_md.py): only a current source makes
// live claims, machines match case-insensitively, `本机` is the controller.

type Obj = Record<string, unknown>;
const MAX_ROWS = 16;
const obj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
export const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;
export const text = (v: unknown, limit = 400): string | undefined => {
  if (typeof v !== "string") return undefined;
  const s = v.replace(/\s+/g, " ").trim().slice(0, limit);
  return s || undefined;
};

export interface MediaTask {
  film: string;
  stage?: string;
  phase?: string;
  progress?: string;
  machine?: string;
  ltx?: Obj;
}
export interface MediaTransfer { film: string; dir?: string; percent?: number }
export interface MediaWorker {
  name: string;
  state: string;
  heartbeat_age_s?: number;
  paused: boolean;
  gpu?: { util?: number; used_mib?: number; vram_mib?: number; power_w?: number };
  current: MediaTask | null;
  staged?: number;
  transfers: MediaTransfer[];
}
export interface MediaDispatch { state?: string; paused: boolean; reason?: string }
export interface MediaSummary {
  counts: Record<string, number>;
  dispatch?: MediaDispatch;
  tasks: MediaTask[];
  tasksMore: number;
  workers?: MediaWorker[];
  workersTotal: number;
  workersMore: number;
}
export type MediaUnavailable =
  { code: "offline" | "stale" | "notRunning" | "expired" };
export interface MediaSource {
  server: HubServer;
  summary: MediaSummary | null;
  unavailable: MediaUnavailable | null;
}

const COUNT_KEYS = ["planning", "active", "confirm", "attention", "archiving", "done"] as const;

function task(v: unknown): MediaTask | null {
  if (!obj(v)) return null;
  const film = text(v.film, 200), stage = text(v.stage, 64);
  if (!film && !stage) return null;
  return {
    film: film ?? "", stage: text(v.stage, 64), phase: text(v.phase, 64), progress: text(v.progress),
    machine: text(v.machine, 64), ltx: obj(v.ltx) ? v.ltx : undefined,
  };
}

function worker(v: unknown): MediaWorker | null {
  if (!obj(v)) return null;
  const name = text(v.name, 64);
  if (!name) return null;
  const gpu = obj(v.gpu) ? v.gpu : undefined;
  // status.md's ranges: a malformed sample is left out rather than shown
  const range = (value: unknown, low: number, high = Infinity, open = false) => {
    const n = num(value);
    return n !== undefined && (open ? n > low : n >= low) && n <= high ? n : undefined;
  };
  const transfers = Array.isArray(v.transfers) ? v.transfers.slice(0, 8) : [];
  return {
    name, state: text(v.state, 32) ?? "unknown", heartbeat_age_s: num(v.heartbeat_age_s),
    paused: v.paused === true,
    gpu: gpu && { util: range(gpu.util, 0, 100), used_mib: range(gpu.used_mib, 0),
      vram_mib: range(gpu.vram_mib, 0, Infinity, true), power_w: range(gpu.power_w, 0) },
    current: task(v.current), staged: num(v.staged),
    transfers: transfers.flatMap(t => obj(t) ? [{ film: text(t.film, 200) ?? "", dir: text(t.dir, 8),
      percent: range(t.percent, 0, 100) }] : []),
  };
}

function summary(workflow: Obj): MediaSummary | null {
  const counts: Record<string, number> = {};
  if (obj(workflow.counts)) {
    for (const key of COUNT_KEYS) {
      const n = num(workflow.counts[key]);
      if (n !== undefined && n >= 0) counts[key] = n;
    }
  }
  let tasks: MediaTask[];
  if (Array.isArray(workflow.currents)) {
    tasks = workflow.currents.slice(0, MAX_ROWS).flatMap(item => task(item) ?? []);
  } else {
    // agents before #351 only report their own current film
    const own = task(workflow.current);
    tasks = own ? [{ ...own, machine: "本机" }] : [];
  }
  if (!Object.keys(counts).length && !tasks.length && !task(workflow.current)) return null;
  const dispatch = obj(workflow.dispatch) ? workflow.dispatch : undefined;
  const workers = Array.isArray(workflow.workers)
    ? workflow.workers.slice(0, MAX_ROWS).flatMap(row => worker(row) ?? []) : undefined;
  const more = (v: unknown) => Math.max(0, num(v) ?? 0);
  return {
    counts,
    dispatch: dispatch && { state: text(dispatch.state, 64), paused: dispatch.paused === true,
      reason: text(dispatch.reason, 200) },
    tasks, tasksMore: more(workflow.currents_more),
    workers, workersTotal: more(workflow.workers_total) || workers?.length || 0,
    workersMore: more(workflow.workers_more),
  };
}

function evaluate(server: HubServer, responseCurrent: boolean): MediaSource {
  const workflow = (server.snapshot as unknown as Obj).workflow;
  const unavailable = (code: MediaUnavailable): MediaSource => ({ server, summary: null, unavailable: code });
  const health = hubServerHealth(server, responseCurrent);
  if (health === "stale") return unavailable({ code: "stale" });
  if (health !== "ok" && health !== "degraded") return unavailable({ code: "offline" });
  if (!obj(workflow)) return unavailable({ code: "notRunning" });
  if (Object.hasOwn(workflow, "sample_age_seconds")) {
    const age = num(workflow.sample_age_seconds);
    if (age === undefined || age < 0 || age > 15) return unavailable({ code: "expired" });
  }
  const parsed = summary(workflow);
  return parsed ? { server, summary: parsed, unavailable: null } : unavailable({ code: "notRunning" });
}

// The controller: the first current source among servers whose status carries
// a `workflow` key. A GPU worker also carries the key (as null, next to its
// `workflow_worker`), so it is only a candidate for a current summary, never
// the one that explains why the media view is unavailable. Otherwise the first
// non-worker candidate gives the reason (stale Hub response, offline/stale
// server, not running, old sample).
export function mediaSource(servers: HubServer[], responseCurrent: boolean): MediaSource | null {
  const candidates = servers.filter(s => obj(s.snapshot) && Object.hasOwn(s.snapshot, "workflow"));
  const evaluated = candidates.map(s => evaluate(s, responseCurrent));
  return evaluated.find(e => !e.unavailable)
    ?? evaluated.find(e => !obj((e.server.snapshot as unknown as Obj).workflow_worker))
    ?? null;
}

export function machineKeys(server: HubServer): Set<string> {
  const machine = obj(server.snapshot) ? server.snapshot.machine : undefined;
  return new Set([server.name, machine].flatMap(v => text(v, 64)?.toLowerCase() ?? []));
}

export interface MachineMedia {
  controller: boolean;
  tasks: MediaTask[];
  worker: MediaWorker | null;
}

// Each machine's running work and worker row, by lower-cased machine name.
export function mediaIndex(source: MediaSource | null): Map<string, MachineMedia> {
  const index = new Map<string, MachineMedia>();
  if (!source?.summary) return index;
  const slot = (key: string) => {
    let entry = index.get(key);
    if (!entry) index.set(key, entry = { controller: false, tasks: [], worker: null });
    return entry;
  };
  const own = machineKeys(source.server);
  for (const key of own) slot(key).controller = true;
  for (const item of source.summary.tasks) {
    const machine = item.machine ?? "";
    for (const key of machine === "" || machine === "本机" ? own : [machine.toLowerCase()]) {
      slot(key).tasks.push(item);
    }
  }
  for (const row of source.summary.workers ?? []) {
    const entry = slot(row.name.toLowerCase());
    entry.worker = entry.worker ?? row;
  }
  return index;
}

export function machineMedia(index: Map<string, MachineMedia>, server: HubServer): MachineMedia | null {
  let found: MachineMedia | null = null;
  for (const key of machineKeys(server)) {
    const entry = index.get(key);
    if (!entry) continue;
    found = found
      ? { controller: found.controller || entry.controller,
          tasks: found.tasks.concat(entry.tasks.filter(t => !found!.tasks.includes(t))),
          worker: found.worker ?? entry.worker }
      : entry;
  }
  if (found && !found.tasks.length && found.worker?.state === "online" && found.worker.current) {
    // the bounded currents may leave a machine out; its heartbeat still knows.
    // A lost node's last heartbeat is not live work.
    found = { ...found, tasks: [found.worker.current] };
  }
  return found;
}

export interface SelfReport {
  state: string;
  connected: boolean;
  paused: boolean;
  busy: boolean;
  current?: { stage?: string; phase?: string };
}

// A worker Agent's own `/status.workflow_worker` (no film name by design).
export function selfReport(server: HubServer): SelfReport | null {
  const own = obj(server.snapshot) ? (server.snapshot as unknown as Obj).workflow_worker : undefined;
  if (!obj(own)) return null;
  const current = obj(own.current)
    ? { stage: text(own.current.stage, 64), phase: text(own.current.phase, 64) } : undefined;
  let state = text(own.state, 32) ?? "unknown";
  if (state === "running" && !current && own.busy !== true) state = "idle";
  return { state, connected: own.connected === true, paused: own.paused === true, busy: own.busy === true, current };
}

// Whether a machine takes part in media work, so its disabled legacy
// Lada/Jasna/Translate monitors are explained rather than read as "off".
export function isMediaMachine(server: HubServer, media: MachineMedia | null): boolean {
  const snap = obj(server.snapshot) ? (server.snapshot as unknown as Obj) : {};
  return media !== null || Object.hasOwn(snap, "workflow") || obj(snap.workflow_worker);
}
export const LEGACY_MEDIA_MONITORS = new Set(["lada", "jasna", "avsubs"]);
