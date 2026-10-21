// 媒体处理中心 over the backend's real responses (U1). The fixture is what the routes return for
// a realistic runtime (taskpaw_v3/tests/test_workflow_ui_contract.py writes and checks it), so a
// field the UI reads but the backend does not send fails here instead of rendering empty.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type FilmDetail, type TargetPreflight, type WorkflowFilms, type WorkflowPlans } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { theme } from "../theme";
import { setLang } from "../i18n";
import contract from "./fixtures/workflow-contract.json";

interface Contract {
  films: WorkflowFilms;
  film: Record<string, FilmDetail>;
  plans: WorkflowPlans;
  target: Record<"candidates" | "selected" | "missing" | "offline", TargetPreflight>;
}
// JSON infers plain strings for the unions; the pytest guarantees the shapes.
const C = contract as unknown as Contract;
const REVIEW = "film-review";

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

function mockApi(target: (performer: string | number | undefined) => TargetPreflight = (p) =>
  (p === undefined ? C.target.candidates : p === "actor" ? C.target.selected : C.target.missing)) {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(C.films);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(C.plans);
  vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => C.film[id]);
  vi.spyOn(api, "workflowTarget").mockImplementation(async (_id, performer) => target(performer));
  vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "HOME-PC", monitors: {} });
  return {
    image: vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,"),
    confirmPlans: vi.spyOn(api, "workflowConfirmPlans").mockResolvedValue({ results: [] }),
    confirm: vi.spyOn(api, "workflowConfirm").mockResolvedValue({ approval_id: "a1" }),
    lookup: vi.spyOn(api, "workflowLookup").mockResolvedValue({ ok: true }),
  };
}

const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
const go = async (label: string) => {
  fireEvent.click(within(rail()).getByRole("button", { name: new RegExp(`^${label}`) }));
  return screen.findByRole("heading", { level: 1, name: label });
};
const posterOf = (id: string) => api.posterPath(id);

beforeEach(() => { setLang("zh-CN"); vi.spyOn(window, "scrollTo").mockImplementation(() => {}); });
afterEach(() => { vi.restoreAllMocks(); });

describe("contract: 概览", () => {
  it("counts from the backend, the running LTX stage, the GPU card and the next film to confirm", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    // badges: counts.planning, the confirm group, the failed film + the stuck package
    await waitFor(() => expect(within(rail()).getByRole("button", { name: /^待处理/ })).toHaveTextContent("待处理2"));
    expect(within(rail()).getByRole("button", { name: /^待确认/ })).toHaveTextContent("待确认1");
    expect(within(rail()).getByRole("button", { name: /^异常/ })).toHaveTextContent("异常2");
    expect(screen.getByText("2 部待安排 · 1 部待入库")).toBeInTheDocument();
    expect(screen.getByText("1 部 LTX · 其余步骤分别计数")).toBeInTheDocument();
    expect(screen.getByText("NVIDIA GeForce RTX 5060 8 GB")).toBeInTheDocument();
    expect(screen.getByText("修复输出上限 1080p")).toBeInTheDocument();
    const bar = await screen.findByRole("progressbar", { name: "movie Encode / 编码 本阶段进度" });
    expect(bar).toHaveAttribute("aria-valuenow", "52");
    expect(screen.getByText("156 / 300 帧")).toBeInTheDocument();
    expect(screen.getByText("5.62 frame/s")).toBeInTheDocument();
    expect(screen.getByText("GPU · 占用中：movie")).toBeInTheDocument();
    expect(screen.getByText("可领取新任务")).toBeInTheDocument();
    // the review peek: the final NFO's title, its final cover
    expect(screen.getByRole("heading", { level: 3, name: "假的标题" })).toBeInTheDocument();
    await waitFor(() => expect(m.image).toHaveBeenCalledWith(posterOf(REVIEW)));
  });
});

