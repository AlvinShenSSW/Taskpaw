import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, HttpError, PLAN_BATCH_MAX, type PlanChoice, type TargetPreflight } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import * as nativePicker from "../components/nativePicker";
import {
  ACTIVE_STATES, filmStatus, lifecycle, matchesStage, progressText, reviewStatus, stageLine, STATUS_META, workItems,
} from "../components/media/mediaModel";
import { PREFLIGHT_MS } from "../components/media/workspace";
import { actions, REMOTE_DEVICES, REMOTE_PLANS, REMOTE_ROWS, W } from "./fixtures/multiMachine";
import { LtxDownload } from "../components/LtxDownload";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import { setLang } from "../i18n";
import {
  counts, detail, FILMS, FILMS_RESPONSE, lookupFailed, PACKAGES, performer, plan, PLANS, preflight, PREFLIGHTS, revisions, row, steps,
} from "./fixtures/mediaWorkspace";

const wrapWith = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { qc, ...render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>) };
};
const wrap = (ui: ReactNode) => wrapWith(ui);

const SLOGANS = ["让每一部影片，顺利归位。", "每部影片，一条完整记录。", "最后一步，由您确认。", "停在可恢复的位置。", "先安排，再开工。",
  "WORKFLOW /", "FILM QUEUE", "HUMAN REVIEW", "EXCEPTIONS /", "PLANNING /", "LOCAL AGENT"];
const FORBIDDEN = /演示|FIXTURE|静态快照|虚构/;

function mockApi() {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS_RESPONSE);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
  vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id));
  vi.spyOn(api, "workflowTarget").mockImplementation(async (id, performer, targetDir) => {
    const pf = PREFLIGHTS[id] ?? preflight(id.toUpperCase(), []);
    if (targetDir) return { ...pf, target_dir: targetDir, manual: true, target_path: `${targetDir}/${pf.film_dir}`, absent: true };
    if (pf.error_code) return pf;
    // as the backend: exactly one candidate is checked without a performer too (F4)
    const items = pf.candidates?.items ?? [];
    const checked = performer ?? (items.length === 1 ? items[0].id : undefined);
    if (checked === undefined) return pf;
    // the target ends in the film folder; an unknown performer is `missing`
    const chosen = items.find((p) => p.id === checked);
    return { ...pf, performer_id: String(checked), absent: chosen ? true : null,
      target_path: chosen ? `${chosen.unc_path}\\${pf.film_dir}` : null, error_code: chosen ? null : "missing" };
  });
  vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
  vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "HOME-PC", monitors: {} });
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { inbox_root: "D:\\TODO" } });
  vi.spyOn(api, "workflowDevices").mockResolvedValue({
    remote_workers: false, server_time: "2026-10-23T08:00:00Z", controller: { name: "本机", stages: ["修复", "识别", "翻译"] }, workers: [],
  });
  return {
    confirmPlans: vi.spyOn(api, "workflowConfirmPlans").mockImplementation(async (plans: PlanChoice[]) =>
      ({ results: plans.map((p) => ({ film_id: p.film_id, ok: true, error: null, message: null })) })),
    confirm: vi.spyOn(api, "workflowConfirm").mockResolvedValue({ approval_id: "a1" }),
    pause: vi.spyOn(api, "workflowPause").mockResolvedValue({ paused: true }),
  };
}

const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
const go = async (label: string) => {
  fireEvent.click(within(rail()).getByRole("button", { name: new RegExp(`^${label}`) }));
  return screen.findByRole("heading", { level: 1, name: label });
};

beforeEach(() => { setLang("zh-CN"); vi.spyOn(window, "scrollTo").mockImplementation(() => {}); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("overview compute devices", () => {
  it("counts the controller and one online worker", async () => {
    mockApi();
    vi.spyOn(api, "workflowDevices").mockResolvedValue({ ...REMOTE_DEVICES, workers: [REMOTE_DEVICES.workers[0]] });
    wrap(<MediaWorkspace />);
    const card = within((await screen.findByText("计算设备")).closest(".stat") as HTMLElement);
    expect(await card.findByText("2", { selector: "strong" })).toBeInTheDocument();
    expect(card.getByText("/ 2 在线")).toBeInTheDocument();
    expect(card.getByText("本机 + 1 台工作机")).toBeInTheDocument();
  });

  it("keeps the local count and GPU label when remote workers are off", async () => {
    mockApi();
    vi.spyOn(api, "workflowDevices").mockResolvedValue({ ...REMOTE_DEVICES, remote_workers: false, workers: [REMOTE_DEVICES.workers[0]] });
    const { qc } = wrap(<MediaWorkspace />);
    const card = within((await screen.findByText("计算设备")).closest(".stat") as HTMLElement);
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(card.getByText("1", { selector: "strong" })).toBeInTheDocument();
    expect(card.getByText("/ 1 在线")).toBeInTheDocument();
    expect(card.getByText(/RTX 5070 Ti/)).toBeInTheDocument();
    expect(card.queryByText(/台工作机/)).toBeNull();
  });
});

describe("#302 finished history", () => {
  it.each([true, false])("shows retained facts, recorded steps and detail buttons (energy=%s)", async (energy) => {
    mockApi();
    const film = row({ film_id: "history", number: "ABC-123", title: "留存标题", actors: ["留存演员"], group: "done",
      resolution: "4K", plan: { ltx_model: "high", machine: "worker" }, steps: steps("done", "done") });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [film], packages: [], counts: counts([film], []) });
    const keys = ["intake", "restore", "return", "asr", "translate", "scrape", "confirm", "archive", "cleanup"];
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail(film.film_id, { ...film,
      has_poster: true, poster_source: "retained-poster", marks: { restored: true, chinese_subtitles: true, uhd: true },
      files: [{ role: "main_video", path: "ABC-123-无码-C.mp4", size: 1000, volume: null }],
      duration_ms: 7293000, archive: { status: "succeeded", target_path: "fake-NAS/Actor/ABC-123", error: null, manual: energy },
      history: { completed_at: "2026-10-24T07:42:00Z", total_seconds: 7200,
        steps: keys.map((key) => ({ key, device: key === "restore" ? "工作机" : "本机", seconds: key === "confirm" ? null : 60,
          result: key === "archive" ? "已校验入库" : key === "cleanup" ? "来源与工作副本已清理" : "已完成" })) },
      timing: { stages: { restore: 60 }, total_restore_to_translate: 90, queue_wait: null,
        restore_breakdown: { scan: 1, basicvsr: 1, ltx: 50, speed_test: 0, ltx_stages: { encode: 50 }, ltx_runs: 1, wall: 60 } },
      energy: energy ? { total_wh: 9999, estimated: true, partial: false, running: false, stages: {
        restore: { wh: 1234, gpu_wh: 1000, cpu_wh: 200, other_wh: 34, seconds: 60, avg_w: null, peak_w: null,
          machine: "worker", attempts: 1, partial: false, running: false },
      } } : null,
    }));
    const open = vi.spyOn(api, "workflowOpenFolder").mockResolvedValue(undefined);
    wrap(<MediaWorkspace />);
    await go("影片队列");
    fireEvent.click(await screen.findByRole("button", { name: "查看 留存标题 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    expect(await within(drawer).findByRole("heading", { name: "ABC-123" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("img", { name: "手动入库" }) !== null).toBe(energy);
    expect(drawer).toHaveTextContent(/4K · 画质 高 · 2:01:33 · 已完成/);
    expect(await within(drawer).findByAltText("留存标题 封面")).toBeInTheDocument();
    for (const text of ["无码", "字幕", "演员", "留存演员", "归档到", "fake-NAS/Actor/ABC-123", "✓ 全部完成", "总用时 2:00:00"]) {
      expect(drawer).toHaveTextContent(text);
    }
    if (energy) expect(drawer).toHaveTextContent("修复耗电 1.234 kWh");
    else expect(drawer).not.toHaveTextContent("修复耗电");
    const table = within(drawer).getByRole("table", { name: "处理历史" });
    expect(within(table).getAllByRole("columnheader").map((e) => e.textContent)).toEqual(["步骤", "设备", "用时", "结果"]);
    expect(within(table).getAllByRole("row")).toHaveLength(10);
    for (const name of ["下载片源", "修复", "回传", "识别", "翻译", "刮削", "确认入库", "归档", "清理"]) {
      expect(within(table).getByText(name)).toBeInTheDocument();
    }
    expect(table).toHaveTextContent("工作机");
    fireEvent.click(within(drawer).getByRole("button", { name: "本片参数" }));
    expect(within(drawer).getByTestId("restore-params")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "LTX 用时明细" }));
    expect(within(drawer).getByTestId("ltx-timing")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "打开归档目录" }));
    await waitFor(() => expect(open).toHaveBeenCalledWith("history", "archive"));
  });

  it("shows the full overview count and pages completed films", async () => {
    mockApi();
    const films = Array.from({ length: 21 }, (_, i) => row({ film_id: `done-${i}`, title: `历史影片${i}`, group: "done", steps: steps("done", "done") }));
    const read = vi.spyOn(api, "workflowFilms").mockImplementation(async (page = 1) => ({ ...FILMS_RESPONSE,
      films: films.slice((page - 1) * 20, page * 20), packages: [], counts: counts(films, []),
      done_paging: { total: 21, size: 20, page, pages: 2 },
    }));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "完成 21" }));
    expect(await screen.findByText("已完成 · 21")).toBeInTheDocument();
    expect(screen.getByText("第 1 页 / 共 2 页")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-film]")).toHaveLength(20);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("历史影片20")).toBeInTheDocument();
    expect(read).toHaveBeenCalledWith(2);
    expect(screen.getByText("第 2 页 / 共 2 页")).toBeInTheDocument();
    expect(screen.queryByText("历史影片0")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(await screen.findByText("历史影片0")).toBeInTheDocument();
  });

  it("keeps a reused 破解 mark and explains an empty LTX breakdown", async () => {
    mockApi();
    const film = row({ film_id: "reused", title: "沿用成品", group: "done", steps: steps("done", "done") });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [film], packages: [], counts: counts([film], []) });
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail(film.film_id, { ...film,
      marks: { restored: true, chinese_subtitles: false, uhd: false },
      files: [{ role: "main_video", path: "ABC-123-破解.mp4", size: 1000, volume: null }],
      timing: { stages: {}, total_restore_to_translate: null, queue_wait: null,
        restore_breakdown: { scan: 5, basicvsr: 0, ltx: 0, speed_test: 0, ltx_stages: {}, ltx_runs: 0, wall: 5 } },
    }));
    wrap(<MediaWorkspace />);
    await go("影片队列");
    fireEvent.click(await screen.findByRole("button", { name: "查看 沿用成品 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    expect(await within(drawer).findByText("破解")).toBeInTheDocument();
    expect(within(drawer).queryByText("无码")).not.toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "LTX 用时明细" }));
    expect(within(drawer).getByText("未记录 LTX 用时")).toBeInTheDocument();
  });

  it("keeps the completed page after a failed next-page request and allows retry", async () => {
    mockApi();
    let fail = true;
    const read = vi.spyOn(api, "workflowFilms").mockImplementation(async (page = 1) => {
      if (page === 2 && fail) throw new Error("temporary failure");
      const films = [row({ film_id: `history-${page}`, title: `第${page}页影片`, group: "done", steps: steps("done", "done") })];
      return { ...FILMS_RESPONSE, films, packages: [], counts: { ...counts(films, []), done: 21 },
        done_paging: { total: 21, size: 20, page, pages: 2 } };
    });
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "完成 21" }));
    await screen.findByText("第1页影片");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(read).toHaveBeenCalledWith(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled());
    expect(screen.getByText("第1页影片")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第2页影片")).toBeInTheDocument();
  });
});

