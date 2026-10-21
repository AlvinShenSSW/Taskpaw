import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type PlanChoice } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { filmStatus, lifecycle, reviewStatus } from "../components/media/mediaModel";
import { LtxDownload } from "../components/LtxDownload";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import { setLang } from "../i18n";
import { detail, FILMS_RESPONSE, PLANS, PREFLIGHTS, row, steps } from "./fixtures/mediaWorkspace";

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

const SLOGANS = ["让每一部影片，顺利归位。", "每部影片，一条完整记录。", "最后一步，由您确认。", "停在可恢复的位置。", "先安排，再开工。",
  "WORKFLOW /", "FILM QUEUE", "HUMAN REVIEW", "EXCEPTIONS /", "PLANNING /", "LOCAL AGENT"];
const FORBIDDEN = /演示|FIXTURE|静态快照|虚构/;

function mockApi() {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS_RESPONSE);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
  vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detail(id));
  vi.spyOn(api, "workflowTarget").mockImplementation(async (id, performer) => {
    const pf = PREFLIGHTS[id] ?? { target_path: null, absent: null, error_code: null, candidates: [] };
    return performer === undefined ? pf : { ...pf, target_path: pf.target_path ?? `\\\\nas\\actors\\${performer}` };
  });
  vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
  vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "HOME-PC", monitors: {} });
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { inbox_root: "D:\\TODO" } });
  return {
    confirmPlans: vi.spyOn(api, "workflowConfirmPlans").mockImplementation(async (plans: PlanChoice[]) =>
      ({ results: plans.map((p) => ({ film_id: p.film_id, ok: true })) })),
    confirm: vi.spyOn(api, "workflowConfirm").mockResolvedValue({ approval_id: "a1" }),
    pause: vi.spyOn(api, "workflowPause").mockResolvedValue({ paused: true }),
  };
}

const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
const go = async (label: string) => {
  fireEvent.click(within(rail()).getByRole("button", { name: new RegExp(`^${label}`) }));
  return screen.findByRole("heading", { level: 1, name: label });
};

beforeEach(() => setLang("zh-CN"));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

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
    for (const i of [5, 7, 8, 9]) expect(items[i]).toHaveTextContent("后续");
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
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ state: "off", reason: "未启用", current: null, intake: null, films: [], packages: [] });
    wrap(<MediaWorkspace />);
    expect(await screen.findByText("媒体流水线未运行：未启用")).toBeInTheDocument();
  });

  it("at 390px hides the rail and navigates through the 「工作台导航」 dialog", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width:900px") || query.includes("max-width: 900px"), media: query, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }));
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    expect(screen.queryByRole("navigation", { name: "媒体处理中心导航" })).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "工作区导航" });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "工作台导航" });
    expect(within(dialog).getByRole("button", { name: "关闭主导航" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /^待确认/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "待确认" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作台导航" })).not.toBeInTheDocument());
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
      [{ film_id: "abf-422", input_revision: 1, ltx_model: "good", machine: "local" }]));
    await waitFor(() => expect(card("abf-422")).toBeNull());
    expect(await screen.findByText("晨雾车站 已确认入队 · 良好画质 · 本机")).toBeInTheDocument();
  });

  it("batch-confirms the unchanged films only and skips changed and stale ones", async () => {
    const m = mockApi();
    m.confirmPlans.mockImplementationOnce(async (plans) => ({
      results: plans.map((p) => (p.film_id === "abf-423" ? { film_id: p.film_id, ok: false, status: 409 } : { film_id: p.film_id, ok: true })),
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
      [{ film_id: "abf-422", input_revision: 1, ltx_model: "standard", machine: "local" }]));
    expect(await screen.findByText("已确认 1 部，进入影片队列。2 部已调整或需重新查看，请单独确认。")).toBeInTheDocument();
    // opening the detail acknowledges the new revision
    fireEvent.click(within(card("abf-423")).getByRole("button", { name: "查看详情" }));
    await screen.findByRole("dialog");
    expect(within(card("abf-423")).queryByText("收件内容已更新，请重新查看后确认")).toBeNull();
  });

  it("shows the empty state", async () => {
    mockApi();
    vi.spyOn(api, "workflowPlans").mockResolvedValue({ items: [], machines: [] });
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, films: FILMS_RESPONSE.films.filter((f) => f.group !== "pending") });
    wrap(<MediaWorkspace />);
    await go("待处理");
    expect(await screen.findByText("没有待处理影片")).toBeInTheDocument();
    expect(screen.getByText("新投入 INBOX 的影片会先出现在这里。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全部按推荐确认" })).toBeDisabled();
  });
});

describe("影片队列 / 待确认 / 异常", () => {
  it("queue cards carry status chips and the step line with 待处理; searching filters", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("影片队列");
    const line = await screen.findByRole("list", { name: "浮光之间 生命周期" });
    const names = within(line).getAllByRole("listitem").map((li) => li.textContent);
    expect(names.slice(0, 3)).toEqual(["入库扫描（已完成）", "待处理（已完成）", "修复"]);
    expect(within(line).getByText("修复")).toHaveAttribute("aria-current", "step");
    expect(screen.getAllByText("LTX · Encode").length).toBeGreaterThan(0);
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
    await waitFor(() => expect(within(dialog).getByText("\\\\nas-a\\actors\\演员星河\\ABF-406")).toBeInTheDocument());
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith("abf-406", 1));
    expect(await screen.findByRole("dialog", { name: "已确认，等待归档" })).toBeInTheDocument();
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

describe("mediaModel", () => {
  it("maps backend rows onto the V3 vocabulary", () => {
    expect(filmStatus(row({ film_id: "a", group: "pending" }))).toBe("pending");
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "pending" }))).toBe("inbox");
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "running" }))).toBe("encode");
    expect(filmStatus(row({ film_id: "a", stage: "restore", status: "running",
      steps: steps("restore", "running", { ltx: { ltx_progress: { mode: "restore", stage_index: 3, stage_total: 4, stage_name: "denoise" } } }) }))).toBe("denoise");
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "scrape", status: "failed" }))).toBe("postprocess_error");
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "restore", status: "unknown" }))).toBe("stopped");
    expect(filmStatus(row({ film_id: "a", group: "attention", stage: "cleanup_input", status: "unknown" }))).toBe("cleanup");
    expect(filmStatus(row({ film_id: "a", group: "archiving", stage: "archive", status: "unknown" }))).toBe("unknown");
    expect(filmStatus(row({ film_id: "a", group: "done" }))).toBe("completed");
    expect(reviewStatus({ target_path: null, absent: null, error_code: "duplicate" })).toBe("duplicate");
    expect(reviewStatus(null)).toBe("ready");
  });

  it("adds 待处理 to an older step line", () => {
    const old = steps("restore").filter((s) => s.key !== "plan");
    expect(lifecycle(old, "active").map((s) => s.key)[1]).toBe("plan");
    expect(lifecycle(old, "active")[1].state).toBe("done");
    expect(lifecycle([{ key: "intake", state: "done", note: null, progress: null }], "pending")[1].state).toBe("waiting");
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
