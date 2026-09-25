import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { TaskLog, TaskLogRows } from "../components/TaskLog";
import { compareLogIds, localLogDay, logDetails, logText, renderLogSentence } from "../components/TaskLog.helpers";
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

// Exact data shapes from the producer call sites, without invented fallback keys.
const producerData: Record<string, Record<string, unknown>> = {
  "agent.started": { version: "3.9.0", previous_exit: "first" }, "agent.stopping": {},
  "operator.start": {}, "operator.stop": {}, "operator.add": {}, "operator.remove": {}, "operator.update": { fields: ["poll_interval"] },
  "task.started": { queued: 3, done: 2, skipped: 1 },
  "task.done": { done: 3, failed: 1, skipped: 2, duration: 12.3456, kept_ja: 4, paused: 1 },
  "task.aborted": { reason: "child_failed", exit_code: 7 }, "task.error": { reason: "no_exe" },
  "task.interrupted": { step: "restore", elapsed: 12.3456 },
  "task.gpu_wait": { holder: "other task" }, "task.gpu_acquired": {},
  "restore.started": { index: 1, total: 3, mode: "plain" }, "restore.finished": { output: "LMNO-123.mp4", duration: 12.3456 },
  "restore.failed": { exit_code: 7, tail: "failed" }, "restore.retry": { mode: "plain" }, "restore.skipped": { reason: "already restored", count: 2 },
  "asr.started": { engine: "faster-whisper" }, "asr.finished": { lines: 17, duration: 12.3456 }, "asr.retry": {},
  "translate.started": { model: "model-A", lines: 17, resumed: 2 },
  "translate.switched": { from: "model-A", to: "model-B", reason: "unavailable", kind: "network" },
  "translate.refused": { model: "model-A", lines: 17 },
  "translate.provider_down": { model: "model-A", reason: "network", minutes: 30 }, "translate.provider_up": { model: "model-A" },
  "translate.deferred": { lines: 17 }, "translate.paused": { minutes: 120 }, "translate.resumed": {},
  "translate.finished": { lines: 17, by_model: { "model-A": 17 }, kept_ja: 2, duration: 12.3456 },
  "subs.published": { srt: "LMNO-123.srt" }, "subs.skipped": { reason: "no_llm_key" }, "subs.failed": { detail: "asr failed", step: "asr" },
  "subs.skipped_bulk": { count: 3, reason: "cancelled" },
  "event.mirrored": { level: "alert", title: "Alert", message: "Original alert" }, "event.suppressed": { count: 17 },
};
const reasonCodes = [
  "setup_or_launch_failed", "child_failed", "ffprobe_missing", "already restored", "name_collision", "name collision",
  "subtitle_planning_failed", "no_exe", "translator_launch_failed", "publish_failed", "consecutive_restore_failures",
  "asr_launch_failed", "cancelled", "scan_failed", "consecutive_subtitle_failures", "no_llm_key", "restore_failed",
  "unstable", "translation_paused", "subtitle exists", "transcript exists", "subtitle state unreadable",
  "asr failed", "subtitle operation failed", "network", "rate_limit", "refusal", "bad_response", "auth", "invalid", "content",
  "unavailable", "recovered", "changed",
];

beforeEach(() => setLang("en"));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); setLang("zh-CN"); });

