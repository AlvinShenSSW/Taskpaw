import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type WorkerConsole, type WorkerFilm, type WorkflowStep } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { workerSteps } from "../components/media/workerModel";
import { WorkerMonitor } from "../components/WorkerMonitor";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import { setLang } from "../i18n";
import { detail, FILMS, FILMS_RESPONSE, PARAMS, PLANS, restoreSteps, row, steps } from "./fixtures/mediaWorkspace";

// P2-3 (#294) §6: the worker's 媒体处理 tab, and remote progress, 设备 and 停止并重新排队 on the
// controller's board and drawer.
const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

const SLOGANS = /让每一部|顺利归位|由您确认|先安排，再开工|WORKER \/|LOCAL AGENT/;
const LTX_STEP = restoreSteps("ltx").find((s) => s.key === "restore") as WorkflowStep;

function running(over: Partial<WorkerConsole> = {}): WorkerConsole {
  return {
    state: "running", reason: "在线", role: "worker", controller: "192.168.1.20:5682", worker_id: "w1",
    credential: "valid", staging_dir: "E:\\Staging", staging_cap_gb: 500, last_heartbeat: "2026-10-23T08:00:00Z",
    reconciled: true, paused: false, controller_paused: false, dispatch_reason: "running",
    films: [
      { film: "f1", rev: 1, name: "ABF-401", position: 0, state: "running", stages: { restore: "running", asr: "pending" } },
      { film: "f2", rev: 1, name: "ABF-402", position: 1, state: "fetching", stages: { restore: "pending", asr: "pending" } },
    ],
    current: {
      film_id: "f1", stage: "restore", attempt_id: "a1", phase: "running", progress: "LTX 2/4 encode", elapsed_s: 600,
      uses_gpu: true, started_at: "2026-10-23T07:50:00Z",
      step: { ...LTX_STEP, params: PARAMS, timing: { scan: 60, basicvsr: 0, ltx: 420, speed_test: 0, ltx_stages: { encode: 420 }, ltx_runs: 1, wall: 480 } },
    },
    transfers: [{ film: "f2", dir: "down", done: 12.4e9, total: 38e9, rate: 610e6 }],
    staging: { used: 76e9, cap: 500e9 }, gpu: { name: "NVIDIA GeForce RTX 5070 Ti", vram_mib: 16303, hold: "workflow" },
    ...over,
  };
}

beforeEach(() => setLang("zh-CN"));
afterEach(() => vi.restoreAllMocks());