describe("MediaWorkspace shell", () => {
  it("lists the ten pages in order with badges on 待处理 / 待确认 / 异常 and 「后续」 on the later pages", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await waitFor(() => expect(within(rail()).getByRole("button", { name: /^待确认/ })).toHaveTextContent("待确认4"));
    const items = within(rail()).getAllByRole("button");
    expect(items.map((b) => b.dataset.nav)).toEqual(
      ["overview", "pending", "queue", "review", "exceptions", "devices", "local", "libraries", "bridge", "settings"]);
    expect(items[1]).toHaveTextContent("待处理3");
    expect(items[4]).toHaveTextContent("异常6");
    for (const i of [7, 8, 9]) expect(items[i]).toHaveTextContent("后续");
    expect(items[5]).not.toHaveTextContent("后续");  // 设备 is live since P2-2
    for (const i of [0, 2, 6]) expect(items[i].querySelector(".nav-count")).toBeNull();
    expect(items[0]).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("TaskPaw / 媒体处理中心 / 概览")).toBeInTheDocument();
  });

  it("renders every page with its compact title and no slogan heading or demo marker", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    for (const label of ["概览", "待处理", "影片队列", "待确认", "异常", "设备", "本机", "片库与规则", "Mac 信息桥", "设置 / 迁移"]) {
      await go(label);
      const main = screen.getByRole("main");
      expect(within(main).getAllByRole("heading", { level: 1 })).toHaveLength(1);
      for (const s of SLOGANS) expect(main.textContent).not.toContain(s);
      expect(document.body.textContent).not.toMatch(FORBIDDEN);
      expect(main).toHaveFocus();
    }
    expect(screen.getAllByText("此功能将在多机工作流阶段提供").length).toBeGreaterThan(0);
  });

  it("shows the reason without the pages when the workflow is not running", async () => {
    mockApi();
    vi.spyOn(api, "workflowFilms").mockResolvedValue({
      state: "off", reason: "未启用", current: null, intake: null, films: [], packages: [], counts: counts([], []),
      dispatch: null, gpu: null,
    });
    wrap(<MediaWorkspace />);
    expect(await screen.findByText("媒体流水线未运行：未启用")).toBeInTheDocument();
  });

  it("at 390px hides the rail and navigates through the 「工作台导航」 dialog", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width:900px") || query.includes("max-width: 900px"), media: query, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }));
    mockApi();
    const { qc, rerender } = wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    expect(screen.queryByRole("navigation", { name: "媒体处理中心导航" })).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "工作区导航" });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "工作台导航" });
    expect(within(dialog).getByRole("button", { name: "关闭主导航" })).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("heading", { name: "工作台导航" })).toHaveFocus());
    // IR01: the title is focused once on open, not again on every rerender (the poll, a keystroke)
    const item = within(dialog).getByRole("button", { name: /^影片队列/ });
    item.focus();
    rerender(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><MediaWorkspace /></QueryClientProvider></ThemeProvider>);
    expect(item).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(trigger).toHaveFocus());
    fireEvent.click(trigger);
    const again = await screen.findByRole("dialog", { name: "工作台导航" });
    fireEvent.click(within(again).getByRole("button", { name: /^待确认/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "待确认" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作台导航" })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });
});

describe("概览", () => {
  it("counts 待处理 + 待确认 in the stat and puts an amber 待处理 after 收件 in the flow strip", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await waitFor(() => expect(screen.getByText("3 部待安排 · 4 部待入库")).toBeInTheDocument());
    const flow = screen.getByRole("group", { name: "各流程阶段影片数量" });
    const buttons = within(flow).getAllByRole("button");
    expect(buttons.map((b) => b.querySelector("span")?.textContent)).toEqual(
      ["收件", "待处理", "修复 / 字幕", "回传 / 刮削", "待确认", "入库 / 清理", "完成"]);
    expect(buttons[1]).toHaveClass("attention");
    expect(buttons[4]).toHaveClass("attention");
    expect(screen.getByText("1 部 LTX · 其余步骤分别计数")).toBeInTheDocument();
    expect(screen.getByText("RTX 5070 Ti 16 GB")).toBeInTheDocument();
    // flow → queue filtered to that stage
    fireEvent.click(buttons[2]);
    await screen.findByRole("heading", { level: 1, name: "影片队列" });
    expect(screen.getByRole("combobox", { name: "按影片阶段筛选" })).toHaveValue("processing");
  });

  it("pauses new claims with a toast and shows the paused notice", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "暂停新领取" }));
    await waitFor(() => expect(m.pause).toHaveBeenCalledWith(true));
    expect(await screen.findByText("已暂停新的领取；所有在途任务继续。")).toBeInTheDocument();
  });

  it("shows the LTX stage block with this stage's percent and counts only", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    const bar = await screen.findByRole("progressbar", { name: "浮光之间 Encode / 编码 本阶段进度" });
    expect(bar).toHaveAttribute("aria-valuenow", "52");
    expect(screen.getByText("156 / 300 帧")).toBeInTheDocument();
    expect(screen.getByText("中文翻译 · 112 / 240 条字幕")).toBeInTheDocument();
  });

  it("shows five active films before folding the rest into 另有 N 部正在处理 (#347)", async () => {
    mockApi();
    const films = [...FILMS,
      row({ film_id: "abf-501", title: "远山来信", stage: "return", status: "running", steps: steps("return") }),
      row({ film_id: "abf-502", title: "潮汐之间", stage: "scrape", status: "running", steps: steps("scrape") })];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    wrap(<MediaWorkspace />);
    const region = await screen.findByRole("region", { name: "正在处理" });
    expect(await within(region).findByText("另有 1 部正在处理")).toBeInTheDocument();
    expect(region.querySelectorAll("article.film-row")).toHaveLength(5);
  });

  it("expands the folded active films from 另有 N 部正在处理 and collapses them again (#353)", async () => {
    mockApi();
    const films = [...FILMS,
      row({ film_id: "abf-501", title: "远山来信", stage: "return", status: "running", steps: steps("return") }),
      row({ film_id: "abf-502", title: "潮汐之间", stage: "scrape", status: "running", steps: steps("scrape") })];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    wrap(<MediaWorkspace />);
    const region = await screen.findByRole("region", { name: "正在处理" });
    const toggle = await within(region).findByRole("button", { name: "另有 1 部正在处理" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent("收起");
    expect(region.querySelectorAll("article.film-row")).toHaveLength(6);
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(region.querySelectorAll("article.film-row")).toHaveLength(5);
  });
});

describe("待处理", () => {
  const card = (id: string) => document.querySelector<HTMLElement>(`[data-pending="${id}"]`) as HTMLElement;

  it("tags the recommended tier, explains it and lets the operator change it", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-421")).toBeTruthy());
    const c = within(card("abf-421"));
    const group = c.getByRole("radiogroup", { name: "画质" });
    const high = within(group).getByRole("radio", { name: /高/ });
    expect(high).toHaveAttribute("aria-checked", "true");
    expect(within(high).getByText("推荐")).toBeInTheDocument();
    expect(c.getByText("演员星河 上次选择 高画质")).toBeInTheDocument();
    expect(c.getByText("本机修复输出上限 1080p")).toBeInTheDocument();
    expect(c.getByText("2:08:14 · 41.6 GB · 3 个文件")).toBeInTheDocument();
    // keyboard: ArrowRight wraps 高 → 标准, Home/End
    high.focus();
    fireEvent.keyDown(high, { key: "ArrowRight" });
    expect(within(group).getByRole("radio", { name: "标准" })).toHaveAttribute("aria-checked", "true");
    expect(within(group).getByRole("radio", { name: "标准" })).toHaveFocus();
    fireEvent.keyDown(within(group).getByRole("radio", { name: "标准" }), { key: "End" });
    expect(high).toHaveAttribute("aria-checked", "true");
    fireEvent.click(within(group).getByRole("radio", { name: "良好" }));
    expect(within(group).getByRole("radio", { name: "良好" })).toHaveAttribute("aria-checked", "true");
    expect(within(group).getAllByRole("radio").filter((r) => r.tabIndex === 0)).toHaveLength(1);
    // a film that needs no restore has no tier control
    expect(within(card("abf-423")).queryByRole("radiogroup")).toBeNull();
    expect(within(card("abf-423")).getByText("无需修复")).toBeInTheDocument();
  });

  it("confirms one film with the chosen tier; it leaves the list", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    fireEvent.click(within(card("abf-422")).getByRole("radio", { name: "良好" }));
    fireEvent.click(within(card("abf-422")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith(
      [{ film_id: "abf-422", input_revision: 1, ltx_model: "good", machine: "w-local" }]));
    await waitFor(() => expect(card("abf-422")).toBeNull());
    expect(await screen.findByText("晨雾车站 已确认入队 · 良好画质 · 本机")).toBeInTheDocument();
  });

  it("batch-confirms the unchanged films only and skips changed and stale ones", async () => {
    const m = mockApi();
    m.confirmPlans.mockImplementationOnce(async (plans) => ({
      results: plans.map((p) => (p.film_id === "abf-423"
        ? { film_id: p.film_id, ok: false, error: "cas_conflict", message: "收件内容已更新，请重新查看后确认" }
        : { film_id: p.film_id, ok: true, error: null, message: null })),
    }));
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-421")).toBeTruthy());
    // abf-423 goes stale: its single confirm hits a changed revision
    fireEvent.click(within(card("abf-423")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(within(card("abf-423")).getByText("收件内容已更新，请重新查看后确认")).toBeInTheDocument());
    expect(within(card("abf-423")).getByRole("button", { name: "确认入队" })).toBeDisabled();
    // abf-421 is changed by the operator
    fireEvent.click(within(card("abf-421")).getByRole("radio", { name: "标准" }));
    m.confirmPlans.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "全部按推荐确认" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith(
      [{ film_id: "abf-422", input_revision: 1, ltx_model: "standard", machine: "w-local" }]));
    expect(await screen.findByText("已确认 1 部，进入影片队列。2 部已调整或需重新查看，请单独确认。")).toBeInTheDocument();
    // opening the detail acknowledges the new revision
    fireEvent.click(within(card("abf-423")).getByRole("button", { name: "查看详情" }));
    await screen.findByRole("dialog");
    expect(within(card("abf-423")).queryByText("收件内容已更新，请重新查看后确认")).toBeNull();
  });

  it("a changed input revision stays flagged until the detail is opened; a failed lookup can be re-queried", async () => {
    mockApi();
    const lookup = vi.spyOn(api, "workflowLookup").mockResolvedValue({ ok: true });
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ ...PLANS, plans: PLANS.plans.map((p) => (p.film_id === "abf-422"
      ? { ...p, input_revision: 2, revision_changed: true,
        lookup: lookupFailed("未查到资料", true) }
      : p.film_id === "abf-423" ? { ...p, lookup: lookupFailed("文件名中没有番号", false) } : p)) });
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    const c = within(card("abf-422"));
    expect(c.getByText("收件内容已更新，请重新查看后确认")).toBeInTheDocument();
    expect(c.getByRole("button", { name: "确认入队" })).toBeDisabled();
    fireEvent.click(c.getByRole("button", { name: "重新查询" }));
    await waitFor(() => expect(lookup).toHaveBeenCalledWith("abf-422"));
    expect(within(card("abf-423")).getByText("文件名中没有番号")).toBeInTheDocument();
    expect(within(card("abf-423")).getByRole("button", { name: "重新查询" })).toBeDisabled();
    fireEvent.click(c.getByRole("button", { name: "查看详情" }));
    fireEvent.keyDown(await screen.findByRole("dialog", { name: "影片详情" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "影片详情" })).not.toBeInTheDocument());
    expect(c.queryByText("收件内容已更新，请重新查看后确认")).toBeNull();
    expect(c.getByRole("button", { name: "确认入队" })).toBeEnabled();
  });

  it("remote mode: a null recommended machine is 自动（推荐）, the first option, and confirms as null (Codex r2 CX5)", async () => {
    const m = mockApi();
    const auto = { ltx_model: "standard" as const, ltx_reason: "默认", machine: null, machine_reason: "自动（推荐）" };
    vi.spyOn(api, "workflowPlans").mockResolvedValue({
      ...PLANS, remote: true,
      plans: PLANS.plans.map((p) => ({ ...p, recommendation: { ...auto, ltx_model: p.needs_gpu ? "standard" : null } })),
      choices: { ...PLANS.choices, machines: [
        { id: "w-a", name: "工作机甲", gpu: "RTX 5070 Ti", vram_mib: 16303, reason: "RTX 5070 Ti 16 GB" },
        { id: "w-b", name: "工作机乙", gpu: null, vram_mib: null, reason: "" }] },
    });
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    const c = within(card("abf-422"));
    const select = c.getByLabelText("设备") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["自动（推荐）", "工作机甲 · RTX 5070 Ti 16 GB", "工作机乙"]);
    expect(select.value).toBe("");
    expect(c.getByText("自动（推荐）", { selector: "p" })).toBeInTheDocument();
    expect(c.getByRole("button", { name: "确认入队" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "全部按推荐确认" })).toBeEnabled();
    fireEvent.click(c.getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith(
      [{ film_id: "abf-422", input_revision: 1, ltx_model: "standard", machine: null }]));
    // picking a worker pins it; the batch then skips the changed film and sends the rest as 自动
    fireEvent.change(within(card("abf-421")).getByLabelText("设备"), { target: { value: "w-b" } });
    m.confirmPlans.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "全部按推荐确认" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith([
      { film_id: "abf-423", input_revision: 1, ltx_model: null, machine: null }]));
    m.confirmPlans.mockClear();
    fireEvent.click(within(card("abf-421")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith(
      [{ film_id: "abf-421", input_revision: 1, ltx_model: "standard", machine: "w-b" }]));
  });

  it("shows the empty state", async () => {
    mockApi();
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ plans: [], default_tier: "standard", choices: { tiers: [], machines: [] } });
    const films = FILMS_RESPONSE.films.filter((f) => f.group !== "planning");
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    wrap(<MediaWorkspace />);
    await go("待处理");
    expect(await screen.findByText("没有待处理影片")).toBeInTheDocument();
    expect(screen.getByText("新投入 INBOX 的影片会先出现在这里。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全部按推荐确认" })).toBeDisabled();
  });
});

