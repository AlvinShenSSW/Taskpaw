import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The native picker (#71) of 设置 · 媒体工作流, inside the Tauri shell only.
const dialog = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => dialog);
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, HttpError, type AgentConfigView, type FilmDetail, type FilmRow, type HubServer, type PlanChoice } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { machineOf } from "../components/media/mediaModel";
import { PowerPage } from "../components/power/PowerPage";
import { WorkflowSettingsSection } from "../components/WorkflowSettings";
import { theme } from "../theme";
import { setLang } from "../i18n";
import { counts, detail, FILMS_RESPONSE, PLANS, row, steps } from "./fixtures/mediaWorkspace";
import {
  actions, FINISH, FINISH_TEXT, lostDetail, NOW, reassignDetail, REMOTE_DEVICES, REMOTE_FILMS, REMOTE_PLANS, REMOTE_ROWS, settingsState,
  settingsView, STOP, W,
} from "./fixtures/multiMachine";
import { POWER_SETTINGS, powerView } from "./fixtures/power";

// P2-4 (#295) §4, the confirmed UI/UX preview: 待处理's machine select, the board's machine column and
// 设备失联 rows, the drawer's 设备 line with 改派 / 判定已停止, 设备, 功耗's automatic peers, 设置 · 媒体工作流
// and the Mac's read-only view. Fixtures only; no live data.
const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};
const SLOGANS = ["让每一部影片，顺利归位。", "每部影片，一条完整记录。", "最后一步，由您确认。", "停在可恢复的位置。", "先安排，再开工。"];

function mockApi(films = REMOTE_FILMS, film: (id: string) => FilmDetail = (id) => (id === "flt-2" ? lostDetail() : reassignDetail({ film_id: id }))) {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(films);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(REMOTE_PLANS);
  vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => film(id));
  vi.spyOn(api, "workflowTarget").mockResolvedValue({ film_dir: "X", performer_id: null, target_dir: null, manual: false, target_path: null, absent: null,
    error_code: null, message: null, candidates: { query: [], items: [] } });
  vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
  vi.spyOn(api, "workflowDevices").mockResolvedValue(REMOTE_DEVICES);
  return {
    confirm: vi.spyOn(api, "workflowConfirmPlans").mockImplementation(async (plans: PlanChoice[]) =>
      ({ results: plans.map((p) => ({ film_id: p.film_id, ok: true, error: null, message: null })) })),
    pin: vi.spyOn(api, "workflowPin").mockResolvedValue({ ok: true }),
    attest: vi.spyOn(api, "workflowAttest").mockResolvedValue({ ok: true }),
  };
}
const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
const go = async (label: string) => {
  fireEvent.click(within(rail()).getByRole("button", { name: new RegExp(`^${label}`) }));
  return screen.findByRole("heading", { level: 1, name: label === "影片队列" ? "队列 · 派单状态" : label });
};
const pendingCard = (id: string) => document.querySelector(`[data-pending="${id}"]`) as HTMLElement;
const optionTexts = (s: HTMLSelectElement) => Array.from(s.options).map((o) => [o.textContent, o.disabled]);

