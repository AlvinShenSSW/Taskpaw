import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type DeviceRow, type DevicesView, type WorkerConsole } from "../api";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { WorkerJoinSection } from "../components/WorkerJoin";
import { Settings } from "../views/Settings";
import { theme } from "../theme";
import { setLang } from "../i18n";
import { FILMS_RESPONSE, PLANS } from "./fixtures/mediaWorkspace";

// P2-2 (#293) 设备 v1 and the worker console: every row state and dialog of contract §5.
const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

const PROFILES = [
  { stage: "restore" as const, tool: "jasna", tool_version: "unpinned", model: "ltx", model_version: "int8", profile: "jasna-ltx", max_width: 1920, max_height: 1080 },
  { stage: "asr" as const, tool: "whisperjav", tool_version: "unpinned", model: "faster", model_version: "japanese", profile: "whisperjav", max_width: 3840, max_height: 2160 },
];
const PROBE = { gpu_model: "NVIDIA GeForce RTX 5060 Ti", vram_mib: 16311, free_disk_bytes: 900e9, profiles: PROFILES };

function device(over: Partial<DeviceRow> = {}): DeviceRow {
  return {
    worker_id: `w-${Math.random()}`, version: 3, name: "5060 Ti-1", address: "192.168.1.31", port: 5680,
    enrollment_state: "verified", admission_state: "enabled", participation: true,
    heartbeat: { state: "online", age_s: 4 },
    gpu: { name: "NVIDIA GeForce RTX 5060 Ti", vram_mib: 16311, used_mib: 900, util: 3, power_w: 21.5, hold: "none" },
    disk: { free: 900e9, staging_used: 40e9, cap: 500e9 },
    tiers_ready: ["standard", "good"], asr_ready: true, agent_version: "3.9.19", version_ok: true,
    boot_reconciled: true, eligibility: "可接单", eligible: true,
    probe: PROBE, approved: { probe: PROBE, profiles: PROFILES },
    restore_allowed: true, unresolved: 0, can_retire: false, ...over,
  };
}

const ROWS: DeviceRow[] = [
  device({ worker_id: "online", name: "5070 Ti" }),
  device({ worker_id: "pairing", name: "新设备", enrollment_state: "pending_pairing", admission_state: "disabled",
    heartbeat: { state: "waiting", age_s: null }, gpu: null, disk: null, tiers_ready: [], asr_ready: false,
    agent_version: null, version_ok: null, probe: null, approved: null, eligibility: "未核验", eligible: false }),
  device({ worker_id: "validate", name: "5060 Ti-2", enrollment_state: "pending_validation", admission_state: "disabled",
    eligibility: "未核验", eligible: false,
    probe: { ...PROBE, profiles: [{ ...PROFILES[0], tool_version: "2" }, PROFILES[1]] } }),
  device({ worker_id: "draining", name: "5060 Ti-3", admission_state: "draining", eligibility: "停领", eligible: false }),
  device({ worker_id: "lost", name: "5060 Ti-4", heartbeat: { state: "lost", age_s: 75 }, eligibility: "离线", eligible: false,
    version_ok: false, agent_version: "3.9.18" }),
  device({ worker_id: "revoked", name: "旧卡", admission_state: "revoked", can_retire: true, eligibility: "已撤销", eligible: false }),
  device({ worker_id: "held", name: "旧卡-2", admission_state: "revoked", unresolved: 1, can_retire: false }),
  device({ worker_id: "small", name: "5060", enrollment_state: "pending_validation", admission_state: "disabled",
    restore_allowed: false, approved: null, probe: { ...PROBE, gpu_model: "NVIDIA GeForce RTX 5060", vram_mib: 8151 } }),
];
const VIEW: DevicesView = {
  remote_workers: true, server_time: "2026-10-23T08:00:00Z", controller: { name: "本机", stages: ["翻译"] }, workers: ROWS,
};

