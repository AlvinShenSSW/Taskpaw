import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setDevControlCredential, type FilmDetail, type HubServer } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import * as nativePicker from "../components/nativePicker";
import { DEVICES_KEY } from "../components/media/workspace";
import { REMOTE_DEVICES } from "./fixtures/multiMachine";
import { HubDashboard } from "../views/HubDashboard";
import { setLang } from "../i18n";
import { theme } from "../theme";
import { detail, FILMS_RESPONSE, lookupFailed, PLANS, restoreSteps, steps } from "./fixtures/mediaWorkspace";

const HUB_BASE = "http://127.0.0.1:15691";
const SERVER: HubServer = { id: 7, name: "Windows controller", ip: "10.0.0.7", port: 5680, enabled: 1, online: true,
  snapshot: { machine: "WINDOWS-GPU", os: "Windows", monitors: {} } };
const IMAGE = "data:image/png;base64,iVBORw0KGgo=";
const MUTATIONS = /确认入队|全部按推荐确认|重新查询|图文核对|确认入库|手动入库|核对入库|查看并处理|重试|下载并继续|稍后|暂停新领取|恢复新领取|停止.*任务|打开文件夹|预览播放/;
const clients: QueryClient[] = [];
type Recorded = { path: string; method: string; origin: string; auth: string | null };
let requests: Recorded[];
let servers: HubServer[];
let available: Set<number>;
let override: Partial<FilmDetail>;
let filmsExtra: Record<string, unknown>;
let planExtra: Record<string, unknown>;

function mount(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(qc);
  const wrapper = (children: ReactNode) => <ThemeProvider theme={theme}>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </ThemeProvider>;
  const view = render(wrapper(ui));
  return { ...view, qc, replace: (next: ReactNode) => view.rerender(wrapper(next)) };
}

function serve() {
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    requests.push({ path: url.pathname, method: init?.method ?? "GET", origin: url.origin,
      auth: new Headers(init?.headers).get("Authorization") });
    if (url.pathname === "/status") return Response.json({ machine: "MAC-HUB", servers, self: {}, acks: {} });
    const match = url.pathname.match(/^\/servers\/(\d+)\/workflow\/(.+)$/);
    if (!match || !available.has(Number(match[1]))) return Response.json({ error: "workflow_unavailable" }, { status: 404 });
    const [, sid, resource] = match;
    if (resource === "films") return Response.json({ ...FILMS_RESPONSE, ...filmsExtra });
    if (resource === "plans") return Response.json({ ...PLANS, plans: PLANS.plans.map(p => ({ ...p,
      has_poster: true, poster_source: "lookup-1", has_cover: true, cover_source: "lookup-1",
      lookup: lookupFailed("未查到资料", true), ...planExtra,
    })) });
    if (/\/poster$|\/cover$/.test(resource)) {
      return new Response(`image-${sid}`, { headers: { "Content-Type": "image/png" } });
    }
    if (resource.startsWith("films/")) return Response.json(detail(resource.slice(6), {
      steps: restoreSteps("ltx"), has_poster: true, poster_source: "lookup-1", has_cover: true, cover_source: "lookup-1",
      timing: { stages: { restore: 400 }, restore_breakdown: null, total_restore_to_translate: null, queue_wait: 10 },
      ...override,
    }));
    return Response.json({}, { status: 404 });
  }));
}

const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
async function go(label: string) {
  fireEvent.click(within(rail()).getByRole("button", { name: new RegExp(`^${label}`) }));
  await screen.findByRole("heading", { level: 1, name: label });
}
function noControls() {
  expect(screen.queryAllByRole("button").filter(b => MUTATIONS.test(b.textContent ?? ""))).toEqual([]);
}
function onlyHubGets() {
  expect(requests.length).toBeGreaterThan(0);
  for (const request of requests) {
    expect(request.method).toBe("GET");
    expect(request.origin).toBe(HUB_BASE);
    expect(request.auth).toBe("Bearer test-hub-control");
    expect(request.path === "/status" || /^\/servers\/\d+\/workflow\/(films|plans)(\/[^/]+(\/(poster|cover))?)?$/.test(request.path)).toBe(true);
  }
}

