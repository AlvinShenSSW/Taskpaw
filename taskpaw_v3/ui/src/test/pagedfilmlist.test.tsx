import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@mui/material/styles";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import i18n from "../i18n";

const row = (name: string, status = "pending") => ({ name, status,
  steps: status === "pre_done" || status === "collision" ? {} : { asr: status },
  percent: null, eta_s: null, duration_s: null });
const page = (n = 2, total = 25, extra = {}) => ({ run: "run-A", total, size: 10,
  page: n, pages: Math.max(1, Math.ceil(total / 10)), focus: "LMNO-11.mp4", focus_page: 2,
  films: Array.from({ length: Math.min(10, Math.max(0, total - (n - 1) * 10)) },
    (_, i) => row(`LMNO-${(n - 1) * 10 + i + 1}.mp4`)), ...extra });
const metrics = { film: "PQRS-current.mp4", steps: [{ key: "asr", state: "active" }],
  films: [row("PQRS-fallback-1.mp4"), row("PQRS-fallback-2.mp4")], films_more: 23 };
const extrasMetrics = { queue_pre_done: 3, queue_completed: 3, queue_total: 3 };
const response = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 404,
  json: async () => body });
function deferred() {
  let resolve!: (value: ReturnType<typeof response>) => void;
  const promise = new Promise<ReturnType<typeof response>>((r) => { resolve = r; });
  return { promise, resolve };
}
let serve: (url: URL) => ReturnType<typeof response> | Promise<ReturnType<typeof response>>;
let qc: QueryClient;
let requests: URL[];
function mount(m: Record<string, unknown> = metrics, taskName: string | undefined = "translate/main") {
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>
    <MonitorMetrics metrics={m} taskName={taskName} />
  </QueryClientProvider></ThemeProvider>);
}
const prev = () => screen.getByRole("button", { name: /Previous|上一页/ });
const next = () => screen.getByRole("button", { name: /Next|下一页/ });
const back = () => screen.getByRole("button", { name: /Back to current film|回到当前影片/ });
const text = (n: number, pages = 3, total = 25) => `Page ${n} / ${pages} · ${total} films`;
const poll = async () => { await act(async () => { await qc.invalidateQueries({ queryKey: ["films"] }); }); };

