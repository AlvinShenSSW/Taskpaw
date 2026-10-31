// #365 (EPIC #361 L3): 片库与规则 (NAS 片库, 演员, 归档规则, 归档历史) and 设置 over the library routes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, HttpError, type LibraryList } from "../api";
import { SCAN_IDLE_MS } from "../components/media/libraryModel";
import { LibraryPage } from "../components/media/LibraryPage";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { SettingsPage } from "../components/media/SettingsPage";
import { WorkspaceContext, type WorkspaceApi } from "../components/media/workspace";
import { theme } from "../theme";
import { setLang } from "../i18n";
import {
  CAT_K, HARU_DETAIL, HISTORY, IMPORT_PREVIEW, IMPORT_RESULT, LIB_A, LIBRARIES, NO_LIBRARIES, PERFORMERS, RECEIPT,
  performer, ROW_SECOND, RULES, SCAN_IDLE,
} from "./fixtures/mediaLibrary";
import { FILMS_RESPONSE, PLANS } from "./fixtures/mediaWorkspace";

let libraries: LibraryList;

function mockLibrary() {
  libraries = LIBRARIES;
  vi.spyOn(api, "libraries").mockImplementation(async () => libraries);
  vi.spyOn(api, "libraryRules").mockResolvedValue(RULES);
  vi.spyOn(api, "libraryScanStatus").mockResolvedValue(SCAN_IDLE);
  vi.spyOn(api, "image").mockResolvedValue("data:image/jpeg;base64,AAAA");
  return {
    performers: vi.spyOn(api, "libraryPerformers").mockImplementation(async (q) =>
      (q.page_size === 1 ? { ...PERFORMERS, items: PERFORMERS.items.slice(0, 1), page_size: 1 } : PERFORMERS)),
    performer: vi.spyOn(api, "libraryPerformer").mockResolvedValue(HARU_DETAIL),
    history: vi.spyOn(api, "libraryHistory").mockResolvedValue(HISTORY),
    receipt: vi.spyOn(api, "libraryReceipt").mockResolvedValue(RECEIPT),
    open: vi.spyOn(api, "libraryOpen").mockResolvedValue(undefined),
    saveRules: vi.spyOn(api, "librarySaveRules").mockImplementation(async (changes) => ({ ...RULES, ...changes })),
    add: vi.spyOn(api, "libraryAdd").mockImplementation(async (body) => ({ ...LIB_A, id: "new", ...body, label: body.label ?? "" })),
    update: vi.spyOn(api, "libraryUpdate").mockImplementation(async (id, patch) => ({ ...CAT_K, id, ...patch })),
    remove: vi.spyOn(api, "libraryRemove").mockResolvedValue(undefined),
    scan: vi.spyOn(api, "libraryScan").mockResolvedValue({ running: "lib-a", queued: [], results: {} }),
    scanAll: vi.spyOn(api, "libraryScanAll").mockResolvedValue({ running: "lib-a", queued: ["cat-k"], results: {} }),
    addAlias: vi.spyOn(api, "libraryAddAlias").mockResolvedValue({ id: 8, alias: "Haruka" }),
    deleteAlias: vi.spyOn(api, "libraryDeleteAlias").mockResolvedValue(undefined),
    preview: vi.spyOn(api, "libraryImportPreview").mockResolvedValue(IMPORT_PREVIEW),
    importRun: vi.spyOn(api, "libraryImport").mockResolvedValue(IMPORT_RESULT),
  };
}

function mount(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ws = { readOnly: false, toast: vi.fn(), navigate: vi.fn() } as unknown as WorkspaceApi;
  render(
    <ThemeProvider theme={theme}><QueryClientProvider client={qc}>
      <WorkspaceContext.Provider value={ws}><div className="mw">{ui}</div></WorkspaceContext.Provider>
    </QueryClientProvider></ThemeProvider>,
  );
  return ws;
}

const card = (name: string) => screen.findByRole("article", { name });
const tab = async (name: string) => fireEvent.click(await screen.findByRole("tab", { name }));