beforeEach(() => {
  vi.spyOn(Response.prototype, "blob").mockImplementation(async function (this: Response) {
    return new Blob([await this.arrayBuffer()], { type: this.headers.get("Content-Type") ?? "" });
  });
  setLang("zh-CN");
  vi.spyOn(nativePicker, "inTauri").mockReturnValue(true);
  setDevControlCredential("hub", "test-hub-control", HUB_BASE);
  requests = []; servers = [SERVER]; available = new Set([7]); override = {}; filmsExtra = {}; planExtra = {};
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  serve();
});
afterEach(() => {
  cleanup(); clients.splice(0).forEach(qc => qc.clear());
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("#286 Hub media read-only workspace", () => {
  it("discovers the enabled workflow source only when the Hub tab mounts", async () => {
    servers = [{ ...SERVER, id: 1, name: "Disabled", enabled: 0 }, { ...SERVER, id: 2, name: "Ordinary" }, SERVER];
    mount(<HubDashboard />);
    await screen.findByText(/MAC-HUB/);
    expect(requests.every(r => r.path === "/status")).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理" }));
    await screen.findByRole("heading", { name: "概览", level: 1 });
    expect(screen.getByText("只读监控 · 操作请在主控电脑上进行")).toBeInTheDocument();
    expect(requests.some(r => r.path === "/servers/2/workflow/films")).toBe(true);
    expect(requests.some(r => r.path.startsWith("/servers/1/"))).toBe(false);
    noControls(); onlyHubGets();
  });

  it("keeps recommendations, status, queue and exceptions without mutation or local-control reads", async () => {
    const { qc } = mount(<MediaWorkspace readOnly server={SERVER} />);
    // A cached local fleet must not turn the observed single-machine queue into a fleet board.
    qc.setQueryData(DEVICES_KEY, REMOTE_DEVICES);
    await screen.findByRole("heading", { name: "概览", level: 1 });
    noControls();
    await go("待处理");
    await screen.findByText("星河之外");
    expect(screen.getAllByText(/推荐/).length).toBeGreaterThan(0);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("combobox", { name: "设备" })).toBeNull();
    noControls();
    await go("影片队列"); noControls();
    await go("待确认");
    expect(screen.getAllByText("待确认").length).toBeGreaterThan(1);
    expect(screen.queryByText("检查中")).toBeNull();
    expect(screen.queryByRole("button", { name: "可核对入库" })).toBeNull();
    noControls();
    for (const label of ["异常", "设备", "本机", "片库与规则", "数据分析", "Mac 信息桥", "设置"]) {
      await go(label); noControls();
      if (label === "本机") expect(screen.getByText("WINDOWS-GPU")).toBeInTheDocument();
      // #361 / analytics D10: the library and 数据分析 live on the controller; the Hub reads none of their routes
      if (label === "片库与规则" || label === "数据分析" || label === "设置") expect(screen.getByText(/请在主控电脑上查看和修改/)).toBeInTheDocument();
      if (label === "数据分析") expect(screen.getByText("只读监控不显示数据分析；请在主控电脑上查看和修改。")).toBeInTheDocument();
    }
    onlyHubGets();
  });

  it("shows the power figures as display only, without the Agent's 功耗详情 link", async () => {
    filmsExtra = { power: { w: 286, gpu_w: 210, cpu_w: 46, other_w: 30, today_wh: 8400, machines: 1, peers: 0, estimated: true },
      energy_rates: [] };
    planExtra = { prediction: { machine: "m", resolution: "4K", min_films: 3, films: { standard: 5, good: 4, high: 3, none: 0 },
      standard: 1400, good: 2300, high: 5300, none: null, estimated: true } };
    mount(<MediaWorkspace readOnly server={SERVER} />);
    await screen.findByRole("heading", { name: "概览", level: 1 });
    const card = await screen.findByTestId("cluster-power");
    expect(card).toHaveTextContent("286W · 1 台计算设备");
    expect(within(card).queryByRole("button")).toBeNull();
    await go("待处理");
    await screen.findByText("星河之外");
    expect(screen.getAllByText(/预计耗电/).length).toBeGreaterThan(0);
    noControls(); onlyHubGets();
  });

  it("keeps LTX details, step line, parameters, timing and authenticated cover lightbox", async () => {
    mount(<MediaWorkspace readOnly server={SERVER} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await within(drawer).findByTestId("restore-live");
    expect(within(drawer).getByTestId("restore-params")).toHaveTextContent("本片参数");
    expect(within(drawer).getAllByText(/用时/).length).toBeGreaterThan(0);
    expect(within(drawer).getByLabelText(/生命周期/)).toBeInTheDocument();
    fireEvent.click(await within(drawer).findByRole("button", { name: /^查看大图/ }));
    const lightbox = await screen.findByRole("dialog", { name: /大图$/ });
    await waitFor(() => expect(within(lightbox).getByRole("img")).toHaveAttribute("src", expect.stringMatching(/^data:image\/png/)));
    fireEvent.click(within(lightbox).getByRole("button", { name: "关闭大图" }));
    noControls(); onlyHubGets();
    expect(requests.some(r => r.path === "/servers/7/workflow/films/abf-401/cover")).toBe(true);
  });

  it.each(["awaiting", "error"])("shows model download state %s without a response control", async state => {
    override = { steps: steps("restore", "failed", { download: {
      state, request_id: "r1", files: ["vae.safetensors.enc"], total_bytes: 1e9, downloaded_bytes: 0,
    } }) };
    mount(<MediaWorkspace readOnly server={SERVER} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await within(drawer).findByText("vae.safetensors.enc");
    noControls(); onlyHubGets();
  });

  it.each([
    ["待处理", "[data-pending=abf-421]"],
    ["待确认", "[data-review=abf-406]"],
    ["异常", "[data-exception=abf-412]"],
    ["异常", "[data-exception=abf-413]"],
  ])("keeps %s detail without enqueue, confirmation or stage retry (%s)", async (page, selector) => {
    mount(<MediaWorkspace readOnly server={SERVER} />);
    await screen.findByRole("heading", { name: "概览", level: 1 });
    await go(page);
    const card = await waitFor(() => {
      const element = document.querySelector<HTMLElement>(selector);
      expect(element).not.toBeNull();
      return element!;
    });
    fireEvent.click(within(card).getByRole("button", { name: "查看详情" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await within(drawer).findByTestId("restore-live");
    expect(screen.queryByRole("dialog", { name: "确认影片与入库目标" })).toBeNull();
    noControls(); onlyHubGets();
  });

  it("isolates list, detail and image caches from the local Agent and another Hub source", async () => {
    available = new Set([7, 8]);
    const view = mount(<MediaWorkspace readOnly server={SERVER} />);
    view.qc.setQueryData(["mediaFilms"], { ...FILMS_RESPONSE, state: "off", reason: "LOCAL-ONLY" });
    view.qc.setQueryData(["mediaFilm", "abf-401"], detail("abf-401", { title: "LOCAL-ONLY" }));
    view.qc.setQueryData(["mediaPoster", "abf-401", "lookup-1"], IMAGE);
    view.qc.setQueryData(["mediaCover", "abf-401", "lookup-1"], IMAGE);
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    await screen.findByTestId("restore-live");
    const cover = await screen.findByRole("button", { name: /^查看大图/ });
    fireEvent.click(cover);
    await screen.findByRole("dialog", { name: /大图$/ });
    await waitFor(() => expect(requests.some(r => r.path === "/servers/7/workflow/films/abf-401/cover")).toBe(true));
    view.replace(<MediaWorkspace readOnly server={{ ...SERVER, id: 8 }} />);
    await waitFor(() => expect(requests.some(r => r.path === "/servers/8/workflow/films")).toBe(true));
    fireEvent.click(await screen.findByRole("button", { name: "查看 浮光之间 详情" }));
    await screen.findByTestId("restore-live");
    fireEvent.click(await screen.findByRole("button", { name: /^查看大图/ }));
    await waitFor(() => expect(requests.some(r => r.path === "/servers/8/workflow/films/abf-401/cover")).toBe(true));
    expect(screen.queryByText("LOCAL-ONLY")).toBeNull();
    onlyHubGets();
  });

  it("does not present an unavailable or removed source as running", async () => {
    const { qc } = mount(<HubDashboard />);
    await screen.findByText(/MAC-HUB/);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理" }));
    await screen.findByRole("heading", { name: "概览", level: 1 });
    servers = [];
    await act(async () => { await qc.invalidateQueries({ queryKey: ["hubStatus"] }); });
    expect(await screen.findByText("没有可用的媒体工作流主控。")).toBeInTheDocument();
    expect(screen.queryByText("主控 · 运行中")).toBeNull();
    onlyHubGets();
  });

  it("removes running state after a failed source refresh, and recovers with GET polling", async () => {
    const { qc } = mount(<HubDashboard />);
    await screen.findByText(/MAC-HUB/);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理" }));
    await screen.findByRole("heading", { name: "概览", level: 1 });
    available.clear();
    await act(async () => { await qc.invalidateQueries({ queryKey: ["mediaFilms"] }); });
    expect(await screen.findByText("没有可用的媒体工作流主控。")).toBeInTheDocument();
    expect(screen.queryByText("主控 · 运行中")).toBeNull();
    available.add(7);
    await act(async () => { await qc.invalidateQueries({ queryKey: ["mediaFilms"] }); });
    await screen.findByRole("heading", { name: "概览", level: 1 });
    onlyHubGets();
  });

  it("renders an honest empty state when enabled agents do not offer workflow reads", async () => {
    available.clear();
    mount(<HubDashboard />);
    await screen.findByText(/MAC-HUB/);
    fireEvent.click(screen.getByRole("tab", { name: "媒体处理" }));
    expect(await screen.findByText("没有可用的媒体工作流主控。")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "概览", level: 1 })).toBeNull();
    onlyHubGets();
  });
});