describe("contract: 待处理", () => {
  const card = (id: string) => document.querySelector<HTMLElement>(`[data-pending="${id}"]`) as HTMLElement;

  it("shows the lookup, the recommendation and its reasons, and confirms with the backend's ids", async () => {
    const m = mockApi();
    m.confirmPlans.mockResolvedValue({ results: [{ film_id: "film-planning", ok: true, error: null, message: null }] });
    wrap(<MediaWorkspace />);
    await go("待处理");
    await waitFor(() => expect(card("film-planning")).toBeTruthy());
    const c = within(card("film-planning"));
    expect(c.getByRole("heading", { level: 3, name: "MOVIE 的标题" })).toBeInTheDocument();
    expect(c.getByText("MOVIE · 演员甲、演员乙")).toBeInTheDocument();
    expect(c.getByText("1080p")).toBeInTheDocument();
    expect(c.getByText("0:02:00 · 50 B · 2 个文件")).toBeInTheDocument();
    const standard = within(c.getByRole("radiogroup", { name: "画质" })).getByRole("radio", { name: /标准/ });
    expect(standard).toHaveAttribute("aria-checked", "true");
    expect(within(standard).getByText("推荐")).toBeInTheDocument();
    expect(c.getByText("默认")).toBeInTheDocument();
    expect(c.getByRole("combobox", { name: "设备" })).toHaveDisplayValue("本机 · NVIDIA GeForce RTX 5060 8 GB");
    expect(c.getByText("NVIDIA GeForce RTX 5060 8 GB")).toBeInTheDocument();
    await waitFor(() => expect(m.image).toHaveBeenCalledWith(posterOf("film-planning")));
    // the film Miko found nothing for
    const u = within(card("film-planning-unknown"));
    expect(u.getByText("未查到资料")).toBeInTheDocument();
    fireEvent.click(u.getByRole("button", { name: "重新查询" }));
    await waitFor(() => expect(m.lookup).toHaveBeenCalledWith("film-planning-unknown"));
    fireEvent.click(c.getByRole("button", { name: "确认入队" }));
    await waitFor(() => expect(m.confirmPlans).toHaveBeenCalledWith(
      [{ film_id: "film-planning", input_revision: 1, ltx_model: "standard", machine: "worker-local" }]));
    expect(await screen.findByText("MOVIE 的标题 已确认入队 · 标准画质 · 本机")).toBeInTheDocument();
  });
});

describe("contract: 影片队列 / 异常", () => {
  const queueCard = (id: string) => document.querySelector<HTMLElement>(`[data-film="${id}"]`) as HTMLElement;

  it("queue cards: the running restore, the review film's final cover and the arriving package", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    await go("影片队列");
    await waitFor(() => expect(queueCard("film-restoring")).toBeTruthy());
    const r = within(queueCard("film-restoring"));
    expect(r.getByText("LTX · Encode")).toBeInTheDocument();
    const line = r.getByRole("list", { name: "movie 生命周期" });
    expect(within(line).getAllByRole("listitem").map((li) => li.textContent).slice(0, 3))
      .toEqual(["入库扫描（已完成）", "待处理（已完成）", "修复"]);
    expect(within(line).getByText("修复")).toHaveAttribute("aria-current", "step");
    const review = within(queueCard(REVIEW));
    expect(review.getByText("ABC-123 · 1080p")).toBeInTheDocument();
    expect(review.getByText("Actor")).toBeInTheDocument();
    await waitFor(() => expect(m.image).toHaveBeenCalledWith(posterOf(REVIEW)));
    expect(within(queueCard("pkg:package-arriving")).getByText("等待收件检查")).toBeInTheDocument();
    expect(screen.getByText("1.0 GB / 4.0 GB")).toBeInTheDocument();
    // unplanned films and packages that need the operator are elsewhere
    expect(queueCard("film-planning")).toBeNull();
    expect(queueCard("pkg:package-stuck")).toBeNull();
  });

  it("exception cards: the failed translate with its reason, and the stuck package", async () => {
    mockApi();
    const retry = vi.spyOn(api, "workflowRetry").mockResolvedValue({ ok: true });
    wrap(<MediaWorkspace />);
    await go("异常");
    expect(await screen.findByText("翻译失败")).toBeInTheDocument();
    expect(screen.getByText("LLM 超时：翻译服务 120 秒没有响应")).toBeInTheDocument();
    expect(screen.getByText("收件需要处理", { selector: "h3" })).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "重试" })[0]);
    await waitFor(() => expect(retry).toHaveBeenCalledWith("film-failed", "translate"));
  });
});

