import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  api, HttpError, type FilmDetail, type FilmRow, type PackageRow, type WorkflowFilms, type WorkflowStep,
} from "../api";
import { MediaWorkflow } from "../views/MediaWorkflow";
import { FilmDrawer } from "../components/FilmDrawer";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import i18n, { setLang } from "../i18n";

const row = (over: Partial<FilmRow>): FilmRow => ({
  film_id: "f1", name: "ABC-001", group: "confirm", stage: "scrape", status: "succeeded",
  note: null, can_confirm: true, can_retry: false, retry_stage: null, created_at: "2026-10-20T00:00:00Z", steps: [], ...over,
});

const LIST: WorkflowFilms = {
  state: "running", reason: null, intake: null, packages: [],
  current: { film_id: "f2", film: "XYZ-002", stage: "restore", progress: 0.4 },
  films: [
    row({}),
    row({ film_id: "f2", name: "XYZ-002", group: "active", stage: "restore", status: "running", note: "修复中 40%", can_confirm: false }),
    row({ film_id: "f3", name: "QQQ-003", group: "active", stage: "asr", status: "pending", can_confirm: false }),
    row({ film_id: "f4", name: "BAD-004", group: "attention", stage: "translate", status: "failed", note: "LLM 超时",
      can_confirm: false, can_retry: true, retry_stage: "translate" }),
    row({ film_id: "f5", name: "OLD-005", group: "done", stage: "cleanup_workcopy", status: "succeeded", can_confirm: false }),
  ],
};

const DETAIL: FilmDetail = {
  film_id: "f1", name: "ABC-001", number: "ABC-001", title: "Some title", actors: ["三上悠亚"],
  marks: { restored: true, chinese_subtitles: true, uhd: false },
  files: [{ role: "video", path: "ABC-001.mp4", size: 1073741824, volume: 1 }],
  inputs: [{ path: "ABC-001/ABC-001.mp4", size: 2048 }], workcopy_count: 1, has_poster: false,
  group: "confirm", can_confirm: true, can_retry: false, retry_stage: null, note: null,
  stages: [{ stage: "scrape", status: "succeeded", note: null }], steps: [],
  archive: { status: null, error: null, target_path: null },
  cleanup: { input: null, workcopy: null, kept: [] },
};

const PERFORMERS = {
  items: [
    { id: 7, name: "三上悠亚", source_label: "NAS-A", unc_path: "\\\\nas\\a\\三上悠亚", has_thumb: false, aliases: ["鬼头桃菜"] },
    { id: 8, name: "三上", source_label: "NAS-B", unc_path: "\\\\nas\\b\\三上", has_thumb: false, aliases: [] },
  ],
};

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

beforeEach(() => setLang("zh-CN"));
afterEach(() => vi.restoreAllMocks());

describe("MediaWorkflow list", () => {
  it("renders groups, notes and the queued fallback", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue(LIST);
    wrap(<MediaWorkflow />);
    const confirm = await screen.findByRole("region", { name: "待确认入库" });
    expect(within(confirm).getByText("ABC-001")).toBeInTheDocument();
    const active = screen.getByRole("region", { name: "处理中" });
    expect(within(active).getByText("修复中 40%")).toBeInTheDocument();
    // f3 has no note and another film is current → queued fallback (D06).
    expect(within(active).getByText("排队中（当前：XYZ-002·修复）")).toBeInTheDocument();
    const attention = screen.getByRole("region", { name: "需处理" });
    expect(within(attention).getByText("LLM 超时")).toBeInTheDocument();
    expect(within(attention).getByText("翻译 · 失败")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "已完成" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "已确认待归档与清理" })).not.toBeInTheDocument();
    // Films waiting for the operator or done are not "queued".
    expect(screen.getAllByText(/排队中/)).toHaveLength(1);
  });

  it("falls back to the state while the host starts with an empty reason", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ state: "starting", reason: "", current: null, intake: null, films: [], packages: [] });
    wrap(<MediaWorkflow />);
    expect(await screen.findByText("媒体流水线未运行：starting")).toBeInTheDocument();
  });

  it("shows the reason without an error when the workflow is not running", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ state: "disabled", reason: "未启用", current: null, intake: null, films: [], packages: [] });
    wrap(<MediaWorkflow />);
    expect(await screen.findByText("媒体流水线未运行：未启用")).toBeInTheDocument();
    expect(screen.queryByText(/无法读取/)).not.toBeInTheDocument();
  });
});