beforeEach(() => { setLang("zh-CN"); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("片库与规则 · first run", () => {
  it("offers the Filmpaw import and manual add, then imports after the preview and follows the scans", async () => {
    const m = mockLibrary();
    libraries = NO_LIBRARIES;
    m.importRun.mockImplementation(async () => { libraries = LIBRARIES; return IMPORT_RESULT; });
    mount(<LibraryPage />);
    expect(await screen.findByRole("heading", { name: "还没有片库" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "手动添加片库" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "NAS 片库" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "从 Filmpaw 导入" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(IMPORT_PREVIEW.path)).toBeInTheDocument();
    expect(within(dialog).getByText("238 位")).toBeInTheDocument();
    expect(within(dialog).getByText("与已有片库重叠，跳过")).toBeInTheDocument();
    expect(within(dialog).getByText("58 组新的")).toBeInTheDocument();
    expect(m.importRun).not.toHaveBeenCalled();  // the preview comes first
    const scans = vi.mocked(api.libraryScanStatus);
    scans.mockResolvedValue({ running: "lib-a", queued: ["lib-b"], results: {} });
    fireEvent.click(within(dialog).getByRole("button", { name: "导入并扫描" }));
    await waitFor(() => expect(m.importRun).toHaveBeenCalledWith(""));
    expect(await within(dialog).findByRole("heading", { name: "正在导入" })).toBeInTheDocument();
    expect(within(dialog).getByText("扫描 A · 主片库")).toBeInTheDocument();
    expect(within(dialog).getByText("进行中…")).toBeInTheDocument();
    expect(within(dialog).getByText(/该目录与已有片库互相包含/)).toBeInTheDocument();
    scans.mockResolvedValue({ running: null, queued: [], results: {
      "lib-a": { library_id: "lib-a", ok: true }, "lib-b": { library_id: "lib-b", ok: false, error: "unreachable" } } });
    expect(await within(dialog).findByRole("heading", { name: "导入完成" }, { timeout: 4000 })).toBeInTheDocument();
    expect(within(dialog).getByText("离线，恢复后自动补扫")).toBeInTheDocument();
    expect(within(dialog).getByText(/已导入 2 个片库、58 组别名/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "查看片库" }));
    expect(await screen.findByRole("tab", { name: "NAS 片库" })).toHaveAttribute("aria-selected", "true");
  });

  it("reads another Filmpaw library when the default one is missing", async () => {
    const m = mockLibrary();
    libraries = NO_LIBRARIES;
    m.preview.mockImplementation(async (path = "") => {
      if (!path) throw new HttpError("not found", 404);
      return IMPORT_PREVIEW;
    });
    mount(<LibraryPage />);
    fireEvent.click(await screen.findByRole("button", { name: "从 Filmpaw 导入" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/没有找到 Filmpaw 片库/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "导入并扫描" })).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/^Filmpaw 片库位置/), { target: { value: "E:\\old\\library.db" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取" }));
    await waitFor(() => expect(m.preview).toHaveBeenCalledWith("E:\\old\\library.db"));
    fireEvent.click(await within(dialog).findByRole("button", { name: "导入并扫描" }));
    await waitFor(() => expect(m.importRun).toHaveBeenCalledWith("E:\\old\\library.db"));
  });

  it("re-reads the same path again after a failed read (#379: no polling to recover it)", async () => {
    const m = mockLibrary();
    libraries = NO_LIBRARIES;
    let failing = true;
    m.preview.mockImplementation(async (path = "") => {
      if (!path || failing) throw new HttpError("busy", 503);
      return IMPORT_PREVIEW;
    });
    mount(<LibraryPage />);
    fireEvent.click(await screen.findByRole("button", { name: "从 Filmpaw 导入" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByLabelText(/^Filmpaw 片库位置/), { target: { value: "E:\\old\\library.db" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取" }));
    await waitFor(() => expect(m.preview).toHaveBeenCalledWith("E:\\old\\library.db"));
    expect(await within(dialog).findByText(/无法读取 Filmpaw 片库/)).toBeInTheDocument();
    failing = false;
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取" }));
    expect(await within(dialog).findByRole("button", { name: "导入并扫描" })).toBeEnabled();
  });
});

describe("片库与规则 · NAS 片库", () => {
  it("shows actor libraries and category directories as cards with their last scan", async () => {
    mockLibrary();
    const ws = mount(<LibraryPage />);
    const a = within(await card("主片库"));
    expect(a.getByText("A")).toBeInTheDocument();
    expect(a.getByText("在线")).toBeInTheDocument();
    expect(a.getByText("入库目标")).toBeInTheDocument();
    expect(a.getByText(/3 个已缺失/)).toBeInTheDocument();
    expect(a.getByText("2.1 TB")).toBeInTheDocument();
    const b = within(await card("备份库"));
    expect(b.getByText("离线")).toBeInTheDocument();
    expect(b.getByText("只读 · 不作入库目标")).toBeInTheDocument();
    expect(b.getByText(/连不上这台 NAS/)).toBeInTheDocument();
    expect(b.getByRole("button", { name: "重新扫描" })).toBeDisabled();
    expect(b.getByRole("button", { name: "打开" })).toBeDisabled();
    const k = within(await card("写真 · OAE"));
    expect(k.getByText("影片直接放入")).toBeInTheDocument();
    expect(k.getByText("前缀 OAE")).toBeInTheDocument();
    expect(k.queryByRole("button", { name: "重新扫描" })).toBeNull();
    expect(await screen.findByText("每 6 小时")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "在设置中修改" }));
    expect(ws.navigate).toHaveBeenCalledWith("settings");
  });

  it("scans one library or all, and shows the running scan from scan-status", async () => {
    const m = mockLibrary();
    mount(<LibraryPage />);
    const a = within(await card("主片库"));
    fireEvent.click(a.getByRole("button", { name: "重新扫描" }));
    await waitFor(() => expect(m.scan).toHaveBeenCalledWith("lib-a"));
    expect(await a.findByText("正在扫描演员目录…")).toBeInTheDocument();
    expect(a.getByRole("progressbar", { name: "扫描进度" })).toBeInTheDocument();
    expect(a.getByRole("button", { name: "扫描中" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "全部重新扫描" }));
    await waitFor(() => expect(m.scanAll).toHaveBeenCalled());
    expect(await within(await card("写真 · OAE")).findByText("排队等待扫描")).toBeInTheDocument();
  });

  it("adds a category directory with number prefixes and an actor library by role", async () => {
    const m = mockLibrary();
    const ws = mount(<LibraryPage />);
    fireEvent.click((await screen.findAllByRole("button", { name: "添加片库" }))[0]);
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText(/番号前缀/)).toBeNull();
    fireEvent.click(within(dialog).getByRole("radio", { name: /类型目录/ }));
    expect(within(dialog).getByRole("heading", { name: "添加类型目录" })).toBeInTheDocument();
    const add = within(dialog).getByRole("button", { name: "添加" });
    expect(add).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "单体" } });
    fireEvent.change(within(dialog).getByLabelText(/^根目录/), { target: { value: "\\\\nas-1\\单体" } });
    fireEvent.change(within(dialog).getByLabelText(/番号前缀/), { target: { value: "oae、ABC" } });
    fireEvent.click(add);
    await waitFor(() => expect(m.add).toHaveBeenCalledWith({ kind: "category", path: "\\\\nas-1\\单体", label: "单体", prefixes: ["OAE", "ABC"] }));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("已添加类型目录「单体」"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    m.add.mockRejectedValueOnce(new HttpError("该目录与已有片库互相包含", 409));
    fireEvent.click(screen.getAllByRole("button", { name: "添加片库" })[0]);
    dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "旧盘" } });
    fireEvent.change(within(dialog).getByLabelText(/^根目录/), { target: { value: "\\\\nas-1\\films\\old" } });
    fireEvent.click(within(dialog).getByRole("radio", { name: /只读/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "添加并扫描" }));
    await waitFor(() => expect(m.add).toHaveBeenLastCalledWith({ kind: "actor", path: "\\\\nas-1\\films\\old", label: "旧盘", role: "readonly" }));
    expect(await within(dialog).findByText("该目录与已有片库互相包含")).toBeInTheDocument();
  });

  it("edits a category's label and prefixes but never its path, and removes a library", async () => {
    const m = mockLibrary();
    const ws = mount(<LibraryPage />);
    fireEvent.click(within(await card("写真 · OAE")).getByRole("button", { name: "编辑" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/^根目录/)).toHaveAttribute("readonly");
    expect(within(dialog).queryByRole("radiogroup", { name: "类型" })).toBeNull();
    fireEvent.change(within(dialog).getByLabelText(/番号前缀/), { target: { value: "OAE, XYZ" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(m.update).toHaveBeenCalledWith("cat-k", { label: "写真 · OAE", prefixes: ["OAE", "XYZ"] }));

    fireEvent.click(within(await card("主片库")).getByRole("button", { name: "移除 主片库" }));
    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/241 位演员的这个位置会从索引里去掉/)).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "移除片库" }));
    await waitFor(() => expect(m.remove).toHaveBeenCalledWith("lib-a"));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("已移除「主片库」，NAS 上的文件没有改动"));
  });

  it("opens a library root through /library/open", async () => {
    const m = mockLibrary();
    mount(<LibraryPage />);
    fireEvent.click(within(await card("主片库")).getByRole("button", { name: "打开" }));
    await waitFor(() => expect(m.open).toHaveBeenCalledWith(LIB_A.path));
  });
});

