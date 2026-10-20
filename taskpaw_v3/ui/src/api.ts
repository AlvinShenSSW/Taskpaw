// Minimal REST client for the V3 backends.
//
// Native credentials are injected into the trusted main frame; development
// credentials remain in memory, isolated by role. Both APIs are local control.

export interface FfmpegStatus {
  on_path: string | null;
  bundled: string | null;
  effective: string | null;
  exe_ok: boolean;
  saved_path_ok: string | null;
  pending_restart: boolean;
  candidates: Array<{ dir: string; exists: boolean }>;
  platform: "windows" | "other";
  error: boolean;
  script: string | null;
}

export interface MonitorSnapshot {
  state: string;
  metrics?: Record<string, unknown>;
  detail?: string;
  alive?: boolean;
  degraded?: boolean;
  // Added by the agent status provider (#57): whether the monitor is enabled
  // (running) and its plugin type, so the console can toggle + label it.
  enabled?: boolean;
  type_id?: string | null;
  // ISO time of this monitor's most recent local event (#130), stamped by the
  // agent status provider. Absent when the monitor has emitted no events yet.
  last_event_at?: string;
  configured?: boolean;
  desired_enabled?: boolean;
  lifecycle?: "starting" | "running" | "stopping" | "stopped" | "remove_pending";
  config_in_sync?: boolean | null;
  persistence?: MonitorMutationResult["persistence"];
  runtime_error_code?: string | null;
}

export interface AgentStatus {
  version?: string;
  machine: string;
  server_id?: string;
  os?: string;
  monitors: Record<string, MonitorSnapshot>;
}

export type LogSeverity = "info" | "warn" | "error";
export interface LogEntry {
  v: 1;
  id: string;
  ts: string;
  task: string;
  task_type: string;
  kind: string;
  severity: LogSeverity;
  film?: string;
  pid?: number;
  proc?: string;
  data?: Record<string, unknown>;
}
export interface LogParams {
  day?: string;
  task?: string;
  severity?: string;
  q?: string;
  before?: string;
  after?: string;
  limit?: number;
}
export interface LogPage { boot: string; entries: LogEntry[]; next_before: string | null }
export interface LogDays { boot: string; days: Array<{ day: string; count: number }> }

export interface HubServer {
  id: number;
  name: string;
  ip: string;
  port: number;
  enabled: number;
  // Per-server poll snapshot (#96): live reachability, last good poll time, and
  // the agent's last parsed /status (null if never polled). A disabled server is
  // forced online=false. Optional so older Hub builds (pre-#96) still type-check.
  online?: boolean;
  last_seen?: string | null;
  snapshot?: AgentStatus | null;
}

export interface HubStatus {
  machine: string;
  servers: HubServer[];
  acks: Record<string, number>;
  self: Record<string, MonitorSnapshot>;
}

// A selectable monitor type from /control/plugins (#57): its form schema drives
// the add/edit dialog.
export interface PluginInfo {
  type_id: string;
  display_name: string;
  category: string;
  config_version: number;
  system: boolean;
  json_schema: Record<string, unknown>;
  ui_schema: Record<string, unknown>;
}

export interface PresetInfo {
  id: string;
  display_name: string;
  description?: string;
  monitors: Array<{ type_id: string; name: string; config: Record<string, unknown> }>;
}

export interface MonitorSpec {
  type_id: string;
  name?: string;
  config: Record<string, unknown>;
  enabled?: boolean;
}

// One row in the event log (#44). Agent-local events carry `time`/`machine`;
// Hub-aggregated events carry `received_at`/`server`. The renderer tolerates both.
export interface EventItem {
  id?: number;
  event_id?: number;
  server_id?: number; // Hub events: id is only unique WITH the server (key needs both)
  time?: string;
  received_at?: string;
  machine?: string;
  server?: string;
  monitor?: string;
  message?: string;
  level?: string;
}