describe("AgentConsole media tab", () => {
  it("renders MediaWorkflow under 「媒体处理中心」", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ machine: "box1", monitors: {} }) })));
    const films = vi.spyOn(api, "workflowFilms").mockResolvedValue(LIST);
    wrap(<AgentConsole />);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理中心" }));
    expect(await screen.findByRole("region", { name: "待确认入库" })).toBeInTheDocument();
    expect(films).toHaveBeenCalled();
    expect(screen.queryByRole("tab", { name: "媒体入库" })).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("names the tab 「Media Center」 in English", () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ machine: "box1", monitors: {} }) })));
    setLang("en");
    wrap(<AgentConsole />);
    expect(screen.getByRole("tab", { name: "Media Center" })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});

const step = (key: WorkflowStep["key"], state: WorkflowStep["state"], note: string | null = null,
  progress: unknown = null): WorkflowStep => ({ key, state, note, progress });

const pkg = (over: Partial<PackageRow>): PackageRow => ({
  package_id: "p1", name: "(HD1080P)(Prestige)(ABF-393)", state: "needs_attention", reason: "ambiguous_grouping",
  grouping_reasons: ["unknown_attachment"], files: ["新建文本文档.txt"], group: "attention",
  steps: [step("intake", "failed", "needs_attention")], ...over,
});

describe("MediaWorkflow lifecycle step line (R5)", () => {
  it("renders done / running with progress / waiting / failed / pending and highlights the current step", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({
      ...LIST, current: null,
      films: [row({
        film_id: "f9", name: "STP-009", group: "active", stage: "asr", status: "running", can_confirm: false,
        steps: [step("intake", "done"), step("restore", "done", "无需修复"), step("asr", "running", null, 0.4),
          step("translate", "waiting", "等待 GPU 空闲"), step("return", "failed", "NAS 不可达")],
      })],
    });
    wrap(<MediaWorkflow />);
    const active = await screen.findByRole("region", { name: "处理中" });
    const line = within(active).getByRole("list", { name: "生命周期" });
    const items = within(line).getAllByRole("listitem");
    expect(items).toHaveLength(10);
    expect(items[0]).toHaveAccessibleName("入库扫描：已完成");
    expect(items[1]).toHaveAccessibleName("修复：已完成（无需修复）");
    expect(items[2]).toHaveAccessibleName("识别：进行中 40%");
    expect(items[3]).toHaveAccessibleName("翻译：等待（等待 GPU 空闲）");
    expect(items[4]).toHaveAccessibleName("回传：失败（NAS 不可达）");
    // the steps the backend did not send are 未开始, in the fixed order
    expect(items.slice(5).map((i) => i.getAttribute("aria-label")))
      .toEqual(["整理：未开始", "待确认：未开始", "归档：未开始", "清理：未开始", "完成：未开始"]);
    expect(items[2]).toHaveAttribute("aria-current", "step");
    expect(items.filter((i) => i.getAttribute("aria-current"))).toHaveLength(1);
    expect(within(line).getByText("识别 40%")).toBeInTheDocument();
  });

  it("omits the step line for a film whose row carries no steps", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue(LIST);
    wrap(<MediaWorkflow />);
    await screen.findByRole("region", { name: "待确认入库" });
    expect(screen.queryByRole("list", { name: "生命周期" })).not.toBeInTheDocument();
  });
});

