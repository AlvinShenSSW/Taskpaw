import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { TaskLog, TaskLogRows } from "../components/TaskLog";
import { compareLogIds, localLogDay, logText, renderLogSentence } from "../components/TaskLog.helpers";
import { api, type LogEntry } from "../api";
import i18n, { setLang } from "../i18n";
import { theme } from "../theme";

const entry = (id = "20260926-9", kind = "task.started", data = {}): LogEntry => ({
  v: 1, id, kind, ts: `${id.slice(0, 4)}-${id.slice(4, 6)}-${id.slice(6, 8)}T12:34:56+09:00`,
  task: "main/任务", task_type: "jasna", severity: "info", film: "movie.mkv", data,
});
const catalog: [string, string, string][] = [
  ["agent.started", "Agent", "代理"], ["agent.stopping", "stopping", "停止"],
  ["operator.start", "started", "启动"], ["operator.stop", "stopped", "停止"],
  ["operator.add", "added", "添加"], ["operator.remove", "removed", "移除"], ["operator.update", "updated", "更新"],
  ["task.started", "started", "开始"], ["task.done", "finished", "完成"], ["task.aborted", "aborted", "终止"],
  ["task.error", "failed", "错误"], ["task.interrupted", "interrupted", "中断"],
  ["task.gpu_wait", "GPU", "GPU"], ["task.gpu_acquired", "GPU", "GPU"],
  ["restore.started", "Restoration started", "开始修复"], ["restore.finished", "Restoration finished", "修复完成"],
  ["restore.failed", "Restoration failed", "修复失败"], ["restore.retry", "Retrying", "重试"], ["restore.skipped", "skipped", "跳过"],
  ["asr.started", "Speech recognition started", "开始语音识别"], ["asr.finished", "Speech recognition finished", "语音识别完成"], ["asr.retry", "Retrying", "重试"],
  ["translate.started", "Translation started", "开始翻译"], ["translate.switched", "unavailable", "暂时不可用"],
  ["translate.refused", "refused", "拒绝"], ["translate.provider_down", "unavailable", "不可用"], ["translate.provider_up", "available", "恢复可用"],
  ["translate.deferred", "deferred", "延后"], ["translate.paused", "paused", "暂停"], ["translate.resumed", "resumed", "继续"],
  ["translate.finished", "Translation finished", "翻译完成"], ["subs.published", "Subtitles published", "字幕已发布"],
  ["subs.skipped", "skipped", "跳过"], ["subs.failed", "failed", "失败"], ["subs.skipped_bulk", "skipped", "跳过"],
  ["event.mirrored", "Alert", "提醒"], ["event.suppressed", "suppressed", "省略"],
];

beforeEach(() => setLang("en"));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); setLang("zh-CN"); });

