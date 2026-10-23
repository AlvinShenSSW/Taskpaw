// #299 R2, R3, R7–R10, §6: the first-run wizard end to end with a faked backend (boards Main,
// Onboard-Network, Onboard-Controller, Onboard-Worker, Onboard-Done), including resume at every step.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, HttpError, type Detection, type SetupState, type WorkflowSettingsState, type WorkflowSettingsView } from "../api";
import { theme } from "../theme";
import { AgentHome, SetupWizard } from "../views/SetupWizard";
import { DETECTED, network } from "./setupFixtures";

const BOOT = "0123456789abcdef0123456789abcdef";
vi.mock("../views/AgentConsole", () => ({ AgentConsole: () => <div>Agent console</div> }));

function view(over: Partial<WorkflowSettingsView> = {}): WorkflowSettingsView {
  return {
    enabled: false, role: "off", inbox_root: null, data_dir: null, ffprobe_path: null, jasna_exe_path: null,
    whisperjav_exe_path: null, ltx_gpu: "auto", mdcx_python: null, mdcx_root: null, mdcx_profile: null, filmpaw_data_dir: null,
    remote_workers: false, worker_api_port: 5682, staging_dir: null, staging_cap_gb: 500, ...over,
  };
}
function settings(over: Partial<WorkflowSettingsView> = {}): WorkflowSettingsState {
  return {
    saved: view(over), running: view(), restart_required: false, busy: { films: 0, remote: 0, unknown: false },
    presets: [{ id: "auto", label: "自动" }, { id: "5070ti-16", label: "5070 Ti 16 GB" }], ffprobe_detected: null, paths: {}, miko: "off",
  };
}
const WORKER_GPU: Detection = {
  ...DETECTED, gpu: { name: "NVIDIA GeForce RTX 5070 Ti", vram_mib: 16303, driver: "581.57", power_limit_w: 300, worker_ok: true },
};

function wrap(state: SetupState) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><SetupWizard state={state} /></QueryClientProvider></ThemeProvider>);
}

let saved: WorkflowSettingsState;
let step: ReturnType<typeof vi.fn>;
beforeEach(() => {
  // no request ever leaves the test (a real agent may listen on this host's 5681)
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline"))));
  window.__TASKPAW__ = { baseUrl: "http://127.0.0.1:5681", controlToken: "shell-token", role: "agent", bootId: BOOT, restart: true };
  saved = settings();
  vi.spyOn(api, "workflowSettings").mockImplementation(async () => saved);
  vi.spyOn(api, "workflowDetect").mockResolvedValue(DETECTED);
  vi.spyOn(api, "inspectDirectory").mockResolvedValue({ exists: false, free_bytes: 3.1 * 1024 ** 4, volume: "D:", local: true });
  vi.spyOn(api, "network").mockResolvedValue(network());
  vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_Generated0Generated0Generated012" });
  vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
  step = vi.fn(async (s, r) => ({ status: "pending", step: s, role: r }));
  vi.spyOn(api, "setupStep").mockImplementation(step as never);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("the wizard gate (R2)", () => {
  it("shows the wizard only while the marker is pending", async () => {
    vi.spyOn(api, "setup").mockResolvedValue({ status: "pending", step: "role", role: null });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><AgentHome /></QueryClientProvider></ThemeProvider>);
    expect(await screen.findByRole("heading", { name: "首次启动 · 选择角色" })).toBeInTheDocument();
    unmount();
    vi.spyOn(api, "setup").mockResolvedValue({ status: "absent", step: null, role: null });
    render(<ThemeProvider theme={theme}><QueryClientProvider client={new QueryClient()}><AgentHome /></QueryClientProvider></ThemeProvider>);
    expect(await screen.findByText("Agent console")).toBeInTheDocument();
  });
});

describe("选择角色 (R3)", () => {
  it("disables GPU 工作机 below 16 GB and shows the controller's GPU", async () => {
    wrap({ status: "pending", step: "role", role: null });
    expect(await screen.findByText("本机显卡不足 16 GB")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^GPU 工作机/ })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("本机 RTX 5060 8 GB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一步：网络与访问" })).toBeDisabled();
  });

  it("without an NVIDIA card the worker stays selectable with a warning", async () => {
    vi.spyOn(api, "workflowDetect").mockResolvedValue({ ...DETECTED, gpu: null });
    wrap({ status: "pending", step: "role", role: null });
    expect(await screen.findByText("未检测到 NVIDIA 显卡")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^GPU 工作机/ })).not.toHaveAttribute("aria-disabled", "true");
  });
});