describe("MediaWorkflow packages (R2/R2a)", () => {
  const PACKAGES: WorkflowFilms = {
    ...LIST, films: [],
    intake: { package_id: "p2", phase: "copy", bytes_completed: 1073741824, total: 4294967296 },
    packages: [
      pkg({}),
      pkg({ package_id: "p2", name: "ABF-394", state: "waiting_stability", reason: "changed", grouping_reasons: [],
        files: [], group: "active", steps: [step("intake", "running", "waiting_stability")] }),
      pkg({ package_id: "p3", name: "ODD-001", state: "blocked", reason: "mystery_reason",
        grouping_reasons: ["unknown_attachment", "brand_new_code"], files: ["setup.exe", "desktop.ini"] }),
    ],
  };

  it("shows stuck packages in 需处理 with readable reasons and file names, and intake progress in 处理中", async () => {
    const detail = vi.spyOn(api, "workflowFilm");
    vi.spyOn(api, "workflowFilms").mockResolvedValue(PACKAGES);
    wrap(<MediaWorkflow />);
    const attention = await screen.findByRole("region", { name: "需处理" });
    expect(within(attention).getByText("(HD1080P)(Prestige)(ABF-393)")).toBeInTheDocument();
    expect(within(attention).getByText("需要处理 · 无法确定影片由哪些文件组成")).toBeInTheDocument();
    expect(within(attention).getByText("有无法识别的文件：新建文本文档.txt")).toBeInTheDocument();
    // unknown codes fall back to the code itself
    expect(within(attention).getByText("受阻 · mystery_reason")).toBeInTheDocument();
    expect(within(attention).getByText("有无法识别的文件；brand_new_code")).toBeInTheDocument();
    expect(within(attention).getByText("文件：setup.exe、desktop.ini")).toBeInTheDocument();
    expect(within(attention).getAllByRole("listitem", { name: "入库扫描：失败（需要处理）" })).toHaveLength(2);

    const active = screen.getByRole("region", { name: "处理中" });
    expect(within(active).getByText("ABF-394")).toBeInTheDocument();
    expect(within(active).getByText("等待文件稳定 · 文件仍在变化")).toBeInTheDocument();
    const line = within(active).getByRole("list", { name: "生命周期" });
    const items = within(line).getAllByRole("listitem");
    expect(items[0]).toHaveAccessibleName("入库扫描：进行中 25%（等待文件稳定 · 复制 1.0 GB / 4.0 GB）");
    expect(items[0]).toHaveAttribute("aria-current", "step");
    expect(items.slice(1).every((i) => i.getAttribute("data-state") === "pending")).toBe(true);
    expect(within(line).getByText("入库扫描 25%")).toBeInTheDocument();
    expect(within(active).getByText("1")).toBeInTheDocument(); // the group count includes packages
    expect(within(attention).getByText("2")).toBeInTheDocument();

    // a package row opens no drawer
    fireEvent.click(within(active).getByText("ABF-394"));
    expect(detail).not.toHaveBeenCalled();
    expect(screen.queryByText("还没有影片。")).not.toBeInTheDocument();
  });

  it("opens the package folder and reports a missing folder inline", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...PACKAGES, packages: [pkg({})] });
    const open = vi.spyOn(api, "workflowOpenPackage").mockRejectedValueOnce(new HttpError("x", 404));
    wrap(<MediaWorkflow />);
    const attention = await screen.findByRole("region", { name: "需处理" });
    fireEvent.click(within(attention).getByRole("button", { name: "打开文件夹" }));
    await waitFor(() => expect(open).toHaveBeenCalledWith("p1"));
    expect(await within(attention).findByText("文件夹不存在。")).toBeInTheDocument();
  });

  it("reports an unsupported platform on 501 and clears it after a successful open", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...PACKAGES, packages: [pkg({})] });
    const open = vi.spyOn(api, "workflowOpenPackage")
      .mockRejectedValueOnce(new HttpError("x", 501)).mockResolvedValueOnce(undefined);
    wrap(<MediaWorkflow />);
    const button = await screen.findByRole("button", { name: "打开文件夹" });
    fireEvent.click(button);
    expect(await screen.findByText("此平台不支持打开文件夹。")).toBeInTheDocument();
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("此平台不支持打开文件夹。")).not.toBeInTheDocument());
  });

  it("shows the empty state only when there are neither films nor packages", async () => {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...LIST, films: [], packages: [] });
    const first = wrap(<MediaWorkflow />);
    expect(await screen.findByText("还没有影片。")).toBeInTheDocument();
    first.unmount();
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...LIST, films: [], packages: [pkg({ group: "active" })] });
    wrap(<MediaWorkflow />);
    expect(await screen.findByRole("region", { name: "处理中" })).toBeInTheDocument();
    expect(screen.queryByText("还没有影片。")).not.toBeInTheDocument();
  });
});

