import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, HttpError, type FilmDetail, type FilmRow, type WorkflowFilms } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { exceptionInfo, filmStatus, STATUS_META, workItems } from "../components/media/mediaModel";
import { theme } from "../theme";
import { setLang } from "../i18n";
import { counts, detail, FILMS_RESPONSE, PLANS, row, steps } from "./fixtures/mediaWorkspace";

// P3 E1-E3 (#304), board Drawer-SourceMissing: 片源丢失 badge, drawer alert, 重新检查 and the guarded
// 删除此任务. Fixtures only; the backend contract is pytest's.
const NOTE = "INBOX 里的文件已不存在 · 不再派发";
const PATH = "D:\\TODO\\ABF-394\\ABF-394.mp4";
const HINT = "如果只是移走了，把文件放回原处后点「重新检查」即可继续。";
const BODY = "只删除任务和它在工作目录里的临时文件，不会动 INBOX、成品目录或 NAS 上的任何文件。删除后不能恢复。";
const FOUND_AT = new Date(2026, 9, 24, 9, 5, 0).toISOString();

const LOST: FilmRow = row({
  film_id: "abf-394", name: "ABF-394", number: "ABF-394", group: "attention", attention_kind: "source_missing",
  stage: "restore", status: "pending", note: NOTE, can_remove: true,
  source_missing: { paths: [PATH], found_at: FOUND_AT }, steps: steps("restore", "waiting", { note: NOTE }),
});
const WAITING: FilmRow = row({
  film_id: "abf-395", name: "ABF-395", number: "ABF-395", stage: "restore", status: "pending", can_remove: true,
  steps: steps("restore", "waiting"),
});
const RUNNING: FilmRow = row({
  film_id: "abf-396", name: "ABF-396", number: "ABF-396", stage: "restore", status: "running", can_remove: false,
  steps: steps("restore", "running"),
});
const ROWS = [LOST, WAITING, RUNNING];
const FILMS: WorkflowFilms = { ...FILMS_RESPONSE, current: null, intake: null, films: ROWS, packages: [], counts: counts(ROWS, []) };

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};
const detailOf = (r: FilmRow, over: Partial<FilmDetail> = {}) =>
  detail(r.film_id, { name: r.name, number: r.number, title: null, group: r.group, note: r.note, steps: r.steps,
    attention_kind: r.attention_kind ?? null, source_missing: r.source_missing ?? null, can_remove: r.can_remove, ...over });