describe("片库与规则 · 演员", () => {
  it("lists one card per name with every location, 3:4 thumbnails and filters", async () => {
    const m = mockLibrary();
    mount(<LibraryPage />);
    await tab("演员");
    const haru = await screen.findByRole("button", { name: "Haru 详情" });
    expect(within(haru).getByText("别名：ハル")).toBeInTheDocument();
    expect(within(haru).getByText("54")).toBeInTheDocument();  // 42 + 12 部
    expect(within(haru).getByText("A").closest(".loc-chip")).toHaveTextContent("A42 部");
    expect(within(haru).getByText("B").closest(".loc-chip")).toHaveTextContent("B12 部 · 离线");
    expect((await within(haru).findByRole("img", { name: "Haru 的 folder.jpg" })).closest(".avatar")).toHaveClass("lg");
    const mio = screen.getByRole("button", { name: "Mio 详情" });
    expect(within(mio).getByRole("img", { name: "Mio：没有头像" })).toHaveClass("none");
    expect(within(screen.getByRole("button", { name: "Nagi 详情" })).getByText(/已缺失/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^有别名\s*3$/ })).toBeInTheDocument();  // counts from `total`

    fireEvent.click(screen.getByRole("button", { name: /^有别名/ }));
    await waitFor(() => expect(m.performers).toHaveBeenCalledWith(expect.objectContaining({ filter: "alias", page: 1, page_size: 50 })));
    fireEvent.change(screen.getByRole("combobox", { name: "按片库筛选" }), { target: { value: "lib-a" } });
    await waitFor(() => expect(m.performers).toHaveBeenCalledWith(expect.objectContaining({ library_id: "lib-a", filter: "alias" })));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索名字或别名" }), { target: { value: "ハル" } });
    await waitFor(() => expect(m.performers).toHaveBeenCalledWith(expect.objectContaining({ q: "ハル" })));
  });

  it("opens the drawer for the location that supplies the card thumbnail", async () => {
    const m = mockLibrary();
    const bare = performer({ id: "p-kei-a", name: "Kei", library_id: "lib-a", has_thumb: false });
    const shot = performer({ id: "p-kei-b", name: "Kei", library_id: "lib-b" });
    m.performers.mockResolvedValue({ items: [bare, shot], total: 1, page: 1, page_size: 50 });
    m.performer.mockResolvedValue({ ...shot, locations: [bare, shot], receipts: [] });
    mount(<LibraryPage />);
    await tab("演员");
    fireEvent.click(await screen.findByRole("button", { name: "Kei 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "演员详情" });
    await waitFor(() => expect(m.performer).toHaveBeenCalledWith("p-kei-b"));
    expect((await within(drawer).findByRole("img", { name: "Kei 的 folder.jpg" })).closest(".avatar")).toHaveClass("lg");
  });

  it("opens the drawer with the large thumbnail, every location and receipt, and edits aliases", async () => {
    const m = mockLibrary();
    const ws = mount(<LibraryPage />);
    await tab("演员");
    fireEvent.click(await screen.findByRole("button", { name: "Haru 详情" }));
    const drawer = await screen.findByRole("dialog", { name: "演员详情" });
    await waitFor(() => expect(m.performer).toHaveBeenCalledWith("p-haru-a"));
    expect((await within(drawer).findByRole("img", { name: "Haru 的 folder.jpg" })).closest(".avatar")).toHaveClass("lg");
    expect(within(drawer).getByText("2 个位置 · 54 部")).toBeInTheDocument();
    expect(within(drawer).getByText("A · 主片库")).toBeInTheDocument();
    expect(within(drawer).getByText("B · 备份库")).toBeInTheDocument();
    expect(within(drawer).getByText("离线")).toBeInTheDocument();
    expect(within(drawer).getByText("OAE-198")).toBeInTheDocument();  // recent receipts

    fireEvent.click(within(drawer).getByRole("button", { name: "删除别名 ハル" }));
    await waitFor(() => expect(m.deleteAlias).toHaveBeenCalledWith(7));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("已删除别名「ハル」"));

    fireEvent.change(within(drawer).getByLabelText("新别名"), { target: { value: " Haruka " } });
    fireEvent.click(within(drawer).getByRole("button", { name: "添加" }));
    await waitFor(() => expect(m.addAlias).toHaveBeenCalledWith("p-haru-a", "Haruka"));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("已添加别名「Haruka」"));

    m.addAlias.mockRejectedValueOnce(new HttpError("该别名已存在", 409));
    fireEvent.change(within(drawer).getByLabelText("新别名"), { target: { value: "ハル" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "添加" }));
    expect(await within(drawer).findByText("该别名已存在")).toBeInTheDocument();
  });
});

