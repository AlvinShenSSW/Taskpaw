// #379 item 8: a poll tick re-renders only the log rows that changed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { TaskLog } from "../components/TaskLog";
import { renderLogSentence } from "../components/TaskLog.helpers";
import type { LogEntry } from "../api";
import { setLang } from "../i18n";
import { theme } from "../theme";

vi.mock("../components/TaskLog.helpers", async (importOriginal) => {
  const m = await importOriginal<typeof import("../components/TaskLog.helpers")>();
  return { ...m, renderLogSentence: vi.fn(m.renderLogSentence) };
});

const entry = (id: string): LogEntry => ({
  v: 1, id, kind: "task.started", ts: `${id.slice(0, 4)}-${id.slice(4, 6)}-${id.slice(6, 8)}T12:34:56+09:00`,
  task: "main/任务", task_type: "jasna", severity: "info", film: "movie.mkv", data: { queued: 1, done: 0, skipped: 0 },
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); setLang("zh-CN"); });

describe("TaskLog rendering", () => {
  it("renders no row on an idle tick and only the new row on a tick that adds one", async () => {
    setLang("en");
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-26T12:00:00"));
    let fresh: LogEntry[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const p = new URL(url).searchParams;
      const body = p.has("days") ? { boot: "boot-1", days: [{ day: "20260926", count: 3 }] }
        : p.has("after") ? { boot: "boot-1", entries: fresh, next_before: null }
        : { boot: "boot-1", entries: [entry("20260926-3"), entry("20260926-2"), entry("20260926-1")], next_before: null };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }));
    render(<ThemeProvider theme={theme}><TaskLog /></ThemeProvider>);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getAllByTestId(/^log-row/)).toHaveLength(3);
    const sentence = vi.mocked(renderLogSentence);
    sentence.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(sentence).toHaveBeenCalledTimes(0);
    fresh = [entry("20260926-4")];
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getAllByTestId(/^log-row/)).toHaveLength(4);
    expect(sentence).toHaveBeenCalledTimes(1);
  });
});
