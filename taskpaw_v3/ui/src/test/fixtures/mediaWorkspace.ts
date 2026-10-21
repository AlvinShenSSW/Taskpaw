// 媒体处理中心 fixture data in the backend's shapes (T06/H2/H3 + U1). Shared by the tests and the
// screenshot harness; nothing here is rendered as-is by the shipped UI.
import type {
  FilmDetail, FilmRevisions, FilmRow, PackageRow, Performer, PlanEntry, PlanLookup, RestoreParams, StepKey, StepState,
  TargetPreflight,
  WorkflowCounts, WorkflowFilms, WorkflowPlans, WorkflowStep,
} from "../../api";

const ORDER: StepKey[] = ["intake", "plan", "restore", "asr", "translate", "return", "scrape", "confirm", "archive", "cleanup", "done"];

// Steps up to `at` done, `at` in `state`, the rest pending.
export function steps(at: StepKey, state: StepState = "running", extra: Partial<WorkflowStep> = {}): WorkflowStep[] {
  const idx = ORDER.indexOf(at);
  return ORDER.map((key, i) => ({
    key, state: i < idx ? "done" : i === idx ? state : "pending", note: null, progress: null, ...(i === idx ? extra : {}),
  }));
}

export const LTX_ENCODE = {
  ltx_progress: {
    mode: "restore", stage_index: 2, stage_total: 4, stage_name: "encode", percent: 52, percent_text: "52",
    current: "156", total: "300", rate_value: "5.62", rate_unit: "frame/s", elapsed: "00:27", eta: "00:25",
    run_eta: "12:12", raw_line: "Encode 52% | 156/300 [00:27<00:25, 5.62frame/s runETA 12:12]",
  },
  current_file: "ABF-401.mp4",
};

// U1c: the restore step's parse state per Jasna phase, as exec_restore reports it (test_jasna_progress).
export const RESTORE_STARTED_AT = "2026-10-22T08:00:00Z";
export const SCAN_LINE = "Mosaic scan:  12%|█▏        | 600/5000 [00:40<04:53, 15.00sample/s]";
export const BASIC_LINE = "Processing video:  15%|███ |Processed: 06:09 (36703f) | Remaining: 30:47 (207454f) | Speed: 112.3fps";
export const LTX_LINE = "LTX 2/4 encode:  52%|█████▏    | 156/300 [00:27<00:25,  5.62frame/s, run 12:12 left]";
export const SPLIT_LINE = "ABF-401.mp4: LTX auto split: 300 s with mosaics, 120 s (40%) of it with LTX";
export const RESTORE_PHASES = {
  starting: {},
  working: { current_file: "ABF-401.mp4", console: "Loading LTX weights (fast) …" },
  scan: {
    current_file: "ABF-401.mp4", console: SCAN_LINE,
    scan_progress: { percent: 12, percent_text: "12", current: "600", total: "5000", elapsed: "00:40", eta: "04:53",
      rate_value: "15.00", rate_unit: "sample/s", raw_line: SCAN_LINE },
  },
  split: { current_file: "ABF-401.mp4", console: SPLIT_LINE, split_note: { kind: "planned", text: SPLIT_LINE } },
  basic: {
    current_file: "ABF-401.mp4", console: BASIC_LINE, split_note: { kind: "planned", text: SPLIT_LINE },
    percent: 15, elapsed: "06:09", processed_frames: 36703, eta: "30:47", remaining_frames: 207454, fps: 112.3,
  },
  ltx: {
    current_file: "ABF-401.mp4", console: "WARNING: VRAM 92% used", split_note: { kind: "planned", text: SPLIT_LINE },
    ltx_progress: { ...LTX_ENCODE.ltx_progress, raw_line: LTX_LINE }, ltx_stage: "2/4 encode", percent: 52, fps: 5.62, eta: "12:12",
  },
} as const satisfies Record<string, Record<string, unknown>>;
export type RestorePhaseKey = keyof typeof RESTORE_PHASES;
// The auto-picked 5070ti-16 preset's launch of a probed 12.2 Mbps 1080p film (the backend's `launch_params`).
export const PARAMS: RestoreParams = {
  ltx_model: "standard", model_name: "ltx", fast: true, precision: "nvfp4", auto_split: 12, large_canvas: false,
  overlap: "auto", codec: "hevc", codec_follows_source: false, cq: 20, match_source_bitrate: true, maxrate_bps: 12_200_000, source_bitrate_bps: 12_200_000,
  detection_model: "rfdetr-v6", clip_size: 180, temporal_overlap: 8, tier: "1080p", source_width: 1920, source_height: 1080,
  gpu: "NVIDIA GeForce RTX 5070 Ti", gpu_vram_mib: 16303, preset: "5070ti-16", preset_label: "RTX 5070 Ti · 16 GB", preset_auto: true,
  argv: "jasna.exe --input ABF-401.mp4 --output ABF-401-破解.tmp.mp4 --max-clip-size 90 --temporal-overlap 8 "
    + "--secondary-restoration none --codec hevc --cq 20 --detection-model rfdetr-v6 --restoration-model-name ltx "
    + "--ltx-fast --ltx-auto-split 12 --working-directory .jasna-work --encoder-settings maxrate=12200000,qmax=51",
};
// The running restore step in one phase, with the heartbeat the board copies onto it.
export function restoreSteps(phase: RestorePhaseKey): WorkflowStep[] {
  const { console: line, ...ltx } = RESTORE_PHASES[phase] as Record<string, unknown>; // console lives on the step only
  return steps("restore", "running", {
    ltx: phase === "starting" ? null : ltx, current_file: (ltx.current_file as string | undefined) ?? null,
    started_at: RESTORE_STARTED_AT, console: (line as string | undefined) ?? null, params: PARAMS,
  });
}

