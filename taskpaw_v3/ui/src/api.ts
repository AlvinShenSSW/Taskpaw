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

// Media workflow (T06-P1 §8; U1 adds the plan gate, preflight, pause and the V3 row facts).
// The shapes are the backend's as built (U1 spec, "API appendix"); the UI tests load the
// responses the real routes produce (fixtures/workflow-contract.json, checked by pytest).
// `planning` is 待处理: the restore waits for the film's plan (U1 V').
export type FilmGroup = "planning" | "confirm" | "active" | "attention" | "archiving" | "done";
export type LtxTier = "standard" | "good" | "high";
export type Resolution = "4K" | "1080p";
// U1 V': the confirmed plan of a film's current input revision (null tier = 无需修复).
export interface FilmPlan { ltx_model: LtxTier | null; machine: string }
// U1 §7.2: the current input and final manifests, so the 图文确认 dialog can see a change.
export interface ManifestRevision { revision: number; hash: string }
export interface FilmRevisions { input: ManifestRevision | null; final: ManifestRevision | null }
export interface FilmRow {
  film_id: string; name: string; group: FilmGroup; stage: string | null; status: string | null;
  note: string | null; can_confirm: boolean; can_retry: boolean; retry_stage: string | null;
  created_at: string | null; steps: WorkflowStep[];
  // final NFO > lookup > input NFO; the cover by the same precedence
  number: string | null; title: string | null; actors: string[];
  has_poster: boolean; poster_source: string | null;
  // U1b: the full landscape jacket the 大图 shows (`coverPath`); `cover_source` is its cache key
  has_cover: boolean; cover_source: string | null;
  resolution: Resolution | null; plan: FilmPlan | null; receipt_id: string | null; revisions: FilmRevisions;
  // U1e: each finished stage's time, the restore breakdown and 修复→翻译
  timing?: FilmTiming | null;
  // PWR-2: the summed probe duration, the estimated energy and, on the GPU-owning running film only,
  // this machine's live estimate (PW12)
  duration_ms?: number | null; energy?: FilmEnergy | null; live_power?: LivePower | null;
}
// PWR-2: a stage's estimated energy over the current revision's attempts (failed ones included);
// `partial`: an attempt was cut off without a value (「部分」); `running`: the attempt so far.
export interface EnergyStage {
  wh: number; gpu_wh: number; cpu_wh: number; other_wh: number; seconds: number;
  avg_w: number | null; peak_w: number | null; machine: string; attempts: number; partial: boolean; running: boolean;
}
export interface FilmEnergy {
  stages: Partial<Record<string, EnergyStage>>; total_wh: number; partial: boolean; running: boolean; estimated: true;
}
// PWR-1 `/status.power`: one machine's live estimate (W) and its calendar energy (Wh).
export interface LivePower {
  w: number; gpu_w: number | null; cpu_w: number | null; other_w: number | null; estimated: true;
  // the CPU utilisation (%) behind cpu_w: the calibration recomputes the CPU share for edited settings
  cpu_pct?: number | null;
  today_wh: number | null; week_wh: number | null; month_wh: number | null; activity: string | null;
}
// PWR-2 集群功耗: this machine plus the 功耗 tab's fresh peers.
export interface ClusterPower {
  w: number; gpu_w: number; cpu_w: number; other_w: number; today_wh: number; machines: number; peers: number; estimated: true;
  today_partial?: true;  // today_wh includes a peer's last reading (not answering now)
}
// PWR-2 PW13: median Wh per film-hour per machine × resolution × tier (null below 3 films).
export interface EnergyRate {
  machine: string; resolution: Resolution; tier: LtxTier | "none"; films: number; wh_per_hour: number | null;
}
export interface PlanPrediction {
  machine: string; resolution: Resolution | null; min_films: number; films: Record<LtxTier | "none", number>;
  standard: number | null; good: number | null; high: number | null; none: number | null; estimated: true;
}
// U1e: the seconds Taskpaw saw each Jasna phase as the current one, over every LTX run and volume.
// `ltx` is the four restore stages (`ltx_stages`, by name) only; `speed_test` is counted apart.
export interface RestoreTiming {
  scan: number; basicvsr: number; ltx: number; speed_test: number;
  ltx_stages: Record<string, number>; ltx_runs: number; wall: number;
}
// U1e: stage → seconds of its succeeded attempt; 修复→翻译 runs from the first of restore / asr /
// translate to the translate end; the queue wait from the plan confirm to the restore start.
export interface FilmTiming {
  stages: Partial<Record<string, number>>; restore_breakdown: RestoreTiming | null;
  total_restore_to_translate: number | null; queue_wait: number | null;
}
// H2 R5 lifecycle step line; U1 V3 adds 「待处理」 (`plan`) after 入库扫描.
export type StepKey =
  "intake" | "plan" | "restore" | "asr" | "translate" | "return" | "scrape" | "confirm" | "archive" | "cleanup" | "done";
