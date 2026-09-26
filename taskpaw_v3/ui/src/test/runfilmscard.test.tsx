import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@mui/material/styles";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { RunFilmsCard } from "../components/RunFilmsCard";
import { readRunFilms, runDuration, runFinishedAt, type RunFilm, type RunFilter } from "../components/runFilmsCard.helpers";
import { readPipeline } from "../components/pipelineProgress.helpers";
import { theme } from "../theme";
import i18n from "../i18n";

const row = (name = "LMNO.mp4", extra: Partial<RunFilm> = {}): RunFilm => ({
  name, restore: "done", restored_before: false, asr: "done", translate: "done",
  percent: null, outcome: "translated", kept_ja: 0, models: [], duration_s: 4320,
  finished_at: new Date(2026, 8, 26, 9, 5).getTime() / 1000, ...extra,
});
const page = (filter: RunFilter = "done", n = 1, extra = {}) => {
  const counts = { done: 23, open: 15, all: 38 };
  const total = counts[filter];
  return { run: "run-A", filter, total, size: 10, page: n, pages: Math.ceil(total / 10),
    counts, totals: { translated: 18, partial: 1, has_subs: 1, untranslated: 1, failed: 1, restore_failed: 1 },
    focus: "LMNO-open-1.mp4", films: Array.from({ length: Math.min(10, total - (n - 1) * 10) },
      (_, i) => row(`LMNO-${filter}-${(n - 1) * 10 + i + 1}.mp4`,
        filter === "open" ? { restore: "active", asr: "pending", translate: "pending", outcome: null,
          duration_s: null, finished_at: null, percent: 34 } : {})), ...extra };
};
const smallPage = (films: RunFilm[], filter: RunFilter = "all") => {
  const done = films.filter(r => r.outcome !== null).length;
  return page(filter, 1, { films, total: films.length, pages: 1,
    counts: { done, open: films.length - done, all: films.length },
    totals: { translated: done, partial: 0, has_subs: 0, untranslated: 0, failed: 0, restore_failed: 0 } });
};
const metrics = { film: "PQRS-current.mp4", steps: [{ key: "restore", state: "active" }],
  films: ["PQRS-fallback-1.mp4", "PQRS-fallback-2.mp4"].map(name => ({ name, steps: { restore: "pending" }, status: "pending" })), films_more: 23 };
const response = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 404, json: async () => body });
function deferred() {
  let resolve!: (value: ReturnType<typeof response>) => void;
  const promise = new Promise<ReturnType<typeof response>>(r => { resolve = r; });
  return { resolve, promise };
}
let serve: (url: URL) => ReturnType<typeof response> | Promise<ReturnType<typeof response>>;
let qc: QueryClient;
let requests: URL[];
const wrapper = ({ children }: { children: React.ReactNode }) => <ThemeProvider theme={theme}>
  <QueryClientProvider client={qc}>{children}</QueryClientProvider></ThemeProvider>;
const mount = () => render(<MonitorMetrics metrics={metrics} taskName="jasna/main" />, { wrapper });
const card = () => within(screen.getByRole("region", { name: /本轮影片|This run’s films/ }));
const filterButton = (filter: RunFilter) => card().getByRole("button", {
  name: filter === "done" ? /^(已完成|Completed) / : filter === "open" ? /^(未完成|Unfinished) / : /^(全部|All) /,
});
const next = () => card().getByRole("button", { name: /下一页|Next/ });
const prev = () => card().getByRole("button", { name: /上一页|Previous/ });
const pager = (n: number, total = 23) => `第 ${n} / ${Math.ceil(total / 10)} 页 · 共 ${total} 部`;
const poll = async () => { await act(async () => { await qc.invalidateQueries({ queryKey: ["runFilms"] }); }); };

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  requests = [];
  serve = url => response(page(url.searchParams.get("filter") as RunFilter, Number(url.searchParams.get("page"))));
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query.includes("min-width"), media: query,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn() })));
  vi.stubGlobal("fetch", vi.fn((input: string) => {
    const url = new URL(input); requests.push(url);
    return Promise.resolve(serve(url));
  }));
});
afterEach(async () => { cleanup(); qc.clear(); vi.useRealTimers(); vi.unstubAllGlobals(); await i18n.changeLanguage("zh-CN"); });