describe("log sentences", () => {
  it.each(catalog)("uses actual producer fields for %s in the list and export", (kind) => {
    const data = producerData[kind];
    expect(data).toBeDefined();
    const templateKind = kind === "translate.switched" ? "translate_unavailable" : kind.replaceAll(".", "_");
    for (const lang of ["en", "zh-CN"] as const) {
      setLang(lang);
      const template = String(i18n.getResource(lang, "translation", `logs.kinds.${templateKind}`));
      for (const [, field] of template.matchAll(/\{\{(\w+)\}\}/g)) {
        expect(Object.hasOwn(data, field === "detail" && kind === "task.error" ? "reason" : field), `${kind}: ${field}`).toBe(true);
      }
      const row = entry(undefined, kind, data);
      const sentence = renderLogSentence(row, i18n.t);
      expect(logText([row], i18n.t)).toContain(sentence);
      if (kind === "translate.refused") expect(sentence).toContain("17");
    }
  });
  it.each(reasonCodes)("localizes producer reason %s in both languages and export", reason => {
    for (const lang of ["en", "zh-CN"] as const) {
      setLang(lang);
      const text = i18n.getResource(lang, "translation", `logs.reasons.${reason}`);
      expect(typeof text).toBe("string");
      expect(text).not.toBe(reason);
      const row = entry(undefined, "task.error", { reason });
      expect(renderLogSentence(row, i18n.t)).toContain(text);
      expect(logText([row], i18n.t)).toContain(text);
    }
    expect(renderLogSentence(entry(undefined, "task.error", { reason: "future_reason" }), i18n.t)).toContain("future_reason");
  });
  it.each([[3780.567, "1 h 3 min", "1 小时 3 分"], [3420.123, "57 min", "57 分"], [12.3456, "12 s", "12 秒"]])("formats duration and elapsed %s", (seconds, en, zh) => {
    for (const [lang, expected] of [["en", en], ["zh-CN", zh]] as const) {
      setLang(lang);
      const row = entry(undefined, "restore.finished", { output: "LMNO-123.mp4", duration: seconds, elapsed: seconds, minutes: 1.234567 });
      expect(renderLogSentence(row, i18n.t)).toContain(expected);
      expect(logDetails(row, i18n.t).filter(([, value]) => value === expected)).toHaveLength(2);
      expect(logText([row], i18n.t)).not.toContain("1.234567");
    }
  });
  it("explains publish failures and GPU waits without a holder", () => {
    for (const [lang, failure, wait] of [["en", "Restored but publishing failed", "Waiting for the GPU"], ["zh-CN", "修复完成但写入失败", "等待 GPU"]] as const) {
      setLang(lang);
      expect(renderLogSentence(entry(undefined, "restore.failed", { exit_code: 0, reason: "publish_failed" }), i18n.t)).toBe(failure);
      expect(renderLogSentence(entry(undefined, "task.gpu_wait", { holder: "" }), i18n.t)).toBe(wait);
    }
  });
  it("uses a holder-free GPU wait sentence", () => {
    expect(renderLogSentence(entry(undefined, "task.gpu_wait", { holder: "" }), i18n.t)).toBe("Waiting for the GPU");
  });
  it.each([`"api_key": "PLANTED secret"`, `'token': 'PLANTED secret'`, `"Authorization": "Bearer PLANTED secret"`, `api_token=PLANTED`, `Bearer PLANTED`])("redacts quoted credentials: %s", credential => {
    const row = entry(undefined, "event.mirrored", { title: credential, message: credential, tail: credential });
    render(<TaskLogRows entries={[row]} />);
    fireEvent.click(screen.getByRole("button", { name: /Details/ }));
    expect(document.body.textContent).not.toContain("PLANTED");
    expect(logText([row], i18n.t)).not.toContain("PLANTED");
  });
  it("removes obsolete agent event UI symbols while retaining Hub events", () => {
    expect(api).not.toHaveProperty("agentEvents");
    expect(api.hubEvents).toBeTypeOf("function");
    for (const lang of ["en", "zh-CN"]) {
      expect(i18n.getResource(lang, "translation", "agent.recentEvents")).toBeUndefined();
      expect(i18n.getResource(lang, "translation", "agent.recentEventsShort")).toBeUndefined();
    }
  });
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
  it.each([false, true])("keeps the session and cursor across midnight (past day: %s)", async past => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T23:59:59"));
    const fetcher = stub(p => ({ entries: p.has("after")
      ? [entry("20260926-9"), entry("20260927-1")]
      : [entry(p.get("day") === "20260924" ? "20260924-1" : "20260926-9")] }));
    show(); await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    if (past) { select("Day", "20260924"); await act(async () => { await vi.advanceTimersByTimeAsync(0); }); }
    fetcher.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByTestId(past ? "log-row-20260924-1" : "log-row-20260926-9")).toBeInTheDocument();
    const queries = () => fetcher.mock.calls.map(([url]) => new URL(url).searchParams).filter(p => !p.has("days"));
    expect(queries().every(p => p.has("after"))).toBe(true);
    if (past) {
      expect(queries()).toHaveLength(0);
      expect(screen.getAllByTestId(/^log-row/)).toHaveLength(1);
    } else {
      expect(queries().map(p => p.get("after"))).toEqual(["20260926-9"]);
      expect(screen.getByText("2026-09-26")).toBeInTheDocument();
      expect(screen.getByText("2026-09-27")).toBeInTheDocument();
      expect(screen.getAllByTestId(/^log-row/)).toHaveLength(2);
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(queries().map(p => p.get("after"))).toEqual(["20260926-9", "20260927-1"]);
      expect(screen.getAllByTestId(/^log-row/)).toHaveLength(2);
    }
  });
  it.each([["en", "Subtitles skipped"], ["zh-CN", "字幕跳过"]] as const)("labels subtitle skips separately in %s", (lang, label) => {
    setLang(lang);
    expect(logDetails(entry(undefined, "task.done", { skipped: 1, subs_skipped: 3 }), i18n.t)).toContainEqual([label, "3"]);
  });
  it.each([["Task", "task"], ["Search film / model / title", "q"]])("debounces typing in %s before querying", async (label, param) => {
    vi.useFakeTimers();
    const fetcher = stub(() => ({ entries: [entry()] }));
    show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fetcher.mockClear();
    for (const value of ["r", "re", "removed"]) {
      select(label, value);
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(fetcher).not.toHaveBeenCalled();
      expect(screen.getByTestId("log-row-20260926-9")).toBeInTheDocument();
    }
    expect(screen.getByLabelText(label)).toHaveValue("removed");
    await act(async () => { await vi.advanceTimersByTimeAsync(199); });
    expect(fetcher).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    const queries = fetcher.mock.calls.map(([url]) => new URL(url).searchParams).filter(p => !p.has("days"));
    expect(queries).toHaveLength(1);
    expect(queries[0].get(param)).toBe("removed");
  });

  it("refreshes only the days list while viewing a past day", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T12:00:00"));
    const fetcher = stub();
    show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    select("Day", "20260924");
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fetcher.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(new URL(fetcher.mock.calls[0][0]).searchParams.has("days")).toBe(true);
  });
  it("allows exact removed task names and excludes empty task suggestions", async () => {
    const fetcher = stub(() => ({ entries: [{ ...entry(undefined, "agent.stopping"), task: "" }] }));
    show(); await screen.findByText("Agent is stopping");
    const input = screen.getByLabelText("Task");
    expect(input.tagName).toBe("INPUT");
    expect(document.querySelector('datalist option[value=""]')).toBeNull();
    select("Task", "removed / LMNO");
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("task") === "removed / LMNO")).toBe(true));
  });
  it("does not offer an empty-labelled agent task", async () => {
    stub(() => ({ entries: [{ ...entry(undefined, "agent.stopping"), task: "" }] }));
    show(); await screen.findByText("Agent is stopping");
    expect([...document.querySelectorAll("option")].some(option => option.textContent === "")).toBe(false);
  });
  it.each([false, true])("updates day labels and export after midnight (past day: %s)", async past => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T23:59:59"));
    const fetcher = stub();
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL() { return "blob:test"; }
      static revokeObjectURL = vi.fn();
    });
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download); });
    show(); await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    if (past) { select("Day", "20260924"); await act(async () => { await vi.advanceTimersByTimeAsync(0); }); }
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("option", { name: "Today" })).toHaveValue("20260927");
    expect(screen.getByRole("option", { name: "Yesterday" })).toHaveValue("20260926");
    expect(screen.getByLabelText("Day")).toHaveValue(past ? "20260924" : "20260927");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Export" })); });
    expect(downloads).toEqual([`taskpaw-${past ? "20260924" : "20260927"}.txt`]);
    expect(fetcher.mock.calls.some(([url]) => {
      const p = new URL(url).searchParams;
      return p.get("limit") === "500" && p.get("day") === (past ? "20260924" : "20260927");
    })).toBe(true);
  });
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
  it("polls after the largest numeric id, dedupes across days, and stops on unmount", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T12:00:00"));
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