describe("worker 媒体处理 monitor", () => {
  it("focuses the active film ahead of queue position and keeps its prefetch secondary", async () => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w, films: [
      { ...w.films![1], position: 0 }, { ...w.films![0], position: 1 },
    ] });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(within(primary).getByRole("listitem", { name: "ABF-401" })).toBeInTheDocument();
    expect(within(primary).getByTestId("restore-live")).toBeInTheDocument();
    expect(within(primary).queryByText(/610 MB\/s/)).not.toBeInTheDocument();
    const secondary = screen.getByRole("region", { name: "预取与排队" });
    expect(within(secondary).getByRole("listitem", { name: "ABF-402" })).toBeInTheDocument();
    expect(within(secondary).getByText(/12.4\/38.0 GB · 610 MB\/s/)).toBeInTheDocument();
    expect(primary.compareDocumentPosition(secondary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it.each(["uploading", "finishing", "settling"] as const)("gives current %s precedence over a stale envelope and stale compute", async (phase) => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w,
      films: [{ ...w.films![0], state: "fetching" }],
      current: { ...w.current!, phase, progress: "STALE COMPUTE 98%" },
      transfers: [{ film: "f1", dir: "down", done: 9e9, total: 10e9, rate: 900e6 },
        { film: "f1", dir: "up", done: 1e9, total: 4e9, rate: 120e6 }],
    });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(primary.querySelector(`[data-status="${phase}"]`)).not.toBeNull();
    expect(primary).not.toHaveTextContent(/STALE COMPUTE|900 MB\/s/);
    expect(within(primary).queryByTestId("restore-live")).not.toBeInTheDocument();
    if (phase === "uploading") {
      expect(within(primary).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
      expect(primary).toHaveTextContent("1.0/4.0 GB · 120 MB/s");
    } else {
      expect(within(primary).queryByRole("progressbar")).not.toBeInTheDocument();
      expect(primary).not.toHaveTextContent(/120 MB\/s/);
      expect(primary).toHaveTextContent(phase === "finishing" ? "等待主控确认接收结果" : "任务进程停止尚未确认");
    }
  });

  it("keeps an orphan current attempt's identity with safe older fields", async () => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running({
      current: { film_id: "orphan-film", stage: "restore", phase: "running" } as WorkerConsole["current"],
    }));
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(primary).toHaveTextContent("orphan-film");
    expect(primary).toHaveTextContent("详细进度暂不可用");
    expect(primary).not.toHaveTextContent(/ABF-401|ABF-402|NaN|00:00/);
    expect(within(primary).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it.each(["queued", "staged"] as const)("shows dispatch reasons for a %s film without inventing active work", async (state) => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w, current: null,
      films: [{ ...w.films![0], state }], dispatch_reason: "等待 GPU：手动任务使用中", transfers: [],
    });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(screen.getByText(/等待 GPU：手动任务使用中/)).toBeInTheDocument();
    expect(within(primary).queryByRole("progressbar")).not.toBeInTheDocument();
    expect(within(primary).queryByTestId("restore-live")).not.toBeInTheDocument();
  });

  it("focuses a download before an attempt exists, using only that film's inbound transfer", async () => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w, current: null,
      films: [{ ...w.films![0], state: "fetching" }], transfers: [
        { film: "f1", dir: "up", done: 9e9, total: 10e9, rate: 900e6 },
        { film: "f1", dir: "down", done: 2e9, total: 4e9, rate: 100e6 },
      ],
    });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(within(primary).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
    expect(primary).toHaveTextContent("2.0/4.0 GB · 100 MB/s");
    expect(primary).not.toHaveTextContent(/900 MB\/s|00:00/);
    expect(within(primary).queryByTestId("restore-live")).not.toBeInTheDocument();
  });

  it("preserves local work while reconnecting and shows the legacy ASR phase and elapsed time", async () => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w, state: "connecting", reconciled: false,
      reason: "无法连接主控，正在重试", current: { ...w.current!, stage: "asr", progress: "正在识别语音",
        step: { key: "asr", state: "running", progress: { percent: 37 }, note: null } },
    });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(primary).toHaveTextContent("语音识别");
    expect(primary).toHaveTextContent("正在识别语音");
    expect(primary).toHaveTextContent("10:00");
    expect(within(primary).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "37");
    expect(screen.getByText(/无法连接主控，正在重试/)).toBeInTheDocument();
    expect(screen.getByText("等待对账", { selector: "dd" })).toBeInTheDocument();
  });

  it("shows unavailable for invalid transfer and elapsed values", async () => {
    const w = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...w,
      current: { ...w.current!, phase: "uploading", elapsed_s: NaN },
      transfers: [{ film: "f1", dir: "up", done: 1e9, total: Infinity, rate: NaN }],
    });
    wrap(<WorkerMonitor />);
    const primary = await screen.findByRole("region", { name: "当前任务" });
    expect(primary).toHaveTextContent("暂不可用");
    expect(primary).not.toHaveTextContent(/NaN|Infinity|00:00/);
    expect(within(primary).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("surfaces a pause failure without claiming the worker paused", async () => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running());
    vi.spyOn(api, "workflowWorkerPause").mockRejectedValue(new Error("request failed"));
    wrap(<WorkerMonitor />);
    fireEvent.click(await screen.findByRole("button", { name: "暂停领取" }));
    expect(await screen.findByText("无法更改领取状态，请重试。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "暂停领取" })).toBeInTheDocument();
    expect(screen.queryByText(/^已暂停领取：/)).not.toBeInTheDocument();
  });

  it.each([["finishing", "回传", "SendOutlinedIcon"], ["settling", "修复", "StopCircleOutlinedIcon"]] as const)("uses the actual %s phase icon on the current step while keeping its stable label", async (phase, label, icon) => {
    const worker = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...worker, current: { ...worker.current!, phase } });
    wrap(<WorkerMonitor />);
    const line = await screen.findByRole("list", { name: "ABF-401 步骤" });
    const badge = within(line).getByText(label, { selector: ".status-badge" });
    expect(badge.parentElement).toHaveAttribute("aria-current", "step");
    expect(badge).toHaveAttribute("data-status", phase);
    expect(badge.querySelector("svg")).toHaveAttribute("data-testid", icon);
    expect(within(line).getByText("下载 ✓")).toHaveAttribute("data-status", "completed");
    expect(within(line).getByText(phase === "finishing" ? "修复" : "回传")).toHaveAttribute("data-status", "waiting");
  });
  it.each([["uploading", "回传中"], ["finishing", "提交结果"], ["settling", "停止未确认"]] as const)("keeps current-card heading on %s instead of compute", async (phase, label) => {
    const worker = running();
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...worker, current: { ...worker.current!, phase } });
    wrap(<WorkerMonitor />);
    expect(await screen.findByRole("heading", { name: `${label} · ABF-401` })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "修复 · ABF-401" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("listitem", { name: "ABF-401" })).queryByRole("progressbar")).not.toBeInTheDocument();
  });
  it("gives all seven worker states icons and uses transfer bytes only for real transfers", async () => {
    const cases = [
      ["queued", "排队中"], ["fetching", "下载源文件"], ["staged", "源文件就绪"], ["running", "正在修复"],
      ["settling", "停止未确认"], ["uploading", "回传中"], ["finishing", "提交结果"],
    ] as const;
    const films: WorkerFilm[] = cases.map(([state]) => ({ film: state, rev: 1, name: `FILM-${state}`, state, position: 0, stages: {} }));
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running({ films, current: null, transfers: [
      { film: "fetching", dir: "down", done: 30, total: 100, rate: 1 },
      { film: "uploading", dir: "up", done: 60, total: 100, rate: 1 },
      { film: "queued", dir: "down", done: 99, total: 100, rate: 1 },
    ] }));
    wrap(<WorkerMonitor />);
    for (const [state, label] of cases) {
      const item = within(await screen.findByRole("listitem", { name: `FILM-${state}` }));
      expect(item.getByText(label).querySelector("svg")).toBeInTheDocument();
      if (state === "fetching" || state === "uploading") {
        expect(item.getByRole("progressbar")).toHaveAttribute("aria-valuenow", state === "fetching" ? "30" : "60");
      } else expect(item.queryByRole("progressbar")).not.toBeInTheDocument();
    }
  });
  it("shows the connection, the films, the running restore with the shared cards, and transfers", async () => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running());
    wrap(<WorkerMonitor />);
    await screen.findByTestId("worker-monitor");
    for (const name of ["连接", "预取与排队", "资源"]) expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    expect(screen.getByText("192.168.1.20:5682")).toBeInTheDocument();
    expect(screen.getByText("已对账")).toBeInTheDocument();
    const first = screen.getByRole("listitem", { name: "ABF-401" });
    expect(within(first).getByText("当前")).toBeInTheDocument();
    expect(within(screen.getByRole("listitem", { name: "ABF-402" })).getByText("预取")).toBeInTheDocument();
    const line = within(first).getByRole("list", { name: "ABF-401 步骤" });
    expect(within(line).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["下载 ✓", "修复", "回传"]);
    expect(within(line).getAllByRole("listitem")[1]).toHaveAttribute("aria-current", "step");
    // the controller's own LtxProgress, 本片参数 and 用时 cards, not a copy
    expect(screen.getByTestId("ltx-progress")).toBeInTheDocument();
    expect(screen.getByTestId("restore-params")).toBeInTheDocument();
    expect(screen.getByTestId("ltx-timing")).toBeInTheDocument();
    expect(screen.getByText("ABF-402 · 下载源文件 12.4/38.0 GB · 610 MB/s")).toBeInTheDocument();
    expect(screen.getByText("76.0 GB / 500.0 GB")).toBeInTheDocument();
    expect(screen.getByText("媒体任务占用").parentElement).toHaveTextContent("NVIDIA GeForce RTX 5070 Ti · 媒体任务占用");
    expect(document.body.textContent).not.toMatch(SLOGANS);
  });

  it("pauses and resumes claiming with 44 px controls; the only control", async () => {
    const read = vi.spyOn(api, "workflowWorker").mockResolvedValue(running());
    const pause = vi.spyOn(api, "workflowWorkerPause").mockResolvedValue({ paused: true });
    wrap(<WorkerMonitor />);
    const button = await screen.findByRole("button", { name: "暂停领取" });
    // the only control: no approval, archive, NAS or model action on a worker
    const names = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    expect(names.filter((n) => /确认|入库|审批|归档|NAS|下载模型|重试/.test(n))).toEqual([]);
    expect(button).toHaveStyle({ minHeight: "44px" });
    read.mockResolvedValue(running({ paused: true }));
    fireEvent.click(button);
    await waitFor(() => expect(pause).toHaveBeenCalledWith(true));
    expect(await screen.findByRole("button", { name: "恢复领取" })).toBeInTheDocument();
    expect(screen.getByText("已暂停领取：进行中的任务、回传和预取继续。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复领取" }));
    await waitFor(() => expect(pause).toHaveBeenLastCalledWith(false));
  });

  it("an idle or held worker says why, and an upload shows on the step line", async () => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running({
      films: [], current: null, transfers: [], reconciled: false, dispatch_reason: "等待对账", reason: "等待对账",
    }));
    wrap(<WorkerMonitor />);
    expect(await screen.findByText("没有派到本机的影片 · 等待对账")).toBeInTheDocument();
    expect(screen.getByText("等待对账", { selector: "dd" })).toBeInTheDocument();
    const film = running().films![0];
    const uploading = { ...running().current!, phase: "uploading" as const };
    expect(workerSteps(film, uploading).map((s) => s.state)).toEqual(["done", "pending", "current"]);
    // OD7: 下载 → 修复 → 回传 only; an ASR claimed before the upgrade adds no step of its own
    const asr = { ...film, stages: { restore: "succeeded", asr: "running" } };
    expect(workerSteps(asr, { ...uploading, stage: "asr", phase: "running" }).map((s) => s.state))
      .toEqual(["done", "done", "done"]);
    expect(workerSteps(asr, { ...uploading, stage: "asr" }).map((s) => s.state)).toEqual(["done", "done", "current"]);
  });

  it("shows a refused result that needs the operator (§11 IR3)", async () => {
    const notice = "主控拒绝了 ABF-401 的失败结果（invalid_request），记录已保留";
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running({ dispatch_notice: notice }));
    wrap(<WorkerMonitor />);
    expect(await screen.findByText(notice)).toBeInTheDocument();
  });

  it("is one column of cards at 390 px, its rows wrapping", async () => {
    vi.stubGlobal("innerWidth", 390);
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running());
    wrap(<WorkerMonitor />);
    const root = await screen.findByTestId("worker-monitor");
    expect(getComputedStyle(root).flexDirection).toBe("column");
    const line = screen.getByRole("list", { name: "ABF-401 步骤" });
    expect(getComputedStyle(line).flexWrap).toBe("wrap");
    vi.unstubAllGlobals();
  });

  it("is the 媒体处理 tab of a worker's console, in place of the workspace", async () => {
    vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "GPU-1", monitors: {} });
    vi.spyOn(api, "plugins").mockResolvedValue([] as never);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { role: "worker" } } as never);
    vi.spyOn(api, "workflowWorker").mockResolvedValue(running());
    const films = vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS_RESPONSE);
    wrap(<AgentConsole />);
    await waitFor(() => expect(api.config).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("tab", { name: /^Media processing$|^媒体处理$/ }));
    expect(await screen.findByTestId("worker-monitor")).toBeInTheDocument();
    expect(films).not.toHaveBeenCalled();
  });
});