// The agent's LLM providers (#178 primary; #190 two optional fallbacks). Each slot's
// config fields are `llm_<field>` (primary) or `llm_fallbackN_<field>`.
export type LlmSlot = "primary" | "fallback1" | "fallback2";
// Where a slot's effective key comes from (its TASKPAW_LLM_*_API_KEY env var wins).
export type LlmKeySource = "env" | "config" | "none";

// GET /control/config: the agent config with every secret masked as "***" (the key
// values never leave the agent), plus each LLM slot's key source and the #192
// failover switch (absent from an older agent → treated as on, its default).
export type AgentConfigView = {
  monitors: MonitorSpec[];
  monitor_operations?: Record<string, { stage: string; retryable: boolean }>;
  llm_api_key_source?: LlmKeySource;
  llm_fallback1_api_key_source?: LlmKeySource;
  llm_fallback2_api_key_source?: LlmKeySource;
  llm_thinking_off?: boolean | null;
  llm_fallback1_thinking_off?: boolean | null;
  llm_fallback2_thinking_off?: boolean | null;
  llm_thinking_off_auto?: boolean;
  llm_fallback1_thinking_off_auto?: boolean;
  llm_fallback2_thinking_off_auto?: boolean;
  llm_failover?: boolean;
} & Record<string, unknown>;

export interface MonitorMutationResult {
  ok: boolean;
  name: string;
  operation: "add" | "remove" | "start" | "stop" | "update";
  outcome: "applied" | "not_applied" | "persisted_runtime_pending" | "persisted_runtime_failed" | "applied_not_persisted" | "stop_incomplete" | "busy";
  persistence: "saved" | "in_memory" | "failed" | "pending" | "not_requested";
  runtime: "applied" | "stopped" | "unchanged" | "starting" | "stopping" | "failed" | "unavailable";
  retryable: boolean;
  error_code: string | null;
}
const MONITOR_CODES = new Set(["persistence_failed", "create_failed", "start_failed", "cleanup_failed", "stop_timeout", "operation_busy", "validation_timeout", "validation_cancelled"]);
export class MonitorOperationError extends Error {
  constructor(public readonly result: MonitorMutationResult) { super("Monitor operation did not complete"); }
}
export function monitorOperationMessageKey(result: MonitorMutationResult): string {
  if (result.error_code === "validation_timeout" || result.error_code === "validation_cancelled") return "agent.validationNotApplied";
  if (result.runtime === "stopping") return "agent.stopIncomplete";
  if (result.outcome === "busy") return "agent.operationBusy";
  if (result.runtime === "stopped" && !["saved", "in_memory"].includes(result.persistence)) return "agent.stopNotSaved";
  if (result.outcome === "persisted_runtime_failed") return "agent.savedRuntimeFailed";
  if (result.outcome === "persisted_runtime_pending") return "agent.runtimePending";
  return "agent.operationNotApplied";
}
function monitorResult(body: unknown): MonitorMutationResult | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  if (typeof b.ok !== "boolean" || typeof b.name !== "string" || typeof b.retryable !== "boolean"
    || !["add", "remove", "start", "stop", "update"].includes(String(b.operation))
    || !["applied", "not_applied", "persisted_runtime_pending", "persisted_runtime_failed", "applied_not_persisted", "stop_incomplete", "busy"].includes(String(b.outcome))
    || !["saved", "in_memory", "failed", "pending", "not_requested"].includes(String(b.persistence))
    || !["applied", "stopped", "unchanged", "starting", "stopping", "failed", "unavailable"].includes(String(b.runtime))
    || !(b.error_code === null || (typeof b.error_code === "string" && MONITOR_CODES.has(b.error_code)))) return null;
  return { ok: b.ok, name: b.name, operation: b.operation, outcome: b.outcome,
    persistence: b.persistence, runtime: b.runtime, retryable: b.retryable, error_code: b.error_code } as MonitorMutationResult;
}

export type LlmTestResult = {
  ok: boolean; model?: string; latency_ms?: number; error?: string;
  note?: "thinking_unsupported";
};