describe("RunFilmsCard #200", () => {
  it("does not import the undeclared MUI utils dependency from src", () => {
    const sources = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
    for (const [path, source] of Object.entries(sources)) {
      expect(source, path).not.toMatch(/(?:from\s*|import\s*\(\s*)["']@mui\/utils(?:\/[^"']*)?["']/);
    }
  });

  it("defaults to completed; consumes the agent URL, counts, six columns and done totals", async () => {
    mount(); await screen.findByText("本轮影片");
    expect(requests[0].origin).toBe("http://127.0.0.1:5681");
    expect(requests[0].pathname).toBe("/control/monitors/run-films");
    expect(Object.fromEntries(requests[0].searchParams)).toEqual({ name: "jasna/main", filter: "done", page: "1", size: "10" });
    expect(filterButton("done")).toHaveTextContent("已完成 23");
    expect(filterButton("done")).toHaveAttribute("aria-pressed", "true");
    expect(filterButton("open")).toHaveTextContent("未完成 15");
    expect(filterButton("all")).toHaveTextContent("全部 38");
    expect(filterButton("all")).toHaveAttribute("aria-pressed", "false");
    expect(card().getByText("已完成：最新完成的在前")).toBeInTheDocument();
    expect(card().getAllByRole("columnheader").map(el => el.textContent)).toEqual(["片名", "修复", "翻译", "用的模型", "用时", "完成于"]);
    for (const label of ["翻译完成 18", "部分保留日文 1", "已有字幕 1", "未翻译 1", "失败 1", "修复失败 1"]) {
      expect(card().getByText(label)).toBeInTheDocument();
    }
    expect(prev()).toBeDisabled(); expect(next()).toBeEnabled();
  });

  it("preserves each server order and highlights data.focus; filter changes reset page 1", async () => {
    const orders = { done: ["LMNO-new.mp4", "LMNO-old.mp4"], open: ["PQRS-focus.mp4", "PQRS-queue.mp4"],
      all: ["PQRS-focus.mp4", "PQRS-queue.mp4", "LMNO-new.mp4", "LMNO-old.mp4"] };
    mount(); await screen.findByText(pager(1)); fireEvent.click(next()); await screen.findByText(pager(2));
    serve = url => {
      const f = url.searchParams.get("filter") as RunFilter;
      return response({ ...smallPage(orders[f].map(name => row(name, name.startsWith("PQRS") ? { outcome: null } : {})), f), focus: "PQRS-focus.mp4" });
    };
    for (const f of ["open", "all", "done"] as const) {
      fireEvent.click(filterButton(f)); await waitFor(() => expect(filterButton(f)).toHaveAttribute("aria-pressed", "true"));
      expect(card().getAllByTestId("run-film-row").map(r => within(r).getAllByRole("cell")[0].textContent)).toEqual(orders[f]);
      expect(requests.at(-1)?.searchParams.get("page")).toBe("1");
      if (f !== "done") expect(card().getAllByTestId("run-film-row")[0]).toHaveAttribute("aria-current", "true");
    }
  });

  it("renders the answered filter while transitions disable filters and pager, but polls do not", async () => {
    mount(); await screen.findByText(pager(1));
    const pending = deferred(); serve = () => pending.promise;
    fireEvent.click(filterButton("open"));
    expect(filterButton("done")).toHaveAttribute("aria-pressed", "true");
    for (const f of ["done", "open", "all"] as const) expect(filterButton(f)).toBeDisabled();
    expect(next()).toBeDisabled();
    expect(card().getByText("已完成：最新完成的在前")).toBeInTheDocument();
    await act(async () => { pending.resolve(response(page("all"))); });
    await waitFor(() => expect(filterButton("all")).toHaveAttribute("aria-pressed", "true"));
    expect(card().getByText("全部：未完成在前，已完成在后")).toBeInTheDocument();
    expect(card().getByText("LMNO-all-1.mp4")).toBeInTheDocument();
    await waitFor(() => expect(requests.at(-1)?.searchParams.get("filter")).toBe("all"));
    const refresh = deferred(); serve = () => refresh.promise;
    let polling!: Promise<void>;
    act(() => { polling = qc.invalidateQueries({ queryKey: ["runFilms"] }); });
    for (const f of ["done", "open", "all"] as const) expect(filterButton(f)).toBeEnabled();
    expect(next()).toBeEnabled();
    await act(async () => { refresh.resolve(response(page("all"))); await polling; });
  });

  it.each(["filter", "page"])("V3-1: failed %s restores both coordinates and the next identical click retries", async change => {
    mount(); await screen.findByText(pager(1)); fireEvent.click(next()); await screen.findByText(pager(2));
    serve = () => response({}, false);
    fireEvent.click(change === "filter" ? filterButton("open") : next());
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    await waitFor(() => expect(next()).toBeEnabled());
    expect(card().getByText(pager(2))).toBeInTheDocument();
    expect(filterButton("done")).toHaveAttribute("aria-pressed", "true");
    expect(requests.at(-1)?.searchParams.get("filter")).toBe("done");
    expect(requests.at(-1)?.searchParams.get("page")).toBe("2");
    serve = url => response(page(url.searchParams.get("filter") as RunFilter, Number(url.searchParams.get("page"))));
    const before = requests.length;
    fireEvent.click(change === "filter" ? filterButton("open") : next());
    await screen.findByText(change === "filter" ? pager(1, 15) : pager(3));
    expect(requests.length).toBeGreaterThan(before);
    if (change === "page") expect(next()).toBeDisabled();
  });

  it("recovery stays enabled even with a pending poll; abandoned recovery cache cannot flash on revisit", async () => {
    mount(); await screen.findByText(pager(1));
    const failed = deferred(); serve = () => failed.promise; fireEvent.click(filterButton("open"));
    await waitFor(() => expect(qc.getQueryCache().find({ queryKey: ["runFilms", "jasna/main", "done", 1] })).toBeUndefined());
    const recovery = deferred(); serve = () => recovery.promise;
    await act(async () => { failed.resolve(response({}, false)); });
    await waitFor(() => expect(requests.filter(u => u.searchParams.get("filter") === "done")).toHaveLength(2));
    expect(filterButton("open")).toBeEnabled(); expect(next()).toBeEnabled();
    serve = () => response(page("open")); fireEvent.click(filterButton("open")); await screen.findByText(pager(1, 15));
    await act(async () => { recovery.resolve(response(page())); });
    const revisit = deferred(); serve = () => revisit.promise; fireEvent.click(filterButton("done"));
    expect(card().queryByText("LMNO-done-1.mp4")).toBeNull();
    expect(card().getByText("LMNO-open-1.mp4")).toBeInTheDocument();
    expect(filterButton("done")).toBeDisabled();
    await act(async () => { revisit.resolve(response(page())); });
    await screen.findByText(pager(1));
  });

  it("new run resets page to 1 and keeps the filter; clamped pages normalize the next request", async () => {
    mount(); await screen.findByText(pager(1)); fireEvent.click(filterButton("open")); await screen.findByText(pager(1, 15));
    fireEvent.click(next()); await screen.findByText(pager(2, 15));
    serve = url => response(page("open", Number(url.searchParams.get("page")), { run: "run-B" }));
    await poll(); await screen.findByText(pager(1, 15));
    expect(requests.at(-1)?.searchParams.get("page")).toBe("1");
    expect(filterButton("open")).toHaveAttribute("aria-pressed", "true");
    serve = () => response(page("open", 1, { run: "run-B" })); fireEvent.click(next());
    await waitFor(() => expect(next()).toBeEnabled());
    serve = () => response(page("open", 2, { run: "run-B" })); fireEvent.click(next());
    await screen.findByText(pager(2, 15));
  });

  it("polls every 5 seconds and clears cache on unmount; failures retain last good rows", async () => {
    vi.useFakeTimers(); const view = mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    serve = () => response({}, false);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(requests).toHaveLength(2); expect(card().getByText(pager(1))).toBeInTheDocument(); expect(next()).toBeEnabled();
    view.unmount(); await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(requests).toHaveLength(2); expect(qc.getQueryCache().findAll({ queryKey: ["runFilms"] })).toHaveLength(0);
  });

  it("task changes and rapid reselect start from that task's capped fallback", async () => {
    const fallback = readPipeline(metrics)!;
    const view = render(<RunFilmsCard key="A" name="A" fallback={fallback} />, { wrapper });
    await screen.findByText(pager(1));
    const pending = deferred(); serve = () => pending.promise;
    view.rerender(<RunFilmsCard key="B" name="B" fallback={fallback} />);
    expect(screen.queryByText("LMNO-done-1.mp4")).toBeNull();
    expect(screen.getByText("还有 23 部")).toBeInTheDocument();
    view.rerender(<RunFilmsCard key="A" name="A" fallback={fallback} />);
    expect(screen.queryByText("LMNO-done-1.mp4")).toBeNull();
    expect(screen.queryByText("本轮影片")).toBeNull();
  });

  it.each(["pending", "404", "malformed", "zero"])("plain capped fallback with no new header/filter/totals (%s)", async mode => {
    const pending = deferred();
    serve = () => mode === "pending" ? pending.promise : response(mode === "zero" ? smallPage([], "done") : {}, mode !== "404");
    mount();
    if (mode !== "pending") await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(screen.getByText("还有 23 部")).toBeInTheDocument();
    expect(screen.getByText("PQRS-fallback-1.mp4")).toBeInTheDocument();
    expect(screen.queryByText("本轮影片")).toBeNull();
    expect(screen.queryByRole("button", { name: /已完成/ })).toBeNull();
  });

  it("a malformed transition retains good data and recovers; zero counts after success uses fallback", async () => {
    mount(); await screen.findByText(pager(1));
    serve = () => response({ ...page("open"), counts: { done: true, open: 15, all: 38 } });
    fireEvent.click(filterButton("open")); await waitFor(() => expect(qc.isFetching()).toBe(0));
    await waitFor(() => expect(filterButton("open")).toBeEnabled());
    expect(card().getByText(pager(1))).toBeInTheDocument();
    serve = () => response(smallPage([], "done")); await poll();
    expect(await screen.findByText("还有 23 部")).toBeInTheDocument();
    expect(screen.queryByText("本轮影片")).toBeNull();
  });

  it.each(["zh-CN", "en"])("empty states, pager and accessible labels (%s)", async lang => {
    await i18n.changeLanguage(lang);
    serve = url => response(smallPage([], url.searchParams.get("filter") as RunFilter));
    render(<RunFilmsCard name="empty" />, { wrapper });
    await screen.findByText(lang === "en" ? "No completed films this run" : "本轮还没有完成的影片");
    fireEvent.click(filterButton("open"));
    await screen.findByText(lang === "en" ? "No unfinished films" : "没有未完成的影片");
    fireEvent.click(filterButton("all"));
    await screen.findByText(lang === "en" ? "No films this run" : "本轮还没有影片");
    expect(card().queryByRole("button", { name: /Next|下一页/ })).toBeNull();
    serve = () => response(page("all")); await poll();
    expect(card().getByText(lang === "en" ? "Page 1 / 4 · 38 films" : pager(1, 38))).toBeInTheDocument();
    expect(next()).toHaveAccessibleName(lang === "en" ? "Next" : "下一页");
  });

  it("avsubs still uses #198; Hub/no name keeps capped FilmList; AV-off does not mount", async () => {
    serve = () => response({}, false);
    const av = render(<MonitorMetrics metrics={{ ...metrics, steps: [{ key: "asr", state: "active" }] }} taskName="av" />, { wrapper });
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(requests.map(u => u.pathname)).toEqual(["/control/monitors/films"]); av.unmount(); requests = [];
    const hub = render(<MonitorMetrics metrics={metrics} />, { wrapper });
    expect(screen.getByText("还有 23 部")).toBeInTheDocument(); expect(requests).toHaveLength(0); hub.unmount();
    render(<MonitorMetrics metrics={{ queue_total: 1 }} taskName="jasna" />, { wrapper });
    expect(requests).toHaveLength(0); expect(screen.queryByText("本轮影片")).toBeNull();
  });
});

const outcomes = [
  ["translated", "翻译完成", "Translated"], ["partial", "部分保留日文 · 3 句", "Partly kept in Japanese · 3 lines"],
  ["no_speech", "未翻译 · 无语音", "Not translated · no speech"], ["has_subs", "已有字幕", "Already had subtitles"],
  ["restore_failed", "未开始（修复失败）", "Not started (restore failed)"], ["asr_failed", "识别失败", "Recognition failed"],
  ["failed", "翻译失败", "Translation failed"], ["future-code", "其它", "Other"], ["skipped:future", "其它", "Other"],
  ["skipped:no_llm_key", "跳过 · 未配置翻译模型", "Skipped · no translation model configured"],
  ["skipped:translation_paused", "跳过 · 翻译服务不可用，可续", "Skipped · translation service unavailable, resumable"],
  ["skipped:subtitle_exists", "跳过 · 已有字幕", "Skipped · already had subtitles"],
  ["skipped:transcript_exists", "跳过 · 已有日文字幕", "Skipped · already had Japanese subtitles"],
  ["skipped:unreadable", "跳过 · 字幕状态无法读取", "Skipped · cannot read subtitle state"],
  ["skipped:unstable", "跳过 · 文件还在写入", "Skipped · file is still being written"],
  ["skipped:cancelled", "跳过 · 本轮已关闭 AV 翻译", "Skipped · AV translation disabled for this run"],
  ["skipped:no_exe", "跳过 · 未安装 WhisperJAV", "Skipped · WhisperJAV is not installed"],
  ["skipped:planning_failed", "跳过 · 无法列出输出目录", "Skipped · cannot list output directory"],
  ["skipped:other", "跳过 · 其它原因", "Skipped · other reason"],
] as const;
describe.each(["zh-CN", "en"])("run film cells (%s)", lang => {
  it.each(outcomes)("outcome %s", async (code, zh, en) => {
    await i18n.changeLanguage(lang);
    serve = () => response(smallPage([row("ABC.mp4", { outcome: code, kept_ja: 3 })]));
    mount(); const r = await screen.findByTestId("run-film-row");
    expect(within(r).getByText(lang === "en" ? en : zh)).toBeInTheDocument();
    expect(r.querySelector(".MuiChip-root")).not.toBeNull();
  });

  it("restore and open translation variants, model descriptions, missing values", async () => {
    await i18n.changeLanguage(lang);
    const rows = [
      row("ABC-1", { restored_before: true, models: [["model-a · host.invalid", 12], ["model-b · second.invalid", 3]] }),
      row("ABC-2", { restore: "failed", outcome: "restore_failed", duration_s: null, finished_at: null }),
      row("ABC-3", { restore: "active", asr: "pending", translate: "pending", percent: 34, outcome: null }),
      row("ABC-4", { restore: "waiting_gpu", asr: "pending", translate: "pending", outcome: null }),
      row("ABC-5", { restore: "queued", asr: "pending", translate: "pending", outcome: null }),
      row("ABC-6", { restore: "pending", asr: "active", translate: "pending", percent: 42, outcome: null }),
      row("ABC-7", { translate: "active", percent: 56, outcome: null }),
      row("ABC-8", { restore: "skipped", asr: "active", translate: "pending", outcome: null }),
      row("ABC-9", { translate: "active", outcome: null }),
    ];
    serve = () => response(smallPage(rows)); mount(); await screen.findByText("ABC-1");
    const text = card();
    for (const label of lang === "en" ? ["Restored before this run", "Failed", "Restoring 34%", "Waiting for GPU", "Queued", "Recognizing 42%", "Translating 56%", "Recognizing", "Translating", "Done", "Skipped", "Waiting"]
      : ["本轮前已修复", "失败", "修复中 34%", "等待 GPU", "排队", "识别中 42%", "翻译中 56%", "识别中", "翻译中", "完成", "跳过", "等待"]) {
      expect(text.getAllByText(label).length).toBeGreaterThan(0);
    }
    const model = text.getByText(lang === "en" ? "model-a · 12 lines" : "model-a · 12 句");
    expect(model).toHaveAccessibleDescription("model-a · host.invalid");
    expect(model.textContent).not.toContain("host.invalid");
    expect(text.getByText("model-b · second.invalid")).toBeInTheDocument();
    expect(within(text.getAllByTestId("run-film-row")[1]).getAllByText("—")).toHaveLength(3);
  });

  it("formats elapsed durations and local dates", async () => {
    await i18n.changeLanguage(lang);
    const t = i18n.t.bind(i18n);
    expect([4320, 3480, 59, 0, null].map(s => runDuration(s, t))).toEqual(lang === "en"
      ? ["1 h 12 min", "58 min", "Less than 1 min", "Less than 1 min", "—"] : ["1 小时 12 分", "58 分", "不到 1 分", "不到 1 分", "—"]);
    const now = new Date(2026, 8, 26, 15);
    expect(runFinishedAt(new Date(2026, 8, 26, 9, 5).getTime() / 1000, now)).toBe("09:05");
    expect(runFinishedAt(new Date(2026, 8, 25, 23, 7).getTime() / 1000, now)).toBe("9/25 23:07");
    expect(runFinishedAt(new Date(2025, 8, 26, 9, 5).getTime() / 1000, now)).toBe("9/26 09:05");
    expect(runFinishedAt(null, now)).toBe("—");
  });

  it("375px uses stacked labelled rows with reachable full models and wrapping controls", async () => {
    await i18n.changeLanguage(lang);
    vi.stubGlobal("innerWidth", 375);
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: false, media: query,
      addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })));
    serve = () => response(page("done", 1, { films: page().films.map(r => ({ ...r,
      name: `${r.name}-${"long".repeat(40)}`, models: [["model-a · host.invalid", 12]] })) }));
    mount(); await screen.findByText("本轮影片").catch(() => screen.findByText("This run’s films"));
    expect(card().queryByRole("table")).toBeNull();
    const first = card().getAllByTestId("run-film-row")[0];
    expect(first.querySelector("dl")).not.toBeNull();
    expect(Array.from(first.querySelectorAll("dt")).map(el => el.textContent)).toEqual(lang === "en"
      ? ["Restore: ", "Translation: ", "Models: ", "Duration: ", "Finished at: "] : ["修复：", "翻译：", "模型：", "用时：", "完成于："]);
    expect(within(first).getByText(lang === "en" ? "model-a · 12 lines" : "model-a · 12 句")).toHaveAccessibleDescription("model-a · host.invalid");
    expect(getComputedStyle(first).overflowWrap).toBe("anywhere");
    expect(getComputedStyle(next().parentElement!).flexWrap).toBe("wrap");
    expect(getComputedStyle(filterButton("all")).minHeight).toBe("40px");
  });
});