describe("remote progress on the controller", () => {
  const MACHINE = { id: "w1", name: "5070 Ti" };
  const remoteSteps = (): WorkflowStep[] => restoreSteps("ltx").map((s) =>
    s.key === "restore" ? { ...s, machine: MACHINE, attempt_id: "a1", phase: "running" } : s);
  const REMOTE = row({ film_id: "abf-401", title: "浮光之间", actors: ["演员浮光"], stage: "restore", status: "running", steps: remoteSteps() });

  function mockController() {
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [REMOTE, ...FILMS.slice(1)] });
    vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, id === "abf-401" ? { steps: remoteSteps() } : {}));
    vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
    vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "HOME-PC", monitors: {} });
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { inbox_root: "D:\\TODO" } });
    vi.spyOn(api, "workflowDevices").mockResolvedValue({
      remote_workers: true, server_time: "2026-10-23T08:00:00Z", controller: { name: "本机", stages: ["翻译"] }, workers: [],
    });
    return vi.spyOn(api, "workflowRequeue").mockResolvedValue({ ok: true });
  }

  it("the drawer shows the 设备, the shared LTX cards and 停止并重新排队 after a confirmation", async () => {
    const requeue = mockController();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await waitFor(() => expect(within(drawer).getByTestId("ltx-progress")).toBeInTheDocument());
    const line = within(drawer).getByRole("list", { name: "浮光之间 生命周期" });
    expect(within(line).getByText("5070 Ti")).toBeInTheDocument();  // 设备 on the running step
    const remote = within(drawer).getByTestId("remote-step");
    expect(within(remote).getByText("5070 Ti · 修复")).toBeInTheDocument();
    fireEvent.click(within(remote).getByRole("button", { name: "停止并重新排队" }));
    const confirm = await screen.findByRole("dialog", { name: "停止并重新排队" });
    expect(requeue).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole("button", { name: "确认停止" }));
    await waitFor(() => expect(requeue).toHaveBeenCalledWith("abf-401", "a1"));
  });

  it("cancelling the dialog stops nothing; a local stage offers no requeue", async () => {
    const requeue = mockController();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "停止并重新排队" }));
    const confirm = await screen.findByRole("dialog", { name: "停止并重新排队" });
    fireEvent.click(within(confirm).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "停止并重新排队" })).not.toBeInTheDocument());
    expect(requeue).not.toHaveBeenCalled();
    const local = detail("abf-402", { steps: steps("asr", "running", { progress: "50%" }) });
    expect(local.steps?.some((s) => s.machine)).toBe(false);
  });

  it("offers no requeue once the worker uploads its finished stage (§11 CX5)", async () => {
    const uploading = (phase: "uploading" | "finishing" | "settling") => restoreSteps("ltx").map((st) =>
      st.key === "restore" ? { ...st, machine: MACHINE, attempt_id: "a1", phase } : st);
    mockController();
    for (const phase of ["uploading", "finishing"] as const) {
      vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, id === "abf-401" ? { steps: uploading(phase) } : {}));
      const view = wrap(<MediaWorkspace />);
      fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
      const remote = await screen.findByTestId("remote-step");
      expect(within(remote).getByText("5070 Ti · 修复 · 回传中")).toBeInTheDocument();
      expect(within(remote).queryByRole("button", { name: "停止并重新排队" })).not.toBeInTheDocument();
      view.unmount();
    }
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, id === "abf-401" ? { steps: uploading("settling") } : {}));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const remote = await screen.findByTestId("remote-step");
    expect(within(remote).getByRole("button", { name: "停止并重新排队" })).toBeInTheDocument();  // still stoppable
  });

  it.each([null, "回传 1.2/4.0 GB · 100 MB/s"])("shows upload instead of Jasna startup with transfer note %s", async (note) => {
    const uploading = restoreSteps("starting").map((st) => st.key === "restore"
      ? { ...st, machine: MACHINE, attempt_id: "a1", phase: "uploading" as const, note, progress: null }
      : st);
    mockController();
    vi.mocked(api.workflowFilms).mockResolvedValue({
      ...FILMS_RESPONSE, films: [{ ...REMOTE, steps: uploading, note }],
    });
    vi.mocked(api.workflowFilm).mockImplementation(async (id) => detail(id, { steps: uploading, note }));
    wrap(<MediaWorkspace />);
    const open = await screen.findByRole("button", { name: "查看 浮光之间 详情" });
    expect(screen.getAllByText(note ?? "回传中").length).toBeGreaterThan(0);
    expect(screen.queryByText(/正在启动 Jasna/)).not.toBeInTheDocument();
    fireEvent.click(open);
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await waitFor(() => expect(within(drawer).getAllByText(note ?? "回传中").length).toBeGreaterThan(0));
    expect(within(drawer).queryByTestId("restore-live")).not.toBeInTheDocument();
    expect(within(drawer).queryByText(/Jasna 正在加载模型|正在启动 Jasna/)).not.toBeInTheDocument();
  });
});