export type StepState = "done" | "running" | "waiting" | "failed" | "pending";
// progress: whatever the executor reports (a 0–1 fraction, a percentage, console text, …) or null.
// H3 M1/M3: the restore step carries the LTX monitor metrics (`ltx`) while running and the
// model download session (`download`) when it failed on missing models. U1c: the running restore
// step also carries its heartbeat, the stage start (`started_at`, UTC ISO) and the latest plain
// console line (`console`), whether or not a progress bar was recognised yet.
export interface WorkflowStep {
  key: StepKey; state: StepState; note: string | null; progress: unknown;
  ltx?: Record<string, unknown> | null; current_file?: string | null; download?: Record<string, unknown> | null;
  started_at?: string | null; console?: string | null; params?: RestoreParams | null;
  // U1e: the running restore's phase times so far (the whole film)
  timing?: RestoreTiming | null;
}
// U1c: the effective Jasna launch of the running video, read from its argv (paths cut to names).
export interface RestoreParams {
  ltx_model: LtxTier | null; model_name: string | null; fast: boolean; precision: "nvfp4" | "int8";
  auto_split: number; large_canvas: boolean; overlap: "auto" | "on" | "off"; codec: string | null; source_codec: string | null; cq: number | null;
  match_source_bitrate: boolean; maxrate_bps: number | null; source_bitrate_bps: number | null;
  detection_model: string | null; clip_size: number | null; temporal_overlap: number | null; tier: string;
  source_width: number | null; source_height: number | null; gpu: string | null; gpu_vram_mib: number | null;
  preset: string; preset_label: string; preset_auto: boolean; argv: string;
}
// H2 R2/R2a: an INBOX package that is not a film yet. The backend sends codes; the UI owns the labels.
export interface PackageRow {
  package_id: string; name: string; state: string; reason: string | null; grouping_reasons: string[];
  files: string[]; group: "active" | "attention"; steps: WorkflowStep[];
}
export interface WorkflowIntake { package_id: string; phase: string; bytes_completed: number; total: number }
export interface WorkflowCurrent {
  film_id: string; film: string; stage: string; progress: unknown; ltx?: Record<string, unknown>;
  uses_gpu?: boolean; energy?: Record<string, unknown> | null; power?: LivePower | null;
}
export interface WorkflowDispatch {
  state: "idle" | "running" | "paused" | "stopping_unconfirmed"; reason: string; paused: boolean;
}
// `max_output`: the frame size the workflow restores on this card (phase 1: 1080p on every card), not the card's class.
export interface WorkflowGpu { name: string; vram_mib: number; max_output: "4K" | "1080p" }
// The rail badges and the overview stats; `waiting_for_you` = planning + confirm (「等待您的确认」).
export interface WorkflowCounts {
  planning: number; active: number; attention: number; confirm: number; archiving: number; done: number;
  packages_attention: number; waiting_for_you: number;
}
export interface WorkflowFilms {
  state: string; reason: string | null; current: WorkflowCurrent | null; intake: WorkflowIntake | null;
  films: FilmRow[]; packages: PackageRow[]; counts: WorkflowCounts;
  // null without a host (dispatch) or without a probed GPU
  dispatch: WorkflowDispatch | null; gpu: WorkflowGpu | null;
  // PWR-2: the cluster's live estimate and the history behind 预计耗电 / 每片能耗
  power?: ClusterPower | null; energy_rates?: EnergyRate[];
}
export interface FilmDetail {
  film_id: string; name: string; number: string | null; title: string | null; actors: string[];
  marks: { restored: boolean; chinese_subtitles: boolean; uhd: boolean };
  files: Array<{ role: string; path: string; size: number | null; volume: number | string | null }>;
  inputs: Array<{ path: string; size: number | null }>;
  workcopy_count: number; has_poster: boolean; poster_source: string | null;
  has_cover: boolean; cover_source: string | null;
  group: FilmGroup; can_confirm: boolean; can_retry: boolean; retry_stage: string | null; note: string | null;
  stages: Array<{ stage: string; status: string; note: string | null }>; steps: WorkflowStep[];
  archive: { status: string | null; error: string | null; target_path: string | null };
  cleanup: { input: string | null; workcopy: string | null; kept: Array<{ path: string; status: string }> };
  resolution: Resolution | null; receipt_id: string | null; revisions: FilmRevisions; plan: FilmPlan | null;
  timing?: FilmTiming | null;
  duration_ms?: number | null; energy?: FilmEnergy | null; live_power?: LivePower | null;
  // null when Miko is not configured
  lookup: PlanLookup | null;
}
export interface Performer {
  id: number | string; name: string; source_label: string | null; unc_path: string | null;
  has_thumb: boolean; aliases: string[];
}
// U1 §7.3: the archive target preflight (read-only) and Filmpaw's candidates for the film's NFO
// actors. Without a performer only `candidates` is filled; `target_path` already ends in `film_dir`.
// `missing`: the chosen performer record is gone; `candidates` is null while Filmpaw is offline.
export type TargetErrorCode = "offline" | "permission" | "missing" | "duplicate";
export interface TargetPreflight {
  film_dir: string; performer_id: string | null; target_path: string | null; absent: boolean | null;
  error_code: TargetErrorCode | null; message: string | null;
  candidates: { query: string[]; items: Performer[] } | null;
}
// U1 P3': one unplanned film on the 待处理 list, with the recommendation and its reasons
// (backend `workflow/planning.py`). `lookup` is the Miko metadata preview (v2 L).
export type LookupStatus = "succeeded" | "failed" | "running" | "queued";
export interface PlanLookup {
  status: LookupStatus; number: string | null; title: string | null; actors: string[];
  studio: string | null; release: string | null; runtime_min: number | null; has_poster: boolean;
  error_class: string | null; message: string | null; can_requery: boolean;
}
export interface PlanRecommendation { ltx_model: LtxTier | null; ltx_reason: string; machine: string; machine_reason: string }
export interface PlanEntry {
  film_id: string; input_revision: number; revision_changed: boolean; name: string;
  title: string | null; number: string | null; actors: string[];
  has_poster: boolean; poster_source: string | null; has_cover: boolean; cover_source: string | null;
  resolution: Resolution | null;
  width: number | null; height: number | null; duration_ms: number | null; size_bytes: number | null;
  video_count: number; item_count: number; needs_gpu: boolean; created_at: string;
  lookup: PlanLookup | null; recommendation: PlanRecommendation;
  // PWR-2 PW13: 预计耗电 on the recommended machine (Wh per tier; null = 历史不足)
  prediction?: PlanPrediction | null;
}
// PWR-1/3: the 功耗 tab. A machine's bounded history (at most 168 points; `avg_w` null = a gap).
export type PowerRange = "24h" | "7d" | "30d";
export type PowerActivity = "restore" | "asr" | "translate" | "idle" | "other";
export interface PowerHistory {
  range: PowerRange; step_s: number; estimated: true; t: number[]; covered_s: number[]; avg_w: Array<number | null>;
  wh: number[]; gpu_wh: number[]; cpu_wh: number[]; other_wh: number[];
  activity_wh: Record<PowerActivity, number[]>; totals: { wh: number; activity_wh: Record<PowerActivity, number> };
}
// PW06: `state` ok | off (sampling off) | no_power (an older TaskPaw) | pending | unreachable | unauthorized
// | invalid_address | invalid | timeout | too_large | http_error | not_found | self (this agent's own address:
// listed, never summed twice — IR06).
// `energy`: the last today / week / month Wh the peer reported while that period still runs (null once it
// rolled over); `energy_stale` when the peer is not answering now. `duplicate`: the same machine as the
// earlier peer `duplicate_of` — listed, never summed.
export interface PeerEnergy { today_wh: number | null; week_wh: number | null; month_wh: number | null }
export interface PowerPeerRow {
  name: string; address: string; state: string; error: string | null; machine: string | null; version: string | null;
  os: string | null; power: LivePower | null; history: PowerHistory | null; age_s: number | null; stale: boolean;
  energy?: PeerEnergy | null; energy_stale?: boolean; duplicate_of?: string | null;
}
export interface PowerView {
  // `energy`: this machine's retained today / week / month Wh, present while `power` is null (sampling off)
  power: LivePower | null; energy?: PeerEnergy | null; history: PowerHistory; machine?: { name: string; os: string }; peers?: PowerPeerRow[];
}
export interface PowerPeerSetting { name: string; address: string }
export interface PowerSettings {
  enabled: boolean; tdp_w: number | null; idle_w: number | null; other_w: number | null;
  psu_efficiency: number | null; cpu_source: "estimate" | "none"; peers: PowerPeerSetting[];
}
export interface PlanMachine { id: string; name: string; gpu: string | null; vram_mib: number | null; reason: string }
export interface WorkflowPlans {
  plans: PlanEntry[]; default_tier: LtxTier;
  choices: { tiers: Array<{ value: LtxTier; label: string }>; machines: PlanMachine[] };
}
export interface PlanChoice { film_id: string; input_revision: number; ltx_model: LtxTier | null; machine: string }
// `error` is a code (`cas_conflict` = the input revision changed); `message` its Chinese text.
export interface PlanResult { film_id: string; ok: boolean; error: string | null; message: string | null }
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
// POST /control/workflow/plans takes 1–500 items (backend `PlansBody.plans` max_length).
export const PLAN_BATCH_MAX = 500;