describe("strict response reader", () => {
  it.each([{}, { ...page(), total: true }, { ...page(), filter: "bad" }, { ...page(), size: 5 },
    { ...page(), page: 0 }, { ...page(), pages: 9 }, { ...page(), films: [] },
    { ...page(), counts: { done: 23, open: 15, all: 37 } }, { ...page(), totals: {} },
    ...[{ restore: "bad" }, { restored_before: 1 }, { percent: -1 }, { percent: "2" }, { duration_s: Infinity },
      { kept_ja: -1 }, { models: [["m", true]] }, { outcome: 123 }, { finished_at: "today" }]
      .map(extra => smallPage([row("ABC", extra as Partial<RunFilm>)])),
  ])("rejects malformed body %#", body => { expect(() => readRunFilms(body)).toThrow(); });
  it("accepts unknown outcomes and clamps progress; locale keys have parity", () => {
    expect(readRunFilms(smallPage([row("ABC", { outcome: "future", percent: 125 })])).films[0].percent).toBe(100);
    const keys = (value: object, prefix = ""): string[] => Object.entries(value).flatMap(([key, v]) =>
      typeof v === "object" ? keys(v, `${prefix}${key}.`) : [`${prefix}${key}`]);
    expect(keys(i18n.getResource("en", "translation", "runFilms"))).toEqual(keys(i18n.getResource("zh-CN", "translation", "runFilms")));
  });
});