function mockApi(films = FILMS) {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(films);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
  vi.spyOn(api, "workflowFilm").mockImplementation(async (id) => detailOf(ROWS.find((r) => r.film_id === id) ?? LOST));
  vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
  vi.spyOn(api, "workflowDevices").mockResolvedValue({
    remote_workers: false, server_time: "2026-10-24T08:00:00Z", controller: { name: "本机", stages: ["修复", "识别", "翻译"] }, workers: [],
  });
  return {
    recheck: vi.spyOn(api, "workflowRecheckSource").mockResolvedValue({ ok: true, source_missing: null }),
    remove: vi.spyOn(api, "workflowRemove").mockResolvedValue({ ok: true, leftover: 0 }),
  };
}
beforeEach(() => { setLang("zh-CN"); vi.spyOn(window, "scrollTo").mockImplementation(() => {}); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("片源丢失 vocabulary", () => {
  it("is its own red status, an exception with the detail action", () => {
    expect(STATUS_META.source_missing).toEqual(["片源丢失", "red"]);
    const item = workItems(FILMS, {})[0];
    expect(filmStatus(LOST)).toBe("source_missing");
    expect(item.status).toBe("source_missing");
    const [title, desc, label, action] = exceptionInfo(item, () => "");
    expect([title, desc, label, action]).toEqual(["片源丢失", NOTE, "处理", "detail"]);
  });
});

describe("概览", () => {
  it("shows the 片源丢失 badge and its note, and opens the drawer", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    const panel = (await screen.findByRole("heading", { name: "片源丢失 · 1" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("ABF-394")).toBeInTheDocument();
    expect(within(panel).getByText("片源丢失", { selector: ".chip" })).toBeInTheDocument();
    expect(within(panel).getAllByText(NOTE)).toHaveLength(2); // the panel's subtitle and the row's note
    expect(panel.querySelector(".panel-head p")).toHaveTextContent(/^INBOX 里的文件已不存在 · 不再派发$/);
    fireEvent.click(within(panel).getByRole("button", { name: "处理 ABF-394" }));
    expect(await screen.findByRole("dialog", { name: "影片详情" })).toBeInTheDocument();
  });

  it("shows no 片源丢失 panel when nothing is lost", async () => {
    mockApi({ ...FILMS, films: [WAITING], counts: counts([WAITING], []) });
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    expect(screen.queryByRole("heading", { name: /片源丢失/ })).toBeNull();
  });
});

describe("drawer: 片源丢失", () => {
  it("shows the alert with the original path, 发现时间 and the hint", async () => {
    mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    const alert = (await within(drawer).findByRole("heading", { name: "片源丢失" })).closest(".notice") as HTMLElement;
    expect(within(alert).getByText(PATH)).toBeInTheDocument();
    expect(alert).toHaveTextContent("发现时间");
    expect(alert).toHaveTextContent("09:05");
    expect(within(alert).getByText(HINT)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "重新检查" })).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "删除此任务" })).toBeInTheDocument();
  });

  it("重新检查 asks the backend and reports a returned source", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(m.recheck).toHaveBeenCalledWith("abf-394"));
    expect(await screen.findByText("文件已找回，影片继续排队。")).toBeInTheDocument();
  });

  it("重新检查 of a still missing source says so", async () => {
    const m = mockApi();
    m.recheck.mockResolvedValue({ ok: true, source_missing: { paths: [PATH], found_at: FOUND_AT } });
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "重新检查" }));
    expect(await screen.findByText("文件仍然不存在。")).toBeInTheDocument();
  });

  it("重新检查 on an unreachable INBOX says so instead of 文件仍然不存在", async () => {
    const m = mockApi();
    m.recheck.mockResolvedValue({ ok: true, unreachable: true, source_missing: { paths: [PATH], found_at: FOUND_AT } });
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "重新检查" }));
    expect(await screen.findByText("INBOX 暂时无法访问，稍后再试")).toBeInTheDocument();
    expect(screen.queryByText("文件仍然不存在。")).toBeNull();
  });

  it("offers 删除此任务 in the alert only when the film can be removed", async () => {
    mockApi();
    vi.spyOn(api, "workflowFilm").mockResolvedValue(detailOf(LOST, { can_remove: false }));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    await within(drawer).findByRole("button", { name: "重新检查" });
    expect(within(drawer).queryByRole("button", { name: "删除此任务" })).toBeNull();
  });

  it("删除此任务 asks first, cancel changes nothing, 删除 deletes and closes the drawer", async () => {
    const m = mockApi();
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "删除此任务" }));
    const ask = await screen.findByRole("alertdialog", { name: "删除 ABF-394 这个任务？" });
    expect(within(ask).getByText(BODY)).toBeInTheDocument();
    fireEvent.click(within(ask).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(m.remove).not.toHaveBeenCalled();
    fireEvent.click(within(drawer).getByRole("button", { name: "删除此任务" }));
    const again = await screen.findByRole("alertdialog", { name: "删除 ABF-394 这个任务？" });
    fireEvent.click(within(again).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(m.remove).toHaveBeenCalledWith("abf-394"));
    expect(await screen.findByText("已删除任务 ABF-394。")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "影片详情" })).toBeNull());
  });

  it("a refused delete keeps the drawer and shows the backend's reason", async () => {
    const m = mockApi();
    m.remove.mockRejectedValue(new HttpError("有阶段正在运行，不能删除", 409, "film_running"));
    wrap(<MediaWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "处理 ABF-394" }));
    const drawer = await screen.findByRole("dialog", { name: "影片详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "删除此任务" }));
    const ask = await screen.findByRole("alertdialog", { name: "删除 ABF-394 这个任务？" });
    fireEvent.click(within(ask).getByRole("button", { name: "删除" }));
    expect(await screen.findByText(/有阶段正在运行，不能删除/)).toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "影片详情" })).toBeInTheDocument();
  });
});

describe("drawer: 删除此任务 for a film that is only waiting", () => {
  const open = async (r: FilmRow) => {
    mockApi();
    wrap(<MediaWorkspace />);
    const rail = await screen.findByRole("navigation", { name: "媒体处理中心导航" });
    fireEvent.click(rail.querySelector('[data-nav="queue"]') as HTMLElement);
    fireEvent.click(await screen.findByRole("button", { name: `查看 ${r.name} 详情` }));
    return screen.findByRole("dialog", { name: "影片详情" });
  };

  it("offers it in the footer, with no 片源丢失 alert", async () => {
    const drawer = await open(WAITING);
    expect(await within(drawer).findByRole("button", { name: "删除此任务" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("heading", { name: "片源丢失" })).toBeNull();
    expect(within(drawer).queryByRole("button", { name: "重新检查" })).toBeNull();
  });

  it("does not offer it while a stage runs", async () => {
    const drawer = await open(RUNNING);
    await within(drawer).findByRole("heading", { name: "ABF-396" });
    expect(within(drawer).queryByRole("button", { name: "删除此任务" })).toBeNull();
  });
});
