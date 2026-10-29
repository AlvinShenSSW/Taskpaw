import { hubRequest } from "./hubTransport";
import type { HubNotificationHealth, HubStatusHealth, HubEventChannel } from "./hubHealth";

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
  status_health?: HubStatusHealth | null;
  event_channel?: HubEventChannel | null;
}

export interface HubStatus {
  machine: string;
  servers: HubServer[];
  acks: Record<string, number>;
  self: Record<string, MonitorSnapshot>;
  notifications?: HubNotificationHealth;
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

// P2-2 (#293): 设备 v1 and the worker console, both on the loopback control API.
export type DeviceProfile = {
  stage: "restore" | "asr" | "translate"; tool: string; tool_version: string; model: string; model_version: string;
  profile: string; max_width: number; max_height: number;
};
export type DeviceProbe = { gpu_model: string; vram_mib: number; free_disk_bytes: number; profiles: DeviceProfile[]; config_digest?: string | null };
export type DeviceRow = {
  worker_id: string; version: number; name: string; address: string; port: number;
  enrollment_state: "pending_pairing" | "pending_validation" | "verified" | "retired";
  admission_state: "disabled" | "enabled" | "draining" | "revoked";
  participation: boolean;
  heartbeat: { state: "online" | "lost" | "waiting" | "off"; age_s: number | null };
  gpu: { name: string | null; vram_mib: number | null; used_mib: number | null; util: number | null; power_w: number | null; hold: string } | null;
  disk: { free: number; staging_used: number; cap: number } | null;
  tiers_ready: string[]; agent_version: string | null; version_ok: boolean | null;  // OD7: no asr_ready
  workflow_protocol?: number | null; required_protocol?: number;  // Absent on older backends.
  boot_reconciled: boolean; eligibility: string; eligible: boolean;
  probe: DeviceProbe | null; approved: { probe: DeviceProbe | null; profiles: DeviceProfile[] } | null;
  restore_allowed: boolean; unresolved: number; can_retire: boolean;
  // P2-4 §2: the row's unresolved compute assignments (判定已停止) and its open routes (排队)
  assignments?: DeviceAssignment[]; route_depth?: number;
};
export interface DeviceAssignment {
  assignment_id: string; version: number; film_id: string; film_name: string; stage: string; state: string;
  stop: FilmStopAction | null;
}
export type DevicesView = {
  remote_workers: boolean; server_time: string; controller: { name: string; stages: string[] }; workers: DeviceRow[];
};
export type PairingCode = { worker_id: string; code: string; expires_at: string };
export type WorkerConsole = {
  state: string; reason: string; role: "controller" | "worker"; controller: string | null;
  worker_id?: string | null; credential: "valid" | "revoked" | null; staging_dir: string | null; staging_cap_gb: number;
  last_heartbeat?: string | null; reconciled?: boolean; paused?: boolean;
  // P2-3 §6: the 媒体处理 tab of a worker
  controller_paused?: boolean; pending?: boolean; films?: WorkerFilm[]; current?: WorkerCurrent | null;
  transfers?: WorkerTransfer[]; dispatch_reason?: string | null;
  // P2-3 §11 IR3: what needs the operator (the controller refused a failed result)
  dispatch_notice?: string | null;
  staging?: { used: number | null; cap: number }; gpu?: { name: string | null; vram_mib: number | null; hold: string };
};
// P2-3: a film routed to this worker (position 0 当前, 1 预取) and its envelope's stage statuses.
export interface WorkerFilm {
  film: string; rev: number; name: string; position: number;
  state: "queued" | "fetching" | "staged" | "running" | "settling" | "uploading" | "finishing";
  stages: Record<string, string | null>;
}
export interface WorkerTransfer { film: string; dir: "down" | "up"; done: number; total: number; rate: number }
// The running stage; `step` is shaped like the board's running 修复 step (LtxProgress, 本片参数, 用时).
export interface WorkerCurrent {
  film_id: string; rev?: number; stage: "restore" | "asr"; attempt_id: string; phase: "running" | "settling" | "uploading" | "finishing";
  progress: string; elapsed_s: number; uses_gpu: boolean; step: WorkflowStep; started_at: string;
  power?: LivePower | null; energy?: Record<string, unknown> | null;
}

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
// P2-4 R2/R3 (remote mode; null on a single machine): the film's open route, its plan's mutable
// machine choice (null worker = 自动) and the 设备失联 attention state.
export interface FilmRouteInfo {
  machine: { id: string; name: string }; kind: "auto" | "pinned" | "local"; reason: string; routed_at: string;
  position: number; live: "online" | "lost" | "waiting" | "local"; silent_s: number | null;
}
export interface FilmPin { worker_id: string | null; name: string; version: number }
// R2/R8: 改派 for an unclaimed planned film; 判定已停止 of an unresolved remote assignment, allowed only
// when both channels (heartbeat, /status) have been dead for 5 min (C4), else `reason` says which is alive.
export interface FilmStopAction {
  assignment_id: string; expected_version: number; stage: string; worker: { id: string; name: string };
  allowed: boolean; reason: string | null; heartbeat_silent_s: number | null; status_quiet_s: number | null;
  // the status channel's address (`host:port`) and how its last read ended (timeout / unreachable / idle / …)
  address?: string; status_state?: string | null;
}
export interface FilmActions { can_reassign: boolean; stop: FilmStopAction | null }
export interface ReassignOption {
  worker_id: string | null; name: string; reason: string | null; current: boolean;
  // 预计 (scheduler.preview): the finish and energy when it can take the film; 自动 names its device
  finish_at?: string | null; energy_wh?: number | null; machine?: { id: string; name: string } | null;
}
// P3 E1 (#304): an INBOX source that vanished after intake — the missing paths and when it was found
export interface SourceMissing { paths: string[]; found_at: string }
export interface FilmRemote {
  attention_kind?: "device_lost" | "source_missing" | null; route?: FilmRouteInfo | null; pin?: FilmPin | null;
  actions?: FilmActions | null;
  // P3 E1/E3: 片源丢失 {paths, found_at}; whether 删除此任务 is offered (no stage running or claimed, not archived)
  source_missing?: SourceMissing | null; can_remove?: boolean;
  // #298 (§9 CX27): the 失联 worker, also when its route is on an older input revision
  lost_worker?: { id: string; name: string } | null;
}
export interface FilmRow extends FilmRemote {
  film_id: string; name: string; group: FilmGroup; stage: string | null; status: string | null;
  note: string | null; can_confirm: boolean; can_retry: boolean; retry_stage: string | null;
  created_at: string | null; steps: WorkflowStep[];
  completed_at?: string | null;
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
  // P2-4 R10: the machines the stage's attempts ran on
  devices?: Array<{ id: string; name: string }>;
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
  // P2-3 R15: a stage running on a remote worker — its 设备 and attempt (停止并重新排队)
  machine?: { id: string; name: string } | null; attempt_id?: string | null;
  phase?: "running" | "settling" | "uploading" | "finishing" | null;
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
  done_paging?: { total: number; size: number; page: number; pages: number };
  // null without a host (dispatch) or without a probed GPU
  dispatch: WorkflowDispatch | null; gpu: WorkflowGpu | null;
  // PWR-2: the cluster's live estimate and the history behind 预计耗电 / 每片能耗
  power?: ClusterPower | null; energy_rates?: EnergyRate[];
}
export interface FilmDetail extends FilmRemote {
  film_id: string; name: string; number: string | null; title: string | null; actors: string[];
  marks: { restored: boolean; chinese_subtitles: boolean; uhd: boolean };
  files: Array<{ role: string; path: string; size: number | null; volume: number | string | null }>;
  inputs: Array<{ path: string; size: number | null }>;
  workcopy_count: number; has_poster: boolean; poster_source: string | null;
  has_cover: boolean; cover_source: string | null;
  group: FilmGroup; can_confirm: boolean; can_retry: boolean; retry_stage: string | null; note: string | null;
  stages: Array<{ stage: string; status: string; note: string | null }>; steps: WorkflowStep[];
  archive: { status: string | null; error: string | null; target_path: string | null; manual: boolean };
  cleanup: { input: string | null; workcopy: string | null; kept: Array<{ path: string; status: string }> };
  resolution: Resolution | null; receipt_id: string | null; revisions: FilmRevisions; plan: FilmPlan | null;
  timing?: FilmTiming | null;
  history?: { completed_at: string | null; total_seconds: number | null;
    steps: Array<{ key: string; device: string; seconds: number | null; result: string }> } | null;
  duration_ms?: number | null; energy?: FilmEnergy | null; live_power?: LivePower | null;
  // null when Miko is not configured
  lookup: PlanLookup | null;
  // P2-4 §2: the 改派 dialog's choices (remote mode only)
  reassign_options?: ReassignOption[] | null;
}
export interface Performer {
  id: number | string; name: string; source_label: string | null; unc_path: string | null;
  has_thumb: boolean; aliases: string[];
}
// #361 §6–§10: the controller's own library (`/control/workflow/library/*`). Counts are the last
// scan's (`last_scan_ok` null = never scanned, false = unreachable then).
export type LibraryKind = "actor" | "category";
export type LibraryRole = "archive" | "readonly";
export interface Library {
  id: string; kind: LibraryKind; path: string; label: string; role: LibraryRole; prefixes: string[]; position: number;
  last_scan_at: string | null; last_scan_ok: boolean | null; last_scan_ms: number | null;
  film_count: number | null; free_bytes: number | null; created_at: string;
  performer_count: number; missing_count: number;
}
export interface LibraryList { items: Library[]; db_path: string }
export interface ScanResult { library_id: string; ok: boolean; at?: string; error?: string | null }
export interface ScanStatus { running: string | null; queued: string[]; results: Record<string, ScanResult> }
export type MultiLocation = "priority" | "most" | "recent";
export interface LibraryRules {
  multi_location: MultiLocation; alias_match: boolean; preselect_single: boolean; fuzzy: boolean;
  allow_new_actor: boolean; new_actor_library: string | null; new_actor_name: "actor" | "ask";
  category_prefix: boolean; second_target_hint: boolean; category_always: boolean;
  scan_on_start: boolean; scan_every_hours: 0 | 2 | 6 | 12 | 24;
}
export interface LibraryPerformer extends Performer {
  id: string; name_norm: string; alias_ids: number[]; library_id: string; role: LibraryRole; is_missing: boolean;
  film_count: number | null; fuzzy: boolean;
}
export type PerformerFilter = "multi" | "alias" | "missing" | "nothumb";
export interface PerformerQuery { q?: string; library_id?: string; filter?: PerformerFilter; page?: number; page_size?: number }
export interface PerformerPage { items: LibraryPerformer[]; total: number; page: number; page_size: number }
export type HistoryKind = "performer" | "category" | "new_actor" | "manual";
export type HistoryFilter = "pending" | "new" | "category" | "manual" | "second";
export interface HistoryRow {
  receipt_id: string; film_id: string; operation_id: string; accepted_at: string; kind: HistoryKind | null;
  film_dir: string; target_path: string; second_target_path: string | null;
  cleanup: { input: string | null; workcopy: string | null }; pending: boolean;
  number: string; title: string | null; actors: string[];
}
export interface HistoryPage { items: HistoryRow[]; total: number; page: number; page_size: number }
export interface HistoryReceipt extends HistoryRow {
  files: Array<{ path: string; size: number; sha256: string }>; message: string | null;
}
export interface PerformerDetail extends LibraryPerformer { locations: LibraryPerformer[]; receipts: HistoryRow[] }
export interface ImportPreview {
  path: string; roots: Array<{ path: string; label: string; performers: number; status: "new" | "exists" | "overlap" }>;
  aliases: number; new_aliases: number;
}
export interface ImportResult { path: string; added: string[]; skipped: Array<{ path: string; reason: string }>; aliases_added: number }
export interface LibraryBody { kind: LibraryKind; path: string; label?: string; role?: LibraryRole; prefixes?: string[] }
export type LibraryPatch = Partial<Pick<Library, "label" | "role" | "prefixes">>;
// U1 §7.3: the archive target preflight (read-only) and the library's candidates for the film's NFO
// actors. With no explicit target, a sole candidate is checked; `target_path` ends in `film_dir`.
// `missing`: the chosen performer or directory is gone; `candidates` is null while the library is offline.
export type TargetErrorCode = "offline" | "permission" | "missing" | "duplicate";
export interface TargetPreflight {
  film_dir: string; performer_id: string | null; target_path: string | null; absent: boolean | null;
  target_dir: string | null; manual: boolean;
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
export interface PlanRecommendation { ltx_model: LtxTier | null; ltx_reason: string; machine: string | null; machine_reason: string }
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
  // P2-4 R5 (remote mode; null on a single machine): per tier ("none" = 无需修复), 自动's predicted
  // device or why it would wait, and every device's own finish or eligibility reason (预计).
  routing?: Record<string, PlanRouting> | null;
}
export interface PlanRoutingDevice {
  id: string; name: string; gpu: string | null; vram_mib: number | null;
  finish_at: string | null; energy_wh: number | null; reason: string | null;
}
export interface PlanRouting {
  auto: { machine: { id: string; name: string }; finish_at: string; energy_wh: number | null; text: string }
    // P2-4 §9 CX2 / OD7 R7: waiting null + text 「无需修复，本机完成」 when no restore needs a worker
    | { waiting: string | null; text?: string };
  devices: PlanRoutingDevice[];
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
// #336: the machine's last sensor sample (LibreHardwareMonitor, else nvidia-smi GPU only), independent of
// estimated watts. `sensors_age_s` beside it is seconds since the controller sampled / received it.
export interface MachineSensors {
  at: number | null; source: "lhm" | "nvidia-smi" | null; cpu_c: number | null; gpu_c: number | null;
  fans: { name: string; rpm: number }[];
}
export interface PowerPeerRow {
  name: string; address: string; state: string; error: string | null; machine: string | null; version: string | null;
  os: string | null; power: LivePower | null; history: PowerHistory | null; age_s: number | null; stale: boolean;
  energy?: PeerEnergy | null; energy_stale?: boolean; duplicate_of?: string | null;
  // P2-4 R11: `workflow` = an enrolled GPU worker that joined automatically (工作机 · 自动加入)
  source?: "config" | "workflow";
  sensors?: MachineSensors | null; sensors_age_s?: number | null;
}
export interface PowerView {
  // `energy`: this machine's retained today / week / month Wh, present while `power` is null (sampling off)
  power: LivePower | null; energy?: PeerEnergy | null; history: PowerHistory; machine?: { name: string; os: string }; peers?: PowerPeerRow[];
  sensors?: MachineSensors | null; sensors_age_s?: number | null;
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
  // P2-4 R5: remote mode (the select starts with 自动; a null machine is 自动)
  remote?: boolean;
}
export interface PlanChoice { film_id: string; input_revision: number; ltx_model: LtxTier | null; machine: string | null }
// P2-4 R13–R16: 设置 · 媒体工作流 (agent.yaml `workflow`; no token or key lives in it).
export type WorkflowRole = "off" | "controller" | "worker";
export interface WorkflowSettingsView {
  enabled: boolean; role: WorkflowRole;
  inbox_root: string | null; data_dir: string | null; output_dir?: string | null; ffprobe_path: string | null;
  jasna_exe_path: string | null; whisperjav_exe_path: string | null; ltx_gpu: string;
  mdcx_python: string | null; mdcx_root: string | null; mdcx_profile: string | null;
  remote_workers: boolean; worker_api_port: number; staging_dir: string | null; staging_cap_gb: number;
}
export interface WorkflowSettingsState {
  output_dir_default?: boolean; // derived location; the wizard follows its chosen data_dir
  saved: WorkflowSettingsView; running: WorkflowSettingsView; restart_required: boolean;
  busy: { films: number; remote: number; unknown: boolean };
  presets: Array<{ id: string; label: string }>;
  // what an empty ffprobe field uses (null: not on PATH)
  ffprobe_detected: string | null;
  // each saved directory / tool found or missing (null: unset), and whether Miko's three fields are complete
  paths?: Partial<Record<keyof WorkflowSettingsView, "ok" | "missing" | null>>;
  miko?: "complete" | "partial" | "off";
  // #299 §12 operator decision: false off Windows (only 不参与 is offered)
  media_supported?: boolean;
}
// #299 review IR10: `create_dirs` names the suggested folders the save makes once it validated
export type WorkflowSettingsPatch = { role: WorkflowRole } & Partial<Omit<WorkflowSettingsView, "enabled" | "role">>
  & { create_dirs?: Array<"inbox_root" | "data_dir" | "output_dir" | "staging_dir"> };
// #299: the first-run wizard, 网络与访问, detection, 立即重启 and the Hub's settings.
export type SetupRole = "agent" | "controller" | "worker";
export type SetupStep = "role" | "network" | "controller" | "worker_fields" | "worker_join" | "done";
export interface SetupState { status: "absent" | "pending" | "done"; step: SetupStep | null; role: SetupRole | null }
export interface NetworkAddress { ip: string; nic: string; primary: boolean }
export interface NetworkView {
  lan_access: boolean; bind_host: string; bind_port: number; control_port: number;
  addresses: NetworkAddress[]; address_changed_from: string | null; running_bind_host?: string;
  token_set: boolean; api_token_hint: string | null;
  firewall: { platform: "windows" | "macos" | "other"; rule: "present" | "absent" | "unknown"; profile: "private" | "public" | "domain" | null };
}
export type FirewallResult = "added" | "declined" | "failed" | "unsupported" | "timeout" | "pending";
export interface ToolFound { found: boolean; path: string | null; source: "saved" | "monitor" | "default" | "path" | "bundled" | null; version: string | null }
export interface MikoFound { status: "complete" | "partial" | "off"; root: string | null; python: string | null; profile: string | null }
export interface GpuFound { name: string; vram_mib: number; driver: string | null; power_limit_w: number | null; worker_ok: boolean }
export interface Detection {
  gpu: GpuFound | null;
  tools: { ffprobe: ToolFound; jasna: ToolFound; whisperjav: ToolFound; miko: MikoFound };
  suggested: { inbox: string | null; data_dir: string | null; staging: string | null };
}
export interface DirectoryFacts { exists: boolean | null; free_bytes: number | null; volume: string; local: boolean }
export interface RestartState { required: boolean; fields: string[]; blocked: string | null; available: boolean }
export interface HubSettingsValues {
  openclaw_enabled: boolean; openclaw_url: string; openclaw_token: string; polling_token: string;
  polling_token_hint: string | null; poll_interval: number; write_status_md: boolean; status_md_path: string | null;
  status_md_default: string; status_log_retention_days: number; lan_access: boolean; bind_host: string; api_token: string;
}
export interface HubSettingsState {
  saved: HubSettingsValues; running: HubSettingsValues; restart_required: boolean; fields: string[];
  addresses: NetworkAddress[]; restart_available: boolean;
  // #299 review IR2: the polled TaskPaw the operator chose as 主控 (null: none chosen)
  controller_server_id: number | null;
}
export type OpenclawTest = { ok: true; at: string; elapsed_s: number } | { ok: false; error: "unreachable" | "token" | "timeout" | "http"; status?: number };
// `error` is a code (`cas_conflict` = the input revision changed); `message` its Chinese text.
export interface PlanResult { film_id: string; ok: boolean; error: string | null; message: string | null }
const seg = (id: string | number) => encodeURIComponent(String(id));
const LIBRARY = "/control/workflow/library";

export type ControlRole = "agent" | "hub";
declare global {
  interface Window {
    __TASKPAW__?: { baseUrl?: string; controlToken?: string; role?: ControlRole; bootId?: string; restart?: boolean };
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
// #299 §3: the shell supervises exit 75 only when it says so; the boot the window was built for.
let injectedBoot: { role: ControlRole; bootId: string; restart: boolean } | null = null;
function consumeInjection() {
  const injected = window.__TASKPAW__;
  if (!injected || consumedInjection === injected) return;
  consumedInjection = injected;
  if ((injected.role === "agent" || injected.role === "hub")
    && typeof injected.baseUrl === "string" && canonicalControlBase(injected.baseUrl)
    && typeof injected.controlToken === "string" && validToken(injected.controlToken)
    && /^[0-9a-f]{32}$/.test(injected.bootId ?? "")) {
    credentials[injected.role] = { baseUrl: injected.baseUrl, controlToken: injected.controlToken, verified: true };
    injectedBoot = { role: injected.role, bootId: injected.bootId as string, restart: injected.restart === true };
  }
}
export function restartSupported(role: ControlRole): boolean { consumeInjection(); return injectedBoot?.role === role && injectedBoot.restart; }
export function controlBootId(role: ControlRole): string | null { consumeInjection(); return injectedBoot?.role === role ? injectedBoot.bootId : null; }
export function hasControlCredential(role: ControlRole): boolean { consumeInjection(); return credentials[role]?.verified === true; }
export function clearControlCredentials(role?: ControlRole) {
  if (!role || injectedBoot?.role === role) injectedBoot = null;
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
// and adds the status so callers can tell e.g. 503 "library unavailable" from other errors.
// POST /control/workflow/plans takes 1–500 items (backend `PlansBody.plans` max_length).
export const PLAN_BATCH_MAX = 500;

export class HttpError extends Error {
  // `code`: the backend's error code when the body names one (a 409's `cas_conflict` = a changed revision)
  // `fields`: a 422's per-field messages (P2-4 设置 · 媒体工作流), never echoing a value
  constructor(message: string, public readonly status: number, public readonly code?: string,
    public readonly fields?: Record<string, string>) { super(message); }
}

async function get<T>(role: "agent" | "hub", path: string, signal?: AbortSignal): Promise<T> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await (role === "hub" ? hubRequest : fetch)(`${baseUrl}${path}`, {
    headers: { Authorization: `Bearer ${controlToken}` },
    ...(role === "hub" ? { signal } : {}),
  });
  checkUnauthorized(role, res);
  if (!res.ok) throw new HttpError(`${path} → ${res.status}`, res.status);
  return res.json() as Promise<T>;
}

// The library's GETs (#361) keep the backend's `detail`, as send() does: 「不是可读的 Filmpaw 片库…」
// says more than "path → 422". The plain get() stays as it is for its other callers.
async function libraryGet<T>(path: string): Promise<T> {
  const { baseUrl, controlToken } = cfg("agent");
  const res = await fetch(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${controlToken}` } });
  checkUnauthorized("agent", res);
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const j: unknown = await res.json();
      if (j && typeof j === "object" && "detail" in j && typeof j.detail === "string") detail = j.detail;
    } catch { /* non-JSON error body */ }
    throw new HttpError(detail, res.status);
  }
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
  const res = await hubRequest(`${baseUrl}${path}`, {
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
  method: "POST" | "DELETE" | "PATCH" | "PUT",
  path: string,
  body?: unknown,
): Promise<T> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await (role === "hub" ? hubRequest : fetch)(`${baseUrl}${path}`, {
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
    let fields: Record<string, string> | undefined;
    try {
      const j = monitor ? payload as { detail?: unknown; code?: unknown; fields?: unknown } : await res.json();
      if (j?.detail) detail = String(j.detail);
      if (typeof j?.code === "string") code = j.code;
      if (j?.fields && typeof j.fields === "object" && !Array.isArray(j.fields)) {
        fields = Object.fromEntries(Object.entries(j.fields as Record<string, unknown>).map(([k, v]) => [k, String(v)]));
      }
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(detail, res.status, code, fields);
  }
  if (res.status === 204) return undefined as T;
  return monitor ? payload as T : res.json() as Promise<T>;
}

// Authenticated image fetch → data: URL (T06 D01): control routes need the bearer
// token and the packaged CSP only allows `img-src 'self' data:`.
async function image(path: string, role: "agent" | "hub" = "agent"): Promise<string> {
  const { baseUrl, controlToken } = cfg(role);
  const res = await (role === "hub" ? hubRequest : fetch)(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${controlToken}` } });
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
  hubStatus: (context?: { signal?: AbortSignal }) => get<HubStatus>("hub", "/status", context?.signal),
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
  hubEvents: (p: { server?: number; level?: string; limit?: number } = {}, signal?: AbortSignal) => {
    const qs = new URLSearchParams();
    if (p.server != null) qs.set("server", String(p.server));
    if (p.level) qs.set("level", p.level);
    qs.set("limit", String(p.limit ?? 200));
    return get<{ events: EventItem[] }>("hub", `/events?${qs.toString()}`, signal);
  },
  // Manage the agents the Hub polls, from the dashboard (#124). Bearer-gated like
  // the rest of the Hub API (#106); errors surface the backend `detail`.
  hubAddServer: (s: { name: string; ip: string; port: number }) =>
    send<HubServer & { ok: boolean }>("hub", "POST", "/servers", s),
  hubUpdateServer: (id: number, patch: { name?: string; ip?: string; port?: number; enabled?: boolean }) =>
    send<HubServer & { ok: boolean }>("hub", "PATCH", `/servers/${id}`, patch),
  // #299 Codex r4: 更换主控 selects/creates the server row for the address and names it the 主控
  hubReplaceController: (ip: string, port: number) =>
    send<{ ok: boolean; id: number; created: boolean }>("hub", "PUT", "/config/controller", { ip, port }),
  hubRemoveServer: (id: number) => send<{ ok: boolean }>("hub", "DELETE", `/servers/${id}`),
  hubSetPollingToken: (polling_token: string) =>
    send<{ ok: boolean }>("hub", "PATCH", "/config", { polling_token }),
  // Media workflow (T06-P1 §8): local control API only; paths are resolved by film id.
  power: (range: PowerRange) => get<PowerView>("agent", `/control/power?range=${range}`),
  hubWorkflowFilms: (serverId: number, donePage = 1) => get<WorkflowFilms>("hub", `/servers/${serverId}/workflow/films?done_page=${donePage}`),
  hubWorkflowFilm: (serverId: number, filmId: string) => get<FilmDetail>("hub", `/servers/${serverId}/workflow/films/${seg(filmId)}`),
  hubWorkflowPlans: (serverId: number) => get<WorkflowPlans>("hub", `/servers/${serverId}/workflow/plans`),
  hubWorkflowImagePath: (serverId: number, filmId: string, kind: "poster" | "cover") =>
    `/servers/${serverId}/workflow/films/${seg(filmId)}/${kind}`,
  workflowFilms: (donePage = 1) => get<WorkflowFilms>("agent", `/control/workflow/films?done_page=${donePage}`),
  workflowFilm: (filmId: string) => get<FilmDetail>("agent", `/control/workflow/films/${seg(filmId)}`),
  performers: (query: string) =>
    get<{ items: Performer[] }>("agent", `/control/workflow/performers?${new URLSearchParams({ q: query })}`),
  workflowConfirm: (filmId: string, performer_id?: Performer["id"], target_dir?: string) =>
    send<{ approval_id: string }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/confirm`,
      target_dir === undefined ? { performer_id } : { target_dir }),
  workflowRetry: (filmId: string, stage: string) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/retry`, { stage }),
  workflowOpenFolder: (filmId: string, which: "output" | "archive") =>
    send<void>("agent", "POST", `/control/workflow/films/${seg(filmId)}/open-folder`, { which }),
  workflowOpen: (filmId: string, what: "inbox" | "output") =>
    send<void>("agent", "POST", `/control/workflow/films/${seg(filmId)}/open`, { what }),
  workflowPlay: (filmId: string) =>
    send<void>("agent", "POST", `/control/workflow/films/${seg(filmId)}/play`),
  // U1: the plan gate (待处理), the archive target preflight, metadata lookup and the global pause.
  workflowPlans: () => get<WorkflowPlans>("agent", "/control/workflow/plans"),
  // At most PLAN_BATCH_MAX items per request (the route's `PlansBody` max_length).
  workflowConfirmPlans: (plans: PlanChoice[]) =>
    send<{ results: PlanResult[] }>("agent", "POST", "/control/workflow/plans", { plans }),
  workflowLookup: (filmId: string) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/lookup`),
  workflowTarget: (filmId: string, performerId?: Performer["id"], targetDir?: string) =>
    get<TargetPreflight>("agent", `/control/workflow/films/${seg(filmId)}/target${
      targetDir !== undefined ? `?${new URLSearchParams({ target_dir: targetDir })}`
        : performerId === undefined ? "" : `?${new URLSearchParams({ performer_id: String(performerId) })}`}`),
  workflowPause: (paused: boolean) =>
    send<{ paused: boolean }>("agent", "POST", "/control/workflow/pause", { paused }),
  // H3 M1: answer the model download offer of the workflow's own session.
  workflowLtxDownload: (request_id: string, approved: boolean) =>
    send<{ ok: boolean }>("agent", "POST", "/control/workflow/ltx-download", { request_id, approved }),
  // H2 R2: open a current INBOX package folder (204 | 404 missing | 501 unsupported platform).
  workflowOpenPackage: (packageId: string) =>
    send<void>("agent", "POST", `/control/workflow/packages/${seg(packageId)}/open`),
  // P2-2: 设备 (every command carries the worker's `expected_version`) and the worker console.
  workflowDevices: () => get<DevicesView>("agent", "/control/workflow/devices"),
  workflowAddDevice: (name: string, address: string) =>
    send<PairingCode>("agent", "POST", "/control/workflow/devices", { name, address }),
  workflowRepairDevice: (id: string, expected_version: number) =>
    send<PairingCode>("agent", "POST", `/control/workflow/devices/${seg(id)}/pairing`, { expected_version }),
  // OD7 (#298 R10): a GPU worker is approved for restore only
  workflowApproveDevice: (id: string, expected_version: number, restore: boolean) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/devices/${seg(id)}/approve`, { expected_version, restore }),
  workflowDeviceAdmission: (id: string, expected_version: number, state: "enabled" | "draining") =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/devices/${seg(id)}/admission`, { expected_version, state }),
  workflowRevokeDevice: (id: string, expected_version: number) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/devices/${seg(id)}/revoke`, { expected_version }),
  workflowRetireDevice: (id: string, expected_version: number) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/devices/${seg(id)}/retire`, { expected_version }),
  workflowWorker: () => get<WorkerConsole>("agent", "/control/workflow/worker"),
  workflowJoin: (code: string, staging_dir: string) =>
    send<WorkerConsole>("agent", "POST", "/control/workflow/worker/join", { code, staging_dir }),
  // P2-3 R12: 暂停领取 / 恢复领取 on the worker's own console
  workflowWorkerPause: (paused: boolean) =>
    send<{ paused: boolean }>("agent", "POST", "/control/workflow/worker/pause", { paused }),
  // P2-3 R9: 停止并重新排队 of a running remote restore or ASR
  workflowRequeue: (filmId: string, attemptId: string) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/requeue`, { attempt_id: attemptId }),
  // P3 E2: 重新检查 of a 片源丢失 film; `source_missing` is null once the files are back; `unreachable`:
  // the INBOX share could not be reached, so nothing was learned
  workflowRecheckSource: (filmId: string) =>
    send<{ ok: boolean; source_missing: SourceMissing | null; unreachable?: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/recheck-source`, {}),
  // P3 E3: 删除此任务 — 409 (coded) while a stage runs or is claimed, or the film was confirmed or archived
  workflowRemove: (filmId: string) =>
    send<{ ok: boolean; leftover: number }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/remove`, {}),
  // P2-4 R7: 改派 (null = 自动) — 409 `route_claimed` 「已开始处理」 when a claim won
  workflowPin: (filmId: string, input_revision: number, expected_version: number, worker_id: string | null) =>
    send<{ ok: boolean }>("agent", "POST", `/control/workflow/films/${seg(filmId)}/pin`, { input_revision, expected_version, worker_id }),
  // P2-4 R8: 判定已停止 of one unresolved remote assignment (C4: both channels dead for 5 min)
  workflowAttest: (filmId: string, assignment_id: string, expected_version: number) =>
    send<Record<string, unknown>>("agent", "POST", `/control/workflow/films/${seg(filmId)}/attest-stopped`, { assignment_id, expected_version }),
  // P2-4 R13–R16: 设置 · 媒体工作流
  workflowSettings: () => get<WorkflowSettingsState>("agent", "/control/workflow/settings"),
  workflowSaveSettings: (patch: WorkflowSettingsPatch) =>
    send<{ ok: boolean; restart_required: boolean; message?: string }>("agent", "PUT", "/control/workflow/settings", patch),
  // 「创建此目录」: a local absolute folder (422 `fields.path` otherwise)
  workflowOpenDir: (path: string) =>
    send<void>("agent", "POST", "/control/workflow/settings/directory/open", { path }),
  workflowCreateDir: (path: string) =>
    send<{ ok: boolean; created: boolean }>("agent", "POST", "/control/workflow/settings/directory", { path }),
  // #299: first-run wizard (§6), 网络与访问 (R4–R6), detection (§5), 立即重启 (§3)
  setup: () => get<SetupState>("agent", "/control/setup"),
  setupStep: (step: SetupStep, role: SetupRole) => send<SetupState>("agent", "PUT", "/control/setup", { step, role }),
  setupComplete: () => send<SetupState>("agent", "POST", "/control/setup/complete"),
  network: () => get<NetworkView>("agent", "/control/network"),
  networkToken: (action: "generate" | "reveal") => send<{ token: string }>("agent", "POST", "/control/network/token", { action }),
  networkFirewall: () => send<{ result: FirewallResult }>("agent", "POST", "/control/network/firewall"),
  workflowDetect: (refresh = false) => get<Detection>("agent", `/control/workflow/detect${refresh ? "?refresh=1" : ""}`),
  inspectDirectory: (path: string) =>
    send<DirectoryFacts>("agent", "POST", "/control/workflow/settings/directory/inspect", { path }),
  restartState: () => get<RestartState>("agent", "/control/restart"),
  restart: (role: ControlRole, boot_id: string) =>
    send<{ restarting: boolean }>(role, "POST", role === "agent" ? "/control/restart" : "/restart", { boot_id }),
  hubSettings: (context?: { signal?: AbortSignal }) => get<HubSettingsState>("hub", "/config/settings", context?.signal),
  hubSaveSettings: (patch: Partial<Record<keyof HubSettingsValues | "controller_server_id", unknown>>) =>
    send<{ ok: boolean; restart_required: boolean; fields: string[] }>("hub", "PUT", "/config/settings", patch),
  hubOpenclawTest: (url: string, token: string) => send<OpenclawTest>("hub", "POST", "/config/openclaw/test", { url, token }),
  posterPath: (filmId: string) => `/control/workflow/films/${seg(filmId)}/poster`,
  // U1b: the full landscape jacket (final fanart > thumb > input fanart > thumb > image attachment)
  coverPath: (filmId: string) => `/control/workflow/films/${seg(filmId)}/cover`,
  thumbPath: (performerId: Performer["id"]) => `/control/workflow/performers/${seg(performerId)}/thumb`,
  // #361 §10: 片库与规则 and 设置 · 片库 (controller only)
  libraries: () => libraryGet<LibraryList>(`${LIBRARY}/libraries`),
  libraryAdd: (body: LibraryBody) => send<Library>("agent", "POST", `${LIBRARY}/libraries`, body),
  libraryUpdate: (id: string, patch: LibraryPatch) => send<Library>("agent", "PATCH", `${LIBRARY}/libraries/${seg(id)}`, patch),
  libraryRemove: (id: string) => send<void>("agent", "DELETE", `${LIBRARY}/libraries/${seg(id)}`),
  // 归档规则's priority: every actor library id once, in the new order
  libraryOrder: (ids: string[]) => send<LibraryList>("agent", "PUT", `${LIBRARY}/order`, { ids }),
  libraryScan: (id: string) => send<ScanStatus>("agent", "POST", `${LIBRARY}/libraries/${seg(id)}/scan`),
  libraryScanAll: () => send<ScanStatus>("agent", "POST", `${LIBRARY}/scan-all`),
  libraryScanStatus: () => libraryGet<ScanStatus>(`${LIBRARY}/scan-status`),
  // the 演员 tab lists missing directories too (the route's default leaves them out, §10) and pages
  // over name groups, each with every location (#365)
  libraryPerformers: (query: PerformerQuery) => {
    const qs = new URLSearchParams({ include_missing: "true", group: "true" });
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== "") qs.set(key, String(value));
    return libraryGet<PerformerPage>(`/control/workflow/performers?${qs}`);
  },
  libraryPerformer: (id: string) => libraryGet<PerformerDetail>(`/control/workflow/performers/${seg(id)}`),
  libraryAddAlias: (performerId: string, alias: string) =>
    send<{ id: number; alias: string }>("agent", "POST", `${LIBRARY}/performers/${seg(performerId)}/aliases`, { alias }),
  libraryDeleteAlias: (aliasId: number) => send<void>("agent", "DELETE", `${LIBRARY}/aliases/${aliasId}`),
  libraryRules: () => libraryGet<LibraryRules>(`${LIBRARY}/rules`),
  librarySaveRules: (changes: Partial<LibraryRules>) => send<LibraryRules>("agent", "PUT", `${LIBRARY}/rules`, changes),
  libraryImportPreview: (path = "") =>
    libraryGet<ImportPreview>(`${LIBRARY}/import/preview${path ? `?${new URLSearchParams({ path })}` : ""}`),
  libraryImport: (path = "") => send<ImportResult>("agent", "POST", `${LIBRARY}/import`, path ? { path } : {}),
  libraryHistory: (filter: HistoryFilter | null, q: string, page: number) => {
    const qs = new URLSearchParams({ page: String(page) });
    if (filter) qs.set("filter", filter);
    if (q) qs.set("q", q);
    return libraryGet<HistoryPage>(`${LIBRARY}/history?${qs}`);
  },
  libraryReceipt: (receiptId: string) => libraryGet<HistoryReceipt>(`${LIBRARY}/history/${seg(receiptId)}`),
  // 打开目录: only a library root's subtree or a receipt's target (403 otherwise)
  libraryOpen: (path: string) => send<void>("agent", "POST", `${LIBRARY}/open`, { path }),
  image,
};