describe("log sentences", () => {
  it.each(catalog)("renders %s in both languages from structured data", (kind, en, zh) => {
    const e = entry(undefined, kind, { reason: "unavailable", from: "grok-4.3", to: "deepseek-chat", title: "Alert", queued: 3, done: 2, skipped: 1 });
    for (const [lang, expected] of [["en", en], ["zh-CN", zh]] as const) {
      setLang(lang);
      const sentence = renderLogSentence(e, i18n.t);
      expect(sentence).toContain(expected);
      expect(sentence).not.toMatch(/undefined|\{\{|logs\./);
    }
  });
  it("explains unavailable, recovered, changed, unclean, and inferred interruption", () => {
    setLang("zh-CN");
    expect(renderLogSentence(entry(undefined, "translate.switched", { from: "grok-4.3", to: "deepseek-chat", reason: "unavailable" }), i18n.t))
      .toBe("grok-4.3 暂时不可用，改用 deepseek-chat");
    expect(renderLogSentence(entry(undefined, "translate.switched", { from: "a", to: "b", reason: "recovered" }), i18n.t)).toContain("恢复");
    expect(renderLogSentence(entry(undefined, "translate.switched", { from: "a", to: "b", reason: "changed" }), i18n.t)).toContain("设置");
    expect(renderLogSentence(entry(undefined, "agent.started", { previous_exit: "unclean" }), i18n.t)).toContain("上次非正常退出");
    expect(renderLogSentence(entry(undefined, "task.interrupted", { reconstructed: true, step: "restore" }), i18n.t)).toContain("可能中断");
  });
  it("orders numeric suffixes and dates, and exports localized details without secret fields", () => {
    expect(compareLogIds("20260926-900", "20260926-1000")).toBeLessThan(0);
    expect(compareLogIds("20260927-1", "20260926-1000")).toBeGreaterThan(0);
    const e = { ...entry(undefined, "restore.failed", { exit_code: 2, tail: "failed", api_key: "SECRET", argv: "SECRET" }), pid: 42, proc: "lada" };
    const text = logText([e], i18n.t);
    expect(text).toContain("2026-09-26");
    expect(text).toContain("12:34:56");
    expect(text).toContain("Exit code: 2");
    expect(text).toContain("PID: 42");
    expect(text).not.toContain("SECRET");
    expect(renderLogSentence(entry(undefined, "future.kind", { model: "model-A", token: "SECRET" }), i18n.t)).toContain("future.kind");
  });
  it("redacts credential forms in titles, films, tails and exports, and renders text literally", () => {
    const e = { ...entry(undefined, "event.mirrored", { title: "<img src=x onerror=alert(1)> token=SECRET", message: "Bearer SECRET", tail: "https://name:SECRET@example.test sk-SECRET", custom_command: "SECRET" }), film: "api_key=SECRET" };
    render(<TaskLogRows entries={[e]} />);
    expect(document.querySelector("img")).toBeNull();
    expect(logText([e], i18n.t)).not.toContain("SECRET");
    expect(logText([e], i18n.t)).toContain("[redacted]");
  });
  it("retains no-call translation, model counts, no-speech and at-stop facts", () => {
    expect(renderLogSentence(entry(undefined, "translate.started", { model: null, lines: 4, resumed: 4 }), i18n.t)).toContain("no provider call");
    expect(renderLogSentence(entry(undefined, "translate.finished", { lines: 4, by_model: { grok: 3, deepseek: 1 }, kept_ja: 0, duration: 5 }), i18n.t)).toContain("grok: 3, deepseek: 1");
    expect(renderLogSentence(entry(undefined, "subs.published", { srt: "film.srt", no_speech: true }), i18n.t)).toContain("no speech");
    expect(logText([entry(undefined, "restore.failed", { exit_code: 5, at_stop: true })], i18n.t)).toContain("Failed when stopped: Yes");
  });
});

function stub(handler: (p: URLSearchParams) => unknown = () => ({})) {
  const fetcher = vi.fn((url: string) => {
    const p = new URL(url).searchParams;
    const body = { ...(p.has("days") ? { boot: "boot-1", days: [{ day: "20260924", count: 2 }] }
      : { boot: "boot-1", entries: [], next_before: null }), ...handler(p) as object };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}
const show = () => render(<ThemeProvider theme={theme}><TaskLog tasks={["main/任务", "other"]} /></ThemeProvider>);
const select = (name: string, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });

describe("TaskLog", () => {
  it("encodes query values and consumes the retained-days shape", async () => {
    const fetcher = stub();
    await api.logs({ day: "20260926", task: "任务 / &", severity: "info,error", q: "a+b & c", before: "20260926-1000", limit: 500 });
    const p = new URL(fetcher.mock.calls[0][0]).searchParams;
    expect(Object.fromEntries(p)).toEqual({ day: "20260926", task: "任务 / &", severity: "info,error", q: "a+b & c", before: "20260926-1000", limit: "500" });
    expect((await api.logDays()).days).toEqual([{ day: "20260924", count: 2 }]);
  });
  it("renders compact alerts, date/time, and expands the original alert and process details", () => {
    render(<TaskLogRows entries={[{ ...entry(undefined, "event.mirrored", { title: "Alert title", message: "Original alert text", exit_code: 2 }), pid: 123, proc: "lada" }]} />);
    expect(screen.getByText("2026-09-26")).toBeInTheDocument();
    expect(screen.getByText("12:34:56")).toBeInTheDocument();
    expect(screen.getByTestId("log-row-20260926-9")).toHaveAttribute("data-compact", "true");
    expect(screen.queryByText("Original alert text")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Details/ }));
    expect(screen.getByText("Original alert text")).toBeInTheDocument();
    expect(screen.getByText("123")).toBeInTheDocument();
  });
  it("requests server-side filters/search, selects days, and loads older pages", async () => {
    const fetcher = stub(p => ({ entries: [entry(p.has("before") ? "20260926-8" : "20260926-9")], next_before: p.has("before") ? null : "20260926-9" }));
    show();
    await screen.findByText("Task started: 0 queued, 0 done, 0 skipped");
    select("Task", "main/任务");
    select("Severity", "warn,error");
    fireEvent.change(screen.getByLabelText("Search film / model / title"), { target: { value: "grok" } });
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => {
      const p = new URL(url).searchParams;
      return p.get("task") === "main/任务" && p.get("severity") === "warn,error" && p.get("q") === "grok";
    })).toBe(true));
    select("Day", "20260924");
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("day") === "20260924")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Load earlier" }));
    await waitFor(() => expect(screen.getAllByTestId(/^log-row/)).toHaveLength(2));
    expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("before") === "20260926-9")).toBe(true);
  });
  it("polls after the largest numeric id, dedupes across midnight, and stops on unmount", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T23:59:59"));
    const fetcher = stub(p => ({ entries: p.has("after")
      ? [entry("20260926-1000"), entry("20260927-1")]
      : [entry("20260926-1000"), entry("20260926-900")] }));
    const view = show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("after") === "20260926-1000")).toBe(true);
    expect(screen.getAllByTestId(/^log-row/)).toHaveLength(3);
    expect(screen.getAllByTestId(/^log-row/)[0]).toHaveAttribute("data-testid", "log-row-20260927-1");
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("after") === "20260927-1")).toBe(true);
    view.unmount(); const count = fetcher.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetcher).toHaveBeenCalledTimes(count);
  });
  it("reloads on boot changes even when an id is reused", async () => {
    vi.useFakeTimers();
    let restarted = false;
    stub(p => {
      if (p.has("after")) restarted = true;
      return { boot: restarted ? "boot-2" : "boot-1", entries: [entry(undefined, restarted ? "agent.stopping" : "task.started")] };
    });
    show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText("Agent is stopping")).toBeInTheDocument();
    expect(screen.queryByText(/Task started:/)).not.toBeInTheDocument();
  });
  it("ignores a late response from a previous day selection", async () => {
    let finish: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const p = new URL(url).searchParams;
      if (p.get("day") === localLogDay()) return new Promise(resolve => { finish = resolve; });
      const body = p.has("days") ? { boot: "boot-1", days: [] } : { boot: "boot-1", entries: [entry("20260925-1", "agent.stopping")], next_before: null };
      return Promise.resolve({ ok: true, json: async () => body });
    }));
    show();
    select("Day", localLogDay(-1));
    await screen.findByText("Agent is stopping");
    await act(async () => { finish!({ ok: true, json: async () => ({ boot: "boot-1", entries: [entry()], next_before: null }) }); });
    expect(screen.queryByText(/Task started:/)).not.toBeInTheDocument();
    expect(screen.getByText("Agent is stopping")).toBeInTheDocument();
  });
  it("starts polling an empty day at zero and drains a full oldest-first page", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T12:00:00"));
    const fetcher = stub(p => {
      const after = p.get("after");
      if (after === "20260926-0") return { entries: Array.from({ length: 500 }, (_, i) => entry(`20260926-${i + 1}`)) };
      if (after === "20260926-500") return { entries: [entry("20260926-501")] };
      return {};
    });
    show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("after") === "20260926-500")).toBe(true);
    expect(screen.getAllByTestId(/^log-row/)).toHaveLength(501);
  });
  it("exports all filtered pages to a localized txt blob", async () => {
    const fetcher = stub(p => ({ entries: [entry(p.has("before") ? "20260926-8" : "20260926-9")], next_before: p.has("before") ? null : "20260926-9" }));
    let output: Blob | undefined;
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL(blob: Blob) { output = blob; return "blob:test"; }
      static revokeObjectURL = vi.fn();
    });
    const downloads: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download); });
    show(); await screen.findByText(/Task started:/);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(downloads[0]).toMatch(/taskpaw-\d{8}\.txt$/);
    const contents = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(output!); });
    expect(contents.match(/Task started:/g)).toHaveLength(2);
    expect(fetcher.mock.calls.some(([url]) => { const p = new URL(url).searchParams; return p.get("limit") === "500" && p.has("before"); })).toBe(true);
  });
  it("shows empty and safe error states with retry", async () => {
    stub(); const view = show();
    expect(await screen.findByText("No log entries match these filters.")).toBeInTheDocument();
    view.unmount();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("SECRET")));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load logs");
    expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
    expect(within(screen.getByRole("alert")).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    stub(() => ({ entries: [entry()] }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Task started:/)).toBeInTheDocument();
  });
  it("caps export at 20,000 rows and reports the cap in Chinese", async () => {
    setLang("zh-CN");
    let pages = 0;
    stub(p => {
      if (p.get("limit") !== "500") return {};
      const start = 30000 - pages++ * 500;
      return { entries: Array.from({ length: 500 }, (_, i) => entry(`20260926-${start - i}`, "agent.stopping")), next_before: `20260926-${start - 499}` };
    });
    let output: Blob | undefined;
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL(blob: Blob) { output = blob; return "blob:test"; }
      static revokeObjectURL = vi.fn();
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    show(); await screen.findByText("没有符合筛选条件的日志。");
    fireEvent.click(screen.getByRole("button", { name: "导出" }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(pages).toBe(40);
    expect(screen.getByRole("alert")).toHaveTextContent("20,000");
    const contents = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(output!); });
    expect(contents.match(/代理正在停止/g)).toHaveLength(20000);
  });
  it("abandons export on a restart and reloads the view", async () => {
    let boot = "boot-1";
    stub(p => {
      if (p.get("limit") === "500") boot = "boot-2";
      return { boot, entries: [entry(undefined, boot === "boot-1" ? "task.started" : "agent.stopping")] };
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    show(); await screen.findByText(/Task started:/);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("agent restarted during export");
    expect(await screen.findByText("Agent is stopping")).toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
  });
  it("shows an export failure without leaking server errors", async () => {
    stub(); show(); await screen.findByText("No log entries match these filters.");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("SECRET")));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not export logs");
    expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
  });
});