describe("影片队列 / 待确认 / 异常", () => {
  it.each([false, true])("pages 25 unfinished films into history without gaps (remote=%s)", async (remote) => {
    mockApi();
    const unfinished = Array.from({ length: 25 }, (_, i) => row({ film_id: `pending-${i}`, stage: "asr", status: i >= 22 ? "running" : "pending" }));
    const done = Array.from({ length: 40 }, (_, i) => row({ film_id: `done-${i}`, group: "done",
      completed_at: new Date(Date.UTC(2026, 9, 25, 0, 0, 40 - i)).toISOString() }));
    const read = vi.spyOn(api, "workflowFilms").mockImplementation(async (page = 1) => ({ ...FILMS_RESPONSE,
      films: [...unfinished, ...done.slice((page - 1) * 20, page * 20).reverse()], packages: [],
      counts: counts([...unfinished, ...done], []), done_paging: { total: 40, size: 20, page, pages: 2 },
    }));
    if (remote) vi.spyOn(api, "workflowDevices").mockResolvedValue(REMOTE_DEVICES);
    const { qc } = wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { name: "概览" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    const order = () => [...document.querySelectorAll("[data-film]")].map((e) => e.getAttribute("data-film"));
    const sorted = [...unfinished.slice(22), ...unfinished.slice(0, 22), ...done].map((f) => f.film_id);
    await waitFor(() => expect(order()).toEqual(sorted.slice(0, 20)));
    expect(screen.getByText("第 1 页 / 共 4 页")).toBeInTheDocument();
    expect(read.mock.calls).toEqual([[]]); // No history needed on the first queue page.
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(order()).toEqual(sorted.slice(20, 40))); // 5 unfinished + 15 done
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    read.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(order()).toEqual(sorted.slice(40, 60))); // spans done pages 1 and 2
    expect(read.mock.calls).toEqual([[1], [2]]);
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    read.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.queryByText("没有符合条件的影片")).not.toBeInTheDocument();
    await waitFor(() => expect(order()).toEqual(sorted.slice(60)));
    expect(read.mock.calls).toEqual([[2]]);
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    // A poll that removes rows clamps the queue page and hides the single-page pager.
    act(() => qc.setQueryData(["mediaFilms"], { ...FILMS_RESPONSE, films: unfinished.slice(0, 1), packages: [],
      counts: counts(unfinished.slice(0, 1), []), done_paging: { total: 0, size: 20, page: 1, pages: 1 } }));
    await waitFor(() => expect(order()).toEqual([unfinished[0].film_id]));
    expect(screen.queryByLabelText("影片队列分页")).not.toBeInTheDocument();
  });

  it("pages stage filters locally and resets search/resolution pages without completed matches", async () => {
    mockApi();
    const unfinished = Array.from({ length: 25 }, (_, i) => row({ film_id: `match-${i}`, stage: "asr", resolution: i === 24 ? "4K" : "1080p" }));
    const done = row({ film_id: "match-done", group: "done" });
    const films = [...unfinished, done];
    const read = vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, packages: [],
      counts: counts(films, []), done_paging: { total: 1, size: 20, page: 1, pages: 1 } });
    wrap(<MediaWorkspace />);
    await go("影片队列");
    fireEvent.change(screen.getByLabelText("按影片阶段筛选"), { target: { value: "processing" } });
    expect(document.querySelectorAll("[data-film]")).toHaveLength(20);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(document.querySelectorAll("[data-film]")).toHaveLength(5);
    expect(screen.getByText("第 2 页 / 共 2 页")).toBeInTheDocument();
    expect(read.mock.calls).toEqual([[]]);
    fireEvent.change(screen.getByLabelText("按影片阶段筛选"), { target: { value: "all" } });
    expect(screen.getByText("第 1 页 / 共 2 页")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(document.querySelectorAll("[data-film]")).toHaveLength(6));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "match" } });
    expect(screen.getByText("第 1 页 / 共 2 页")).toBeInTheDocument();
    expect(screen.getByText("25 部")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(document.querySelectorAll("[data-film]")).toHaveLength(5);
    expect(document.querySelector('[data-film="match-done"]')).toBeNull();
    fireEvent.change(screen.getByLabelText("按分辨率筛选"), { target: { value: "1080p" } });
    expect(screen.getByText("第 1 页 / 共 2 页")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(document.querySelectorAll("[data-film]")).toHaveLength(4);
    expect(document.querySelector('[data-film="match-done"]')).toBeNull();
    fireEvent.change(screen.getByLabelText("按分辨率筛选"), { target: { value: "4K" } });
    expect(document.querySelectorAll("[data-film]")).toHaveLength(1);
    expect(screen.queryByLabelText("影片队列分页")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("按影片阶段筛选"), { target: { value: "completed" } });
    await waitFor(() => expect(document.querySelector('[data-film="match-done"]')).toBeInTheDocument());
    expect(screen.queryByLabelText("影片队列分页")).not.toBeInTheDocument();
  });

  it.each([false, true])("orders active, waiting and newest completed films with completion times (remote=%s)", async (remote) => {
    mockApi();
    const films = [
      row({ film_id: "done-null", group: "done", completed_at: null }),
      row({ film_id: "queued" }),
      row({ film_id: "done-old", group: "done", completed_at: new Date(2026, 9, 24, 8, 5).toISOString() }),
      row({ film_id: "translating", stage: "translate", status: "running" }),
      row({ film_id: "waiting-asr", stage: "asr" }),
      row({ film_id: "done-new", group: "done", completed_at: new Date(2026, 9, 25, 9, 7).toISOString() }),
      row({ film_id: "restoring", status: "running" }),
      row({ film_id: "failed", group: "attention", stage: "asr", status: "failed" }),
      row({ film_id: "review", group: "confirm", stage: "archive" }),
    ];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, packages: [PACKAGES[0]], counts: counts(films, [PACKAGES[0]]) });
    if (remote) vi.spyOn(api, "workflowDevices").mockResolvedValue(REMOTE_DEVICES);
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    await screen.findByRole("heading", { level: 1, name: remote ? "队列 · 派单状态" : "影片队列" });
    const order = () => [...document.querySelectorAll("[data-film]")].map((e) => e.getAttribute("data-film"));
    const done = ["done-new", "done-old", "done-null"];
    await waitFor(() => expect(order()).toEqual(["translating", "restoring", "queued", "waiting-asr", "failed", "review", "pkg:p1", ...done]));
    const newest = document.querySelector('[data-film="done-new"]')!;
    expect(newest.querySelector(remote ? "td:last-child" : ".card-foot")).toHaveTextContent("完成于 10-25 09:07");
    fireEvent.change(screen.getByLabelText("按影片阶段筛选"), { target: { value: "processing" } });
    expect(order()).toEqual(["translating", "restoring", "waiting-asr"]);
    fireEvent.change(screen.getByLabelText("按影片阶段筛选"), { target: { value: "completed" } });
    await waitFor(() => expect(order()).toEqual(done));
    expect(document.querySelector('[data-film="done-new"] .card-foot')).toHaveTextContent("完成于 10-25 09:07");
  });

  it("queue cards carry status chips and the step line with 待处理; searching filters", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("影片队列");
    const line = await screen.findByRole("list", { name: "浮光之间 生命周期" });
    const names = within(line).getAllByRole("listitem").map((li) => li.textContent);
    expect(names.slice(0, 3)).toEqual(["入库扫描（已完成）", "待处理（已完成）", "修复"]);
    expect(within(line).getByText("修复")).toHaveAttribute("aria-current", "step");
    expect(screen.getAllByText("LTX · 编码").length).toBeGreaterThan(0);
    expect(screen.getByText("等待收件检查")).toBeInTheDocument();
    expect(screen.queryByText("星河之外")).toBeNull(); // unplanned films are not in the queue
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索影片、演员或编号" }), { target: { value: "演员月色" } });
    expect(screen.getByText("1 部")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索影片、演员或编号" }), { target: { value: "zzz" } });
    expect(screen.getByText("没有符合条件的影片")).toBeInTheDocument();
  });

  it("review cards follow the preflight: ready, several candidates, none, NAS offline", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待确认");
    const reviewCard = (id: string) => document.querySelector<HTMLElement>(`[data-review="${id}"]`) as HTMLElement;
    await waitFor(() => expect(within(reviewCard("abf-407")).getByText("多个目标候选")).toBeInTheDocument());
    expect(within(reviewCard("abf-406")).getByRole("button", { name: "图文核对入库" })).toBeInTheDocument();
    expect(await within(reviewCard("abf-406")).findByText("最终成品包 4 个文件 · Miko 已完成")).toBeInTheDocument();
    expect(within(reviewCard("abf-407")).getByText("2 个演员 / 目录候选，必须明确选择")).toBeInTheDocument();
    expect(within(reviewCard("abf-408")).getByText("暂无演员匹配")).toBeInTheDocument();
    expect(within(reviewCard("abf-409")).getByText("目标 NAS 离线，禁止提交")).toBeInTheDocument();
    expect(screen.getByText("4 部影片 · 每次单独确认")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "需要先处理" }));
    expect(screen.getByText("3 部影片 · 每次单独确认")).toBeInTheDocument();
  });

  it("exception cards name the problem and the one action", async () => {
    mockApi();
    const retry = vi.spyOn(api, "workflowRetry").mockResolvedValue({ ok: true });
    wrap(<MediaWorkspace />);
    await go("异常");
    expect(await screen.findByText("入库结果待核对")).toBeInTheDocument();
    expect(screen.getByText("最终封面尚未准备好")).toBeInTheDocument();
    expect(screen.getByText("翻译失败")).toBeInTheDocument();
    expect(screen.getByText("收件需要处理", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByText("有无法识别的文件：新建文本文档.txt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试后处理" }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith("abf-412", "scrape"));
    fireEvent.click(screen.getByRole("button", { name: "核对结果" }));
    expect(await screen.findByRole("dialog", { name: "先核对入库结果" })).toBeInTheDocument();
    expect(screen.getByText("盲目重传、覆盖同名包、先删来源")).toBeInTheDocument();
  });
});