// Media workflow (T06-P1 §8, frozen shapes).
export type FilmGroup = "confirm" | "active" | "attention" | "archiving" | "done";
export interface FilmRow {
  film_id: string; name: string; group: FilmGroup; stage: string; status: string;
  note: string | null; can_confirm: boolean; can_retry: boolean; retry_stage: string | null;
  created_at: string | null; steps: WorkflowStep[];
}
// H2 R5 lifecycle step line (frozen): one fixed line per film/package, computed by the backend.
export type StepKey =
  "intake" | "restore" | "asr" | "translate" | "return" | "scrape" | "confirm" | "archive" | "cleanup" | "done";
export type StepState = "done" | "running" | "waiting" | "failed" | "pending";
// progress: whatever the executor reports (a 0–1 fraction, a percentage, console text, …) or null.
export interface WorkflowStep { key: StepKey; state: StepState; note: string | null; progress: unknown }
// H2 R2/R2a: an INBOX package that is not a film yet. The backend sends codes; the UI owns the labels.
export interface PackageRow {
  package_id: string; name: string; state: string; reason: string | null; grouping_reasons: string[];
  files: string[]; group: "active" | "attention"; steps: WorkflowStep[];
}
export interface WorkflowIntake { package_id: string; phase: string; bytes_completed: number; total: number }
export interface WorkflowCurrent { film_id: string; film: string; stage: string; progress: unknown }
export interface WorkflowFilms {
  state: string; reason: string | null; current: WorkflowCurrent | null; intake: WorkflowIntake | null;
  films: FilmRow[]; packages: PackageRow[];
}
export interface FilmDetail {
  film_id: string; name: string; number: string | null; title: string | null; actors: string[];
  marks: { restored: boolean; chinese_subtitles: boolean; uhd: boolean };
  files: Array<{ role: string; path: string; size: number | null; volume: number | string | null }>;
  inputs: Array<{ path: string; size: number | null }>;
  workcopy_count: number; has_poster: boolean;
  group: FilmGroup; can_confirm: boolean; can_retry: boolean; retry_stage: string | null; note: string | null;
  stages: Array<{ stage: string; status: string; note: string | null }>; steps: WorkflowStep[];
  archive: { status: string | null; error: string | null; target_path: string | null };
  cleanup: { input: string | null; workcopy: string | null; kept: Array<{ path: string; status: string }> };
}
export interface Performer {
  id: number | string; name: string; source_label: string | null; unc_path: string | null;
  has_thumb: boolean; aliases: string[];
}
const seg = (id: string | number) => encodeURIComponent(String(id));

