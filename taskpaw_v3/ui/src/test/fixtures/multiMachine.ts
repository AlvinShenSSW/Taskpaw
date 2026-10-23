// P2-4 (#295) remote-mode fixtures in the backend's shapes as built (workflow_reads.board_inputs,
// planning.routing_view, devices.device_view, workflow_settings): the confirmed preview's scenes —
// 待处理 with 自动 and per-device finishes, a 设备失联 film, the controller-disk gate and the settings.
import type {
  DeviceRow, DevicesView, FilmActions, FilmDetail, FilmRouteInfo, FilmRow, FilmStopAction, PlanRouting,
  WorkflowFilms, WorkflowPlans, WorkflowRole, WorkflowSettingsState, WorkflowSettingsView,
} from "../../api";
import { counts, detail, FILMS_RESPONSE, plan, PLANS, row, steps } from "./mediaWorkspace";

// The finishes are built from the test's own clock so 「预计 HH:MM」 is today.
const at = (h: number, m: number) => { const d = new Date(); d.setHours(h, m, 0, 0); return d.toISOString(); };
const hm = (iso: string) => { const d = new Date(iso); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };

export const W = {
  ti70: { id: "w-5070", name: "5070 Ti" }, ti1: { id: "w-5060-1", name: "5060 Ti-1" },
  ti2: { id: "w-5060-2", name: "5060 Ti-2" }, ti3: { id: "w-5060-3", name: "5060 Ti-3" },
};

export const FINISH = { ti70: at(21, 40), ti1: at(22, 35) };
export const FINISH_TEXT = { ti70: hm(FINISH.ti70), ti1: hm(FINISH.ti1) };

const device = (w: { id: string; name: string }, over: Partial<PlanRouting["devices"][number]> = {}) =>
  ({ id: w.id, name: w.name, gpu: "NVIDIA GeForce RTX 5060 Ti", vram_mib: 16311, finish_at: null, energy_wh: null, reason: null, ...over });

// `routing[tier]` of a 1080p film: 自动 picks the 5070 Ti; 5060 Ti-2 lacks the 高 model; 5060 Ti-3 is 离线.
export function routing1080(tier: "standard" | "good" | "high"): PlanRouting {
  return {
    auto: { machine: W.ti70, finish_at: FINISH.ti70, energy_wh: 900,
      text: `预计 ${FINISH_TEXT.ti70} 在 5070 Ti 完成 · 约 0.9 度` },
    devices: [
      device(W.ti70, { gpu: "NVIDIA GeForce RTX 5070 Ti", finish_at: FINISH.ti70, energy_wh: 900 }),
      device(W.ti1, { finish_at: FINISH.ti1, energy_wh: 800 }),
      device(W.ti2, tier === "high" ? { reason: "缺少高画质模型" } : { finish_at: FINISH.ti1, energy_wh: 800 }),
      device(W.ti3, { reason: "离线" }),
    ],
  };
}
const routing4k: PlanRouting = {
  auto: { waiting: "等待设备：显存不足 4K" },
  devices: [W.ti70, W.ti1, W.ti2].map((w) => device(w, { reason: "显存不足 4K" })).concat(device(W.ti3, { reason: "离线" })),
};
const noGpu: PlanRouting = { auto: { waiting: null, text: "无需修复，本机完成" }, devices: [] };

const AUTO = { machine: null, machine_reason: "自动（推荐）" };
export const REMOTE_PLANS: WorkflowPlans = {
  ...PLANS, remote: true,
  plans: [
    plan({ film_id: "far-1", title: "远岸来信", recommendation: { ltx_model: "high", ltx_reason: "默认", ...AUTO },
      routing: { standard: routing1080("standard"), good: routing1080("good"), high: routing1080("high") } }),
    plan({ film_id: "mtn-7", title: "山海一页", recommendation: { ltx_model: "good", ltx_reason: "默认", ...AUTO },
      routing: { standard: routing1080("standard"), good: routing1080("good"), high: routing1080("high") } }),
    plan({ film_id: "qlx-1", title: "青岚序曲", resolution: "4K", width: 3840, height: 2160,
      recommendation: { ltx_model: "high", ltx_reason: "默认", ...AUTO },
      routing: { standard: routing4k, good: routing4k, high: routing4k } }),
    plan({ film_id: "pap-3", title: "纸上潮汐", needs_gpu: false, recommendation: { ltx_model: null, ltx_reason: "无需修复", ...AUTO },
      routing: { none: noGpu } }),
  ],
  choices: { ...PLANS.choices, machines: [W.ti70, W.ti1, W.ti2, W.ti3].map((w) => ({ ...w, gpu: null, vram_mib: null, reason: "" })) },
};