beforeEach(() => {
  setLang("zh-CN");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  // the 预计 finishes are on NOW's day: only Date is pinned, timers stay real
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("待处理 machine select (R5/R6)", () => {
  it("offers 自动（推荐） first with its finish and energy, then each device or its reason, per tier", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("far-1")).toBeTruthy());
    const c = within(pendingCard("far-1"));
    const select = c.getByLabelText("机器") as HTMLSelectElement;
    expect(optionTexts(select)).toEqual([
      [`自动（推荐）· 预计 ${FINISH_TEXT.ti70} 在 5070 Ti 完成 · 约 0.9 度`, false],
      [`5070 Ti · 预计 ${FINISH_TEXT.ti70} 完成 · 约 0.9 度`, false],
      [`5060 Ti-1 · 预计 ${FINISH_TEXT.ti1} 完成 · 约 0.8 度`, false],
      ["5060 Ti-2 · 缺少高画质模型", true],
      ["5060 Ti-3 · 离线", true],
    ]);
    expect(select.value).toBe("");
    expect(c.getByText(`预计 ${FINISH_TEXT.ti70} 在 5070 Ti 完成 · 约 0.9 度`, { selector: "p" })).toBeInTheDocument();
    // 良好 makes 5060 Ti-2 eligible
    fireEvent.click(c.getByRole("radio", { name: /良好/ }));
    expect((c.getByLabelText("机器") as HTMLSelectElement).options[3].disabled).toBe(false);
    // picking a device shows its own line
    fireEvent.change(c.getByLabelText("机器"), { target: { value: W.ti2.id } });
    expect(c.getByText(`预计 ${FINISH_TEXT.ti1} 在 5060 Ti-2 完成 · 约 0.8 度`, { selector: "p" })).toBeInTheDocument();
    // 高 greys it out again, so the choice goes back to 自动
    fireEvent.click(c.getByRole("radio", { name: /高/ }));
    expect((c.getByLabelText("机器") as HTMLSelectElement).value).toBe("");
  });

  it("confirms 自动 as null and a picked device as its id", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("far-1")).toBeTruthy());
    fireEvent.click(within(pendingCard("far-1")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith([{ film_id: "far-1", input_revision: 1, ltx_model: "high", machine: null }]));
    fireEvent.change(within(pendingCard("mtn-7")).getByLabelText("机器"), { target: { value: W.ti1.id } });
    fireEvent.click(within(pendingCard("mtn-7")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirm).toHaveBeenLastCalledWith([{ film_id: "mtn-7", input_revision: 1, ltx_model: "good", machine: W.ti1.id }]));
  });

  it("全部按推荐确认 sends 自动 (null), including the 4K film waiting for a device", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("qlx-1")).toBeTruthy());
    expect(screen.getByText("4 部可按推荐派发")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全部按推荐确认" }));
    await waitFor(() => expect(m.confirm).toHaveBeenCalled());
    const sent = m.confirm.mock.calls[0][0];
    expect(sent.map((p) => p.film_id)).toEqual(["far-1", "mtn-7", "qlx-1", "pap-3"]);
    expect(sent.every((p) => p.machine === null)).toBe(true);
  });

  it("IR9: a disabled 全部按推荐确认 says why", async () => {
    mockApi();
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ ...REMOTE_PLANS,
      plans: REMOTE_PLANS.plans.filter((p) => p.film_id === "qlx-1").map((p) => ({
        ...p, recommendation: { ...p.recommendation, ltx_model: null },
      })),
    });
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("qlx-1")).toBeTruthy());
    const all = screen.getByRole("button", { name: "全部按推荐确认" });
    expect(all).toBeDisabled();
    const why = "没有可按推荐确认的影片：已调整、需重新查看或缺少选择的请单独确认";
    expect(all).toHaveAccessibleDescription(why);
    expect(all).toHaveAttribute("title", why);
  });

  it("4K: shows device reasons when every worker is ineligible and confirms 自动 normally", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("qlx-1")).toBeTruthy());
    const c = within(pendingCard("qlx-1"));
    const select = c.getByLabelText("机器") as HTMLSelectElement;
    expect(select).toHaveAccessibleDescription("等待设备：4K 需重新核验");
    expect(select).toBeEnabled();
    expect(optionTexts(select)).toEqual([
      ["自动（推荐）· 等待设备：4K 需重新核验", false],
      ["5070 Ti · 4K 需重新核验", true],
      ["5060 Ti-1 · 4K 需重新核验", true],
      ["5060 Ti-2 · 4K 需重新核验", true],
      ["5060 Ti-3 · 离线", true],
    ]);
    expect(screen.queryByText(/4K 暂/)).toBeNull();
    for (const radio of c.getAllByRole("radio")) expect(radio).toBeEnabled();
    const confirm = c.getByRole("button", { name: "确认入队" });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith([
      { film_id: "qlx-1", input_revision: 1, ltx_model: "high", machine: null },
    ]));
    await waitFor(() => expect(pendingCard("qlx-1")).toBeNull());
  });

  it("a film needing no GPU reads 「无需修复，本机完成」 with no device option", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("pap-3")).toBeTruthy());
    const select = within(pendingCard("pap-3")).getByLabelText("机器") as HTMLSelectElement;
    expect(optionTexts(select)).toEqual([["自动（推荐）· 无需修复，本机完成", false]]);
  });

  it("a single machine keeps its select unchanged (no 机器 label, no 自动)", async () => {
    mockApi(FILMS_RESPONSE, (id) => detail(id));
    vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(pendingCard("abf-422")).toBeTruthy());
    const c = within(pendingCard("abf-422"));
    expect(c.queryByLabelText("机器")).toBeNull();
    expect(optionTexts(c.getByLabelText("设备") as HTMLSelectElement)).toEqual([["本机 · RTX 5070 Ti 16 GB", false]]);
    expect(screen.queryByText(/部可按推荐派发/)).toBeNull();
  });
});