describe("片库与规则 · 归档规则", () => {
  it("saves each switch and choice on its own and lists priority and fixed rules", async () => {
    const m = mockLibrary();
    const ws = mount(<LibraryPage />);
    await tab("归档规则");
    const alias = await screen.findByRole("switch", { name: "用别名匹配" });
    expect(alias).toHaveAttribute("aria-checked", "true");
    fireEvent.click(alias);
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ alias_match: false }));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("规则已保存"));
    await waitFor(() => expect(screen.getByRole("switch", { name: "用别名匹配" })).toHaveAttribute("aria-checked", "false"));

    const prio = screen.getByRole("list", { name: "片库优先级" });
    expect(within(prio).getByText("A · 主片库").closest("li")).toHaveTextContent(/^1/);
    expect(within(prio).getByText("B · 备份库").closest("li")).toHaveTextContent("只读");
    const most = screen.getByRole("radio", { name: "影片最多的位置" });
    await waitFor(() => expect(most).toBeEnabled());
    fireEvent.click(most);
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ multi_location: "most" }));
    expect(await screen.findByText("A（影片最多）")).toBeInTheDocument();

    const name = screen.getByLabelText(/^目录名/);
    await waitFor(() => expect(name).toBeEnabled());
    fireEvent.change(name, { target: { value: "ask" } });
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ new_actor_name: "ask" }));
    const allowNew = screen.getByRole("switch", { name: "确认时允许新建演员目录" });
    await waitFor(() => expect(allowNew).toBeEnabled());
    fireEvent.click(allowNew);
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ allow_new_actor: false }));
    await waitFor(() => expect(screen.queryByLabelText(/^默认放到/)).toBeNull());
    for (const name of ["演员不在片库时，按番号前缀预选类型目录", "演员已在片库时，前缀命中就提示「第二目标」", "待确认里总是列出类型目录"]) {
      expect(screen.getByRole("switch", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("同名影片目录已存在就拒绝")).toBeInTheDocument();
    expect(document.querySelectorAll(".fixed-rule")).toHaveLength(7);
  });
});