const route = (w: { id: string; name: string }, over: Partial<FilmRouteInfo> = {}): FilmRouteInfo => ({
  machine: w, kind: "auto", reason: `自动 · 约 1 小时后在 ${w.name} 完成`, routed_at: "2026-10-23T12:02:00Z", position: 0,
  live: "online", silent_s: 2, ...over,
});
export const STOP: FilmStopAction = {
  assignment_id: "as-lost", expected_version: 4, stage: "restore", worker: W.ti3, allowed: true, reason: null,
  heartbeat_silent_s: 420, status_quiet_s: 360, address: "192.168.1.34:5680", status_state: "timeout",
};
export const actions = (over: Partial<FilmActions> = {}): FilmActions => ({ can_reassign: false, stop: null, ...over });
const LTX41 = { ltx_progress: { mode: "restore", stage_index: 2, stage_total: 4, stage_name: "encode", percent: 41, percent_text: "41",
  current: "41", total: "100", rate_value: "2.31", rate_unit: "s/step" } };

export const REMOTE_ROWS: FilmRow[] = [
  // 设备失联 (R3): its claimed restore on 5060 Ti-3, both channels dead for 7 / 6 minutes
  row({ film_id: "flt-2", title: "浮光之间", group: "attention", attention_kind: "device_lost", stage: "restore", status: "running",
    note: "设备失联：5060 Ti-3 · 后台重连中", route: route(W.ti3, { live: "lost", silent_s: 420 }),
    pin: { worker_id: null, name: "自动", version: 2 }, actions: actions({ stop: STOP }),
    steps: steps("restore", "running", { ltx: LTX41, machine: W.ti3, attempt_id: "at-lost", phase: "running" }) }),
  // a running remote restore
  row({ film_id: "day-4", title: "白昼回声", stage: "restore", status: "running", route: route(W.ti70),
    pin: { worker_id: null, name: "自动", version: 1 },
    actions: actions({ stop: { ...STOP, assignment_id: "as-day", worker: W.ti70, allowed: false,
      reason: "心跳通道：5070 Ti 2 秒前仍有心跳", heartbeat_silent_s: 2, status_quiet_s: 0 } }),
    steps: steps("restore", "running", { ltx: LTX41, machine: W.ti70, attempt_id: "at-day", phase: "running" }) }),
  // translate runs on the controller
  row({ film_id: "moon-6", title: "月色剪影", stage: "translate", status: "running", actions: actions(),
    steps: steps("translate", "running", { progress: { completed: 112, total: 240 } }) }),
  // the controller-disk gate (R1): no route, the pass's reason, never 「等待 GPU 设备」
  row({ film_id: "far-3", title: "远岸来信", stage: "restore", status: "pending", note: "主控磁盘空间不足",
    pin: { worker_id: null, name: "自动", version: 1 }, actions: actions({ can_reassign: true }),
    steps: steps("restore", "waiting", { note: "主控磁盘空间不足" }) }),
  // routed to 5070 Ti, not claimed yet
  row({ film_id: "mist-18", title: "微光笔记", stage: "restore", status: "pending", note: "已派往 5070 Ti",
    route: route(W.ti70, { position: 1 }), pin: { worker_id: W.ti70.id, name: "5070 Ti", version: 3 },
    actions: actions({ can_reassign: true }), steps: steps("restore", "waiting", { note: "已派往 5070 Ti" }) }),
];

export const REMOTE_FILMS: WorkflowFilms = {
  ...FILMS_RESPONSE, current: null, intake: null, films: REMOTE_ROWS, packages: [], counts: counts(REMOTE_ROWS, []),
};