const hash = (seed: string) => seed.repeat(64).slice(0, 64);
export const revisions = (input: number, final: number | null = null): FilmRevisions => ({
  input: { revision: input, hash: hash(`a${input}`) }, final: final === null ? null : { revision: final, hash: hash(`b${final}`) },
});

export const row = (over: Partial<FilmRow> & { film_id: string }): FilmRow => ({
  name: over.film_id.toUpperCase(), group: "active", stage: "restore", status: "pending", note: null,
  can_confirm: false, can_retry: false, retry_stage: null, created_at: "2026-10-21T00:00:00Z", steps: [],
  resolution: "1080p", number: over.film_id.toUpperCase(), title: null, actors: [], receipt_id: null,
  revisions: revisions(1), plan: { ltx_model: "standard", machine: "w-local" }, has_poster: false, poster_source: null,
  has_cover: false, cover_source: null,
  ...over,
});

export const FILMS: FilmRow[] = [
  row({ film_id: "abf-401", title: "浮光之间", actors: ["演员浮光"], resolution: "4K", stage: "restore", status: "running",
    steps: steps("restore", "running", { ltx: LTX_ENCODE, current_file: "ABF-401.mp4" }) }),
  row({ film_id: "abf-402", title: "白昼回声", actors: ["演员回声"], stage: "translate", status: "running",
    steps: steps("translate", "running", { progress: { completed: 112, total: 240 } }) }),
  row({ film_id: "abf-403", title: "薄雾旅行", actors: ["演员薄雾"], stage: "return", status: "running", steps: steps("return") }),
  row({ film_id: "abf-404", title: "月色剪影", actors: ["演员月色"], resolution: "4K", stage: "scrape", status: "running", steps: steps("scrape") }),
  row({ film_id: "abf-405", title: "青岚序曲", actors: ["演员青岚"], stage: "restore", status: "pending", note: "等待 GPU 空闲",
    steps: steps("restore", "waiting", { note: "等待 GPU 空闲" }) }),
  row({ film_id: "abf-406", title: "星河慢行", actors: ["演员星河"], resolution: "4K", group: "confirm", stage: "archive",
    status: "pending", can_confirm: true, steps: steps("confirm", "waiting"), revisions: revisions(1, 2) }),
  row({ film_id: "abf-407", title: "夜航日志", actors: ["演员夜航"], group: "confirm", stage: "archive", status: "pending",
    can_confirm: true, steps: steps("confirm", "waiting") }),
  row({ film_id: "abf-408", title: "未命名的岛", actors: [], group: "confirm", stage: "archive", status: "pending",
    can_confirm: true, steps: steps("confirm", "waiting") }),
  row({ film_id: "abf-409", title: "晴窗片段", actors: ["演员晴窗"], resolution: "4K", group: "confirm", stage: "archive",
    status: "pending", can_confirm: true, steps: steps("confirm", "waiting") }),
  row({ film_id: "abf-410", title: "海风有声", actors: ["演员海风"], group: "archiving", stage: "archive", status: "unknown",
    steps: steps("archive", "waiting") }),
  row({ film_id: "abf-411", title: "长夏切片", actors: ["演员长夏"], group: "attention", stage: "cleanup_input", status: "unknown",
    note: "待清理：1 个文件未能删除（blocked_writing）", receipt_id: "R-411", steps: steps("cleanup", "waiting") }),
  row({ film_id: "abf-412", title: "云层以外", actors: ["演员云层"], group: "attention", stage: "scrape", status: "failed",
    can_retry: true, retry_stage: "scrape", note: "最终封面缺失", steps: steps("scrape", "failed") }),
  row({ film_id: "abf-413", title: "林间拾光", actors: ["演员林间"], group: "attention", stage: "translate", status: "failed",
    can_retry: true, retry_stage: "translate", note: "LLM 超时", steps: steps("translate", "failed", { note: "LLM 超时" }) }),
  row({ film_id: "abf-414", title: "春日留白", actors: ["演员春日"], resolution: "4K", group: "done", stage: "cleanup_workcopy",
    status: "succeeded", receipt_id: "R-414", steps: steps("done", "done") }),
  // unplanned films: also on /films with the 待处理 group (U1 V')
  ...["abf-421", "abf-422", "abf-423"].map((id) => row({ film_id: id, group: "planning", stage: "restore", status: "pending",
    plan: null, steps: steps("plan", "waiting") })),
];