export type ControlRole = "agent" | "hub";
declare global {
  interface Window {
    __TASKPAW__?: { baseUrl?: string; controlToken?: string; role?: ControlRole; bootId?: string };
  }
}
const DEFAULT_PORT = { agent: 5681, hub: 5691 } as const;
type Credential = { baseUrl: string; controlToken: string; verified: boolean };
const credentials: Partial<Record<ControlRole, Credential>> = {};
const roleBases: Record<ControlRole, string> = { agent: "http://127.0.0.1:5681", hub: "http://127.0.0.1:5691" };
export const controlBaseForRole = (role: ControlRole) => roleBases[role];
let consumedInjection: Window["__TASKPAW__"];
let revision = 0;
const listeners = new Set<() => void>();
const changed = () => { revision++; listeners.forEach(fn => fn()); };
export const subscribeControlCredentials = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
export const controlCredentialRevision = () => revision;
export class ControlCredentialError extends Error {
  constructor() { super("Local control credentials are unavailable or expired"); }
}
export function canonicalControlBase(base: string): boolean {
  try {
    const url = new URL(base);
    return ["http:", "https:"].includes(url.protocol)
      && ["127.0.0.1", "[::1]"].includes(url.hostname)
      && !url.username && !url.password && !url.search && !url.hash
      && url.pathname === "/"
      && base === `${url.protocol}//${url.hostname}:${url.port || (url.protocol === "http:" ? "80" : "443")}`;
  } catch { return false; }
}
function validToken(token: string): boolean { return /^[\x21-\x7e]{1,1024}$/.test(token); }
function consumeInjection() {
  const injected = window.__TASKPAW__;
  if (!injected || consumedInjection === injected) return;
  consumedInjection = injected;
  if ((injected.role === "agent" || injected.role === "hub")
    && typeof injected.baseUrl === "string" && canonicalControlBase(injected.baseUrl)
    && typeof injected.controlToken === "string" && validToken(injected.controlToken)
    && /^[0-9a-f]{32}$/.test(injected.bootId ?? "")) {
    credentials[injected.role] = { baseUrl: injected.baseUrl, controlToken: injected.controlToken, verified: true };
  }
}
export function hasControlCredential(role: ControlRole): boolean { consumeInjection(); return credentials[role]?.verified === true; }
export function clearControlCredentials(role?: ControlRole) {
  if (role) delete credentials[role];
  else { delete credentials.agent; delete credentials.hub; roleBases.agent = "http://127.0.0.1:5681"; roleBases.hub = "http://127.0.0.1:5691"; }
  if (!role || window.__TASKPAW__?.role === role) {
    if (window.__TASKPAW__) delete window.__TASKPAW__.controlToken;
    consumedInjection = window.__TASKPAW__;
  }
  changed();
}
export function setDevControlCredential(role: ControlRole, token: string, baseUrl = `http://127.0.0.1:${DEFAULT_PORT[role]}`) {
  if (!import.meta.env.DEV || window.__TASKPAW__ || !validToken(token) || !canonicalControlBase(baseUrl)) throw new ControlCredentialError();
  roleBases[role] = baseUrl;
  credentials[role] = { baseUrl, controlToken: token, verified: true };
  changed();
}
export async function connectDevControlCredential(role: ControlRole, token: string, baseUrl = controlBaseForRole(role)) {
  if (!import.meta.env.DEV || window.__TASKPAW__ || !validToken(token) || !canonicalControlBase(baseUrl)) throw new ControlCredentialError();
  roleBases[role] = baseUrl;
  credentials[role] = { baseUrl, controlToken: token, verified: false };
  try {
    await (role === "agent" ? api.agentStatus() : api.hubStatus());
    const candidate = credentials[role];
    if (!candidate || candidate.controlToken !== token) throw new ControlCredentialError();
    candidate.verified = true; changed();
  } catch {
    clearControlCredentials(role); throw new ControlCredentialError();
  }
}
function cfg(role: ControlRole): Credential {
  consumeInjection();
  const value = credentials[role];
  if (!value) throw new ControlCredentialError();
  return value;
}
function checkUnauthorized(role: ControlRole, res: Response) {
  if (res.status === 401) { clearControlCredentials(role); throw new ControlCredentialError(); }
}

// A non-2xx control response. Keeps the old message (detail text or "path → status")
// and adds the status so callers can tell e.g. 503 "Filmpaw down" from other errors.
export class HttpError extends Error {
  constructor(message: string, public readonly status: number) { super(message); }
}

async function get<T>(role: "agent" | "hub", path: string): Promise<T> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await fetch(`${baseUrl}${path}`, {
    headers: { Authorization: `Bearer ${controlToken}` },
  });
  checkUnauthorized(role, res);
  if (!res.ok) throw new HttpError(`${path} → ${res.status}`, res.status);
  return res.json() as Promise<T>;
}

const FILM_ERROR_CODES = new Set([
  "invalid_parameters", "unknown_server", "agent_disabled", "agent_offline",
  "film_list_unavailable", "agent_auth_failed", "agent_timeout",
  "invalid_agent_response", "agent_request_failed",
]);

export class FilmRequestError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super("Film list request failed");
  }
}