function mockApi(view: DevicesView = VIEW) {
  vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS_RESPONSE);
  vi.spyOn(api, "workflowPlans").mockResolvedValue(PLANS);
  vi.spyOn(api, "image").mockResolvedValue("data:image/png;base64,");
  vi.spyOn(api, "agentStatus").mockResolvedValue({ machine: "HOME-PC", monitors: {} });
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: {} });
  return {
    devices: vi.spyOn(api, "workflowDevices").mockResolvedValue(view),
    add: vi.spyOn(api, "workflowAddDevice").mockResolvedValue({
      worker_id: "new", code: "TP1:192.168.1.20:5682:00000000-0000-4000-8000-000000000001:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK", expires_at: "2026-10-23T08:15:00Z",
    }),
    approve: vi.spyOn(api, "workflowApproveDevice").mockResolvedValue({ ok: true }),
    admission: vi.spyOn(api, "workflowDeviceAdmission").mockResolvedValue({ ok: true }),
    repair: vi.spyOn(api, "workflowRepairDevice").mockResolvedValue({
      worker_id: "online", code: "TP1:192.168.1.20:5682:00000000-0000-4000-8000-000000000002:ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqpon", expires_at: "2026-10-23T08:15:00Z",
    }),
    revoke: vi.spyOn(api, "workflowRevokeDevice").mockResolvedValue({ ok: true }),
    retire: vi.spyOn(api, "workflowRetireDevice").mockResolvedValue({ ok: true }),
  };
}

const rail = () => screen.getByRole("navigation", { name: "媒体处理中心导航" });
async function openDevices() {
  wrap(<MediaWorkspace />);
  await screen.findByRole("heading", { level: 1, name: "概览" });
  fireEvent.click(within(rail()).getByRole("button", { name: /^设备/ }));
  await screen.findByRole("heading", { level: 1, name: "设备" });
  return screen.findByRole("article", { name: "本机" });
}
const card = (name: string) => screen.getByRole("article", { name });

