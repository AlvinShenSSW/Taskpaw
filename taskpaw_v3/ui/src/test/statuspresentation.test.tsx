import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { createTheme, ThemeProvider } from "@mui/material/styles";
import { StatusChip } from "../components/media/parts";
import { STATUS_META, statusLabel, type StatusKey } from "../components/media/mediaModel";
import { presentFilmStatus, StatusProgress } from "../components/media/statusPresentation";
import { LTX_ENCODE, row, steps } from "./fixtures/mediaWorkspace";
import { REMOTE_ROWS } from "./fixtures/multiMachine";

afterEach(() => vi.restoreAllMocks());

describe("queue status presentation", () => {
  it("provides an icon and text for every known state", () => {
    const { container } = render(<>{(Object.keys(STATUS_META) as StatusKey[]).map((status) =>
      <div key={status} data-case={status}><StatusChip status={status} /></div>)}</>);
    for (const item of container.querySelectorAll("[data-case]")) {
      expect(item.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(item.textContent?.trim()).not.toBe("");
    }
  });

  it.each(["dark", "light"] as const)("separates active phases and reserves completion green in %s mode", (mode) => {
    const phases: StatusKey[] = ["restore", "asr", "translate", "return", "scrape", "cleanup", "completed"];
    const { container } = render(<ThemeProvider theme={createTheme({ palette: { mode } })}>
      {phases.map((status) => <div key={status} data-case={status}><StatusChip status={status} /></div>)}
    </ThemeProvider>);
    const colors = [...container.querySelectorAll("[data-case] > span")].map((element) => getComputedStyle(element).color);
    expect(new Set(colors).size).toBe(phases.length);
  });

  it.each([
    ["uploading", "回传中"], ["finishing", "提交结果"], ["settling", "停止未确认"],
  ] as const)("shows %s instead of stale compute progress", (phase, label) => {
    const { container, rerender } = render(<></>);
    for (const status of ["restore", "asr"] as const) {
      const film = row({ film_id: "returning", steps: steps(status, "running", { phase, ltx: LTX_ENCODE, progress: { percent: 74 } }) });
      rerender(<StatusChip status={status} film={film} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(container.textContent).not.toMatch(/修复中|语音识别|加载|52%|74%/);
      expect(presentFilmStatus(status, film).percent).toBeNull();
    }
  });

  it("keeps exceptions ahead of a leftover running step", () => {
    const film = row({ film_id: "lost", steps: steps("restore", "running", { phase: "uploading" }) });
    render(<StatusChip status="device_lost" film={film} />);
    expect(screen.getByText("设备失联")).toBeInTheDocument();
    expect(screen.queryByText("回传中")).not.toBeInTheDocument();
  });

  it("distinguishes approved waiting from actual archive verification", () => {
    const { rerender } = render(<StatusChip status="archiving" film={row({ film_id: "archive", steps: steps("archive", "waiting") })} />);
    expect(screen.getByText("已确认 · 等待归档")).toHaveAttribute("data-waiting", "true");
    rerender(<StatusChip status="archiving" film={row({ film_id: "archive", steps: steps("archive", "running", {
      progress: { phase: "verifying", copied_bytes: 100, verified_bytes: 50, total_bytes: 100 },
    }) })} />);
    expect(screen.getByText("校验归档")).toHaveAttribute("data-waiting", "false");
  });

  it("does not invent downloading or zero percent from a route alone", () => {
    const routed = { ...REMOTE_ROWS[4], note: null };
    expect(statusLabel("inbox", routed)).toBe("排队中 · 等 5070 Ti");
  });

  it("shows real ASR progress and omits missing or invalid progress", () => {
    const film = (progress: unknown) => row({ film_id: "asr", steps: steps("asr", "running", { progress }) });
    const { rerender } = render(<StatusProgress label="ASR" presentation={presentFilmStatus("asr", film({ percent: 37 }))} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "37");
    for (const progress of [null, { percent: NaN }, { percent: -1 }, { percent: 101 }, "识别中"]) {
      rerender(<StatusProgress label="ASR" presentation={presentFilmStatus("asr", film(progress))} />);
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    }
  });
});