describe("普通 Agent: role → network → done", () => {
  it("walks a fresh install to 开始使用", async () => {
    vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false });
    const complete = vi.spyOn(api, "setupComplete").mockResolvedValue({ status: "done", step: "done", role: "agent" });
    vi.spyOn(api, "restartState").mockResolvedValue({ required: false, fields: [], blocked: null, available: true });
    const restart = vi.spyOn(api, "restart");
    wrap({ status: "pending", step: "role", role: null });
    fireEvent.click(await screen.findByRole("radio", { name: /普通 Agent/ }));
    fireEvent.click(screen.getByRole("button", { name: "下一步：网络与访问" }));
    await waitFor(() => expect(step).toHaveBeenCalledWith("network", "agent"));
    expect(await screen.findByRole("heading", { name: "网络与访问" })).toBeInTheDocument();
    expect(await screen.findByRole("switch")).not.toBeDisabled(); // not forced for a plain agent
    await screen.findByText("已自动生成");
    fireEvent.click(screen.getByRole("button", { name: "下一步：完成" }));
    await waitFor(() => expect(step).toHaveBeenCalledWith("done", "agent"));
    expect(screen.getAllByText("选择角色").length).toBeGreaterThan(0);
    const start = await screen.findByRole("button", { name: "开始使用" });
    fireEvent.click(start);
    await waitFor(() => expect(complete).toHaveBeenCalled());
    expect(restart).not.toHaveBeenCalled();
  });
});