beforeEach(() => { setLang("zh-CN"); vi.spyOn(window, "scrollTo").mockImplementation(() => {}); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("设备 v1", () => {
  it("is live in the rail, with one compact title and the 本机 row without actions", async () => {
    mockApi();
    const local = await openDevices();
    expect(within(rail()).getByRole("button", { name: /^设备/ })).not.toHaveTextContent("后续");
    expect(within(screen.getByRole("main")).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(local).toHaveTextContent("主控 · 运行 翻译");
    expect(within(local).queryByRole("button")).toBeNull();
    expect(screen.queryByText("此功能将在多机工作流阶段提供")).toBeNull();
  });

  it("shows each row's enrollment, heartbeat, GPU, disk, tiers, version and reason as text", async () => {
    mockApi();
    await openDevices();
    const online = card("5070 Ti");
    expect(online).toHaveTextContent("已启用");
    expect(online).toHaveTextContent("在线 4s 前");
    expect(online).toHaveTextContent("NVIDIA GeForce RTX 5060 Ti · 16 GB · 3% · 22W");
    expect(online).toHaveTextContent("可用 900 GB · 暂存 40 GB / 500 GB");
    expect(online).toHaveTextContent("标准✓ 良好✓ 高✗ · 识别✓");
    expect(online).toHaveTextContent("可接单");
    expect(within(online).getByRole("button", { name: "停领" })).toBeInTheDocument();
    expect(within(online).queryByRole("button", { name: "核验并启用" })).toBeNull();
    expect(card("新设备")).toHaveTextContent("待配对");
    expect(card("新设备")).toHaveTextContent("等待心跳");
    expect(card("5060 Ti-2")).toHaveTextContent("待核验");
    expect(within(card("5060 Ti-3")).getByRole("button", { name: "恢复" })).toBeInTheDocument();
    const lost = card("5060 Ti-4");
    expect(lost).toHaveTextContent("失联");
    expect(lost).toHaveTextContent("3.9.18 · 版本不一致");
    expect(lost).toHaveTextContent("离线");
    expect(within(card("旧卡")).getByRole("button", { name: "退休" })).toBeInTheDocument();
    expect(within(card("旧卡")).queryByRole("button", { name: "撤销" })).toBeNull();
    const held = card("旧卡-2");
    expect(within(held).queryByRole("button", { name: "退休" })).toBeNull();
    expect(held).toHaveTextContent("仍有 1 个未结束的任务，结束后才能退休。");
  });

  it("adds a device and shows its code with copy and expiry", async () => {
    const calls = mockApi();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    await openDevices();
    fireEvent.click(screen.getByRole("button", { name: "添加设备" }));
    const dialog = await screen.findByRole("dialog", { name: "添加设备" });
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "5060 Ti-5" } });
    fireEvent.change(within(dialog).getByLabelText("地址"), { target: { value: "192.168.1.35" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "生成配对码" }));
    await waitFor(() => expect(calls.add).toHaveBeenCalledWith("5060 Ti-5", "192.168.1.35"));
    const code = await screen.findByRole("dialog", { name: "5060 Ti-5 的配对码" });
    expect(within(code).getByLabelText("配对码")).toHaveTextContent(/^TP1:192\.168\.1\.20:5682:/);
    expect(code).toHaveTextContent("只能使用一次");
    fireEvent.click(within(code).getByRole("button", { name: "复制" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/^TP1:/)));
    expect(await within(code).findByText("已复制")).toBeInTheDocument();
  });

  it("核验并启用 compares the probe with the last approval and sends the switches", async () => {
    const calls = mockApi();
    await openDevices();
    fireEvent.click(within(card("5060 Ti-2")).getByRole("button", { name: "核验并启用" }));
    const dialog = await screen.findByRole("dialog", { name: "核验并启用 · 5060 Ti-2" });
    const changed = within(dialog).getAllByText("已变化");
    expect(changed).toHaveLength(1);
    expect(changed[0].closest("tr")).toHaveTextContent("修复");
    expect(dialog).toHaveTextContent("启用后，修复与识别交给远程设备");
    fireEvent.click(within(dialog).getByRole("switch", { name: "识别" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "核验并启用" }));
    await waitFor(() => expect(calls.approve).toHaveBeenCalledWith("validate", 3, true, false));
  });

  it("disables 修复 under 16 GB with the reason", async () => {
    const calls = mockApi();
    await openDevices();
    fireEvent.click(within(card("5060")).getByRole("button", { name: "核验并启用" }));
    const dialog = await screen.findByRole("dialog", { name: "核验并启用 · 5060" });
    expect(within(dialog).getByRole("switch", { name: "修复" })).toBeDisabled();
    expect(dialog).toHaveTextContent("显存不足，不参与修复");
    expect(dialog).toHaveTextContent("首次核验");
    fireEvent.click(within(dialog).getByRole("button", { name: "核验并启用" }));
    await waitFor(() => expect(calls.approve).toHaveBeenCalledWith("small", 3, false, true));
  });

  it("drains, re-pairs, revokes after confirmation and retires with the row version", async () => {
    const calls = mockApi();
    await openDevices();
    fireEvent.click(within(card("5070 Ti")).getByRole("button", { name: "停领" }));
    await waitFor(() => expect(calls.admission).toHaveBeenCalledWith("online", 3, "draining"));
    fireEvent.click(within(card("5060 Ti-3")).getByRole("button", { name: "恢复" }));
    await waitFor(() => expect(calls.admission).toHaveBeenCalledWith("draining", 3, "enabled"));
    fireEvent.click(within(card("5070 Ti")).getByRole("button", { name: "重新配对" }));
    expect(await screen.findByRole("dialog", { name: "5070 Ti 的配对码" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(within(card("5070 Ti")).getByRole("button", { name: "撤销" }));
    const confirm = await screen.findByRole("dialog", { name: "撤销 5070 Ti" });
    expect(calls.revoke).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole("button", { name: "确认撤销" }));
    await waitFor(() => expect(calls.revoke).toHaveBeenCalledWith("online", 3));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(within(card("旧卡")).getByRole("button", { name: "退休" })).toBeEnabled());
    fireEvent.click(within(card("旧卡")).getByRole("button", { name: "退休" }));
    await waitFor(() => expect(calls.retire).toHaveBeenCalledWith("revoked", 3));
  });

  it("explains a controller without remote workers and disables 添加设备", async () => {
    mockApi({ ...VIEW, remote_workers: false, workers: [], controller: { name: "本机", stages: ["修复", "识别", "翻译"] } });
    await openDevices();
    expect(screen.getByRole("button", { name: "添加设备" })).toBeDisabled();
    expect(screen.getByText(/未开启远程设备/)).toBeInTheDocument();
  });

  it("keeps 44 px controls and a single column at 390 px", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width:900px") || query.includes("max-width: 900px"), media: query, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }));
    mockApi();
    wrap(<MediaWorkspace />);
    await screen.findByRole("heading", { level: 1, name: "概览" });
    fireEvent.click(screen.getByRole("button", { name: "工作区导航" }));
    const nav = await screen.findByRole("dialog");
    fireEvent.click(within(nav).getByRole("button", { name: /^设备/ }));
    await screen.findByRole("heading", { level: 1, name: "设备" });
    const list = (await screen.findByRole("article", { name: "本机" })).parentElement as HTMLElement;
    expect(list).toHaveClass("stack");
    for (const button of within(card("5070 Ti")).getAllByRole("button")) expect(button).toHaveClass("btn");
  });
});