describe("unresolved stops and unretryable failures (Codex r3 F3)", () => {
  it("a local stage still unknown waits for the stop; 重试 only where the backend allows it", async () => {
    mockApi();
    const rows = [
      row({ film_id: "stp-1", title: "停止未确认", group: "attention", stage: "restore", status: "unknown",
        note: "等待确认停止", steps: steps("restore", "waiting", { note: "等待确认停止" }) }),
      row({ film_id: "bad-1", title: "不可重试的失败", group: "attention", stage: "bogus", status: "failed",
        can_retry: false, retry_stage: null, note: "未知阶段", steps: steps("restore", "failed") }),
      row({ film_id: "bad-2", title: "可重试的失败", group: "attention", stage: "translate", status: "failed",
        can_retry: true, retry_stage: "translate", note: "LLM 超时", steps: steps("translate", "failed") }),
    ];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: rows, packages: [], counts: counts(rows, []) });
    wrap(<MediaWorkspace />);
    await go("异常");
    const card = async (id: string) => {
      await waitFor(() => expect(document.querySelector(`[data-exception="${id}"]`)).not.toBeNull());
      return document.querySelector(`[data-exception="${id}"]`) as HTMLElement;
    };
    const stopped = await card("stp-1");
    expect(within(stopped).getAllByText("正在确认停止").length).toBeGreaterThan(0);
    expect(stopped.textContent).not.toContain("本机任务已停止");
    expect(within(stopped).queryByRole("button", { name: "重试" })).toBeNull();
    expect(within(await card("bad-1")).queryByRole("button", { name: "重试" })).toBeNull();
    expect(within(await card("bad-2")).getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});

describe("待处理 rejection messages (Codex r4 F3)", () => {
  const toast = () => document.querySelector('[role="status"]')?.textContent;
  const card = (id: string) => document.querySelector<HTMLElement>(`[data-pending="${id}"]`) as HTMLElement;
  const confirmOnly = async (error: string, message: string) => {
    const m = mockApi();
    m.confirmPlans.mockImplementation(async (plans) => ({
      results: plans.map((p) => ({ film_id: p.film_id, ok: false, error, message })),
    }));
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    fireEvent.click(within(card("abf-422")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalled());
    return m;
  };
  it.each([
    ["plan_exists", "该影片已有处理计划"],
    ["invalid_machine", "设备不可用"],
    ["precondition_failed", "当前状态不允许此操作"],
  ])("a single confirm refused with %s keeps the server's message", async (error, message) => {
    await confirmOnly(error, message);
    await waitFor(() => expect(toast()).toBe(message));
    await new Promise((r) => setTimeout(r, 50));
    expect(toast()).toBe(message);
  });
  it("only cas_conflict reads 「收件内容已更新」", async () => {
    await confirmOnly("cas_conflict", "服务端文字");
    await waitFor(() => expect(toast()).toBe("收件内容已更新，请重新查看后确认"));
  });
  it("a whole-request 409 follows its code, not its status", async () => {
    const m = mockApi();
    m.confirmPlans.mockRejectedValueOnce(new HttpError("当前状态不允许此操作", 409, "precondition_failed"));
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    fireEvent.click(within(card("abf-422")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(toast()).toBe("当前状态不允许此操作"));
    expect(within(card("abf-422")).queryByText("收件内容已更新，请重新查看后确认")).toBeNull();
    m.confirmPlans.mockRejectedValueOnce(new HttpError("影片状态已变化，请刷新后重试", 409, "cas_conflict"));
    fireEvent.click(within(card("abf-422")).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(toast()).toBe("收件内容已更新，请重新查看后确认"));
  });
  it("the batch toast names the other refusals' reason", async () => {
    const m = mockApi();
    m.confirmPlans.mockImplementation(async (plans) => ({
      results: plans.map((p) => (p.film_id === "abf-422"
        ? { film_id: p.film_id, ok: false, error: "invalid_machine", message: "设备不可用" }
        : { film_id: p.film_id, ok: true, error: null, message: null })),
    }));
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("abf-421")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "全部按推荐确认" }));
    await waitFor(() => expect(toast()).toContain("设备不可用"));
    expect(toast()).not.toContain("收件内容已更新");
  });
});