export class HttpError extends Error {
  // `code`: the backend's error code when the body names one (a 409's `cas_conflict` = a changed revision)
  constructor(message: string, public readonly status: number, public readonly code?: string) { super(message); }
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
    let code: string | undefined;
    try {
      const j = monitor ? payload as { detail?: unknown; code?: unknown } : await res.json();
      if (j?.detail) detail = String(j.detail);
      if (typeof j?.code === "string") code = j.code;
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(detail, res.status, code);
  }
  if (res.status === 204) return undefined as T;
  return monitor ? payload as T : res.json() as Promise<T>;
}

// Authenticated image fetch → data: URL (T06 D01): control routes need the bearer
// token and the packaged CSP only allows `img-src 'self' data:`.
async function image(path: string, role: "agent" | "hub" = "agent"): Promise<string> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await fetch(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${controlToken}` } });
  checkUnauthorized(role, res);
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
  power: (range: PowerRange) => get<PowerView>("agent", `/control/power?range=${range}`),
  hubWorkflowFilms: (serverId: number) => get<WorkflowFilms>("hub", `/servers/${serverId}/workflow/films`),
  hubWorkflowFilm: (serverId: number, filmId: string) => get<FilmDetail>("hub", `/servers/${serverId}/workflow/films/${seg(filmId)}`),
  hubWorkflowPlans: (serverId: number) => get<WorkflowPlans>("hub", `/servers/${serverId}/workflow/plans`),
  hubWorkflowImagePath: (serverId: number, filmId: string, kind: "poster" | "cover") =>
    `/servers/${serverId}/workflow/films/${seg(filmId)}/${kind}`,
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
  // U1: the plan gate (待处理), the archive target preflight, metadata lookup and the global pause.
  workflowPlans: () => get<WorkflowPlans>("agent", "/control/workflow/plans"),
  // At most PLAN_BATCH_MAX items per request (the route's `PlansBody` max_length).
  workflowConfirmPlans: (plans: PlanChoice[]) =>
    send<{ results: PlanResult[] }>("agent", "POST", "/control/workflow/plans", { plans }),
  workflowLookup: (filmId: string) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/lookup`),
  workflowTarget: (filmId: string, performerId?: Performer["id"]) =>
    get<TargetPreflight>("agent", `/control/workflow/films/${seg(filmId)}/target${
      performerId === undefined ? "" : `?${new URLSearchParams({ performer_id: String(performerId) })}`}`),
  workflowPause: (paused: boolean) =>
    send<{ paused: boolean }>("agent", "POST", "/control/workflow/pause", { paused }),
  // H3 M1: answer the model download offer of the workflow's own session.
  workflowLtxDownload: (request_id: string, approved: boolean) =>
    send<{ ok: boolean }>("agent", "POST", "/control/workflow/ltx-download", { request_id, approved }),
  // H2 R2: open a current INBOX package folder (204 | 404 missing | 501 unsupported platform).
  workflowOpenPackage: (packageId: string) =>
    send<void>("agent", "POST", `/control/workflow/packages/${seg(packageId)}/open`),
  posterPath: (filmId: string) => `/control/workflow/films/${seg(filmId)}/poster`,
  // U1b: the full landscape jacket (final fanart > thumb > input fanart > thumb > image attachment)
  coverPath: (filmId: string) => `/control/workflow/films/${seg(filmId)}/cover`,
  thumbPath: (performerId: Performer["id"]) => `/control/workflow/performers/${seg(performerId)}/thumb`,
  image,
};