describe("worker console", () => {
  const unjoined: WorkerConsole = {
    state: "unjoined", reason: "尚未加入媒体工作流", role: "worker", controller: null, worker_id: null,
    credential: null, staging_dir: "E:\\Staging", staging_cap_gb: 500,
  };

  it("shows only on a worker and joins with the code and staging dir", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { role: "worker" } } as never);
    vi.spyOn(api, "workflowWorker").mockResolvedValue(unjoined);
    const join = vi.spyOn(api, "workflowJoin").mockResolvedValue({
      ...unjoined, state: "connecting", reason: "正在连接主控", controller: "192.168.1.20:5682", worker_id: "w1", credential: "valid",
    });
    wrap(<Settings role="agent" />);
    expect(await screen.findByText("媒体工作流")).toBeInTheDocument();
    expect(screen.getByText(/状态：未加入/)).toBeInTheDocument();
    expect(screen.getByLabelText("暂存目录")).toHaveValue("E:\\Staging");
    fireEvent.change(screen.getByLabelText("配对码"), { target: { value: " TP1:code " } });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    await waitFor(() => expect(join).toHaveBeenCalledWith("TP1:code", "E:\\Staging"));
    expect(await screen.findByText(/主控：192\.168\.1\.20:5682/)).toBeInTheDocument();
    expect(screen.getByText(/凭据：有效/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加入" })).toBeInTheDocument();
    expect(screen.getByLabelText("配对码")).toHaveValue("");
  });

  it("shows a refused join without echoing the code", async () => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ ...unjoined, worker_id: "w1", credential: "revoked", state: "revoked" });
    vi.spyOn(api, "workflowJoin").mockRejectedValue(new Error("配对码无效、已使用或已过期"));
    wrap(<WorkerJoinSection />);
    expect(await screen.findByText(/凭据：已撤销，请重新加入/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("配对码"), { target: { value: "TP1:secret-code" } });
    fireEvent.click(screen.getByRole("button", { name: "重新加入" }));
    expect(await screen.findByText("配对码无效、已使用或已过期")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret-code");
  });

  it("is absent on a controller", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], workflow: { role: "controller" } } as never);
    const worker = vi.spyOn(api, "workflowWorker");
    wrap(<Settings role="agent" />);
    await screen.findAllByText(/LLM API/);
    expect(screen.queryByText("媒体工作流")).toBeNull();
    expect(worker).not.toHaveBeenCalled();
  });
});