export const PACKAGES: PackageRow[] = [
  { package_id: "p1", name: "(HD1080P)(Prestige)(ABF-430)", state: "waiting_stability", reason: "stable", grouping_reasons: [],
    files: [], group: "active", steps: steps("intake", "running").map((s) => (s.key === "plan" ? { ...s, state: "pending" } : s)) },
  { package_id: "p2", name: "(HD1080P)(Prestige)(ABF-431)", state: "needs_attention", reason: "ambiguous_grouping",
    grouping_reasons: ["unknown_attachment"], files: ["新建文本文档.txt"], group: "attention", steps: steps("intake", "failed") },
];

// `board.counts` of the rows above.
export function counts(films: FilmRow[], packages: PackageRow[]): WorkflowCounts {
  const n = (group: FilmRow["group"]) => films.filter((f) => f.group === group).length;
  return {
    planning: n("planning"), active: n("active"), attention: n("attention"), confirm: n("confirm"),
    archiving: n("archiving"), done: n("done"), packages_attention: packages.filter((p) => p.group === "attention").length,
    waiting_for_you: n("planning") + n("confirm"),
  };
}

export const FILMS_RESPONSE: WorkflowFilms = {
  state: "running", reason: "",
  current: { film_id: "abf-401", film: "ABF-401", stage: "restore", progress: "修复 52%" },
  intake: { package_id: "p1", phase: "copy", bytes_completed: 1073741824, total: 4294967296 },
  films: FILMS, packages: PACKAGES, counts: counts(FILMS, PACKAGES),
  dispatch: { state: "running", reason: "", paused: false },
  gpu: { name: "RTX 5070 Ti", vram_mib: 16303, max_output: "1080p" },
};

export const performer = (id: number, name: string, label: string): Performer =>
  ({ id, name, source_label: label, unc_path: `\\\\${label.toLowerCase()}\\actors\\${name}`, has_thumb: false, aliases: [] });

// The preflight without a performer: only the candidates (U1 §7.3).
export const preflight = (filmDir: string, items: Performer[] | null, over: Partial<TargetPreflight> = {}): TargetPreflight => ({
  film_dir: filmDir, performer_id: null, target_path: null, absent: null, error_code: null, message: null,
  candidates: items === null ? null : { query: items.map((p) => p.name), items }, ...over,
});