describe("local card honesty (Codex r4 F1)", () => {
  it("概览 and 本机 state the workflow's output limit and never advertise 4K", async () => {
    mockApi();  // a 16 GB card whose workflow limit is still 1080p
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    expect(await screen.findByText("修复输出上限 1080p")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("4K 优先");
    await go("本机");
    expect(screen.getByText("修复输出上限 1080p")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("4K 优先");
  });
  it("shows 4K only when the backend says the workflow restores it", async () => {
    mockApi();
    vi.spyOn(api, "workflowFilms").mockResolvedValue({
      ...FILMS_RESPONSE, gpu: { name: "RTX 5070 Ti", vram_mib: 16303, max_output: "4K" } });
    wrap(<MediaWorkspace />);
    expect(await screen.findByText("修复输出上限 4K")).toBeInTheDocument();
  });
});

describe("drawer and 图文确认", () => {
  it("the drawer shows the step line, the full LtxProgress and the plan facts", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await waitFor(() => expect(within(drawer).getByTestId("ltx-progress")).toBeInTheDocument());
    expect(within(drawer).getByText("由阶段日志提供；不包含翻译、回传、刮削与入库。")).toBeInTheDocument();
    expect(within(drawer).getByRole("list", { name: "浮光之间 生命周期" })).toBeInTheDocument();
    expect(within(drawer).getByText("标准画质")).toBeInTheDocument();
    expect(within(drawer).getByText("本机 · RTX 5070 Ti 16 GB")).toBeInTheDocument();
    expect(within(drawer).getByText("尚未入库")).toBeInTheDocument();
    await waitFor(() => expect(within(drawer).getByRole("heading", { name: "影片详情" })).toHaveFocus());
    fireEvent.click(within(drawer).getByRole("button", { name: "关闭对话框" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "影片详情" })).not.toBeInTheDocument());
  });

  it("a pending film's drawer says 待安排 and its step line stops at 待处理", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(document.querySelector('[data-pending="abf-421"]')).toBeTruthy());
    fireEvent.click(within(document.querySelector('[data-pending="abf-421"]') as HTMLElement).getByRole("button", { name: "查看详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await waitFor(() => expect(within(drawer).getAllByText("待安排")).toHaveLength(2));
    expect(within(drawer).getByText("待处理", { selector: "[aria-current=step]" })).toBeInTheDocument();
  });

  it("the drawer offers the model download through the workflow route", async () => {
    mockApi();
    const respond = vi.spyOn(api, "workflowLtxDownload").mockResolvedValue({ ok: true });
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, {
      steps: steps("restore", "failed", { note: "缺少 LTX 模型文件：vae.safetensors.enc",
        download: { state: "awaiting", request_id: "r1", files: ["vae.safetensors.enc"], total_bytes: 1e9, downloaded_bytes: 0 } }),
    }));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "下载并继续" }));
    await waitFor(() => expect(respond).toHaveBeenCalledWith("r1", true));
  });

  const openReview = async (title: string) => {
    await go("待确认");
    const cardEl = await waitFor(() => {
      const el = [...document.querySelectorAll<HTMLElement>("[data-review]")].find((c) => c.textContent?.includes(title));
      if (!el) throw new Error("no card");
      return el;
    });
    fireEvent.click(within(cardEl).getByRole("button", { name: /图文核对入库|查看并处理/ }));
    return screen.findByRole("dialog", { name: "确认影片与入库目标" });
  };

  it("one candidate is pre-selected and one click confirms", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    await waitFor(() => expect(within(dialog).getByText("演员星河", { selector: "strong" })).toBeInTheDocument());
    expect(within(dialog).queryByText("选择演员与目标")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "手动入库" })).toBeNull();
    await waitFor(() => expect(within(dialog).getByText("\\\\nas-a\\actors\\演员星河\\ABF-406")).toBeInTheDocument());
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith("abf-406", 1));
    expect(await screen.findByRole("dialog", { name: "已确认，等待归档" })).toBeInTheDocument();
  });

  it("预览播放 sits immediately before 打开文件夹 and posts workflowPlay", async () => {
    mockApi();
    const play = vi.spyOn(api, "workflowPlay");
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const preview = await within(dialog).findByRole("button", { name: "预览播放" });
    expect(preview).toHaveClass("btn", "ghost");
    expect(preview.nextElementSibling).toBe(within(dialog).getByRole("button", { name: "打开文件夹" }));
    fireEvent.click(preview);
    await waitFor(() => expect(play).toHaveBeenCalledWith("abf-406"));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/control\/workflow\/films\/abf-406\/play$/), expect.objectContaining({ method: "POST" }),
    ));
  });

  it("预览播放 reports VLC errors through the workspace toast", async () => {
    mockApi();
    vi.spyOn(api, "workflowPlay").mockRejectedValue(new HttpError("未找到 VLC 播放器", 404));
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    fireEvent.click(await within(dialog).findByRole("button", { name: "预览播放" }));
    expect(await screen.findByText("未找到 VLC 播放器")).toBeInTheDocument();
  });

  it("手动入库 picks a directory, preflights it and posts target_dir", async () => {
    const readTarget = api.workflowTarget;
    const postConfirm = api.workflowConfirm;
    const m = mockApi();
    const original = vi.mocked(api.workflowTarget).getMockImplementation()!;
    vi.mocked(api.workflowTarget).mockImplementation((id, performer, targetDir) =>
      targetDir ? readTarget(id, performer, targetDir) : original(id, performer));
    m.confirm.mockImplementation(postConfirm);
    const directory = "D:/自选 目录";
    vi.spyOn(nativePicker, "inTauri").mockReturnValue(true);
    const picker = vi.spyOn(nativePicker, "pickPath").mockResolvedValue(directory);
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => Response.json(init?.method === "POST"
      ? { approval_id: "manual-approval" }
      : preflight("ABF-406", PREFLIGHTS["abf-406"].candidates!.items, {
        target_dir: directory, manual: true, target_path: `${directory}/ABF-406`, absent: true,
      })));
    vi.stubGlobal("fetch", fetch);
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    fireEvent.click(await within(dialog).findByRole("button", { name: "手动入库" }));
    expect(picker).toHaveBeenCalledWith(true, "选择入库目录");
    const icon = await within(dialog).findByRole("img", { name: "手动入库" });
    expect(icon.parentElement).toHaveTextContent(`${directory}/ABF-406`);
    const targetUrl = new URL(fetch.mock.calls[0][0]);
    expect(targetUrl.pathname).toMatch(/\/abf-406\/target$/);
    expect([...targetUrl.searchParams]).toEqual([["target_dir", directory]]);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认入库" }));
    await screen.findByRole("dialog", { name: "已确认，等待归档" });
    const post = fetch.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(post[0]).toMatch(/\/abf-406\/confirm$/);
    expect(JSON.parse(String(post[1]?.body))).toEqual({ target_dir: directory });
  });

  it("cancelling changes nothing, and choosing the sole performer leaves manual mode", async () => {
    const m = mockApi();
    vi.spyOn(nativePicker, "inTauri").mockReturnValue(true);
    const picker = vi.spyOn(nativePicker, "pickPath").mockResolvedValueOnce(null).mockResolvedValue("D:/manual");
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    const calls = vi.mocked(api.workflowTarget).mock.calls.length;
    const manual = within(dialog).getByRole("button", { name: "手动入库" });
    await act(async () => { fireEvent.click(manual); });
    expect(picker).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.workflowTarget).mock.calls).toHaveLength(calls);
    expect(ok).toBeEnabled();
    expect(within(dialog).queryByRole("img", { name: "手动入库" })).toBeNull();
    fireEvent.click(manual);
    await within(dialog).findByRole("img", { name: "手动入库" });
    fireEvent.click(within(dialog).getByRole("button", { name: /演员星河/ }));
    await waitFor(() => expect(ok).toBeEnabled());
    expect(within(dialog).queryByRole("img", { name: "手动入库" })).toBeNull();
    fireEvent.click(ok);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith("abf-406", 1));
  });

  it("a duplicate manual target blocks confirmation with the manual message", async () => {
    mockApi();
    vi.spyOn(nativePicker, "inTauri").mockReturnValue(true);
    vi.spyOn(nativePicker, "pickPath").mockResolvedValue("D:/manual");
    const original = vi.mocked(api.workflowTarget).getMockImplementation()!;
    vi.mocked(api.workflowTarget).mockImplementation(async (id, performer, targetDir) => ({
      ...await original(id, performer, targetDir), ...(targetDir ? { absent: false, error_code: "duplicate" as const } : {}),
    }));
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    fireEvent.click(await within(dialog).findByRole("button", { name: "手动入库" }));
    expect(await within(dialog).findByText("目标目录下已有同名目录")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
  });

  it("the detail target row marks a manual archive", async () => {
    mockApi();
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, {
      archive: { status: "pending", error: null, target_path: "D:/manual/ABF-406", manual: true },
    }));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    const icon = await within(drawer).findByRole("img", { name: "手动入库" });
    expect(icon.parentElement).toHaveTextContent("D:/manual/ABF-406");
  });

  it("several candidates are never pre-selected", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    const dialog = await openReview("夜航日志");
    const group = await within(dialog).findByRole("group", { name: "演员与目标候选" });
    const buttons = within(group).getAllByRole("button");
    expect(buttons).toHaveLength(2);
    buttons.forEach((b) => expect(b).toHaveAttribute("aria-pressed", "false"));
    expect(within(dialog).getByText("请选择演员与目标路径。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
    fireEvent.click(buttons[1]);
    expect(buttons[1]).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
  });

  it("no candidate asks for a manual search and never creates an actor folder", async () => {
    mockApi();
    const performers = vi.spyOn(api, "performers").mockResolvedValue({ items: [] });
    wrap(<MediaWorkspace />);
    const dialog = await openReview("未命名的岛");
    const box = await within(dialog).findByRole("searchbox", { name: "搜索已有演员" });
    fireEvent.change(box, { target: { value: "演员新岛" } });
    await waitFor(() => expect(performers).toHaveBeenCalledWith("演员新岛"));
    expect(await within(dialog).findByText("没有匹配的演员。不会自动新建演员目录。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
  });

  it("an offline target blocks the confirm with its reason", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    const dialog = await openReview("晴窗片段");
    expect(await within(dialog).findByText("目标 NAS 暂时离线，请稍后重试。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "确认影片与入库目标" })).not.toBeInTheDocument());
  });
});

describe("图文确认 review fixes", () => {
  const openReview = async (title: string) => {
    await go("待确认");
    const cardEl = await waitFor(() => {
      const el = [...document.querySelectorAll<HTMLElement>("[data-review]")].find((c) => c.textContent?.includes(title));
      if (!el) throw new Error("no card");
      return el;
    });
    fireEvent.click(within(cardEl).getByRole("button", { name: /图文核对入库|查看并处理/ }));
    return screen.findByRole("dialog", { name: "确认影片与入库目标" });
  };
  const STAR_TARGET = "\\\\nas-a\\actors\\演员星河\\ABF-406";
  const withPerformer = (pf: TargetPreflight, over: Partial<TargetPreflight>): TargetPreflight => ({
    ...pf, performer_id: "1", target_path: STAR_TARGET, absent: true, ...over,
  });

  it.each([["running", true], ["pending", false], ["running", false]] as const)("the receipt shows archive progress only while the step is %s with progress=%s", async (state, live) => {
    const m = mockApi();
    let after = false;
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => (after && id === "abf-406"
      ? detail(id, {
        can_confirm: false, archive: { status: state, error: null, target_path: STAR_TARGET, manual: false },
        steps: steps("archive", state, { progress: live ? {
          phase: "copying", copied_bytes: 2e9, verified_bytes: 0, total_bytes: 4e9,
          files_done: 0, files_total: 1, items: [{ name: "ABF-406", bytes: 2e9, total: 4e9 }],
          target: STAR_TARGET, started_at: "2026-10-24T08:00:00Z",
        } : null }),
      })
      : detail(id)));
    m.confirm.mockImplementation(async () => { after = true; return { approval_id: "a1" }; });
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    const receipt = within(await screen.findByRole("dialog", { name: "已确认，等待归档" }));
    const notice = "后台正在归档并校验；完成后显示入库回执。";
    if (live) {
      const panel = within(await receipt.findByRole("region", { name: "归档进度" }));
      expect(panel.getByText("25%")).toBeInTheDocument();
      expect(panel.getByRole("progressbar", { name: "归档进度" })).toHaveAttribute("aria-valuenow", "25");
      expect(panel.getByText("已复制").closest("li")).toHaveTextContent("2.0 / 4.0 GB");
      expect(receipt.queryByText(notice)).not.toBeInTheDocument();
    } else {
      expect(await receipt.findByText(notice)).toBeInTheDocument();
      expect(receipt.queryByRole("region", { name: "归档进度" })).not.toBeInTheDocument();
    }
  });

  it.each([
    ["failed", "上次入库未完成：NAS 写入中断。可重新确认入库。", true],
    ["unknown", "入库结果待核对，暂不能重新上传。", false],
  ] as const)("a submitted film whose archive ends %s shows the review controls again", async (status, text, enabled) => {
    const m = mockApi();
    let after = false;
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => (after && id === "abf-406"
      ? detail(id, {
        archive: { status, error: status === "failed" ? "NAS 写入中断" : null, target_path: null, manual: false },
        can_confirm: status === "failed",
      })
      : detail(id)));
    m.confirm.mockImplementation(async () => { after = true; return { approval_id: "a1" }; });
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledTimes(1));
    const back = await screen.findByRole("dialog", { name: "确认影片与入库目标" });
    expect(await within(back).findByText(text)).toBeInTheDocument();
    const again = within(back).getByRole("button", { name: "确认入库" });
    if (enabled) await waitFor(() => expect(again).toBeEnabled());
    else expect(again).toBeDisabled();
  });

  it("确认入库 waits for a passed target check of the chosen performer", async () => {
    mockApi();
    let answer: (pf: TargetPreflight) => Promise<TargetPreflight> = () => new Promise(() => {});
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, performer) =>
      (performer === undefined || id !== "abf-406" ? PREFLIGHTS[id] : answer(PREFLIGHTS["abf-406"])));
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    await waitFor(() => expect(within(dialog).getByText("演员星河", { selector: "strong" })).toBeInTheDocument());
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    expect(await within(dialog).findByText("正在检查目标…")).toBeInTheDocument();
    expect(ok).toBeDisabled();
    const recheck = () => act(() => qc.resetQueries({
      predicate: (q) => q.queryKey[0] === "mediaTarget" && q.queryKey[1] === "abf-406" && q.queryKey[3] === 1,
    }));
    // an HTTP error is no pass
    answer = async () => { throw new HttpError("服务暂不可用", 503); };
    await recheck();
    expect(await within(dialog).findByText("目标检查失败：服务暂不可用")).toBeInTheDocument();
    expect(ok).toBeDisabled();
    // an answer without a target path, or a folder not known to be absent, is no pass either
    for (const over of [{ target_path: null }, { absent: null }] as Array<Partial<TargetPreflight>>) {
      answer = async (pf) => withPerformer(pf, over);
      await recheck();
      expect(await within(dialog).findByText("目标检查未通过，请重新选择或稍后重试。")).toBeInTheDocument();
      expect(ok).toBeDisabled();
    }
    answer = async (pf) => withPerformer(pf, {});
    await recheck();
    await waitFor(() => expect(ok).toBeEnabled());
  });

  it("a new final revision needs its own passed target check; 重新查看 keeps no stale one", async () => {
    mockApi();
    let final = 2;
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) =>
      detail(id, id === "abf-406" ? { revisions: revisions(1, final) } : {}));
    let release: () => void = () => {};
    const calls: Array<[string, unknown]> = [];
    const inner = vi.mocked(api.workflowTarget).getMockImplementation() as typeof api.workflowTarget;
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, performer) => {
      calls.push([id, performer]);
      if (id === "abf-406" && final === 3) await new Promise<void>((resolve) => { release = resolve; });
      return inner(id, performer);
    });
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    final = 3;
    await act(() => qc.invalidateQueries({ queryKey: ["mediaFilm", "abf-406"] }));
    expect(await within(dialog).findByText("成品或来源已更新，请重新查看后确认。")).toBeInTheDocument();
    expect(ok).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "重新查看" }));
    // acknowledged, but revision 2's passed check does not stand in for revision 3's
    expect(await within(dialog).findByText("正在检查目标…")).toBeInTheDocument();
    expect(ok).toBeDisabled();
    await waitFor(() => expect(calls.filter(([id]) => id === "abf-406").length).toBeGreaterThan(1));
    await act(async () => { release(); });
    await waitFor(() => expect(ok).toBeEnabled());
    expect(qc.getQueryData(["mediaTarget", "abf-406", `3:${revisions(1, 3).final?.hash}`])).toMatchObject({
      performer_id: "1", absent: true,
    });
  });

  it("an automatic sole-candidate selection follows the final revision (Codex r3 F1)", async () => {
    mockApi();
    let final = 2;
    const sole = (id: number, name: string) => preflight("ABF-406", [performer(id, name, "NAS-A")]);
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) =>
      detail(id, id === "abf-406" ? { revisions: revisions(1, final) } : {}));
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, chosen) => {
      if (id !== "abf-406") return PREFLIGHTS[id] ?? preflight(id.toUpperCase(), []);
      const pf = final === 2 ? sole(1, "演员星河") : sole(7, "演员海岛");
      const items = pf.candidates?.items ?? [];
      const picked = chosen ?? items[0].id;
      return { ...pf, performer_id: String(picked), absent: true, target_path: `${items[0].unc_path}\\ABF-406` };
    });
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    await waitFor(() => expect(within(dialog).getByText("演员星河", { selector: "strong" })).toBeInTheDocument());
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    final = 3;
    await act(() => qc.invalidateQueries({ queryKey: ["mediaFilm", "abf-406"] }));
    expect(await within(dialog).findByText("成品或来源已更新，请重新查看后确认。")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "重新查看" }));
    await waitFor(() => expect(within(dialog).getByText("演员海岛", { selector: "strong" })).toBeInTheDocument());
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    const confirm = vi.mocked(api.workflowConfirm);
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(String(confirm.mock.calls[0][1])).toBe("7");
  });

  it("a manual selection survives a new revision only while it is still a candidate (F1)", async () => {
    mockApi();
    let final = 2;
    const pairs: Record<number, [number, number]> = { 2: [2, 3], 3: [3, 4], 4: [4, 5] };
    const two = () => preflight("ABF-407", pairs[final].map((n) => performer(n, `演员${n}`, "NAS-A")));
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) =>
      detail(id, id === "abf-407" ? { revisions: revisions(1, final) } : {}));
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, chosen) => {
      if (id !== "abf-407") return PREFLIGHTS[id] ?? preflight(id.toUpperCase(), []);
      const pf = two();
      const hit = pf.candidates?.items.find((p) => p.id === chosen);
      return chosen === undefined ? pf
        : { ...pf, performer_id: String(chosen), absent: true, target_path: hit ? `${hit.unc_path}\\ABF-407` : null,
          error_code: hit ? null : "missing" };
    });
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("夜航日志");
    fireEvent.click(await within(dialog).findByRole("button", { name: /演员3/ }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
    const bump = async (to: number) => {
      final = to;
      await act(() => qc.invalidateQueries({ queryKey: ["mediaFilm", "abf-407"] }));
      fireEvent.click(await within(dialog).findByRole("button", { name: "重新查看" }));
    };
    await bump(3);  // 演员3 is still a candidate of revision 3: kept
    await waitFor(() => expect(within(dialog).getByRole("button", { name: /演员3/ })).toHaveAttribute("aria-pressed", "true"));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
    await bump(4);  // revision 4 no longer lists 演员3: cleared, the picker asks again
    await waitFor(() => expect(within(dialog).getByRole("button", { name: /演员4/ })).toBeInTheDocument());
    expect(within(dialog).getByRole("button", { name: /演员4/ })).toHaveAttribute("aria-pressed", "false");
    expect(within(dialog).getByText("请选择演员与目标路径。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
  });

  const blockedTarget = (state: { up: boolean }, code: "offline" | "permission") =>
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, chosen) => {
      if (id !== "abf-407") return PREFLIGHTS[id] ?? preflight(id.toUpperCase(), []);
      const pf = PREFLIGHTS["abf-407"];
      if (chosen === undefined) return pf;
      const hit = pf.candidates?.items.find((p) => p.id === chosen);
      return state.up ? { ...pf, performer_id: String(chosen), absent: true, target_path: `${hit?.unc_path}\\ABF-407` }
        : { ...pf, performer_id: String(chosen), error_code: code, message: "x" };
    });

  it("a blocked check of the chosen performer recovers through 重新检查 (F2)", async () => {
    mockApi();
    const state = { up: false };
    blockedTarget(state, "offline");
    wrap(<MediaWorkspace />);
    const dialog = await openReview("夜航日志");
    fireEvent.click(await within(dialog).findByRole("button", { name: /演员夜航乙/ }));
    expect(await within(dialog).findByText("目标 NAS 暂时离线，请稍后重试。")).toBeInTheDocument();
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    expect(ok).toBeDisabled();
    state.up = true;  // the NAS comes back
    fireEvent.click(within(dialog).getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(ok).toBeEnabled());
    expect(within(dialog).queryByRole("button", { name: "重新检查" })).toBeNull();
  });

  it("clicking the same candidate again refetches a blocked check (F2)", async () => {
    mockApi();
    const state = { up: false };
    const target = blockedTarget(state, "permission");
    wrap(<MediaWorkspace />);
    const dialog = await openReview("夜航日志");
    const pick = await within(dialog).findByRole("button", { name: /演员夜航乙/ });
    fireEvent.click(pick);
    expect(await within(dialog).findByText("目标暂不可写，请检查权限后重试。")).toBeInTheDocument();
    const own = () => target.mock.calls.filter(([id, p]) => id === "abf-407" && p !== undefined).length;
    const before = own();
    state.up = true;
    fireEvent.click(pick);
    await waitFor(() => expect(own()).toBeGreaterThan(before));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
  });

  it("a blocked check is polled at the shared preflight interval while the dialog is open (F2)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockApi();
      const state = { up: false };
      blockedTarget(state, "offline");
      wrap(<MediaWorkspace />);
      const dialog = await openReview("夜航日志");
      fireEvent.click(await within(dialog).findByRole("button", { name: /演员夜航乙/ }));
      expect(await within(dialog).findByText("目标 NAS 暂时离线，请稍后重试。")).toBeInTheDocument();
      state.up = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(PREFLIGHT_MS + 1000); });
      await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["a target refusal keeps the backend's message", new HttpError("目标 NAS 暂时离线，请稍后重试", 409), "目标 NAS 暂时离线，请稍后重试"],
    ["a duplicate keeps the backend's message", new HttpError("演员目录下已有同名目录：ABF-406", 409), "演员目录下已有同名目录：ABF-406"],
    ["a real revision conflict says the revision changed", new HttpError("影片状态已变化，请刷新后重试", 409, "cas_conflict"),
      "成品或来源已更新，请重新查看后确认。"],
  ])("confirm 409: %s (Codex r4 F2)", async (_name, error, text) => {
    const m = mockApi();
    m.confirm.mockRejectedValue(error);
    wrap(<MediaWorkspace />);
    const dialog = await openReview("星河慢行");
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    expect(await within(dialog).findByText(text)).toBeInTheDocument();
    if (text !== "成品或来源已更新，请重新查看后确认。") {
      expect(within(dialog).queryByText("成品或来源已更新，请重新查看后确认。")).toBeNull();
    }
  });

  it("one candidate: the list and the dialog read the same target check (F4)", async () => {
    mockApi();
    const duplicate = withPerformer(PREFLIGHTS["abf-406"], { absent: false, error_code: "duplicate", message: "同名" });
    const target = vi.spyOn(api, "workflowTarget").mockImplementation(async (id) =>
      (id === "abf-406" ? duplicate : PREFLIGHTS[id] ?? preflight(id.toUpperCase(), [])));
    wrap(<MediaWorkspace />);
    await go("待确认");  // the list asks (IR05: only where it is shown) …
    await waitFor(() => expect(target).toHaveBeenCalledWith("abf-406"));
    await go("异常");  // … and its answer files the film under 异常
    await waitFor(() => expect(document.querySelector("main")?.textContent).toContain("星河慢行"));
    const dialog = await openReview("星河慢行");
    expect(await within(dialog).findByText("目标已有同名影片包，不能覆盖；请打开文件夹核对。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
    // the dialog asked no second, diverging question for that performer
    expect(target.mock.calls.filter(([id, p]) => id === "abf-406" && p !== undefined)).toEqual([]);
  });

  it.each(["zero", "failed"] as const)("a lone candidate arriving after a %s first load is selected", async (first) => {
    mockApi();
    let one = false;
    const lone = preflight("ABF-408", [
      { id: "9", name: "演员孤岛", source_label: "NAS-A", unc_path: "\\\\nas-a\\actors\\演员孤岛", has_thumb: false, aliases: [] },
    ]);
    vi.spyOn(api, "workflowTarget").mockImplementation(async (id, performer) => {
      if (id !== "abf-408") return PREFLIGHTS[id];
      if (performer !== undefined) {
        return { ...lone, performer_id: String(performer), target_path: "\\\\nas-a\\actors\\演员孤岛\\ABF-408", absent: true };
      }
      if (one) return lone;
      if (first === "failed") throw new HttpError("暂时无法读取", 503);
      return PREFLIGHTS["abf-408"];
    });
    vi.spyOn(api, "performers").mockResolvedValue({ items: [] });
    const { qc } = wrapWith(<MediaWorkspace />);
    const dialog = await openReview("未命名的岛");
    await within(dialog).findByRole("searchbox", { name: "搜索已有演员" });
    one = true;
    await act(() => qc.invalidateQueries({ queryKey: ["mediaTarget", "abf-408", null], exact: true }));
    await waitFor(() => expect(within(dialog).getByText("演员孤岛", { selector: "strong" })).toBeInTheDocument());
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeEnabled());
  });
});