describe("contract: drawer", () => {
  it("the running film: LtxProgress, the plan facts and the manifest versions", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 movie 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await waitFor(() => expect(within(drawer).getByTestId("ltx-progress")).toBeInTheDocument());
    expect(within(drawer).getByText("标准画质")).toBeInTheDocument();
    expect(within(drawer).getByText("本机 · NVIDIA GeForce RTX 5060 8 GB")).toBeInTheDocument();
    expect(within(drawer).getByText("尚无成品")).toBeInTheDocument();
    const hash = C.film["film-restoring"].revisions.input?.hash.slice(0, 16);
    expect(within(drawer).getByText(`R1 · ${hash}`)).toBeInTheDocument();
    expect(within(drawer).getByText("主控 INBOX / movie · 本次收件 2 项")).toBeInTheDocument();
  });

  it("an unplanned film: the lookup's names and 待安排 with the recommendation", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    await go("待处理");
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-pending="film-planning"]');
      if (!el) throw new Error("no card");
      return el;
    });
    fireEvent.click(within(card).getByRole("button", { name: "查看详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    expect(await within(drawer).findByRole("heading", { level: 2, name: "MOVIE 的标题" })).toBeInTheDocument();
    expect(within(drawer).getByText("演员甲、演员乙")).toBeInTheDocument();
    expect(within(drawer).getAllByText("待安排")).toHaveLength(2);
    expect(within(drawer).getByText("待处理", { selector: "[aria-current=step]" })).toBeInTheDocument();
  });
});

describe("contract: 待确认 and 图文确认", () => {
  const openReview = async () => {
    await go("待确认");
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>(`[data-review="${REVIEW}"]`);
      if (!el) throw new Error("no card");
      return el;
    });
    return card;
  };

  it("the review card: one candidate, its target and the final package", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    const card = within(await openReview());
    expect(card.getByRole("heading", { level: 3, name: "假的标题" })).toBeInTheDocument();
    expect(card.getByText("ABC-123 · Actor")).toBeInTheDocument();
    await waitFor(() => expect(card.getByText("NAS / Actor")).toBeInTheDocument());
    expect(card.getByText("待确认")).toBeInTheDocument();
    expect(await card.findByText("最终成品包 7 个文件 · Miko 已完成")).toBeInTheDocument();
  });

  it("pre-selects the one candidate, shows the backend's target and confirms", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(within(await openReview()).getByRole("button", { name: "图文核对入库" }));
    const dialog = await screen.findByRole("dialog", { name: "确认影片与入库目标" });
    await waitFor(() => expect(within(dialog).getByText("Actor", { selector: "strong" })).toBeInTheDocument());
    expect(await within(dialog).findByText(C.target.selected.target_path as string)).toBeInTheDocument();
    expect(within(dialog).getByText("\\\\nas-a\\actors\\Actor\\ABC-123")).toBeInTheDocument();
    expect(within(dialog).getByText("最终文件包 · 7 个文件")).toBeInTheDocument();
    const ok = within(dialog).getByRole("button", { name: "确认入库" });
    await waitFor(() => expect(ok).toBeEnabled());
    fireEvent.click(ok);
    await waitFor(() => expect(m.confirm).toHaveBeenCalledWith(REVIEW, "actor"));
  });

  it("a performer record that is gone blocks the confirm and asks for a new choice", async () => {
    // the lone candidate's record is gone: the backend's one call says so (F4)
    mockApi(() => ({ ...C.target.missing, performer_id: C.target.candidates.performer_id }));
    wrap(<MediaWorkspace />);
    fireEvent.click(within(await openReview()).getByRole("button", { name: /图文核对入库|查看并处理/ }));
    const dialog = await screen.findByRole("dialog", { name: "确认影片与入库目标" });
    expect(await within(dialog).findByText("演员记录已不存在，请重新选择。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
  });

  it("Filmpaw offline: no candidates, the card and the dialog say the NAS is offline", async () => {
    mockApi(() => C.target.offline);
    wrap(<MediaWorkspace />);
    const card = within(await openReview());
    await waitFor(() => expect(card.getByText("目标 NAS 离线，禁止提交")).toBeInTheDocument());
    fireEvent.click(card.getByRole("button", { name: "查看并处理" }));
    const dialog = await screen.findByRole("dialog", { name: "确认影片与入库目标" });
    expect(await within(dialog).findByText("目标 NAS 暂时离线，请稍后重试。")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认入库" })).toBeDisabled();
  });
});