async function hubFilmGet(path: string): Promise<unknown> {
  const { baseUrl, controlToken } = cfg("hub");
  const res = await fetch(`${baseUrl}${path}`, {
    headers: { Authorization: `Bearer ${controlToken}` },
  });
  checkUnauthorized("hub", res);
  if (!res.ok) {
    let code = "agent_request_failed";
    try {
      const body: unknown = await res.json();
      if (body && typeof body === "object" && "error" in body
        && typeof body.error === "string" && FILM_ERROR_CODES.has(body.error)) code = body.error;
    } catch { /* Non-JSON failures get the fixed generic code, never raw body text. */ }
    throw new FilmRequestError(res.status, code);
  }
  return res.json();
}

// Non-GET control calls (#57). On error, surface the backend's `detail` (the
// admin's ValueError message → 400) so the UI can show why an edit was rejected.
async function send<T>(
  role: "agent" | "hub",
  method: "POST" | "DELETE" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${controlToken}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  checkUnauthorized(role, res);
  const monitor = role === "agent" && path.startsWith("/control/monitors");
  let payload: unknown;
  if (monitor) {
    try { payload = await res.json(); } catch { /* Generic HTTP handling below. */ }
    const result = monitorResult(payload);
    if (result && (!result.ok || result.outcome !== "applied")) throw new MonitorOperationError(result);
  }
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const j = monitor ? payload as { detail?: unknown } : await res.json();
      if (j?.detail) detail = String(j.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return monitor ? payload as T : res.json() as Promise<T>;
}

// Authenticated image fetch → data: URL (T06 D01): control routes need the bearer
// token and the packaged CSP only allows `img-src 'self' data:`.
async function image(path: string): Promise<string> {
  const { baseUrl, controlToken } = cfg("agent");
  const res = await fetch(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${controlToken}` } });
  checkUnauthorized("agent", res);
  if (!res.ok) throw new HttpError(`${path} → ${res.status}`, res.status);
  const blob = await res.blob();
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("image read failed"));
    reader.readAsDataURL(blob);
  });
}

const q = (name: string) => `?name=${encodeURIComponent(name)}`;

export const api = {
  respondPrompt: (name: string, request_id: string, approved: boolean) =>
    send<{ ok: boolean }>("agent", "POST", `/control/monitors/respond${q(name)}`, { request_id, approved }),
  hubFilms: (serverId: number, name: string, page?: number, size = 10) =>
    hubFilmGet(`/servers/${serverId}/monitors/films${q(name)}${page === undefined ? "" : `&page=${page}`}&size=${size}`),
  hubRunFilms: (serverId: number, name: string, filter: "done" | "open" | "all", page: number, size = 10) =>
    hubFilmGet(`/servers/${serverId}/monitors/run-films${q(name)}&filter=${filter}&page=${page}&size=${size}`),
  ffmpeg: (whisperjav?: string) => get<FfmpegStatus>("agent",
    `/control/ffmpeg${whisperjav === undefined ? "" : `?${new URLSearchParams({ whisperjav })}`}`),
  runFilms: (name: string, filter: "done" | "open" | "all", page: number, size = 10) =>
    get<unknown>("agent", `/control/monitors/run-films${q(name)}&filter=${filter}&page=${page}&size=${size}`),
  films: (name: string, page?: number, size = 10) =>
    get<unknown>("agent", `/control/monitors/films${q(name)}${page === undefined ? "" : `&page=${page}`}&size=${size}`),
  logs: (params: LogParams = {}) => {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") qs.set(key, String(value));
    }
    return get<LogPage>("agent", `/control/logs?${qs}`);
  },
  logDays: () => get<LogDays>("agent", "/control/logs?days=true"),
  agentStatus: () => get<AgentStatus>("agent", "/control/status"),
  hubStatus: () => get<HubStatus>("hub", "/status"),
  plugins: () => get<{ plugins: PluginInfo[]; presets: PresetInfo[] }>("agent", "/control/plugins"),
  // Full agent config (secrets masked as "***") — used to pre-fill the edit form.
  config: () => get<AgentConfigView>("agent", "/control/config"),
  // Edit top-level agent config from the Settings UI (#43). Returns
  // {ok, restart_required}. A blank/"***" api_token or LLM key keeps the stored one;
  // a null LLM key clears it.
  updateConfig: (patch: Record<string, unknown>) =>
    send<{ ok: boolean; restart_required: boolean }>("agent", "PATCH", "/control/config", patch),
  // Test one slot's candidate LLM settings (#178/#190) without persisting — the
  // agent sends its real translation probe; a blank key means "use the effective
  // one". Failures come back as {ok: false, error} and never carry the key.
  llmTest: (candidate: Record<string, unknown>, slot: LlmSlot) =>
    send<LlmTestResult>("agent", "POST", "/control/llm-test", { ...candidate, slot }),
  addMonitor: (spec: MonitorSpec) => send<MonitorMutationResult>("agent", "POST", "/control/monitors", spec),
  removeMonitor: (name: string) => send<MonitorMutationResult>("agent", "DELETE", `/control/monitors${q(name)}`),
  updateMonitor: (name: string, patch: { config?: Record<string, unknown>; enabled?: boolean }) =>
    send<MonitorMutationResult>("agent", "PATCH", `/control/monitors${q(name)}`, patch),
  startMonitor: (name: string) => send<MonitorMutationResult>("agent", "POST", `/control/monitors/start${q(name)}`),
  stopMonitor: (name: string) => send<MonitorMutationResult>("agent", "POST", `/control/monitors/stop${q(name)}`),
  // The Hub reads durable aggregated event history, by server id + level.
  hubEvents: (p: { server?: number; level?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (p.server != null) qs.set("server", String(p.server));
    if (p.level) qs.set("level", p.level);
    qs.set("limit", String(p.limit ?? 200));
    return get<{ events: EventItem[] }>("hub", `/events?${qs.toString()}`);
  },
  // Manage the agents the Hub polls, from the dashboard (#124). Bearer-gated like
  // the rest of the Hub API (#106); errors surface the backend `detail`.
  hubAddServer: (s: { name: string; ip: string; port: number }) =>
    send<HubServer & { ok: boolean }>("hub", "POST", "/servers", s),
  hubUpdateServer: (id: number, patch: { name?: string; ip?: string; port?: number; enabled?: boolean }) =>
    send<HubServer & { ok: boolean }>("hub", "PATCH", `/servers/${id}`, patch),
  hubRemoveServer: (id: number) => send<{ ok: boolean }>("hub", "DELETE", `/servers/${id}`),
  hubSetPollingToken: (polling_token: string) =>
    send<{ ok: boolean }>("hub", "PATCH", "/config", { polling_token }),
  // Media workflow (T06-P1 §8): local control API only; paths are resolved by film id.
  workflowFilms: () => get<WorkflowFilms>("agent", "/control/workflow/films"),
  workflowFilm: (filmId: string) => get<FilmDetail>("agent", `/control/workflow/films/${seg(filmId)}`),
  performers: (query: string) =>
    get<{ items: Performer[] }>("agent", `/control/workflow/performers?${new URLSearchParams({ q: query })}`),
  workflowConfirm: (filmId: string, performer_id: Performer["id"]) =>
    send<{ approval_id: string }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/confirm`, { performer_id }),
  workflowRetry: (filmId: string, stage: string) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/retry`, { stage }),
  workflowOpen: (filmId: string, what: "inbox" | "output") =>
    send<void>("agent", "POST", `/control/workflow/films/${seg(filmId)}/open`, { what }),
  // H2 R2: open a current INBOX package folder (204 | 404 missing | 501 unsupported platform).
  workflowOpenPackage: (packageId: string) =>
    send<void>("agent", "POST", `/control/workflow/packages/${seg(packageId)}/open`),
  posterPath: (filmId: string) => `/control/workflow/films/${seg(filmId)}/poster`,
  thumbPath: (performerId: Performer["id"]) => `/control/workflow/performers/${seg(performerId)}/thumb`,
  image,
};