describe("board: machine column, routing reasons and 设备失联 (R1–R3)", () => {
  it("machineOf: the route, 本机, the running stage's worker, 待派发", () => {
    expect(REMOTE_ROWS.map(machineOf)).toEqual(["5060 Ti-3", "5070 Ti", "本机", "自动", "5070 Ti"]);
  });

  it("CX30: after the GPU work, 本机 for the controller's stages and none once done — never 待派发", () => {
    const at = (film_id: string, group: FilmRow["group"], key: Parameters<typeof steps>[0], state: Parameters<typeof steps>[1]) =>
      row({ film_id, group, stage: key, status: state === "done" ? "succeeded" : "running", steps: steps(key, state), actions: actions() });
    expect(machineOf(at("w", "active", "restore", "waiting"))).toBe("待派发");
    expect(machineOf(at("a", "active", "asr", "running"))).toBe("本机");
    expect(machineOf(at("s", "active", "scrape", "running"))).toBe("本机");
    expect(machineOf(at("c", "confirm", "confirm", "waiting"))).toBe("本机");
    expect(machineOf(at("d", "done", "done", "done"))).toBeNull();
    // a failed restore waits for a device again
    expect(machineOf(at("f", "attention", "restore", "failed"))).toBe("待派发");
  });

  it("CX34 (OD7): ASR runs on the controller — 本机 for ASR, 待派发 only while the restore is unfinished", () => {
    const at = (key: Parameters<typeof steps>[0], state: Parameters<typeof steps>[1]) =>
      row({ film_id: `${key}-${state}`, group: "active", stage: key, status: "pending", steps: steps(key, state), actions: actions() });
    expect(machineOf(at("asr", "waiting"))).toBe("本机");
    expect(machineOf(at("asr", "running"))).toBe("本机");
    expect(machineOf(at("translate", "waiting"))).toBe("本机");
    expect(machineOf(at("restore", "waiting"))).toBe("待派发");
    expect(machineOf(at("restore", "running"))).toBe("待派发");
  });

  it("概览 lists the 设备失联 film with its note, last heartbeat and 处理 → the drawer", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    const lost = await screen.findByRole("region", { name: "设备失联 · 1" });
    expect(lost).toHaveTextContent("设备失联：5060 Ti-3 · 后台重连中");
    expect(lost).toHaveTextContent("修复 · 最后进度 41%");
    expect(lost).toHaveTextContent("最后心跳 7 分钟前");
    expect(lost).toHaveTextContent("5060 Ti-3");
    // the waiting films show the pass's reasons, never 「等待 GPU 设备」 for the disk gate
    const waiting = screen.getByRole("region", { name: "等待 · 2" });
    expect(waiting).toHaveTextContent("主控磁盘空间不足");
    expect(waiting).toHaveTextContent("待派发");
    expect(waiting).toHaveTextContent("已派往 5070 Ti");
    expect(waiting).not.toHaveTextContent("等待 GPU 设备");
    const active = screen.getByRole("region", { name: "正在处理" });
    expect(within(active).getAllByTestId("film-machine").map((e) => e.textContent)).toEqual(["5070 Ti", "本机"]);
    fireEvent.click(within(lost).getByRole("button", { name: "处理 浮光之间" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    expect(await within(drawer).findByTestId("device-line")).toHaveTextContent("5060 Ti-3");
    for (const s of SLOGANS) expect(document.body.textContent).not.toContain(s);
  });

  it("概览's 另有 N 部等待 expands the folded waiting films and collapses them again (#353)", async () => {
    const extra = ["a", "b", "c"].map((k) => row({ film_id: `far-${k}`, title: `远岸来信 ${k}`, stage: "restore", status: "pending",
      note: "主控磁盘空间不足", pin: { worker_id: null, name: "自动", version: 1 }, actions: actions({ can_reassign: true }),
      steps: steps("restore", "waiting", { note: "主控磁盘空间不足" }) }));
    const films = [...REMOTE_ROWS, ...extra];
    mockApi({ ...REMOTE_FILMS, films, counts: counts(films, []) });
    wrap(<MediaWorkspace />);
    const waiting = await screen.findByRole("region", { name: "等待 · 5" });
    expect(waiting.querySelectorAll("article.film-row")).toHaveLength(4);
    const toggle = within(waiting).getByRole("button", { name: "另有 1 部等待" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(waiting.querySelectorAll("article.film-row")).toHaveLength(5);
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(waiting.querySelectorAll("article.film-row")).toHaveLength(4);
  });

  it("影片队列 shows each film's machine; 异常 lists 设备失联 with 处理", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("影片队列");
    const machine = (id: string) => (document.querySelector(`[data-film="${id}"]`) as HTMLElement).children[1];
    expect(machine("far-3")).toHaveTextContent("自动");
    expect(machine("mist-18")).toHaveTextContent("5070 Ti");
    expect(machine("moon-6")).toHaveTextContent("本机");
    await go("异常");
    const card = document.querySelector('[data-exception="flt-2"]') as HTMLElement;
    expect(card).toHaveTextContent("设备失联");
    expect(card).toHaveTextContent("5060 Ti-3");
    fireEvent.click(within(card).getByRole("button", { name: "处理 浮光之间" }));
    expect(await screen.findByRole("dialog", { name: "影片详情" })).toBeInTheDocument();
  });
});

describe("drawer: 设备, 改派 and 判定已停止 (R7/R8)", () => {
  const openDrawer = async (id: string) => {
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("影片队列");
    fireEvent.click(within(document.querySelector(`[data-film="${id}"]`) as HTMLElement).getByRole("button", { name: /^查看/ }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    return within(await within(drawer).findByTestId("device-line"));
  };

  it("an unclaimed film on a lost worker: 失联 7 分钟, the reason, and 改派 to a device", async () => {
    const m = mockApi();
    const line = await openDrawer("mist-18");
    expect(line.getByText("失联 7 分钟")).toBeInTheDocument();
    expect(line.getByText(/心跳中断 · 后台重连中 · 最后心跳/)).toBeInTheDocument();
    expect(line.queryByRole("button", { name: "判定已停止" })).toBeNull();
    fireEvent.click(line.getByRole("button", { name: "改派" }));
    const dialog = await screen.findByRole("dialog", { name: "改派 山海一页" });
    expect(dialog).toHaveTextContent("当前 5060 Ti-3 · 失联 7 分钟");
    const select = within(dialog).getByLabelText("改派到") as HTMLSelectElement;
    expect(optionTexts(select)).toEqual([
      ["自动（推荐） · 当前", false], ["5070 Ti", false], ["5060 Ti-1", false],
      ["5060 Ti-2 · 缺少高画质模型", true], ["5060 Ti-3 · 离线", true],
    ]);
    fireEvent.change(select, { target: { value: W.ti70.id } });
    // IR4: the old worker releases its staging once the film is routed elsewhere (P2-3 R10)
    expect(within(dialog).getByText("还没开始修复，可以直接改派：新机器重新下载源文件；5060 Ti-3 恢复后会自动清理它的暂存。"))
      .toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认改派" }));
    await waitFor(() => expect(m.pin).toHaveBeenCalledWith("mist-18", 1, 5, W.ti70.id));
  });

  it("CX29: the current 自动 on a 失联 worker's route can be confirmed again — 重新自动派单 (null)", async () => {
    const m = mockApi();
    const line = await openDrawer("mist-18");
    fireEvent.click(line.getByRole("button", { name: "改派" }));
    const dialog = await screen.findByRole("dialog", { name: "改派 山海一页" });
    expect((within(dialog).getByLabelText("改派到") as HTMLSelectElement).value).toBe("");
    expect(within(dialog).queryByRole("button", { name: "确认改派" })).toBeNull();
    const again = within(dialog).getByRole("button", { name: "重新自动派单" });
    expect(again).toBeEnabled();
    expect(dialog).toHaveTextContent("5060 Ti-3 恢复后会自动清理它的暂存");
    fireEvent.click(again);
    await waitFor(() => expect(m.pin).toHaveBeenCalledWith("mist-18", 1, 5, null));
    expect(await screen.findByText("山海一页 已重新自动派单")).toBeInTheDocument();
  });

  it("CX29/IR9: the current 自动 on a healthy route stays disabled and says why; estimates per choice", async () => {
    mockApi(REMOTE_FILMS, () => reassignDetail({
      route: { ...reassignDetail().route!, machine: W.ti70, live: "online", silent_s: 2 },
      reassign_options: [
        { worker_id: null, name: "自动", reason: null, current: true, machine: W.ti70, finish_at: FINISH.ti70, energy_wh: 900 },
        { worker_id: W.ti70.id, name: "5070 Ti", reason: null, current: false, finish_at: FINISH.ti70, energy_wh: 900 },
        { worker_id: W.ti2.id, name: "5060 Ti-2", reason: "缺少高画质模型", current: false, finish_at: null, energy_wh: null },
      ],
    }));
    const line = await openDrawer("mist-18");
    fireEvent.click(line.getByRole("button", { name: "改派" }));
    const dialog = await screen.findByRole("dialog", { name: "改派 山海一页" });
    expect(optionTexts(within(dialog).getByLabelText("改派到") as HTMLSelectElement)).toEqual([
      [`自动（推荐） · 预计 ${FINISH_TEXT.ti70} 在 5070 Ti 完成 · 约 0.9 度 · 当前`, false],
      [`5070 Ti · 预计 ${FINISH_TEXT.ti70} 完成 · 约 0.9 度`, false],
      ["5060 Ti-2 · 缺少高画质模型", true],
    ]);
    const confirm = within(dialog).getByRole("button", { name: "确认改派" });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAccessibleDescription("与当前派单相同");
    // 自动 keeps a healthy route (IR8): no cleanup is promised
    expect(within(dialog).getByText("还没开始修复，可以直接改派：新机器重新下载源文件。")).toBeInTheDocument();
  });

  it("IR1: a claim that won the race closes the dialog, refreshes the film and says 判定已停止 comes first", async () => {
    const m = mockApi();
    m.pin.mockRejectedValue(new HttpError("已开始处理", 409, "route_claimed"));
    const line = await openDrawer("mist-18");
    const film = vi.mocked(api.workflowFilm);
    const films = vi.mocked(api.workflowFilms);
    fireEvent.click(line.getByRole("button", { name: "改派" }));
    const dialog = await screen.findByRole("dialog", { name: "改派 山海一页" });
    fireEvent.change(within(dialog).getByLabelText("改派到"), { target: { value: W.ti1.id } });
    const [filmCalls, filmsCalls] = [film.mock.calls.length, films.mock.calls.length];
    fireEvent.click(within(dialog).getByRole("button", { name: "确认改派" }));
    expect(await screen.findByText("已开始处理，需先判定已停止")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "改派 山海一页" })).toBeNull());
    await waitFor(() => expect(film.mock.calls.length).toBeGreaterThan(filmCalls));
    await waitFor(() => expect(films.mock.calls.length).toBeGreaterThan(filmsCalls));
  });

  it("CX30: the drawer of a film past its GPU work shows 本机; a done film has no 设备 line", async () => {
    const past = (over: Partial<FilmDetail>) => () => reassignDetail({ route: null, note: null, actions: actions(), ...over });
    mockApi(REMOTE_FILMS, past({ group: "active", steps: steps("scrape", "running") }));
    const line = await openDrawer("mist-18");
    expect(line.getByText("本机")).toBeInTheDocument();
    const drawer = screen.getByRole("dialog", { name: "影片详情" });
    expect(drawer).not.toHaveTextContent("待派发");
    expect(within(drawer).getAllByText("本机").length).toBeGreaterThan(0);
  });

  it("CX30: a film in 确认入库 shows 本机, a done one shows none", async () => {
    mockApi(REMOTE_FILMS, (id) => reassignDetail({ film_id: id, route: null, note: null, actions: actions(),
      ...(id === "mist-18" ? { group: "done" as const, steps: steps("done", "done") } : { group: "confirm" as const, steps: steps("confirm", "waiting") }) }));
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("影片队列");
    fireEvent.click(within(document.querySelector('[data-film="far-3"]') as HTMLElement).getByRole("button", { name: /^查看/ }));
    let drawer = await screen.findByRole("dialog", { name: "影片详情" });
    expect(await within(drawer).findByTestId("device-line")).toHaveTextContent("本机");
    expect(drawer).not.toHaveTextContent("待派发");
    fireEvent.click(within(drawer).getByRole("button", { name: "关闭对话框" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "影片详情" })).toBeNull());
    fireEvent.click(within(document.querySelector('[data-film="mist-18"]') as HTMLElement).getByRole("button", { name: /^查看/ }));
    drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await within(drawer).findByRole("heading", { name: "山海一页" });
    expect(within(drawer).queryByTestId("device-line")).toBeNull();
    expect(drawer).not.toHaveTextContent("待派发");
  });

  it("a claimed lost film: 改派 disabled with its reason; 判定已停止 shows both channels and confirms", async () => {
    const m = mockApi();
    const line = await openDrawer("flt-2");
    const reassign = line.getByRole("button", { name: "改派" });
    expect(reassign).toBeDisabled();
    expect(reassign).toHaveAccessibleDescription("已开始修复，需先判定已停止");
    fireEvent.click(line.getByRole("button", { name: "判定已停止" }));
    const dialog = await screen.findByRole("alertdialog", { name: "判定 5060 Ti-3 已停止" });
    expect(dialog).toHaveTextContent("浮光之间 · 修复 41%");
    expect(dialog).toHaveTextContent("两个通道都联系不上这台机器");
    expect(dialog).toHaveTextContent("已中断 7 分钟");
    expect(dialog).toHaveTextContent("无响应 6 分钟 · 192.168.1.34:5680 · 超时");
    // IR8: the safe choice has the initial focus
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    expect(dialog).toHaveTextContent("本阶段按取消计（免费重试），影片将重新派单，该机器之后交回的结果会被丢弃。");
    expect(within(dialog).getByRole("button", { name: "取消" })).toBeInTheDocument();
    const go = within(dialog).getByRole("button", { name: "我确认该机器已关机或任务已停止" });
    expect(go).toHaveClass("danger");
    expect(m.attest).not.toHaveBeenCalled();
    fireEvent.click(go);
    await waitFor(() => expect(m.attest).toHaveBeenCalledWith("flt-2", STOP.assignment_id, STOP.expected_version));
  });

  it("判定已停止 is disabled with the channel that is still alive", async () => {
    mockApi(REMOTE_FILMS, () => lostDetail({ route: { ...lostDetail().route!, live: "online", silent_s: 2 },
      actions: { can_reassign: false, stop: { ...STOP, allowed: false, reason: "心跳通道：5060 Ti-3 2 秒前仍有心跳", heartbeat_silent_s: 2 } } }));
    const line = await openDrawer("flt-2");
    const stop = line.getByRole("button", { name: "判定已停止" });
    expect(stop).toBeDisabled();
    expect(stop).toHaveAccessibleDescription("心跳通道：5060 Ti-3 2 秒前仍有心跳");
    expect(line.getByText("在线")).toBeInTheDocument();
  });

  it("the 能耗 rows name the machine the stage ran on", async () => {
    mockApi();
    await openDrawer("flt-2");
    expect(await screen.findByTestId("energy-devices")).toHaveTextContent("5060 Ti-3");
  });
});

describe("设备 (§4)", () => {
  it("shows the summary, the current film with 排队, and 判定已停止 on unresolved assignments of a lost worker", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("设备");
    expect(await screen.findByText("工作机 3 · 可领取 1 · 待核验 0 · 失联 2")).toBeInTheDocument();
    const online = screen.getByRole("article", { name: "5070 Ti" });
    expect(online).toHaveTextContent("白昼回声 · 修复 41%");
    expect(online).toHaveTextContent("排队 1");
    expect(online).toHaveTextContent("可用 2.6 TB · 暂存 212 GB / 500 GB");
    expect(online).toHaveTextContent("98% · 268 W · 显存 14.1 GB");
    expect(within(online).queryByRole("button", { name: "判定已停止" })).toBeNull();
    const lost = screen.getByRole("article", { name: "5060 Ti-3" });
    expect(lost).toHaveTextContent("失联 7 分钟");
    const list = within(lost).getByRole("region", { name: "5060 Ti-3 未结派单" });
    expect(list).toHaveTextContent("浮光之间 · 修复 41%");
    const held = within(screen.getByRole("region", { name: "5060 Ti-5 未结派单" })).getByRole("button", { name: "判定已停止" });
    expect(held).toBeDisabled();
    expect(held).toHaveAccessibleDescription("状态通道：仅 130 秒无响应");
    fireEvent.click(within(list).getByRole("button", { name: "判定已停止" }));
    const dialog = await screen.findByRole("alertdialog", { name: "判定 5060 Ti-3 已停止" });
    fireEvent.click(within(dialog).getByRole("button", { name: "我确认该机器已关机或任务已停止" }));
    await waitFor(() => expect(m.attest).toHaveBeenCalledWith("flt-2", "as-lost", 4));
  });

  it("the expandable 领取资格 matrix shows 1080p by tier without the obsolete 4K hold", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("设备");
    const card = await screen.findByRole("article", { name: "5070 Ti" });
    fireEvent.click(within(card).getByRole("button", { name: "详情" }));
    const matrix = within(card).getByRole("region", { name: "5070 Ti 领取资格" });
    const rows = within(matrix).getAllByRole("row").map((r) => r.textContent);
    expect(rows).toEqual(["分辨率标准良好高", "1080p可领取可领取可领取"]);
    expect(matrix).not.toHaveTextContent(/后续版本开放|暂不派发/);
  });

  it("本机's 「主控设置」 opens 设置 · 媒体工作流 where the host offers it", async () => {
    mockApi();
    const open = vi.fn();
    wrap(<MediaWorkspace onOpenSettings={open} />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("设备");
    const local = await screen.findByRole("article", { name: "本机" });
    fireEvent.click(within(local).getByRole("button", { name: "主控设置" }));
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("no 「主控设置」 without a host link", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("设备");
    const local = await screen.findByRole("article", { name: "本机" });
    expect(within(local).queryByRole("button", { name: "主控设置" })).toBeNull();
  });

  it("no agent.yaml notice: remote work off points to 设置 · 媒体工作流", async () => {
    mockApi();
    vi.spyOn(api, "workflowDevices").mockResolvedValue({ ...REMOTE_DEVICES, remote_workers: false, workers: [] });
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await go("设备");
    expect(await screen.findByText(/设置 · 媒体工作流/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("agent.yaml");
  });

  it("keeps 44 px controls and one column at 390 px (phone)", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width:900px") || query.includes("max-width: 900px"), media: query, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }));
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(screen.getByRole("button", { name: "工作区导航" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: /^设备/ }));
    const card = await screen.findByRole("article", { name: "5060 Ti-3" });
    expect(card.parentElement).toHaveClass("stack");
    for (const b of within(card).getAllByRole("button")) expect(b).toHaveClass("btn");
  });
});

describe("功耗: automatic peers (R11)", () => {
  it("tags an enrolled worker 「工作机 · 自动加入」 and never offers to remove it", async () => {
    vi.spyOn(api, "power").mockImplementation(async (range) => {
      const v = powerView(range);
      return { ...v, peers: (v.peers ?? []).map((p, i) => (i === 0 ? { ...p, name: "5070 Ti", source: "workflow" as const } : p)) };
    });
    vi.spyOn(api, "workflowFilms").mockResolvedValue(REMOTE_FILMS);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
    wrap(<PowerPage />);
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-machine="peer:5070 Ti"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(row).toHaveTextContent("工作机 · 自动加入");
    fireEvent.click(screen.getByRole("button", { name: "估算参数" }));
    const auto = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-auto-peer="5070 Ti"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(auto).toHaveTextContent("工作机 · 自动加入");
    expect(within(auto).queryByRole("button")).toBeNull();
  });
});

describe("设置 · 媒体工作流 (R13–R16)", () => {
  const roleSelect = () => screen.getByRole("combobox", { name: /^本机角色/ });
  const pickRole = async (label: string) => {
    fireEvent.mouseDown(roleSelect());
    fireEvent.click(await screen.findByRole("option", { name: label }));
  };

  it("loading, then the 主控 fields with ffprobe found on PATH", async () => {
    let answer: (v: ReturnType<typeof settingsState>) => void = () => {};
    vi.spyOn(api, "workflowSettings").mockReturnValue(new Promise((r) => { answer = r; }));
    wrap(<WorkflowSettingsSection />);
    expect(screen.getByText("正在读取设置…")).toBeInTheDocument();
    answer(settingsState("controller"));
    expect(await screen.findByLabelText("INBOX 目录")).toHaveValue("E:\\TODO");
    expect(roleSelect()).toHaveTextContent("媒体主控");
    expect(screen.getByText("在 PATH 找到 · C:\\ffmpeg\\bin\\ffprobe.exe")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "允许 GPU 工作机接入" })).toBeChecked();
    expect(screen.getByText("开启后修复交给 GPU 工作机，识别仍在本机")).toBeInTheDocument();
    expect(screen.getByLabelText("工作机接口端口")).toHaveValue("5682");
    // OD7 R11: the 主控 always transcribes; its Jasna only without GPU workers
    expect(screen.getByLabelText("WhisperJAV")).toBeInTheDocument();
    expect(screen.queryByLabelText("Jasna")).toBeNull();
    expect(screen.queryByLabelText("暂存目录")).toBeNull();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();  // nothing changed
    expect(screen.getByRole("button", { name: "保存" })).toHaveAccessibleDescription("没有更改");  // IR9
    // a 主控 without GPU workers runs its own tools
    fireEvent.click(screen.getByRole("switch", { name: "允许 GPU 工作机接入" }));
    expect(screen.getByLabelText("Jasna")).toHaveValue("C:\\Jasna\\jasna.exe");
    expect(screen.getByRole("combobox", { name: /^LTX 显卡预设/ })).toBeInTheDocument();
    expect(screen.getByLabelText("WhisperJAV")).toBeInTheDocument();
  });

  it("GPU 工作机 fields, 不参与 has none; a save sends only the role and the changed fields", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller"));
    const save = vi.spyOn(api, "workflowSaveSettings").mockResolvedValue({ ok: true, restart_required: true });
    wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("INBOX 目录");
    await pickRole("GPU 工作机");
    expect(screen.getByLabelText("暂存目录")).toBeInTheDocument();
    expect(screen.getByLabelText("暂存上限（GB）")).toHaveValue("500");
    expect(screen.queryByLabelText("INBOX 目录")).toBeNull();
    // OD7 R11: a GPU 工作机 restores only — Jasna, no WhisperJAV
    expect(screen.getByLabelText("Jasna")).toBeInTheDocument();
    expect(screen.queryByLabelText("WhisperJAV")).toBeNull();
    fireEvent.change(screen.getByLabelText("暂存目录"), { target: { value: " E:\\Staging " } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ role: "worker", staging_dir: "E:\\Staging" }));
    await pickRole("不参与（普通 Agent，仅监控）");
    expect(screen.queryByLabelText("暂存目录")).toBeNull();
    expect(screen.queryByLabelText("INBOX 目录")).toBeNull();
  });

  it("shows each field error in Chinese and a summary; a bad number never leaves the page", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller"));
    const save = vi.spyOn(api, "workflowSaveSettings").mockRejectedValue(
      new HttpError("设置有误", 422, undefined, { inbox_root: "目录不存在", worker_api_port: "端口与控制端口相同" }));
    wrap(<WorkflowSettingsSection />);
    const inbox = await screen.findByLabelText("INBOX 目录");
    fireEvent.change(screen.getByLabelText("工作机接口端口"), { target: { value: "56x" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("请输入整数")).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("工作机接口端口"), { target: { value: "5681" } });
    fireEvent.change(inbox, { target: { value: "E:\\Nope" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("目录不存在")).toBeInTheDocument();
    expect(screen.getByText("端口与控制端口相同")).toBeInTheDocument();
    expect(screen.getByLabelText("INBOX 目录")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("INBOX 目录：目录不存在");
  });

  it("saving, then the busy refusal with 暂停派发, and the restart banner text only", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller"));
    let fail: (e: unknown) => void = () => {};
    vi.spyOn(api, "workflowSaveSettings").mockReturnValue(new Promise((_r, j) => { fail = j; }));
    const pause = vi.spyOn(api, "workflowPause").mockResolvedValue({ paused: true });
    wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("INBOX 目录");
    await pickRole("GPU 工作机");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("button", { name: "正在保存…" })).toBeDisabled();
    fail(new HttpError("还有 3 部影片在处理中（2 部在 GPU 工作机上），请先暂停派发并等待完成", 409, "workflow_busy"));
    const alert = await screen.findByRole("alert");
    // IR5: the refusal names the role it did not switch to
    expect(alert).toHaveTextContent("未切换到 GPU 工作机：还有 3 部影片在处理中（2 部在 GPU 工作机上），请先暂停派发并等待完成");
    fireEvent.click(within(alert).getByRole("button", { name: "暂停派发" }));
    await waitFor(() => expect(pause).toHaveBeenCalledWith(true));
    expect(await screen.findByText("已暂停派发；在途影片继续，完成后再保存。")).toBeInTheDocument();
  });

  it("the restart banner while saved and running differ, with no restart button (#299)", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller", {
      saved: settingsView("controller", { inbox_root: "F:\\INBOX" }), restart_required: true }));
    wrap(<WorkflowSettingsSection />);
    expect(await screen.findByRole("status")).toHaveTextContent("已保存，重启 TaskPaw 后生效（INBOX 目录）");
    expect(screen.queryByRole("button", { name: /重启/ })).toBeNull();
  });

  it("IR3: a missing INBOX offers 「创建此目录」; the tools and Miko show what was found", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller", {
      saved: settingsView("controller", { remote_workers: false, mdcx_python: "C:\\mdcx\\python.exe" }),
      running: settingsView("controller", { remote_workers: false, mdcx_python: "C:\\mdcx\\python.exe" }),
      paths: { inbox_root: "missing", jasna_exe_path: "ok", mdcx_python: "ok", mdcx_root: null, mdcx_profile: null },
      miko: "partial",
    }));
    const create = vi.spyOn(api, "workflowCreateDir").mockResolvedValue({ ok: true, created: true });
    wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("INBOX 目录");
    expect(screen.getByText("INBOX 目录不存在")).toBeInTheDocument();
    expect(screen.getByLabelText("Jasna")).toHaveAccessibleDescription("已找到");
    expect(screen.getByText(/缺少 Miko 目录、Miko 配置/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建此目录" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith("E:\\TODO"));
    expect(await screen.findByText("已创建 INBOX 目录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建此目录" })).toBeNull();
  });

  it("IR3: 「创建此目录」 after a 422 「目录不存在」, and the backend's refusal on the field", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller"));
    vi.spyOn(api, "workflowSaveSettings").mockRejectedValue(new HttpError("设置有误", 422, undefined, { inbox_root: "目录不存在" }));
    vi.spyOn(api, "workflowCreateDir").mockRejectedValue(new HttpError("设置有误", 422, undefined, { path: "无法创建目录" }));
    wrap(<WorkflowSettingsSection />);
    fireEvent.change(await screen.findByLabelText("INBOX 目录"), { target: { value: "Z:\\INBOX" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.click(await screen.findByRole("button", { name: "创建此目录" }));
    expect(await screen.findByText("无法创建目录")).toBeInTheDocument();
    expect(screen.getByLabelText("INBOX 目录")).toHaveAttribute("aria-invalid", "true");
  });

  it("IR3: 选择 / 更改 open the native picker inside the shell only", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("controller", {
      saved: settingsView("controller", { remote_workers: false }), running: settingsView("controller", { remote_workers: false }) }));
    const first = wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("INBOX 目录");
    expect(screen.queryByRole("button", { name: "选择 INBOX 目录" })).toBeNull();  // a browser has no native dialog
    first.unmount();
    vi.stubGlobal("__TAURI_INTERNALS__", {});
    dialog.open.mockResolvedValueOnce("F:\\INBOX").mockResolvedValueOnce("D:\\Jasna\\jasna.exe");
    wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("INBOX 目录");
    fireEvent.click(screen.getByRole("button", { name: "选择 INBOX 目录" }));
    await waitFor(() => expect(screen.getByLabelText("INBOX 目录")).toHaveValue("F:\\INBOX"));
    expect(dialog.open).toHaveBeenLastCalledWith(expect.objectContaining({ directory: true, multiple: false }));
    fireEvent.click(screen.getByRole("button", { name: "更改 Jasna" }));
    await waitFor(() => expect(screen.getByLabelText("Jasna")).toHaveValue("D:\\Jasna\\jasna.exe"));
    expect(dialog.open).toHaveBeenLastCalledWith(expect.objectContaining({ directory: false }));
  });

  it("加入 shows only while the running role is GPU 工作机", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState("worker", { running: settingsView("controller") }));
    const worker = vi.spyOn(api, "workflowWorker");
    wrap(<WorkflowSettingsSection />);
    await screen.findByLabelText("暂存目录");
    expect(screen.getByText("保存并重启后，在这里粘贴主控的配对码加入。")).toBeInTheDocument();
    expect(worker).not.toHaveBeenCalled();
  });
});