export const PREFLIGHTS: Record<string, TargetPreflight> = {
  "abf-406": preflight("ABF-406", [performer(1, "演员星河", "NAS-A")]),
  "abf-407": preflight("ABF-407", [performer(2, "演员夜航", "NAS-A"), performer(3, "演员夜航乙", "NAS-B")]),
  "abf-408": preflight("ABF-408", []),
  "abf-409": preflight("ABF-409", null, { error_code: "offline", message: "Filmpaw 未运行" }),
};

export const LOOKUP_DONE: PlanLookup = {
  status: "succeeded", number: null, title: null, actors: [], studio: null, release: null, runtime_min: null,
  has_poster: false, error_class: null, message: null, can_requery: true,
};
export const lookupFailed = (message: string, can_requery: boolean): PlanLookup =>
  ({ ...LOOKUP_DONE, status: "failed", error_class: "needs_attention", message, can_requery });

export const plan = (over: Partial<PlanEntry> & { film_id: string }): PlanEntry => ({
  name: over.film_id.toUpperCase(), input_revision: 1, revision_changed: false, number: over.film_id.toUpperCase(),
  title: null, actors: [], resolution: "1080p", width: 1920, height: 1080, duration_ms: 7094000, size_bytes: 7301444403,
  video_count: 1, item_count: 2, has_poster: false, poster_source: null, has_cover: false, cover_source: null, needs_gpu: true, created_at: "2026-10-21T00:00:00Z",
  lookup: LOOKUP_DONE,
  recommendation: { ltx_model: "standard", ltx_reason: "默认", machine: "w-local", machine_reason: "RTX 5070 Ti 16 GB" },
  ...over,
});

export const PLANS: WorkflowPlans = {
  plans: [
    plan({ film_id: "abf-421", title: "星河之外", actors: ["演员星河"], resolution: "4K", duration_ms: 7694000, size_bytes: 44667659878,
      item_count: 3, recommendation: { ltx_model: "high", ltx_reason: "演员星河 上次选择 高画质", machine: "w-local",
        machine_reason: "本机修复输出上限 1080p" } }),
    plan({ film_id: "abf-422", title: "晨雾车站", actors: ["演员晨雾"] }),
    plan({ film_id: "abf-423", title: "纸上潮汐", actors: ["演员潮汐"], needs_gpu: false,
      recommendation: { ltx_model: null, ltx_reason: "无需修复", machine: "w-local", machine_reason: "RTX 5070 Ti 16 GB" } }),
  ],
  default_tier: "standard",
  choices: {
    tiers: [{ value: "standard", label: "标准" }, { value: "good", label: "良好" }, { value: "high", label: "高" }],
    machines: [{ id: "w-local", name: "本机", gpu: "RTX 5070 Ti", vram_mib: 16303, reason: "RTX 5070 Ti 16 GB" }],
  },
};

export function detail(id: string, over: Partial<FilmDetail> = {}): FilmDetail {
  const r = FILMS.find((f) => f.film_id === id) ?? FILMS[0];
  return {
    film_id: id, name: r.name, number: r.number ?? null, title: r.title ?? null, actors: r.actors ?? [],
    marks: { restored: true, chinese_subtitles: true, uhd: r.resolution === "4K" },
    files: r.group === "confirm" ? [
      { role: "main_video", path: `${r.name}.mp4`, size: 5368709120, volume: null },
      { role: "generated_subtitle", path: `${r.name}.zh.srt`, size: 40960, volume: null },
      { role: "nfo", path: `${r.name}.nfo`, size: 4096, volume: null },
      { role: "poster", path: "poster.jpg", size: 204800, volume: null },
    ] : [],
    inputs: [{ path: `${r.name}/${r.name}.mp4`, size: 7301444403 }, { path: `${r.name}/${r.name}.nfo`, size: 2048 }],
    workcopy_count: 0, has_poster: r.group === "confirm", group: r.group, can_confirm: r.can_confirm, can_retry: r.can_retry,
    retry_stage: r.retry_stage, note: r.note, stages: [], steps: r.steps,
    archive: { status: null, error: null, target_path: null }, cleanup: { input: null, workcopy: null, kept: [] },
    resolution: r.resolution, receipt_id: r.receipt_id, revisions: r.revisions, plan: r.plan, poster_source: null, has_cover: false, cover_source: null, lookup: null,
    ...over,
  };
}