describe("片库与规则 · 归档规则 · 片库优先级", () => {
  it("moves an archive library past the other one, leaving read-only ones unranked, and saves the order", async () => {
    const m = mockLibrary();
    const second = { ...LIB_A, id: "lib-c", label: "第二片库", path: "\\\\nas-3\\films\\", position: 3 };
    libraries = { ...LIBRARIES, items: [LIB_A, LIBRARIES.items[1], second, CAT_K] };
    const order = vi.spyOn(api, "libraryOrder").mockImplementation(async (ids) => {
      libraries = { ...libraries, items: [...ids.map((id, i) => ({ ...libraries.items.find((l) => l.id === id)!, position: i + 1 })), CAT_K] };
      return libraries;
    });
    const ws = mount(<LibraryPage />);
    await tab("归档规则");
    const prio = await screen.findByRole("list", { name: "片库优先级" });
    expect(within(prio).getByRole("button", { name: "上移 主片库" })).toBeDisabled();
    expect(within(prio).getByRole("button", { name: "下移 第二片库" })).toBeDisabled();
    expect(within(prio).queryByRole("button", { name: /备份库/ })).toBeNull();  // read-only: — / 只读
    expect(within(prio).getByText("C · 第二片库").closest("li")).toHaveTextContent(/^2/);
    fireEvent.click(within(prio).getByRole("button", { name: "上移 第二片库" }));
    // the read-only library keeps its place; every actor library is sent once
    await waitFor(() => expect(order).toHaveBeenCalledWith(["lib-c", "lib-b", "lib-a"]));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("规则已保存"));
    await waitFor(() => expect(within(prio).getByText("A · 第二片库").closest("li")).toHaveTextContent(/^1/));
    expect(within(prio).getByText("C · 主片库").closest("li")).toHaveTextContent(/^2/);
    expect(m.saveRules).not.toHaveBeenCalled();
  });

  it("IR4-4: a refused order reads the libraries again", async () => {
    mockLibrary();
    const second = { ...LIB_A, id: "lib-c", label: "第二片库", path: "\\\\nas-3\\films\\", position: 3 };
    libraries = { ...LIBRARIES, items: [LIB_A, second, CAT_K] };
    vi.spyOn(api, "libraryOrder").mockRejectedValue(new HttpError("顺序必须恰好包含每个演员片库一次", 422));
    const ws = mount(<LibraryPage />);
    await tab("归档规则");
    const prio = await screen.findByRole("list", { name: "片库优先级" });
    const reads = vi.mocked(api.libraries).mock.calls.length;
    fireEvent.click(within(prio).getByRole("button", { name: "上移 第二片库" }));
    await waitFor(() => expect(ws.toast).toHaveBeenCalledWith("顺序必须恰好包含每个演员片库一次"));
    await waitFor(() => expect(vi.mocked(api.libraries).mock.calls.length).toBeGreaterThan(reads));
  });
});

