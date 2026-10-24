import { afterEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { WorkflowStep } from "../api";
import { ArchiveLivePanel } from "../components/media/ArchiveLivePanel";
import { StageBlock } from "../components/media/parts";
import { row } from "./fixtures/mediaWorkspace";

afterEach(() => vi.restoreAllMocks());

it("computes speed and ETA from samples, advances verification and finishes each copied item", () => {
  const clock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-24T08:01:00Z"));
  const progress = { phase: "copying", copied_bytes: 2e9, verified_bytes: 0, total_bytes: 4e9,
    files_done: 0, files_total: 1, items: [{ name: "ABC-123", bytes: 2e9, total: 4e9 }],
    target: "fake-NAS/ABC-123", started_at: "2026-10-24T08:00:00Z" };
  const step: WorkflowStep = { key: "archive", state: "running", note: null, progress };
  const view = (s: WorkflowStep) => <>
    <ArchiveLivePanel step={s} />
    <StageBlock item={{ kind: "film", id: "sample", status: "archiving", row: row({ film_id: "sample", steps: [s] }) }} />
  </>;
  const { rerender } = render(view(step));
  expect(screen.getByTestId("archive-live")).toHaveTextContent("速度—");
  expect(screen.getByRole("progressbar", { name: "ABC-123 复制进度" })).toHaveAttribute("aria-valuenow", "50");
  clock.mockReturnValue(Date.parse("2026-10-24T08:01:05Z"));
  rerender(view({ ...step, progress: { ...progress, copied_bytes: 3e9 } }));
  expect(screen.getByTestId("archive-live")).toHaveTextContent("速度200.0 MB/s");
  expect(screen.getByTestId("archive-live")).toHaveTextContent("预计剩余 约 25 秒");
  expect(screen.getByText("归档 · 37% · 复制 3.0 / 4.0 GB · 约 25 秒")).toBeInTheDocument();
  clock.mockReturnValue(Date.parse("2026-10-24T08:01:15Z"));
  rerender(view({ ...step, progress: { ...progress, phase: "verifying", copied_bytes: 4e9, verified_bytes: 1e9,
    items: [{ name: "ABC-123", bytes: 4e9, total: 4e9 }] } }));
  expect(screen.getByRole("progressbar", { name: "归档进度" })).toHaveAttribute("aria-valuenow", "62");
  expect(screen.getByText("✓ ABC-123")).toBeInTheDocument();
  expect(screen.queryByRole("progressbar", { name: "ABC-123 复制进度" })).not.toBeInTheDocument();
  expect(screen.getByTestId("archive-live")).toHaveTextContent("预计剩余 约 15 秒");
});

it("preflight with unknown size has no invented percent or speed", () => {
  render(<ArchiveLivePanel step={{ key: "archive", state: "running", note: null, progress: {
    phase: "copying", copied_bytes: 0, verified_bytes: 0, total_bytes: 0, files_done: 0, files_total: 1,
    items: [], target: "fake-NAS/ABC-123", started_at: new Date().toISOString(),
  } }} />);
  expect(screen.getByRole("progressbar", { name: "归档进度" })).not.toHaveAttribute("aria-valuenow");
  expect(screen.getByTestId("archive-live")).not.toHaveTextContent("%");
  expect(screen.getByTestId("archive-live")).toHaveTextContent("速度—");
});