describe("FilmDrawer", () => {
  const confirmButton = () => screen.getByRole("button", { name: "确认入库" });

  it("shows the lifecycle stepper with each step's state and note", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue({
      ...DETAIL, group: "attention", can_confirm: false,
      steps: [step("intake", "done"), step("restore", "done", "无需修复"), step("asr", "done"),
        step("translate", "done", "没有需要翻译的字幕"), step("return", "done"), step("scrape", "done"),
        step("confirm", "done"), step("archive", "done"),
        step("cleanup", "waiting", "保留 1 个文件：pkg/a.mp4"), step("done", "pending")],
    });
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    expect(await screen.findByText("保留 1 个文件：pkg/a.mp4")).toBeInTheDocument();
    expect(screen.getByText("没有需要翻译的字幕")).toBeInTheDocument(); // a "no work" outcome is done with its note (G3)
    expect(screen.getByText("整理")).toBeInTheDocument();
    expect(screen.getAllByText("已完成")).toHaveLength(8);
    expect(screen.getByText("等待")).toBeInTheDocument();
    expect(screen.getByText("未开始")).toBeInTheDocument();
    expect(document.querySelector('[aria-current="step"]')).toHaveTextContent("清理");
    expect(screen.queryByText("阶段")).not.toBeInTheDocument(); // the stepper replaces the stage list
  });

  it("shows a running step's progress in the stepper", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue({
      ...DETAIL, group: "active", can_confirm: false,
      steps: [step("intake", "done"), step("restore", "running", null, "frame 120/300 40%")],
    });
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    expect(await screen.findByText("进行中 40%")).toBeInTheDocument();
    expect(screen.getByText("frame 120/300 40%")).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar").some((p) => p.getAttribute("aria-valuenow") === "40")).toBe(true);
    expect(document.querySelector('[aria-current="step"]')).toHaveTextContent("修复");
  });

  it("keeps confirm disabled until a performer is picked; one click → one call; success message", async () => {
    let current: FilmDetail = DETAIL;
    vi.spyOn(api, "workflowFilm").mockImplementation(() => Promise.resolve(current));
    const search = vi.spyOn(api, "performers").mockResolvedValue(PERFORMERS);
    let resolve!: (v: { approval_id: string }) => void;
    const confirm = vi.spyOn(api, "workflowConfirm").mockImplementation(() => new Promise((r) => { resolve = r; }));
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    expect(await screen.findByText("NAS-A", { exact: false })).toBeInTheDocument();
    expect(search).toHaveBeenCalledWith("三上悠亚");
    expect(screen.getByText("别名：鬼头桃菜")).toBeInTheDocument();
    expect(screen.getByText("中文字幕")).toBeInTheDocument();
    expect(screen.getByText("无封面")).toBeInTheDocument();
    expect(confirmButton()).toBeDisabled(); // nothing auto-selected
    fireEvent.click(screen.getByText("三上悠亚 · NAS-A"));
    expect(confirmButton()).toBeEnabled();
    fireEvent.click(confirmButton());
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(confirmButton()).toBeDisabled(); // in flight (D15)
    fireEvent.click(confirmButton());
    expect(confirm).toHaveBeenCalledWith("f1", 7);
    current = { ...DETAIL, group: "archiving", can_confirm: false }; // what the refetch reads
    resolve({ approval_id: "a1" });
    expect(await screen.findByText("已确认，等待归档")).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it("shows the picker again when a failed archive makes the film confirmable", async () => {
    let current: FilmDetail = DETAIL;
    vi.spyOn(api, "workflowFilm").mockImplementation(() => Promise.resolve(current));
    vi.spyOn(api, "performers").mockResolvedValue(PERFORMERS);
    vi.spyOn(api, "workflowConfirm").mockImplementation(() => {
      current = { ...DETAIL, group: "archiving", can_confirm: false };
      return Promise.resolve({ approval_id: "a1" });
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>
      <FilmDrawer filmId="f1" onClose={() => {}} />
    </QueryClientProvider></ThemeProvider>);
    fireEvent.click(await screen.findByText("三上悠亚 · NAS-A"));
    fireEvent.click(confirmButton());
    expect(await screen.findByText("已确认，等待归档")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认入库" })).not.toBeInTheDocument());
    current = {
      ...DETAIL, can_confirm: true, note: "NAS 可用空间不足",
      archive: { status: "failed", error: "NAS 可用空间不足", target_path: null },
    };
    await qc.invalidateQueries({ queryKey: ["mediaFilm", "f1"] });
    expect(await screen.findByRole("button", { name: "确认入库" })).toBeDisabled(); // pick again
    expect(screen.queryByText("已确认，等待归档")).not.toBeInTheDocument();
  });

  it("lets the operator confirm again when the archive fails before the refetch", async () => {
    // Codex T06 r1: the refetch can already be confirmable (failed) — never a dead button
    let current: FilmDetail = DETAIL;
    vi.spyOn(api, "workflowFilm").mockImplementation(() => Promise.resolve(current));
    vi.spyOn(api, "performers").mockResolvedValue(PERFORMERS);
    const confirmApi = vi.spyOn(api, "workflowConfirm").mockImplementation(() => {
      current = {
        ...DETAIL, can_confirm: true, note: "NAS 可用空间不足",
        archive: { status: "failed", error: "NAS 可用空间不足", target_path: null },
      };
      return Promise.resolve({ approval_id: "a1" });
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>
      <FilmDrawer filmId="f1" onClose={() => {}} />
    </QueryClientProvider></ThemeProvider>);
    fireEvent.click(await screen.findByText("三上悠亚 · NAS-A"));
    fireEvent.click(confirmButton());
    await waitFor(() => expect(confirmApi).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(confirmButton()).toBeEnabled());
    fireEvent.click(confirmButton());
    await waitFor(() => expect(confirmApi).toHaveBeenCalledTimes(2));
    // Codex T06 r2: identical failures (same detail object) and a different target stay possible
    await waitFor(() => expect(confirmButton()).toBeEnabled());
    fireEvent.click(await screen.findByText("三上 · NAS-B"));
    fireEvent.click(confirmButton());
    await waitFor(() => expect(confirmApi).toHaveBeenCalledTimes(3));
    expect(confirmApi).toHaveBeenLastCalledWith("f1", 8);
  });

  it("labels kept cleanup members by why they were kept", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue({
      ...DETAIL, group: "attention", can_confirm: false,
      cleanup: { input: "unknown", workcopy: "succeeded", kept: [
        { path: "pkg/a.mp4", status: "blocked_changed" }, { path: "pkg/b.mp4", status: "blocked_identity" },
        { path: "pkg/c.mp4", status: "blocked_writing" }, { path: "pkg/d.mp4", status: "unknown" },
      ] },
    });
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    expect(await screen.findByText("已变化")).toBeInTheDocument();
    expect(screen.getByText("已被替换")).toBeInTheDocument();
    expect(screen.getByText("正在写入")).toBeInTheDocument();
    expect(screen.getByText("未知")).toBeInTheDocument();
    expect(screen.getByText("INBOX 清理：状态未知")).toBeInTheDocument();
  });

  it("shows the backend reason on 409", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue(DETAIL);
    vi.spyOn(api, "performers").mockResolvedValue(PERFORMERS);
    vi.spyOn(api, "workflowConfirm").mockRejectedValue(new HttpError("目标目录不在 Filmpaw 库中", 409));
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText("三上 · NAS-B"));
    fireEvent.click(confirmButton());
    expect(await screen.findByText("目标目录不在 Filmpaw 库中")).toBeInTheDocument();
    expect(screen.queryByText("已确认，等待归档")).not.toBeInTheDocument();
  });

  it("shows 「Filmpaw 未运行」 and keeps confirm disabled when the search returns 503", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue(DETAIL);
    vi.spyOn(api, "performers").mockRejectedValue(new HttpError("/control/workflow/performers → 503", 503));
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    expect(await screen.findByText("Filmpaw 未运行")).toBeInTheDocument();
    expect(confirmButton()).toBeDisabled();
  });

  it("retries the failed stage", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue({
      ...DETAIL, group: "attention", can_confirm: false, can_retry: true, retry_stage: "translate", note: "LLM 超时",
      stages: [{ stage: "translate", status: "failed", note: "LLM 超时" }],
    });
    const retry = vi.spyOn(api, "workflowRetry").mockResolvedValue({ ok: true });
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "重试翻译" }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith("f1", "translate"));
    expect(screen.queryByRole("button", { name: "确认入库" })).not.toBeInTheDocument();
  });

  it("shows open-folder errors inline", async () => {
    vi.spyOn(api, "workflowFilm").mockResolvedValue(DETAIL);
    vi.spyOn(api, "performers").mockResolvedValue(PERFORMERS);
    const open = vi.spyOn(api, "workflowOpen").mockRejectedValue(new HttpError("x", 404));
    wrap(<FilmDrawer filmId="f1" onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "打开成品文件夹" }));
    expect(await screen.findByText("文件夹不存在。")).toBeInTheDocument();
    expect(open).toHaveBeenCalledWith("f1", "output");
  });
});