describe("待处理 batch limit", () => {
  it("全部按推荐确认 sends at most PLAN_BATCH_MAX films per request and reports once", async () => {
    const m = mockApi();
    const many = Array.from({ length: 501 }, (_, i) => plan({ film_id: `f-${String(i).padStart(3, "0")}`, title: `影片 ${i}` }));
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ ...PLANS, plans: many });
    m.confirmPlans.mockImplementation(async (plans: PlanChoice[]) => ({
      results: plans.map((p) => (p.film_id === "f-500"
        ? { film_id: p.film_id, ok: false, error: "invalid_machine", message: "设备不可用" }
        : { film_id: p.film_id, ok: true, error: null, message: null })),
    }));
    wrap(<MediaWorkspace />);
    // 501 cards: role and text queries over the whole DOM are far too slow, so plain selectors
    fireEvent.click(within(rail()).getByRole("button", { name: /^待处理/ }));
    await waitFor(() => expect(document.querySelectorAll("[data-pending]")).toHaveLength(501), { timeout: 20000 });
    const all = [...document.querySelectorAll<HTMLButtonElement>("main button")].find((b) => b.textContent === "全部按推荐确认");
    fireEvent.click(all as HTMLButtonElement);
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledTimes(2), { timeout: 20000 });
    expect(PLAN_BATCH_MAX).toBe(500);
    expect(m.confirmPlans.mock.calls.map(([body]) => body.length)).toEqual([500, 1]);
    // one summary toast, not one per film
    const toast = () => document.querySelector('[role="status"]')?.textContent;
    await waitFor(() => expect(toast()).toBe("已确认 500 部，进入影片队列。1 部已调整或需重新查看，请单独确认。（设备不可用）"),
      { timeout: 20000 });
  }, 60000);
});