beforeEach(async () => {
  await i18n.changeLanguage("en");
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  requests = [];
  serve = (url) => response(page(Number(url.searchParams.get("page") ?? 2)));
  vi.stubGlobal("fetch", vi.fn((input: string) => {
    const url = new URL(input);
    if (url.pathname === "/control/monitors/films") { requests.push(url); return Promise.resolve(serve(url)); }
    return Promise.resolve(response({ boot: "test", entries: [], days: [], next_before: null }));
  }));
});
afterEach(async () => {
  cleanup(); qc.clear(); vi.useRealTimers(); vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("film paging (#198)", () => {
  it("opens on the focus page, encodes slash names, shows at most ten rows and no capped remainder", async () => {
    mount();
    expect(await screen.findByText(text(2))).toBeInTheDocument();
    expect(screen.getAllByTestId("film-row")).toHaveLength(10);
    expect(requests[0].searchParams.get("name")).toBe("translate/main");
    expect(requests[0].searchParams.get("size")).toBe("10");
    expect(requests[0].searchParams.has("page")).toBe(false);
    expect(screen.queryByText(/23 more|还有/)).toBeNull();
    const focus = screen.getByText("LMNO-11.mp4").closest('[data-testid="film-row"]');
    expect(focus).toHaveAttribute("aria-current", "true");
    expect(screen.queryByRole("button", { name: /Back to current film/ })).toBeNull();
  });

  it("follows a moving focus until manual paging, then returns to following", async () => {
    mount(); await screen.findByText(text(2));
    serve = (url) => response(page(Number(url.searchParams.get("page") ?? 3), 25,
      { focus: "LMNO-21.mp4", focus_page: 3 }));
    await poll(); await screen.findByText(text(3));
    expect(next()).toBeDisabled();
    fireEvent.click(prev()); await screen.findByText(text(2));
    expect(back()).toBeInTheDocument();
    await poll(); expect(screen.getByText(text(2))).toBeInTheDocument();
    expect(requests.at(-1)?.searchParams.get("page")).toBe("2");
    fireEvent.click(back()); await screen.findByText(text(3));
    expect(requests.at(-1)?.searchParams.has("page")).toBe(false);
    expect(screen.queryByRole("button", { name: /Back to current film/ })).toBeNull();
  });

  it("disables ends and page transitions, but not background polls; navigation uses the returned page", async () => {
    mount(); await screen.findByText(text(2));
    const pending = deferred(); serve = () => pending.promise;
    let refreshing!: Promise<void>;
    act(() => { refreshing = qc.invalidateQueries({ queryKey: ["films"] }); });
    expect(prev()).toBeEnabled(); expect(next()).toBeEnabled();
    await act(async () => { pending.resolve(response(page(2))); await refreshing; });
    const changing = deferred(); serve = () => changing.promise;
    fireEvent.click(prev());
    expect(prev()).toBeDisabled(); expect(next()).toBeDisabled();
    await act(async () => { changing.resolve(response(page(1))); });
    await screen.findByText(text(1)); expect(prev()).toBeDisabled(); expect(next()).toBeEnabled();
    // The server clamps a request to page 2 down to page 1 after a shrink.
    serve = () => response(page(1, 11));
    fireEvent.click(next()); await screen.findByText(text(1, 2, 11));
    await waitFor(() => expect(next()).toBeEnabled());
    serve = (url) => response(page(Number(url.searchParams.get("page")), 11));
    fireEvent.click(next()); await screen.findByText(text(2, 2, 11));
    expect(requests.at(-1)?.searchParams.get("page")).toBe("2");
    expect(next()).toBeDisabled();
  });

  it("a new run while manually paged resets to following", async () => {
    mount(); await screen.findByText(text(2));
    fireEvent.click(prev()); await screen.findByText(text(1));
    serve = (url) => response(page(Number(url.searchParams.get("page") ?? 3), 25,
      { run: "run-B", focus: "LMNO-21.mp4", focus_page: 3 }));
    await poll(); await screen.findByText(text(3));
    expect(requests.at(-1)?.searchParams.has("page")).toBe(false);
    expect(screen.queryByRole("button", { name: /Back to current film/ })).toBeNull();
  });

  it("a new run stays in following mode if its first focus request fails", async () => {
    mount(); await screen.findByText(text(2));
    fireEvent.click(prev()); await screen.findByText(text(1));
    serve = (url) => url.searchParams.has("page")
      ? response(page(1, 25, { run: "run-B" })) : response({}, false);
    await poll();
    await waitFor(() => expect(requests.at(-1)?.searchParams.has("page")).toBe(false));
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Back to current film/ })).toBeNull());
    expect(screen.getByText(text(1))).toBeInTheDocument();
    serve = () => response(page(3, 25, { run: "run-B", focus: "LMNO-21.mp4", focus_page: 3 }));
    await poll(); await screen.findByText(text(3));
    expect(requests.at(-1)?.searchParams.has("page")).toBe(false);
  });

  it("polls every five seconds, refreshes row status and stops after unmount", async () => {
    vi.useFakeTimers();
    const view = mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(screen.getByText(text(2))).toBeInTheDocument();
    serve = () => response(page(2, 25, { films: page().films.map((r) => row(r.name, "done")) }));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(requests).toHaveLength(2);
    expect(within(screen.getAllByTestId("film-row")[0]).getByText("Done")).toBeInTheDocument();
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(requests).toHaveLength(2);
    expect(qc.getQueryCache().findAll({ queryKey: ["films"] })).toHaveLength(0);
  });

  it.each(["en", "zh-CN"])("localizes paging and extra rows with no step chips (%s)", async (lang) => {
    await i18n.changeLanguage(lang);
    serve = () => response(page(2, 12, { films: [row("ABC.mp4", "pre_done"), row("PQRS.mp4", "collision")] }));
    mount();
    await screen.findByText(lang === "en" ? text(2, 2, 12) : "第 2 / 2 页 · 共 12 部");
    expect(prev()).toBeEnabled(); expect(next()).toBeDisabled();
    expect(screen.getByText(lang === "en" ? "Already had subtitles" : "已有字幕")).toBeInTheDocument();
    expect(screen.getByText(lang === "en" ? "Name collision, skipped" : "同名冲突，未处理")).toBeInTheDocument();
    for (const r of screen.getAllByTestId("film-row")) expect(r.querySelector(".MuiChip-root")).toBeNull();
  });

  it.each([2, 10])("total %s has no pager", async (total) => {
    serve = () => response(page(1, total, { focus: null, focus_page: null })); mount();
    await screen.findByText("LMNO-1.mp4");
    expect(screen.queryByRole("button", { name: /Previous|Next/ })).toBeNull();
  });

  it.each([0, 1])("total %s is hidden without fallback rows", async (total) => {
    serve = () => response(page(1, total, { focus: null, focus_page: null })); mount(extrasMetrics);
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(screen.queryByTestId("pipeline-films")).toBeNull();
  });

  it("uses capped status rows on first load and all-failed requests", async () => {
    const pending = deferred(); serve = () => pending.promise; mount();
    expect(screen.getByText("PQRS-fallback-1.mp4")).toBeInTheDocument();
    expect(screen.getByText("23 more")).toBeInTheDocument();
    await act(async () => { pending.resolve(response({}, false)); });
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    await poll(); expect(screen.getByText("23 more")).toBeInTheDocument();
  });

  it("falls back when a good zero-total response arrives while status still has rows", async () => {
    mount(); await screen.findByText(text(2));
    serve = () => response(page(1, 0, { focus: null, focus_page: null }));
    await poll(); expect(await screen.findByText("23 more")).toBeInTheDocument();
  });

  it.each(["following", "manual"])("failed page change retains the last good page from %s and the next click really retries", async (mode) => {
    mount(); await screen.findByText(text(2));
    if (mode === "manual") { fireEvent.click(prev()); await screen.findByText(text(1)); }
    const shown = mode === "manual" ? 1 : 2;
    serve = () => response({}, false); fireEvent.click(next());
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    await waitFor(() => expect(next()).toBeEnabled());
    expect(screen.getByText(text(shown))).toBeInTheDocument();
    expect(screen.queryByText("23 more")).toBeNull();
    serve = (url) => response(page(Number(url.searchParams.get("page") ?? 2)));
    const before = requests.length;
    fireEvent.click(next()); await screen.findByText(text(shown + 1));
    expect(requests.length).toBeGreaterThan(before);
    expect(requests.at(-1)?.searchParams.get("page")).toBe(String(shown + 1));
  });

  it("failed polls retain the good page and enabled pager", async () => {
    mount(); await screen.findByText(text(2));
    serve = () => response({}, false); await poll();
    await waitFor(() => expect(next()).toBeEnabled());
    expect(screen.getByText(text(2))).toBeInTheDocument();
    expect(screen.queryByText("23 more")).toBeNull();
  });

  it("a failed change restores an enabled pager even while the recovery poll is pending", async () => {
    mount(); await screen.findByText(text(2));
    const recovery = deferred();
    serve = (url) => url.searchParams.has("page") ? response({}, false) : recovery.promise;
    fireEvent.click(next());
    await waitFor(() => expect(requests.filter((u) => !u.searchParams.has("page"))).toHaveLength(2));
    await waitFor(() => expect(next()).toBeEnabled());
    expect(screen.getByText(text(2))).toBeInTheDocument();
    serve = (url) => response(page(Number(url.searchParams.get("page") ?? 2)));
    fireEvent.click(next()); await screen.findByText(text(3));
    await act(async () => { recovery.resolve(response(page(2))); });
    expect(screen.getByText(text(3))).toBeInTheDocument();
  });

  it.each([{}, page(2, 25, { films: [row("ABC", "bogus")] }), page(2, 25, { page: 0 }),
    page(2, 25, { films: "bad" }), page(2, 25, { total: true })])("malformed bodies count as failures: %j", async (body) => {
    serve = () => response(body); mount();
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(screen.getByText("23 more")).toBeInTheDocument();
    expect(qc.getQueryCache().find({ queryKey: ["films", "translate/main", undefined] })?.state.status).toBe("error");
    serve = () => response(page()); await poll(); await screen.findByText(text(2));
    serve = () => response(body); fireEvent.click(next());
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    await waitFor(() => expect(next()).toBeEnabled());
    expect(screen.getByText(text(2))).toBeInTheDocument();
  });

  it("avsubs without steps mounts its extras-only page; 404 stays hidden", async () => {
    serve = () => response({}, false); mount(extrasMetrics);
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(screen.queryByTestId("pipeline-films")).toBeNull();
    serve = () => response(page(1, 3, { focus: null, focus_page: null,
      films: [row("ABC-1.mp4", "pre_done"), row("ABC-2.mp4", "pre_done"), row("ABC-3.mp4", "pre_done")] }));
    await poll(); expect(await screen.findByText("ABC-1.mp4")).toBeInTheDocument();
    expect(screen.getAllByText("Already had subtitles")).toHaveLength(3);
  });

  it("no-name / Hub and non-avsubs no-steps paths do not fetch films", () => {
    render(<MonitorMetrics metrics={metrics} />);
    expect(screen.getByText("23 more")).toBeInTheDocument(); expect(requests).toHaveLength(0);
    cleanup(); mount({ queue_total: 3, queue_completed: 3 }); expect(requests).toHaveLength(0);
  });

  it("AgentConsole passes task names and switching tasks never shows old rows (including reselect)", async () => {
    const pending = deferred();
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      const url = new URL(input);
      if (url.pathname === "/control/status") return Promise.resolve(response({ machine: "box", monitors: {
        "translate/main": { state: "running", type_id: "avsubs", metrics },
        "translate/other": { state: "running", type_id: "avsubs", metrics: extrasMetrics },
      } }));
      if (url.pathname === "/control/monitors/films") {
        requests.push(url); return requests.length === 1 ? Promise.resolve(response(page())) : pending.promise;
      }
      return Promise.resolve(response({ boot: "test", plugins: [], presets: [], entries: [], days: [], next_before: null }));
    }));
    render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><AgentConsole /></QueryClientProvider></ThemeProvider>);
    await screen.findByText(text(2));
    fireEvent.click(screen.getByRole("button", { name: /translate\/other/ }));
    expect(screen.queryByText("LMNO-11.mp4")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /translate\/main/ }));
    expect(screen.queryByText("LMNO-11.mp4")).toBeNull();
    expect(screen.getByText("23 more")).toBeInTheDocument();
  });
});