describe("Mac read-only (#286)", () => {
  const SERVER: HubServer = { id: 7, name: "Windows controller", ip: "10.0.0.7", port: 5680, enabled: 1, online: true };
  it("shows the machine names and 设备失联, and hides every mutation", async () => {
    vi.spyOn(api, "hubWorkflowFilms").mockResolvedValue(REMOTE_FILMS);
    vi.spyOn(api, "hubWorkflowPlans").mockResolvedValue(REMOTE_PLANS);
    vi.spyOn(api, "hubWorkflowFilm").mockImplementation(async () => lostDetail());
    vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
    wrap(<MediaWorkspace readOnly server={SERVER} />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    const lost = await screen.findByRole("region", { name: "设备失联 · 1" });
    expect(lost).toHaveTextContent("5060 Ti-3");
    fireEvent.click(within(lost).getByRole("button", { name: "查看详情 浮光之间" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    const line = await within(drawer).findByTestId("device-line");
    expect(line).toHaveTextContent("失联 7 分钟");
    expect(within(line).queryByRole("button")).toBeNull();
    expect(within(drawer).queryByRole("button", { name: /改派|判定已停止|停止并重新排队/ })).toBeNull();
    fireEvent.click(within(drawer).getByRole("button", { name: "关闭对话框" }));
    await go("待处理");
    await waitFor(() => expect(pendingCard("far-1")).toBeTruthy());
    expect(within(pendingCard("far-1")).queryByRole("combobox")).toBeNull();
    expect(pendingCard("far-1")).toHaveTextContent(`预计 ${FINISH_TEXT.ti70} 在 5070 Ti 完成`);
  });
});


it("成品目录 offers choose, open, create and saves the changed path (#301)", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline"))));
  dialog.open.mockResolvedValueOnce("E:\\Other");
  const state = settingsState("controller");
  state.saved.output_dir = "E:\\Output";
  state.running.output_dir = state.saved.output_dir;
  state.paths = { output_dir: "missing" };
  vi.spyOn(api, "workflowSettings").mockResolvedValue(state);
  const save = vi.spyOn(api, "workflowSaveSettings").mockResolvedValue({ ok: true, restart_required: true });
  const create = vi.spyOn(api, "workflowCreateDir").mockResolvedValue({ ok: true, created: true });
  const open = vi.spyOn(api, "workflowOpenDir").mockResolvedValue(undefined);
  wrap(<WorkflowSettingsSection />);
  expect(await screen.findByLabelText("成品目录")).toHaveValue(state.saved.output_dir);
  expect(screen.getByText("刮削完成、等待确认和归档的影片放在这里，每部一个文件夹（即最终的归档文件夹）；归档校验通过后删除。")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开 成品目录" }));
  await waitFor(() => expect(open).toHaveBeenCalledWith(state.saved.output_dir));
  fireEvent.click(screen.getByRole("button", { name: "创建此目录" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith(state.saved.output_dir));
  fireEvent.click(screen.getByRole("button", { name: "选择 成品目录" }));
  await waitFor(() => expect(screen.getByLabelText("成品目录")).toHaveValue("E:\\Other"));
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({ role: "controller", output_dir: "E:\\Other" }));
});