describe("L3 review fixes", () => {
  it("CX3-1/CX3-2: one card per name group (name_norm), whatever the display names", async () => {
    const m = mockLibrary();
    const trad = performer({ id: "p-z1", name: "張小雨", name_norm: "张小雨", library_id: "lib-a", film_count: 3 });
    const simp = performer({ id: "p-z2", name: "张小雨", name_norm: "张小雨", library_id: "lib-b", film_count: 4 });
    m.performers.mockResolvedValue({ items: [trad, simp], total: 1, page: 1, page_size: 50 });
    mount(<LibraryPage />);
    await tab("演员");
    const row = await screen.findByRole("button", { name: "張小雨 详情" });
    expect(row.querySelectorAll(".loc-chip")).toHaveLength(2);
    expect(within(row).getByText("7")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "张小雨 详情" })).toBeNull();
    expect(screen.getByText("显示 1 / 1 位")).toBeInTheDocument();
  });

  it("lists a card's locations in library-letter order, whatever order the page returned", async () => {
    const m = mockLibrary();
    const inB = performer({ id: "p-ren-b", name: "Ren", library_id: "lib-b", film_count: 2 });
    const inA = performer({ id: "p-ren-a", name: "Ren", library_id: "lib-a", film_count: 5 });
    m.performers.mockResolvedValue({ items: [inB, inA], total: 1, page: 1, page_size: 50 });
    mount(<LibraryPage />);
    await tab("演员");
    const card = await screen.findByRole("button", { name: "Ren 详情" });
    expect([...card.querySelectorAll(".loc-chip b")].map((b) => b.textContent)).toEqual(["A", "B"]);
  });

  it("CX3-1: the 演员 listing asks the route for whole name groups", async () => {
    const fetchMock = vi.fn(async () => Response.json({ items: [], total: 0, page: 1, page_size: 50 }));
    vi.stubGlobal("fetch", fetchMock);
    await api.libraryPerformers({ filter: "alias" });
    const url = new URL(String((fetchMock.mock.calls[0] as unknown[])[0]));
    expect(url.pathname).toBe("/control/workflow/performers");
    expect(Object.fromEntries(url.searchParams)).toEqual({ include_missing: "true", group: "true", filter: "alias" });
  });

  it("CX3-3: an idle scanner is still asked, so a scan the backend started shows up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockLibrary();
      mount(<LibraryPage />);
      const a = within(await card("主片库"));
      expect(a.queryByText("正在扫描演员目录…")).toBeNull();
      const reads = vi.mocked(api.libraryScanStatus).mock.calls.length;
      vi.mocked(api.libraryScanStatus).mockResolvedValue({ running: "lib-a", queued: [], results: {} });
      await act(async () => { await vi.advanceTimersByTimeAsync(SCAN_IDLE_MS); });
      expect(vi.mocked(api.libraryScanStatus).mock.calls.length).toBeGreaterThan(reads);
      expect(await a.findByText("正在扫描演员目录…")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a scan that ran between two idle polls re-reads the libraries, actors and thumbnails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const m = mockLibrary();
      mount(<LibraryPage />);
      await tab("演员");
      const haru = await screen.findByRole("button", { name: "Haru 详情" });
      await within(haru).findByRole("img", { name: "Haru 的 folder.jpg" });
      const reads = [vi.mocked(api.libraries), m.performers, vi.mocked(api.image)].map((f) => f.mock.calls.length);
      // idle before and after: only the new result tells that a scan finished
      vi.mocked(api.libraryScanStatus).mockResolvedValue({
        running: null, queued: [], results: { "lib-a": { library_id: "lib-a", ok: true, at: "2026-10-30T10:00:00Z" } },
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(SCAN_IDLE_MS); });
      await waitFor(() => {
        const now = [vi.mocked(api.libraries), m.performers, vi.mocked(api.image)].map((f) => f.mock.calls.length);
        now.forEach((n, i) => expect(n).toBeGreaterThan(reads[i]));
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("IR4-2: a refused import preview shows the backend's reason", async () => {
    const m = mockLibrary();
    libraries = NO_LIBRARIES;
    m.preview.mockRestore();
    vi.stubGlobal("fetch", vi.fn(async () =>
      Response.json({ detail: "不是可读的 Filmpaw 片库：file is not a database" }, { status: 422 })));
    mount(<LibraryPage />);
    fireEvent.click(await screen.findByRole("button", { name: "从 Filmpaw 导入" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("无法读取 Filmpaw 片库：不是可读的 Filmpaw 片库：file is not a database")).toBeInTheDocument();
  });

  it("CX3-4: with no libraries, 归档历史 stays reachable and the empty-state buttons still work", async () => {
    const m = mockLibrary();
    libraries = NO_LIBRARIES;
    mount(<LibraryPage />);
    expect(await screen.findByRole("heading", { name: "还没有片库" })).toBeInTheDocument();
    await tab("归档历史");
    expect(await screen.findByRole("row", { name: "OAE-198 回执" })).toBeInTheDocument();
    expect(m.history).toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "还没有片库" })).toBeNull();
    await tab("NAS 片库");
    fireEvent.click(await screen.findByRole("button", { name: "手动添加片库" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("CX3-5: a total that shrinks under the viewer falls back to the last page, with a working 上一页", async () => {
    const m = mockLibrary();
    const many = { ...PERFORMERS, total: 120, page_size: 50 };
    let total = 120;
    m.performers.mockImplementation(async (q) => {
      if (q.page_size === 1) return { ...PERFORMERS, items: PERFORMERS.items.slice(0, 1), page_size: 1 };
      return (q.page ?? 1) > Math.ceil(total / 50) ? { ...many, items: [], total, page: q.page ?? 1 } : { ...many, total, page: q.page ?? 1 };
    });
    mount(<LibraryPage />);
    await tab("演员");
    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(m.performers).toHaveBeenCalledWith(expect.objectContaining({ page: 2 })));
    total = 20;  // the data shrinks to one page; page 3 is then past the end
    await act(async () => { await vi.waitFor(() => expect(screen.getByText("2 / 3")).toBeInTheDocument()); });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(m.performers).toHaveBeenCalledWith(expect.objectContaining({ page: 3 })));
    await waitFor(() => expect(m.performers).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1 })));
    expect(await screen.findByRole("button", { name: "Haru 详情" })).toBeInTheDocument();
  });
});

describe("片库与规则 · 归档历史", () => {
  it("lists receipts with 打开目录 per target, filters, and opens the receipt drawer", async () => {
    const m = mockLibrary();
    mount(<LibraryPage />);
    await tab("归档历史");
    const second = await screen.findByRole("row", { name: "OAE-198 回执" });
    expect(within(second).getByText("+ 写真 · OAE")).toBeInTheDocument();
    expect(within(second).getByText("Haru")).toBeInTheDocument();
    fireEvent.click(within(second).getByRole("button", { name: "类型目录" }));
    await waitFor(() => expect(m.open).toHaveBeenCalledWith(ROW_SECOND.second_target_path));
    fireEvent.click(within(second).getByRole("button", { name: "演员目录" }));
    await waitFor(() => expect(m.open).toHaveBeenCalledWith(ROW_SECOND.target_path));
    expect(screen.queryByRole("dialog")).toBeNull();  // the buttons do not open the receipt
    const category = screen.getByRole("row", { name: "OAE-203 回执" });
    expect(within(category).getByText("类型 ·")).toBeInTheDocument();
    expect(within(category).getAllByRole("button")).toHaveLength(1);
    expect(within(screen.getByRole("row", { name: "DEMO-110 回执" })).getByText("待清理")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "待清理" }));
    await waitFor(() => expect(m.history).toHaveBeenCalledWith("pending", "", 1));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索番号或演员" }), { target: { value: "OAE" } });
    await waitFor(() => expect(m.history).toHaveBeenCalledWith("pending", "OAE", 1));

    fireEvent.click(screen.getByRole("row", { name: "OAE-198 回执" }));
    const drawer = await screen.findByRole("dialog", { name: "归档回执" });
    await waitFor(() => expect(m.receipt).toHaveBeenCalledWith("r-1"));
    expect(await within(drawer).findByText("两份各 2/2 个文件 SHA-256 一致 · 1.9 GB ×2")).toBeInTheDocument();
    expect(within(drawer).getByText("9f3a…c21e")).toHaveAttribute("title", RECEIPT.files[0].sha256);
    expect(within(drawer).getByText("第 2 份")).toBeInTheDocument();
    expect(within(drawer).getByText(/已清理主控 INBOX/)).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "打开「写真 · OAE」" }));
    await waitFor(() => expect(m.open).toHaveBeenLastCalledWith(ROW_SECOND.second_target_path));
  });
});