describe("target preflight polling (IR05)", () => {
  const confirmRow = (i: number) => row({
    film_id: `c-${String(i).padStart(2, "0")}`, title: `确认片 ${i}`, group: "confirm",
    stage: "archive", status: "pending", can_confirm: true, steps: steps("confirm", "waiting"),
  });

  it("polls only on 概览 / 待确认 / 图文确认, for the top ten films; the rest read 检查中", async () => {
    mockApi();
    const films = [...FILMS.filter((f) => f.group !== "confirm"), ...Array.from({ length: 12 }, (_, i) => confirmRow(i))];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    const target = vi.spyOn(api, "workflowTarget").mockImplementation(async (id) => preflight(id.toUpperCase(), [
      { id: `p-${id}`, name: `演员${id}`, source_label: "NAS-A", unc_path: `\\\\nas-a\\${id}`, has_thumb: false, aliases: [] },
    ]));
    wrap(<MediaWorkspace />);
    // on 影片队列 nothing asks for the targets
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    await screen.findByRole("heading", { level: 1, name: "影片队列" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(document.body.textContent).toContain("确认片 11"));
    expect(target).not.toHaveBeenCalled();
    await go("待确认");
    await waitFor(() => expect(target).toHaveBeenCalledTimes(10));
    expect(new Set(target.mock.calls.map(([id]) => id))).toEqual(
      new Set(Array.from({ length: 10 }, (_, i) => `c-${String(i).padStart(2, "0")}`)));
    const card = (id: string) => document.querySelector<HTMLElement>(`[data-review="${id}"]`) as HTMLElement;
    await waitFor(() => expect(within(card("c-00")).getByRole("button", { name: "图文核对入库" })).toBeInTheDocument());
    expect(within(card("c-11")).getAllByText("检查中").length).toBeGreaterThan(0);
    expect(within(card("c-11")).getByRole("button", { name: "查看并处理" })).toBeInTheDocument();
  });
});

describe("mediaModel", () => {
  it("maps backend rows onto the V3 vocabulary", () => {
    expect(filmStatus(row({ film_id: "a", group: "planning" }))).toBe("pending");
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "pending" }))).toBe("inbox");
    // U1c: a running restore without an LTX stage yet (loading, mosaic scan, BasicVSR++)
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "running" }))).toBe("restore");
    expect(STATUS_META.restore[0]).toBe("修复中");
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "running",
      steps: steps("restore", "running", { ltx: { ltx_progress: { mode: "restore", stage_index: 3, stage_total: 4, stage_name: "denoise" } } }) }))).toBe("denoise");
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "scrape", status: "failed" }))).toBe("postprocess_error");
    // a local stage still `unknown` has not confirmed its stop (Codex r3 F3)
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "restore", status: "unknown" }))).toBe("stopping");
    expect(STATUS_META.stopping[0]).toBe("正在确认停止");
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "cleanup_input", status: "unknown" }))).toBe("cleanup");
    expect(filmStatus(row({ film_id: "a", group: "archiving", stage: "archive", status: "unknown" }))).toBe("unknown");
    expect(filmStatus(row({ film_id: "a", group: "done" }))).toBe("completed");
    expect(reviewStatus(preflight("A", [], { error_code: "duplicate", absent: false }))).toBe("duplicate");
    // the chosen performer record is gone: choose again, as with no match
    expect(reviewStatus(preflight("A", [], { error_code: "missing" }))).toBe("nomatch");
    expect(reviewStatus(preflight("A", null, { error_code: "offline" }))).toBe("offline");
    // no preflight yet (not polled, or still loading): 检查中, never ready (IR05)
    expect(reviewStatus(null)).toBe("checking");
    expect(reviewStatus(undefined)).toBe("checking");
    expect(STATUS_META.checking[0]).toBe("检查中");
    expect(filmStatus(row({ film_id: "a", group: "confirm" }))).toBe("checking");
  });

  it("shows a queued asr / translate / return / scrape stage as waiting, not processing (Codex r5 F2)", () => {
    const miko = "Miko 未配置，刮削已暂停";
    for (const stage of ["asr", "translate", "return", "scrape"]) {
      expect(filmStatus(row({ film_id: "a", stage, status: "running" }))).toBe(stage);
      expect(filmStatus(row({ film_id: "a", stage, status: "pending" }))).toBe("waiting");
    }
    const films = [
      row({ film_id: "run", stage: "asr", status: "running" }),
      row({ film_id: "idle", stage: "translate", status: "pending", note: "暂停新领取" }),
      row({ film_id: "off", stage: "scrape", status: "pending", note: miko }),
    ];
    const items = workItems({ ...FILMS_RESPONSE, films, packages: [] }, {});
    expect(items.filter((i) => ACTIVE_STATES.includes(i.status)).map((i) => i.id)).toEqual(["run"]);
    const off = items.find((i) => i.id === "off")!;
    expect(off.status).toBe("waiting");
    expect(stageLine(off)).toBe(miko);
    expect(stageLine(items[1])).toBe("暂停新领取");
    expect(items.filter((i) => matchesStage(i, "processing")).map((i) => i.id)).toEqual(["run", "idle"]);
    expect(items.filter((i) => matchesStage(i, "preparing")).map((i) => i.id)).toEqual(["off"]);
  });

  it("keeps the eleven steps in order and reads a step not sent as not started", () => {
    const line = lifecycle(steps("restore").filter((s) => s.key !== "asr"));
    expect(line.map((s) => s.key)).toEqual(
      ["intake", "plan", "restore", "asr", "translate", "return", "scrape", "confirm", "archive", "cleanup", "done"]);
    expect(line[3].state).toBe("pending");
  });
});

describe("LtxDownload onRespond", () => {
  it("answers through onRespond instead of the monitor route", async () => {
    const respondPrompt = vi.spyOn(api, "respondPrompt");
    const onRespond = vi.fn().mockResolvedValue(undefined);
    wrap(<LtxDownload value={{ state: "awaiting", request_id: "q1", total_bytes: 0, downloaded_bytes: 0 }} onRespond={onRespond} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "暂不下载" })); });
    expect(onRespond).toHaveBeenCalledWith("q1", false);
    expect(respondPrompt).not.toHaveBeenCalled();
  });
});

describe("AgentConsole media tab", () => {
  it("mounts the workspace under 「媒体处理中心」", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ machine: "box1", monitors: {} }) })));
    mockApi();
    wrap(<AgentConsole />);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理中心" }));
    expect(await screen.findByTestId("media-workspace")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "概览" })).toBeInTheDocument();
  });

  it("names the tab 「Media Center」 in English", () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ machine: "box1", monitors: {} }) })));
    setLang("en");
    wrap(<AgentConsole />);
    expect(screen.getByRole("tab", { name: "Media Center" })).toBeInTheDocument();
  });
});

describe("U1b covers", () => {
  it("every cover opens the lightbox: 待处理, queue, review, overview, drawer and 图文确认", async () => {
    mockApi();
    const films = FILMS.map((f) => ({ ...f, has_poster: true, poster_source: `input-${f.film_id}` }));
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ ...PLANS, plans: PLANS.plans.map((p) => ({ ...p, has_poster: true, poster_source: "lookup-1" })) });
    vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id, {
      has_poster: true, poster_source: "final-1", has_cover: true, cover_source: "final-2" }));
    wrap(<MediaWorkspace />);
    const lightbox = async (scope: HTMLElement, name: string) => {
      fireEvent.click(await within(scope).findByRole("button", { name: /^查看大图/ }));
      const box = await screen.findByRole("dialog", { name: `${name} 大图` });
      fireEvent.click(within(box).getByRole("button", { name: "关闭大图" }));
      await waitFor(() => expect(screen.queryByRole("dialog", { name: `${name} 大图` })).toBeNull());
    };
    // 概览: the active rows and the review peeks
    await screen.findByRole("heading", { level: 1, name: "概览" });
    await waitFor(() => expect(document.querySelector(".film-row .cover-btn")).toBeTruthy());
    await waitFor(() => expect(document.querySelector(".review-peek .cover-btn")).toBeTruthy());
    const peek = document.querySelector(".review-peek") as HTMLElement;
    await lightbox(peek, within(peek).getByRole("heading", { level: 3 }).textContent as string);
    await go("待处理");
    await waitFor(() => expect(document.querySelector('[data-pending="abf-422"] .cover-btn')).toBeTruthy());
    await lightbox(document.querySelector('[data-pending="abf-422"]') as HTMLElement, "晨雾车站");
    await go("影片队列");
    await waitFor(() => expect(document.querySelector('[data-film="abf-401"] .cover-btn')).toBeTruthy());
    await lightbox(document.querySelector('[data-film="abf-401"]') as HTMLElement, "浮光之间");
    await go("待确认");
    await waitFor(() => expect(document.querySelector('[data-review="abf-406"] .cover-btn')).toBeTruthy());
    const review = document.querySelector('[data-review="abf-406"]') as HTMLElement;
    const title = within(review).getByRole("heading", { level: 3 }).textContent as string;
    await lightbox(review, title);
    // the drawer hero and the 图文确认 dialog ask for the full jacket
    fireEvent.click(within(review).getByRole("button", { name: /图文核对入库|查看并处理/ }));
    const dialog = await screen.findByRole("dialog", { name: "确认影片与入库目标" });
    fireEvent.click(await within(dialog).findByRole("button", { name: /^查看大图/ }));
    await screen.findByRole("dialog", { name: `${title} 大图` });
    await waitFor(() => expect(api.image).toHaveBeenCalledWith(api.coverPath("abf-406")));
    fireEvent.keyDown(screen.getByRole("dialog", { name: `${title} 大图` }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: `${title} 大图` })).toBeNull());
    // Esc closed only the lightbox
    expect(screen.getByRole("dialog", { name: "确认影片与入库目标" })).toBeInTheDocument();
  });
});

describe("deferred Codex r6 P2s", () => {
  const confirmRow = (i: number) => row({
    film_id: `c-${String(i).padStart(2, "0")}`, title: `确认片 ${i}`, group: "confirm",
    stage: "archive", status: "pending", can_confirm: true, steps: steps("confirm", "waiting"),
  });

  it("the 异常 page refreshes the target checks too, for the top ten films", async () => {
    mockApi();
    const films = [...FILMS.filter((f) => f.group !== "confirm"), ...Array.from({ length: 12 }, (_, i) => confirmRow(i))];
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, counts: counts(films, PACKAGES) });
    const target = vi.spyOn(api, "workflowTarget").mockImplementation(async (id) =>
      preflight(id.toUpperCase(), null, { error_code: "offline", message: "片库不可用" }));
    wrap(<MediaWorkspace />);
    fireEvent.click(within(rail()).getByRole("button", { name: /^异常/ }));
    await screen.findByRole("heading", { level: 1, name: "异常" });
    await waitFor(() => expect(target).toHaveBeenCalledTimes(10));
    expect(new Set(target.mock.calls.map(([id]) => id))).toEqual(
      new Set(Array.from({ length: 10 }, (_, i) => `c-${String(i).padStart(2, "0")}`)));
  });

  it("a confirmed film hidden on 待处理 shows again when a newer unplanned revision arrives", async () => {
    const m = mockApi();
    const plans = vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
    m.confirmPlans.mockImplementation(async (sent: PlanChoice[]) => {
      // the refreshed list still has abf-422, now at a newer input revision nobody planned
      plans.mockResolvedValue({ ...PLANS, plans: PLANS.plans.map((p) => (p.film_id === "abf-422"
        ? { ...p, input_revision: 2, revision_changed: true } : p)) });
      return { results: sent.map((p) => ({ film_id: p.film_id, ok: true, error: null, message: null })) };
    });
    wrap(<MediaWorkspace />);
    await go("待处理");
    const card = (id: string) => document.querySelector<HTMLElement>(`[data-pending="${id}"]`);
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    fireEvent.click(within(card("abf-422") as HTMLElement).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalled());
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    expect(within(card("abf-422") as HTMLElement).getByText("收件内容已更新，请重新查看后确认")).toBeInTheDocument();
  });

  it("the same revision stays hidden while the list still has it", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    const card = (id: string) => document.querySelector<HTMLElement>(`[data-pending="${id}"]`);
    await waitFor(() => expect(card("abf-422")).toBeTruthy());
    fireEvent.click(within(card("abf-422") as HTMLElement).getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalled());
    await waitFor(() => expect(card("abf-422")).toBeNull());
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(card("abf-422")).toBeNull();
  });
});