describe("media labels", () => {
  // Every value the backend can send (workflow/models.py Stage, StageStatus, WorkflowCleanupItem).
  const STAGES = ["intake", "restore", "asr", "translate", "return", "scrape", "archive", "cleanup_input", "cleanup_workcopy"];
  const STATUSES = ["pending", "running", "succeeded", "skipped", "failed", "unknown", "blocked"];
  const KEPT = ["blocked_changed", "blocked_identity", "blocked_writing", "failed", "unknown"];
  const STEPS = ["intake", "restore", "asr", "translate", "return", "scrape", "confirm", "archive", "cleanup", "done"];
  const STEP_STATES = ["done", "running", "waiting", "failed", "pending"];
  // workflow/intake_models.py IntakeState and Reason
  const INTAKE_STATES = ["receiving", "waiting_stability", "snapshotting", "snapshot_ready", "operational_ready",
    "needs_attention", "blocked", "waiting_capacity", "unknown_owner", "missing"];
  const INTAKE_REASONS = ["observed", "stable", "changed", "partial_observation", "unknown_identity", "writer_busy",
    "writer_unknown", "temporary_member", "empty_package", "missing_video", "ambiguous_grouping", "volume_gap",
    "duplicate_volume", "subtitle_ambiguous", "subtitle_language_unknown", "unknown_attachment", "invalid_nfo",
    "film_id_required", "profile_required", "media_unknown", "eligible", "waiting_capacity", "snapshot_complete",
    "source_changed", "cancelled", "probe_failed", "copy_failed", "owner_unknown", "disabled", "limit_exceeded",
    "recovery_required", "publication_blocked", "permission_denied", "tool_missing"];

  it.each(["zh-CN", "en"])("has a label for every stage and status in %s", (lng) => {
    const missing = [
      ...STAGES.map((s) => `media.stage.${s}`),
      ...STATUSES.map((s) => `media.status.${s}`),
      ...KEPT.map((s) => `media.keptStatus.${s}`),
      ...STEPS.map((s) => `media.step.${s}`),
      ...STEP_STATES.map((s) => `media.stepState.${s}`),
      ...INTAKE_STATES.map((s) => `media.intakeState.${s}`),
      ...INTAKE_REASONS.map((s) => `media.intakeReason.${s}`),
    ].filter((key) => !i18n.exists(key, { lng }));
    expect(missing).toEqual([]);
  });

  it("names the intake and cleanup stages", () => {
    const zh = i18n.getFixedT("zh-CN");
    expect(["intake", "cleanup_input", "cleanup_workcopy"].map((s) => zh(`media.stage.${s}`)))
      .toEqual(["入库扫描", "INBOX 清理", "工作副本清理"]);
    expect(zh("media.status.skipped")).toBe("已跳过");
    const en = i18n.getFixedT("en");
    expect(["intake", "cleanup_input", "cleanup_workcopy"].map((s) => en(`media.stage.${s}`)))
      .toEqual(["Intake", "INBOX cleanup", "Work-copy cleanup"]);
    expect(en("media.status.skipped")).toBe("Skipped");
  });

  it("names the page and the lifecycle steps", () => {
    const zh = i18n.getFixedT("zh-CN");
    expect(zh("media.tab")).toBe("媒体处理中心");
    expect(STEPS.map((s) => zh(`media.step.${s}`)))
      .toEqual(["入库扫描", "修复", "识别", "翻译", "回传", "整理", "待确认", "归档", "清理", "完成"]);
    expect(i18n.getFixedT("en")("media.tab")).toBe("Media Center");
  });
});

describe("api.image", () => {
  it("fetches with the control token and returns a data: URL", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({
      ok: true, status: 200, blob: () => Promise.resolve(new Blob(["abc"], { type: "image/jpeg" })),
    }));
    vi.stubGlobal("fetch", fetchMock);
    const url = await api.image(api.posterPath("f 1"));
    expect(url).toBe(`data:image/jpeg;base64,${btoa("abc")}`);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("http://127.0.0.1:5681/control/workflow/films/f%201/poster");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer test-agent-control");
    vi.unstubAllGlobals();
  });

  it("rejects with the status on 404", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: false, status: 404 })));
    await expect(api.image(api.thumbPath(7))).rejects.toMatchObject({ status: 404 });
    vi.unstubAllGlobals();
  });
});