describe("媒体主控: role → network → controller → done", () => {
  it("prefills from detection, creates the suggested directories, saves and restarts", async () => {
    const create = vi.spyOn(api, "workflowCreateDir").mockResolvedValue({ ok: true, created: true });
    const saveWorkflow = vi.spyOn(api, "workflowSaveSettings").mockImplementation(async (patch) => {
      saved = settings({ ...patch, enabled: true, role: "controller" } as Partial<WorkflowSettingsView>);
      return { ok: true, restart_required: true };
    });
    wrap({ status: "pending", step: "network", role: "controller" }); // resumed at 网络与访问
    expect(await screen.findByRole("switch")).toBeDisabled(); // forced on
    await screen.findByText("已自动生成");
    fireEvent.click(screen.getByRole("button", { name: "下一步：主控设置" }));
    await waitFor(() => expect(step).toHaveBeenCalledWith("controller", "controller"));
    expect(await screen.findByLabelText("INBOX 目录（把影片复制到这里）")).toHaveValue("D:\\TaskpawInbox");
    expect(screen.getByLabelText("工作目录（处理中的副本）")).toHaveValue("D:\\TaskpawWork");
    expect(screen.getByText("3 / 3 已找到")).toBeInTheDocument(); // GPU workers on: no Jasna row
    expect(screen.queryByText("Jasna")).toBeNull();
    fireEvent.click(screen.getByRole("switch"));
    expect(await screen.findByText("4 / 4 已找到")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "下一步：完成" }));
    await waitFor(() => expect(saveWorkflow).toHaveBeenCalled());
    expect(create.mock.calls.map((c) => c[0])).toEqual(["D:\\TaskpawInbox", "D:\\TaskpawWork"]);
    expect(saveWorkflow.mock.calls[0][0]).toMatchObject({
      role: "controller", inbox_root: "D:\\TaskpawInbox", data_dir: "D:\\TaskpawWork", ffprobe_path: null,
      whisperjav_exe_path: "C:\\WhisperJAV\\Scripts\\whisperjav.exe", remote_workers: true, worker_api_port: 5682,
      mdcx_root: "D:\\Work\\engines\\MDCX304",
    });
    expect(saveWorkflow.mock.calls[0][0]).not.toHaveProperty("jasna_exe_path");
    await waitFor(() => expect(step).toHaveBeenCalledWith("done", "controller"));

  });

  it("the done step shows the saved rows, adds the rule, completes and restarts", async () => {
    saved = settings({ enabled: true, role: "controller", inbox_root: "D:\\TODO", data_dir: "D:\\TaskpawWork", remote_workers: true,
      whisperjav_exe_path: "C:\\WhisperJAV\\Scripts\\whisperjav.exe" });
    vi.spyOn(api, "network").mockResolvedValue(network({ lan_access: true, bind_host: "192.168.1.20", token_set: true, api_token_hint: "7Q2f" }));
    vi.spyOn(api, "restartState").mockResolvedValue({ required: true, fields: ["bind_host"], blocked: null, available: true });
    const fw = vi.spyOn(api, "networkFirewall").mockResolvedValue({ result: "declined" });
    const complete = vi.spyOn(api, "setupComplete").mockResolvedValue({ status: "done", step: "done", role: "controller" });
    const restart = vi.spyOn(api, "restart").mockResolvedValue({ restarting: true });
    wrap({ status: "pending", step: "done", role: "controller" });
    expect(await screen.findByText("192.168.1.20:5680")).toBeInTheDocument();
    expect(screen.getByText("tp_••••••7Q2f")).toBeInTheDocument();
    expect(screen.getByText("添加入站规则（专用网络）")).toBeInTheDocument();
    expect(screen.getByText("跑识别，不跑修复")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "立即重启 TaskPaw" }));
    await waitFor(() => expect(complete).toHaveBeenCalled());
    await waitFor(() => expect(restart).toHaveBeenCalledWith("agent", BOOT));
    expect(fw).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalled();
    expect(fw.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
    expect(await screen.findByText(/未添加防火墙规则，其他设备可能连不上本机/)).toBeInTheDocument();
    expect(await screen.findByText("正在重启 TaskPaw…")).toBeInTheDocument();
  });

  it("修改 goes back to its step without writing", async () => {
    saved = settings({ enabled: true, role: "controller", inbox_root: "D:\\TODO" });
    vi.spyOn(api, "restartState").mockResolvedValue({ required: false, fields: [], blocked: null, available: true });
    wrap({ status: "pending", step: "done", role: "controller" });
    const group = (await screen.findByText("目录与工作机")).closest("div")!.parentElement!;
    fireEvent.click(within(group).getByRole("button", { name: "修改" }));
    expect(await screen.findByLabelText("INBOX 目录（把影片复制到这里）")).toHaveValue("D:\\TODO");
    expect(step).not.toHaveBeenCalled();
  });
});