describe("ASR live progress (#300)", () => {
  afterEach(() => vi.useRealTimers());
  const labels = ["提取音频", "划分场景", "语音增强", "语音分段", "识别语音", "生成场景字幕", "合并字幕", "过滤与清理"];
  const consoleLine = "[DecoupledPipeline] Generating scene 214/346 (26.1s audio)...";
  const progress = {
    phase: 5, phase_n: 8, phase_label: "识别语音", scene: 214, scenes: 346, percent: 62,
    eta_s: 660, elapsed_s: 1122, console: consoleLine,
    phases: labels.map((label, i) => ({ n: i + 1, label, state: i < 4 ? "done" : i === 4 ? "current" : "pending",
      seconds: i < 4 ? 30 : i === 4 ? 1002 : null })),
  };
  it.each([[62, "62%"], [0, "0%"], [null, null]] as const)("reads ASR percent %s for the device summary", (percent, expected) => {
    const film = row({ film_id: "asr-live", stage: "asr", status: "running", steps: steps("asr", "running", { progress: { ...progress, percent } }) });
    expect(progressText({ kind: "film", id: film.film_id, row: film, status: "asr" })).toBe(expected);
  });
  function setup(p: Record<string, unknown> = progress) {
    mockApi();
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-24T08:18:42Z"));
    const film = row({ film_id: "asr-live", title: "识别测试", stage: "asr", status: "running",
      steps: steps("asr", "running", { progress: p, started_at: "2026-10-24T08:00:00Z", console: String(p.console) }) });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [film], packages: [], counts: counts([film], []) });
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail(film.film_id, { ...film, actors: [] }));
    return wrap(<MediaWorkspace />);
  }
  async function open() {
    fireEvent.click(await screen.findByRole("button", { name: "查看 识别测试 详情" }));
    return screen.findByRole("dialog", { name: "影片详情" });
  }

  it("renders the approved drawer heartbeat, percent, tiles, phases, console and step chip", async () => {
    setup();
    const drawer = await open();
    const panel = await within(drawer).findByTestId("asr-live");
    expect(panel).toHaveTextContent("已用 18:42");
    expect(panel).toHaveTextContent("第 5/8 阶段 · 识别语音");
    expect(within(panel).getByText("62%")).toBeInTheDocument();
    expect(within(panel).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "62");
    const tiles = within(panel).getByRole("list", { name: "识别数据" });
    expect(within(tiles).getAllByRole("listitem")).toHaveLength(4);
    expect(tiles).toHaveTextContent("场景214 / 346");
    expect(tiles).toHaveTextContent("已用18:42");
    expect(tiles).toHaveTextContent("预计剩余约 11 分钟");
    expect(tiles).toHaveTextContent("设备本机 · RTX 5070 Ti");
    const phases = within(panel).getByRole("list", { name: "识别阶段" });
    const entries = within(phases).getAllByRole("listitem");
    expect(entries).toHaveLength(8);
    labels.forEach((label, i) => expect(entries[i]).toHaveTextContent(label));
    expect(entries[0]).toHaveTextContent("✓");
    expect(entries[0]).toHaveTextContent("00:30");
    expect(entries[4]).toHaveTextContent("●");
    expect(entries[4]).toHaveTextContent("场景 214/346");
    expect(entries[4]).toHaveTextContent("16:42");
    expect(entries[5]).toHaveTextContent("○");
    fireEvent.click(within(panel).getByText("最新输出"));
    expect(within(panel).getByTestId("asr-console-raw")).toHaveTextContent(consoleLine);
    expect(within(drawer).getByText("识别 · 62%", { selector: "[aria-current=step]" })).toBeInTheDocument();
  });

  it("shows percent, scenes and ETA in the overview row", async () => {
    setup();
    expect(await screen.findByText("识别 · 62% · 场景 214/346 · 约 11 分钟")).toBeInTheDocument();
    expect(screen.queryByText("此阶段不提供影片总体百分比")).not.toBeInTheDocument();
  });

  it("an untagged pipeline shows only a ticking heartbeat and console, with no percent", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    setup({ elapsed_s: 1122, console: "Loading balanced pipeline" });
    const drawer = await open();
    const panel = await within(drawer).findByTestId("asr-live");
    expect(panel).toHaveTextContent("已用 18:42");
    expect(within(panel).queryByRole("progressbar")).not.toBeInTheDocument();
    expect(within(panel).queryByRole("list")).not.toBeInTheDocument();
    expect(panel).not.toHaveTextContent("%");
    expect(panel).not.toHaveTextContent("预计剩余");
    expect(within(panel).getByTestId("asr-console-raw")).toHaveTextContent("Loading balanced pipeline");
    expect(within(drawer).getByText("识别", { selector: "[aria-current=step]" })).toBeInTheDocument();
    expect(screen.queryByText("此阶段不提供影片总体百分比")).not.toBeInTheDocument();
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-24T08:18:44Z"));
    act(() => vi.advanceTimersByTime(2000));
    expect(panel).toHaveTextContent("已用 18:44");
  });
});


describe("archive output (#301)", () => {
  const progress = { phase: "copying", copied_bytes: 2e9, verified_bytes: 0, total_bytes: 4e9,
    files_done: 0, files_total: 1, items: [{ name: "ABC-123", bytes: 2e9, total: 4e9 }],
    target: "fake-NAS/Actor/ABC-123", started_at: "2026-10-24T08:00:00Z" };

  it("shows archive progress in the drawer, chip and overview", async () => {
    mockApi();
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-24T08:01:00Z"));
    const film = row({ film_id: "archive-live", title: "归档测试", group: "archiving", stage: "archive", status: "running",
      steps: steps("archive", "running", { progress }) });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [film], packages: [], counts: counts([film], []) });
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail(film.film_id, { ...film }));
    wrap(<MediaWorkspace />);
    expect(await screen.findByText("归档 · 25% · 复制 2.0 / 4.0 GB")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看 归档测试 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    const panel = await within(drawer).findByTestId("archive-live");
    expect(panel).toHaveTextContent("已用 01:00");
    expect(panel).toHaveTextContent("复制 → 校验");
    expect(within(panel).getByRole("progressbar", { name: "归档进度" })).toHaveAttribute("aria-valuenow", "25");
    expect(panel).toHaveTextContent("已复制2.0 / 4.0 GB");
    expect(panel).toHaveTextContent("已校验0.0 / 4.0 GB");
    expect(panel).toHaveTextContent("文件夹0 / 1");
    expect(panel).toHaveTextContent("速度—");
    expect(panel).toHaveTextContent(progress.target);
    expect(panel).toHaveTextContent("复制完会逐个校验；校验通过后才清理主控上的工作副本。");
    expect(within(drawer).getByText("归档 · 25%", { selector: "[aria-current=step]" })).toBeInTheDocument();
  });

  it.each([false, true])("opens the recorded folder, including history (archived=%s)", async (archived) => {
    mockApi();
    const open = vi.spyOn(api, "workflowOpenFolder").mockResolvedValue(undefined);
    const film = row({ film_id: "folder-film", title: "目录测试", group: archived ? "done" : "confirm",
      stage: archived ? "done" : "archive", status: archived ? "succeeded" : "pending",
      steps: steps(archived ? "done" : "confirm", archived ? "done" : "waiting") });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: [film], packages: [], counts: counts([film], []) });
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail(film.film_id, { ...film,
      archive: { status: archived ? "succeeded" : "pending", target_path: archived ? "fake-NAS/ABC-123" : null, error: null, manual: false },
      revisions: revisions(1, 1) }));
    wrap(<MediaWorkspace />);
    await go("影片队列");
    fireEvent.click(await screen.findByRole("button", { name: "查看 目录测试 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: archived ? "打开归档目录" : "打开成品目录" }));
    await waitFor(() => expect(open).toHaveBeenCalledWith("folder-film", archived ? "archive" : "output"));
  });
});


describe("#305 dispatch board", () => {
  function dispatchRows() {
    const base = { actions: actions(), steps: steps("restore", "waiting") };
    const route = REMOTE_ROWS[1].route!;
    return [
      row({ ...base, film_id: "auto", pin: { worker_id: null, name: "自动", version: 1 }, note: "第 1 位 · 哪台先空给哪台" }),
      row({ ...base, film_id: "pin", pin: { worker_id: W.ti70.id, name: W.ti70.name, version: 1 }, note: "第 1 位" }),
      row({ ...base, film_id: "down", route, note: "下载到 5070 Ti · 25%" }),
      row({ ...base, film_id: "ready", route: { ...route, position: 1 }, note: "已到 5070 Ti · 等开始" }),
      REMOTE_ROWS[1], REMOTE_ROWS[0],
    ];
  }
  function setup(films = dispatchRows()) {
    mockApi();
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films, packages: [] });
    vi.spyOn(api, "workflowDevices").mockResolvedValue(REMOTE_DEVICES);
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detail("pin", { ...films[1] }));
    wrap(<MediaWorkspace />);
  }
  it("shows remote-disabled workers as neutral metadata instead of waiting for heartbeat", async () => {
    setup();
    vi.mocked(api.workflowDevices).mockResolvedValue({ ...REMOTE_DEVICES, workers: REMOTE_DEVICES.workers.map((worker) =>
      ({ ...worker, heartbeat: { ...worker.heartbeat, state: "off" } })) });
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    const worker = within(await screen.findByRole("region", { name: W.ti70.name }));
    const badge = worker.getByText("远程未启用", { selector: ".status-badge" });
    expect(badge).toHaveAttribute("data-tone", "waiting");
    expect(badge.querySelector("svg")).toHaveAttribute("data-testid", "PowerSettingsNewOutlinedIcon");
    expect(worker.queryByText("等待心跳")).not.toBeInTheDocument();
  });
  it("renders the approved worker cards and dispatch table with exact states", async () => {
    setup();
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    await screen.findByRole("heading", { level: 1, name: "队列 · 派单状态" });
    const table = within(screen.getByRole("table", { name: "派单状态" }));
    for (const label of ["影片", "机器", "状态", "说明"]) expect(table.getByRole("columnheader", { name: label })).toBeInTheDocument();
    expect(table.getAllByText("排队中", { selector: ".status-badge" })).toHaveLength(4);
    expect(table.getByText("LTX · 编码", { selector: ".status-badge" })).toBeInTheDocument();
    for (const label of ["下载到 5070 Ti · 25%", "已到 5070 Ti · 等开始"]) {
      // Notes remain factual; a note alone cannot create an executing state or a progress bar.
      const note = table.getByText(label, { selector: "td" });
      expect(within(note.closest("tr")!).queryByRole("progressbar")).not.toBeInTheDocument();
    }
    expect(table.getAllByText("自动派单")).toHaveLength(3);
    const worker = within(screen.getByRole("region", { name: W.ti70.name }));
    expect(worker.getByText("LTX · 编码")).toBeInTheDocument();
    expect(worker.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "41");
    expect(screen.getByText("下一部：READY · 已到 5070 Ti · 等开始")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: W.ti3.name })).toHaveTextContent("设备失联");
    expect(screen.getByRole("region", { name: W.ti3.name })).not.toHaveTextContent("空闲");
    expect(screen.getAllByText("无预取").length).toBeGreaterThan(0);
  });
  it("uses the same queue states in overview and drawer", async () => {
    setup();
    expect((await screen.findAllByText("排队中", { selector: ".status-badge" })).length).toBeGreaterThan(0);
    expect(screen.getByText("LTX · 编码", { selector: ".status-badge" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看 PIN 详情" }));
    const drawer = within(await screen.findByRole("dialog"));
    expect(await drawer.findByText("排队中", { selector: ".status-badge" })).toBeInTheDocument();
  });
  it.each([["uploading", "回传中"], ["finishing", "提交结果"]] as const)("keeps worker strip and row on %s without old compute percent", async (phase, label) => {
    const film = REMOTE_ROWS[1];
    setup([{ ...film, steps: film.steps.map((step) => step.key === "restore" ? { ...step, phase } : step) }]);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^影片队列/ }));
    await screen.findByRole("heading", { level: 1, name: "队列 · 派单状态" });
    for (const region of [screen.getByRole("table", { name: "派单状态" }), screen.getByRole("region", { name: W.ti70.name })]) {
      expect(within(region).getByText(label, { selector: ".status-badge" })).toBeInTheDocument();
      expect(within(region).queryByRole("progressbar")).not.toBeInTheDocument();
      expect(region).not.toHaveTextContent(/41%|修复中|加载模型/);
    }
  });
});


it("#305 lets a busy pinned machine accept another controller-queued film", async () => {
  const { confirmPlans } = mockApi();
  const plans = structuredClone(REMOTE_PLANS);
  plans.plans = [plans.plans[0]];
  for (const routing of Object.values(plans.plans[0].routing!)) {
    routing.devices[0].reason = "正在修复，下一部已排好";
    routing.devices[0].finish_at = null;
  }
  vi.spyOn(api, "workflowPlans").mockResolvedValue(plans);
  wrap(<MediaWorkspace />);
  await go("待处理");
  const option = await screen.findByRole("option", { name: /5070 Ti · 正在修复，下一部已排好/ });
  expect(option).toBeEnabled();
  fireEvent.change(option.parentElement!, { target: { value: W.ti70.id } });
  fireEvent.click(screen.getByRole("button", { name: "确认入队" }));
  await waitFor(() => expect(confirmPlans).toHaveBeenCalledWith(expect.arrayContaining([
    expect.objectContaining({ machine: W.ti70.id }),
  ])));
});