// The drawer of a film routed to the lost 5060 Ti-3 that has not started (Drawer-Reassign).
export function reassignDetail(over: Partial<FilmDetail> = {}): FilmDetail {
  return detail("mtn-7", {
    title: "山海一页", group: "active", note: "已派往 5060 Ti-3", steps: steps("restore", "waiting"),
    route: route(W.ti3, { live: "lost", silent_s: 420 }), pin: { worker_id: null, name: "自动", version: 5 },
    actions: actions({ can_reassign: true }), attention_kind: null,
    reassign_options: [
      { worker_id: null, name: "自动", reason: null, current: true },
      { worker_id: W.ti70.id, name: "5070 Ti", reason: null, current: false },
      { worker_id: W.ti1.id, name: "5060 Ti-1", reason: null, current: false },
      { worker_id: W.ti2.id, name: "5060 Ti-2", reason: "缺少高画质模型", current: false },
      { worker_id: W.ti3.id, name: "5060 Ti-3", reason: "离线", current: false },
    ],
    ...over,
  });
}
// The drawer of the 设备失联 film (Drawer-Attest): claimed, so 改派 waits for 判定已停止.
export function lostDetail(over: Partial<FilmDetail> = {}): FilmDetail {
  const r = REMOTE_ROWS[0];
  return detail("flt-2", {
    title: r.title, group: r.group, note: r.note, steps: r.steps, route: r.route, pin: r.pin, actions: r.actions,
    attention_kind: "device_lost", reassign_options: [], revisions: r.revisions,
    energy: { stages: { restore: { wh: 410, gpu_wh: 300, cpu_wh: 60, other_wh: 50, seconds: 3600, avg_w: 410, peak_w: 460, machine: "w-5060-3",
      attempts: 1, partial: false, running: true, devices: [W.ti3] } }, total_wh: 410, partial: false, running: true, estimated: true },
    ...over,
  });
}

const devRow = (over: Partial<DeviceRow> & { worker_id: string; name: string }): DeviceRow => ({
  version: 3, address: "192.168.1.31", port: 5682, enrollment_state: "verified", admission_state: "enabled", participation: true,
  heartbeat: { state: "online", age_s: 2 },
  gpu: { name: "RTX 5070 Ti", vram_mib: 16303, used_mib: 14438, util: 98, power_w: 268, hold: "none" },
  disk: { free: 2.6e12, staging_used: 212e9, cap: 500e9 }, tiers_ready: ["standard", "good", "high"],
  agent_version: "3.9.20", version_ok: true, boot_reconciled: true, eligibility: "可接单", eligible: true,
  probe: null, approved: null, restore_allowed: true, unresolved: 0, can_retire: false, assignments: [], route_depth: 0,
  ...over,
});
export const REMOTE_DEVICES: DevicesView = {
  remote_workers: true, server_time: new Date().toISOString(), controller: { name: "本机", stages: ["翻译"] },
  workers: [
    devRow({ worker_id: W.ti70.id, name: "5070 Ti", route_depth: 2, unresolved: 1,
      assignments: [{ assignment_id: "as-day", version: 2, film_id: "day-4", film_name: "白昼回声", stage: "restore", state: "running",
        stop: REMOTE_ROWS[1].actions?.stop ?? null }] }),
    devRow({ worker_id: W.ti3.id, name: "5060 Ti-3", address: "192.168.1.34", heartbeat: { state: "lost", age_s: 420 },
      eligibility: "离线", eligible: false, unresolved: 1, route_depth: 1,
      assignments: [{ assignment_id: "as-lost", version: 4, film_id: "flt-2", film_name: "浮光之间", stage: "restore", state: "running", stop: STOP }] }),
    devRow({ worker_id: "w-held", name: "5060 Ti-5", address: "192.168.1.35", heartbeat: { state: "lost", age_s: 130 },
      eligibility: "离线", eligible: false, unresolved: 1, route_depth: 1,
      assignments: [{ assignment_id: "as-held", version: 1, film_id: "x-1", film_name: "林间拾光", stage: "restore", state: "running",
        stop: { ...STOP, assignment_id: "as-held", worker: { id: "w-held", name: "5060 Ti-5" }, allowed: false,
          reason: "状态通道：仅 130 秒无响应", heartbeat_silent_s: 130, status_quiet_s: 130 } }] }),
  ],
};

export function settingsView(role: WorkflowRole, over: Partial<WorkflowSettingsView> = {}): WorkflowSettingsView {
  return {
    enabled: role !== "off", role, inbox_root: "E:\\TODO", data_dir: "D:\\TaskpawWork", ffprobe_path: null,
    jasna_exe_path: "C:\\Jasna\\jasna.exe", whisperjav_exe_path: null, ltx_gpu: "auto", mdcx_python: null, mdcx_root: null,
    mdcx_profile: null, filmpaw_data_dir: null, remote_workers: true, worker_api_port: 5682, staging_dir: null, staging_cap_gb: 500,
    ...over,
  };
}
export function settingsState(role: WorkflowRole, over: Partial<WorkflowSettingsState> = {}): WorkflowSettingsState {
  return {
    saved: settingsView(role), running: settingsView(role), restart_required: false, busy: { films: 0, remote: 0, unknown: false },
    presets: [{ id: "auto", label: "自动" }, { id: "5070ti-16", label: "RTX 5070 Ti · 16 GB" }],
    ffprobe_detected: "C:\\ffmpeg\\bin\\ffprobe.exe", ...over,
  };
}