describe("GPU 工作机: 3a saves and restarts, 3b joins", () => {
  it("saves the fields, records 加入主控 and restarts", async () => {
    vi.spyOn(api, "workflowDetect").mockResolvedValue(WORKER_GPU);
    vi.spyOn(api, "workflowCreateDir").mockResolvedValue({ ok: true, created: true });
    const saveWorkflow = vi.spyOn(api, "workflowSaveSettings").mockResolvedValue({ ok: true, restart_required: true });
    const restart = vi.spyOn(api, "restart").mockResolvedValue({ restarting: true });
    wrap({ status: "pending", step: "worker_fields", role: "worker" });
    expect(await screen.findByText("NVIDIA GeForce RTX 5070 Ti · 16 GB")).toBeInTheDocument();
    expect(screen.getByText(/驱动 581.57 · 显存 16303 MiB · 功耗上限 300 W/)).toBeInTheDocument();
    expect(screen.getByLabelText("暂存目录")).toHaveValue("D:\\TaskpawStaging");
    expect(await screen.findByText(/本地磁盘 · 3.1 TB 可用/)).toBeInTheDocument(); // D5
    fireEvent.change(screen.getByLabelText("暂存上限"), { target: { value: "4000" } });
    expect(await screen.findByText("超过暂存目录所在磁盘的可用空间")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("暂存上限"), { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并启动工作机（约 10 秒）" }));
    await waitFor(() => expect(restart).toHaveBeenCalledWith("agent", BOOT));
    expect(saveWorkflow.mock.calls[0][0]).toEqual({
      role: "worker", jasna_exe_path: "C:\\Jasna\\jasna.exe", ltx_gpu: "auto", staging_dir: "D:\\TaskpawStaging", staging_cap_gb: 500,
    });
    expect(step).toHaveBeenCalledWith("worker_join", "worker");
    expect(step.mock.invocationCallOrder[0]).toBeLessThan(restart.mock.invocationCallOrder[0]);
    expect(await screen.findByText("正在重启 TaskPaw…")).toBeInTheDocument();
  });

  it("resumed at 加入主控: local check, refused code, then joined → 下一步", async () => {
    const unjoined = { state: "unjoined", reason: null, worker_id: null, controller: null, credential: "none", staging_dir: "D:\\TaskpawStaging", staging_cap_gb: 500 };
    const worker = vi.spyOn(api, "workflowWorker").mockResolvedValue(unjoined as never);
    const join = vi.spyOn(api, "workflowJoin").mockRejectedValueOnce(new HttpError(
      "配对码无效、已使用或已过期（15 分钟内有效，只能用一次）。请在主控 设备 → 添加设备 重新生成，再粘贴一次。", 422));
    wrap({ status: "pending", step: "worker_join", role: "worker" });
    const next = await screen.findByRole("button", { name: "下一步：完成" });
    expect(next).toBeDisabled();
    expect(screen.getByText("加入主控后才能继续")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("配对码"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("配对码格式不对，应以 TP1: 开头");
    expect(join).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("配对码"), { target: { value: "TP1:secret-code-never-shown" } });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("15 分钟内有效，只能用一次");
    expect(join).toHaveBeenCalledWith("TP1:secret-code-never-shown", "D:\\TaskpawStaging");
    expect(screen.getByLabelText("配对码")).toHaveValue(""); // never kept
    expect(document.body.textContent).not.toContain("secret-code-never-shown");
    const joined = { ...unjoined, state: "connecting", worker_id: "w-1", controller: "192.168.1.20:5682", credential: "valid" };
    join.mockResolvedValueOnce(joined as never);
    worker.mockResolvedValue(joined as never);
    fireEvent.change(screen.getByLabelText("配对码"), { target: { value: "TP1:good" } });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(await screen.findByText(/已加入主控 192.168.1.20:5682/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一步：完成" }));
    await waitFor(() => expect(step).toHaveBeenCalledWith("done", "worker"));
  });
});

describe("resume after a restart (§6)", () => {
  it.each([
    ["role", null, "首次启动 · 选择角色"],
    ["network", "agent", "网络与访问"],
    ["controller", "controller", "主控设置"],
    ["worker_fields", "worker", "GPU 工作机设置"],
    ["worker_join", "worker", "GPU 工作机设置"],
    ["done", "agent", "完成"],
    ["done", "controller", "完成并重启"],
  ] as const)("step %s (%s) opens where it stopped", async (at, role, title) => {
    vi.spyOn(api, "workflowWorker").mockResolvedValue({ state: "unjoined", worker_id: null } as never);
    vi.spyOn(api, "restartState").mockResolvedValue({ required: false, fields: [], blocked: null, available: true });
    wrap({ status: "pending", step: at, role });
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  });

  it("is usable at 390 px (no fixed-width grid)", async () => {
    window.matchMedia = ((q: string) => ({ matches: q.includes("max-width"), media: q, addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false, onchange: null })) as never;
    wrap({ status: "pending", step: "role", role: null });
    expect(await screen.findByRole("radiogroup", { name: "这台电脑的角色" })).toBeInTheDocument();
    delete (window as { matchMedia?: unknown }).matchMedia;
  });
});