describe("设置", () => {
  it("embeds the 媒体工作流 section and saves the scan schedule to the library rules", async () => {
    const m = mockLibrary();
    vi.spyOn(api, "workflowSettings").mockRejectedValue(new Error("offline"));
    mount(<SettingsPage />);
    expect(screen.getByRole("heading", { level: 1, name: "设置" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "媒体工作流" })).toBeInTheDocument();
    const schedule = await screen.findByLabelText(/自动重新扫描/);
    expect(schedule).toHaveValue("6");
    fireEvent.change(schedule, { target: { value: "12" } });
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ scan_every_hours: 12 }));
    const onStart = screen.getByRole("switch", { name: "启动时扫描" });
    await waitFor(() => expect(onStart).toBeEnabled());
    fireEvent.click(onStart);
    await waitFor(() => expect(m.saveRules).toHaveBeenCalledWith({ scan_on_start: false }));
    expect(screen.getByText("D:\\TaskPaw\\work\\library.db")).toBeInTheDocument();
    expect(screen.getByText("2 个片库 · 1 个类型目录 · 334 个演员目录")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "再次导入" }));
    expect(await within(await screen.findByRole("dialog")).findByText(IMPORT_PREVIEW.path)).toBeInTheDocument();
  });
});

describe("MediaWorkspace · 片库与规则 and 设置", () => {
  it("renders both pages from the rail without waiting for the pipeline", async () => {
    mockLibrary();
    vi.spyOn(api, "workflowFilms").mockResolvedValue({ ...FILMS_RESPONSE, state: "off", reason: "未启用" });
    vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
    vi.spyOn(api, "workflowSettings").mockRejectedValue(new Error("offline"));
    vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><MediaWorkspace /></QueryClientProvider></ThemeProvider>);
    const rail = screen.getByRole("navigation", { name: "媒体处理中心导航" });
    fireEvent.click(within(rail).getByRole("button", { name: /^片库与规则/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "片库与规则" })).toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: "NAS 片库" })).toBeInTheDocument();
    fireEvent.click(within(rail).getByRole("button", { name: /^设置/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "设置" })).toBeInTheDocument();
    expect(await screen.findByLabelText(/自动重新扫描/)).toBeInTheDocument();
  });
});
