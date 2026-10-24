// #299 R4–R6, R11–R13: 设置 sections, 网络与访问, 功耗与主机监控, RestartBanner and the detection rows.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, clearControlCredentials, HttpError, setDevControlCredential, type WorkflowSettingsState } from "../api";
import { DETECTED, network } from "./setupFixtures";
import { theme } from "../theme";
import { Settings } from "../views/Settings";
import { NetworkAccess } from "../components/NetworkAccess";
import { RestartBanner } from "../components/RestartBanner";
import { FIREWALL_TASKBAR_MS, RESTART_WAIT_MS, tokenProblem } from "../components/setupModel";
import { WorkflowSettingsSection } from "../components/WorkflowSettings";

const BOOT = "0123456789abcdef0123456789abcdef";

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{node}</QueryClientProvider></ThemeProvider>);
}

function inShell(restart = true) {
  window.__TASKPAW__ = { baseUrl: "http://127.0.0.1:5681", controlToken: "shell-token", role: "agent", bootId: BOOT, restart };
}

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline")))); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("RestartBanner (R11, §3)", () => {
  it("restarts through the shell, then waits and says it failed after the wait", async () => {
    inShell();
    const restart = vi.spyOn(api, "restart").mockResolvedValue({ restarting: true });
    wrap(<RestartBanner role="agent" text="已保存，重启 TaskPaw 后生效（本机地址）" />);
    expect(screen.getByRole("status")).toHaveTextContent("已保存，重启 TaskPaw 后生效（本机地址）");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole("button", { name: "立即重启" }));
    await waitFor(() => expect(restart).toHaveBeenCalledWith("agent", BOOT));
    expect(await screen.findByText("正在重启 TaskPaw…")).toBeInTheDocument();
    // review IR11: a labelled modal dialog that takes the focus
    const dialog = screen.getByRole("alertdialog", { name: "正在重启 TaskPaw…" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(dialog).toHaveFocus());
    act(() => { vi.advanceTimersByTime(RESTART_WAIT_MS - 1000); });
    expect(screen.getByText("正在重启 TaskPaw…")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(await screen.findByText("重启失败，请退出并重新打开 TaskPaw")).toBeInTheDocument();
  });

  it("waits longer than the old shutdown budget plus readiness (review IR8)", () => {
    // the shell may wait 5 s (graceful stop) + 30 s (readiness) + the window rebuild
    expect(RESTART_WAIT_MS).toBeGreaterThanOrEqual(75_000);
  });

  it("shows a busy refusal and offers no button outside the shell", async () => {
    inShell();
    vi.spyOn(api, "restart").mockRejectedValue(new HttpError("暂不能重启：本机正在运行：修复 ABC-123", 409, "restart_busy"));
    const { unmount } = wrap(<RestartBanner role="agent" text="已保存" />);
    fireEvent.click(screen.getByRole("button", { name: "立即重启" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂不能重启：本机正在运行：修复 ABC-123");
    unmount();
    delete window.__TASKPAW__;
    clearControlCredentials();
    setDevControlCredential("agent", "test-agent-control");
    wrap(<RestartBanner role="agent" text="已保存" />);
    expect(screen.queryByRole("button", { name: "立即重启" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("请退出并重新打开 TaskPaw");
    fireEvent.click(screen.getByRole("button", { name: "稍后" }));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("网络与访问 (R4–R6)", () => {
  it("generates a token on entering and saves the switch, address, ports and token", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network());
    const gen = vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_Generated0Generated0Generated012" });
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
    const saved = vi.fn();
    wrap(<NetworkAccess forceLan wizard tokenMode="generate" submitLabel="下一步：主控设置" onSaved={saved} />);
    await waitFor(() => expect(gen).toHaveBeenCalledWith("generate"));
    expect(await screen.findByText("已自动生成")).toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeChecked();
    expect(screen.getByRole("switch")).toBeDisabled(); // forced on for 主控 / 工作机
    expect(screen.getByText("192.168.1.20")).toBeInTheDocument();
    expect(screen.getByText("完成设置时自动添加入站规则（仅专用网络）")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一步：主控设置" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({
      lan_access: true, bind_host: "192.168.1.20", bind_port: 5680, control_port: 5681, api_token: "tp_Generated0Generated0Generated012",
    }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
  });

  it("an existing token is masked, revealed on demand, and kept on save", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network({ lan_access: true, bind_host: "10.0.0.5", token_set: true, api_token_hint: "7Q2f" }));
    const token = vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_SavedSavedSavedSaved7Q2f" });
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false });
    wrap(<NetworkAccess />);
    const field = await screen.findByLabelText("访问令牌");
    expect(field).toHaveValue("tp_••••••7Q2f");
    expect(token).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "显示" }));
    await waitFor(() => expect(field).toHaveValue("tp_SavedSavedSavedSaved7Q2f"));
    expect(token).toHaveBeenCalledWith("reveal");
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    expect(screen.getByRole("alert")).toHaveTextContent("重新生成后，家里其他 TaskPaw 和 Hub 都要换成新令牌。");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ lan_access: true, bind_host: "10.0.0.5", bind_port: 5680, control_port: 5681 }));
  });

  it("a saved replacement token replaces the cached one (Codex r2 CX5)", async () => {
    const view = vi.spyOn(api, "network").mockResolvedValue(network({ lan_access: true, bind_host: "10.0.0.5", token_set: true, api_token_hint: "7Q2f" }));
    const token = vi.spyOn(api, "networkToken").mockImplementation(async () => ({ token: "tp_SavedSavedSavedSaved7Q2f" }));
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false });
    wrap(<NetworkAccess />);
    const field = await screen.findByLabelText("访问令牌");
    fireEvent.click(screen.getByRole("button", { name: "显示" }));
    await waitFor(() => expect(field).toHaveValue("tp_SavedSavedSavedSaved7Q2f")); // the old token is now cached
    fireEvent.click(screen.getByRole("button", { name: "粘贴主控的访问令牌" }));
    fireEvent.change(await screen.findByLabelText("访问令牌"), { target: { value: "tp_ReplacementReplacementReplace9Zz" } });
    view.mockResolvedValue(network({ lan_access: true, bind_host: "10.0.0.5", token_set: true, api_token_hint: "e9Zz" }));
    token.mockImplementation(async () => ({ token: "tp_ReplacementReplacementReplace9Zz" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ api_token: "tp_ReplacementReplacementReplace9Zz" })));
    await waitFor(() => expect(screen.getByLabelText("访问令牌")).toHaveValue("tp_••••••e9Zz"));
    token.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "显示" }));
    await waitFor(() => expect(screen.getByLabelText("访问令牌")).toHaveValue("tp_ReplacementReplacementReplace9Zz"));
    expect(token).toHaveBeenCalledWith("reveal"); // fetched again, not the obsolete cached value
  });

  it("a machine other than the 主控 pastes the home token (review IR2)", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network());
    const gen = vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_Generated0Generated0Generated012" });
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
    wrap(<NetworkAccess forceLan wizard tokenMode="paste" submitLabel="下一步：工作机设置" />);
    const field = await screen.findByLabelText("访问令牌");
    expect(gen).not.toHaveBeenCalled(); // never a second home token by default
    expect(field).not.toHaveAttribute("readonly");
    expect(screen.getByText("家里所有 TaskPaw 用同一个访问令牌：在主控 设置 → 网络与访问 复制")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("配对时会自动带上令牌");
    fireEvent.click(screen.getByRole("button", { name: "下一步：工作机设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("访问令牌：请粘贴主控的访问令牌，或生成新令牌");
    fireEvent.change(field, { target: { value: "tp_tooShort" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步：工作机设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("访问令牌：令牌格式不对");
    expect(save).not.toHaveBeenCalled();
    vi.stubGlobal("navigator", { ...navigator, clipboard: { readText: async () => "  tp_HomeTokenHomeTokenHomeTokenHome12  " } });
    fireEvent.click(screen.getByRole("button", { name: "粘贴" }));
    await waitFor(() => expect(field).toHaveValue("tp_HomeTokenHomeTokenHomeTokenHome12"));
    fireEvent.click(screen.getByRole("button", { name: "下一步：工作机设置" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({
      lan_access: true, bind_host: "192.168.1.20", bind_port: 5680, control_port: 5681, api_token: "tp_HomeTokenHomeTokenHomeTokenHome12",
    }));
  });

  it("the very first machine may generate instead of pasting", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network());
    const gen = vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_Generated0Generated0Generated012" });
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
    wrap(<NetworkAccess wizard tokenMode="paste" submitLabel="下一步：完成" />);
    fireEvent.click(await screen.findByRole("button", { name: "生成新令牌" }));
    await waitFor(() => expect(gen).toHaveBeenCalledWith("generate"));
    expect(await screen.findByText("已自动生成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch")); // a plain agent turns LAN on
    fireEvent.click(screen.getByRole("button", { name: "下一步：完成" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ api_token: "tp_Generated0Generated0Generated012" })));
  });

  it("a plain agent with LAN off needs no token", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network());
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false });
    wrap(<NetworkAccess wizard tokenMode="paste" submitLabel="下一步：完成" />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步：完成" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ lan_access: false, bind_port: 5680, control_port: 5681 }));
  });

  it("设置 can paste a token; it replaces the saved one only on 保存", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network({ lan_access: true, bind_host: "192.168.1.20", token_set: true, api_token_hint: "7Q2f" }));
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false });
    wrap(<NetworkAccess />);
    fireEvent.click(await screen.findByRole("button", { name: "粘贴主控的访问令牌" }));
    const field = screen.getByLabelText("访问令牌");
    expect(field).toHaveAttribute("type", "password"); // masked
    fireEvent.change(field, { target: { value: "legacy-home-token-1234" } }); // an existing ≥ 16-char token
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ api_token: "legacy-home-token-1234" })));
  });

  it("the token rule: tp_ + ≥ 32 base62, or any other ≥ 16-character token", () => {
    expect(tokenProblem("tp_" + "a".repeat(32))).toBeNull();
    expect(tokenProblem("tp_" + "Z9".repeat(20))).toBeNull();
    expect(tokenProblem("legacy-home-token-1234")).toBeNull();
    expect(tokenProblem("tp_" + "a".repeat(31))).not.toBeNull();
    expect(tokenProblem("tp_" + "a".repeat(31) + "-")).not.toBeNull();
    expect(tokenProblem("short-token")).not.toBeNull();
    expect(tokenProblem("has space in token 1234")).not.toBeNull();
  });

  it("an elevation still open after 5 s points at the taskbar; a timeout says so (review IR3)", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network({ lan_access: true, bind_host: "192.168.1.20", token_set: true }));
    let answer: (v: { result: "timeout" }) => void = () => {};
    vi.spyOn(api, "networkFirewall").mockImplementation(() => new Promise((resolve) => { answer = resolve; }));
    wrap(<NetworkAccess />);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(await screen.findByRole("button", { name: "添加入站规则" }));
    await screen.findByRole("button", { name: "等待管理员确认…" });
    expect(screen.queryByText("请在任务栏找到 Windows 管理员确认窗口")).toBeNull();
    act(() => { vi.advanceTimersByTime(FIREWALL_TASKBAR_MS); });
    expect(await screen.findByText("请在任务栏找到 Windows 管理员确认窗口")).toBeInTheDocument();
    act(() => answer({ result: "timeout" }));
    expect(await screen.findByText(/管理员确认未完成（超时）/)).toBeInTheDocument();
    expect(screen.queryByText("请在任务栏找到 Windows 管理员确认窗口")).toBeNull();
  });

  it("shows per-field refusals, the address change, the firewall retry and the public profile", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network({
      lan_access: true, bind_host: "192.168.1.20", token_set: true, api_token_hint: null, address_changed_from: "192.168.1.9",
      firewall: { platform: "windows", rule: "absent", profile: "public" },
    }));
    vi.spyOn(api, "updateConfig").mockRejectedValue(new HttpError("设置有误", 422, undefined, { bind_port: "端口已被占用" }));
    const fw = vi.spyOn(api, "networkFirewall").mockResolvedValue({ result: "declined" });
    wrap(<NetworkAccess />);
    expect(await screen.findByText(/本机地址已从 192.168.1.9 变为 192.168.1.20/)).toBeInTheDocument();
    expect(screen.getByText(/公用网络/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "添加入站规则" }));
    await waitFor(() => expect(fw).toHaveBeenCalled());
    expect(await screen.findByText(/未添加防火墙规则，其他设备可能连不上本机/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("未保存，请修改：局域网端口：端口已被占用")).toBeInTheDocument();
  });

  it("LAN off saves loopback and macOS gets its note", async () => {
    vi.spyOn(api, "network").mockResolvedValue(network({
      lan_access: true, bind_host: "192.168.1.20", token_set: true, firewall: { platform: "macos", rule: "unknown", profile: null },
    }));
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
    wrap(<NetworkAccess />);
    expect(await screen.findByText(/macOS 会询问是否允许传入连接/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ lan_access: false, bind_port: 5680, control_port: 5681 }));
  });
});

describe("设置 sections (R12)", () => {
  it("has a side nav on desktop and a select at 390 px; 功耗与主机监控 saves its switches", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], machine: "m", host_metrics: true, power: { enabled: true } } as never);
    vi.spyOn(api, "restartState").mockResolvedValue({ required: true, fields: ["host_metrics"], blocked: null, available: true });
    const save = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: true });
    const { unmount } = wrap(<Settings role="agent" />);
    const nav = screen.getByRole("navigation", { name: "设置" });
    for (const name of ["通用", "网络与访问", "媒体工作流", "功耗与主机监控", "关于"]) {
      expect(within(nav).getByText(name)).toBeInTheDocument();
    }
    fireEvent.click(within(nav).getByText("功耗与主机监控"));
    expect(await screen.findByRole("status")).toHaveTextContent("已保存，重启 TaskPaw 后生效（主机监控）");
    const [power, host] = screen.getAllByRole("switch");
    fireEvent.click(power);
    await waitFor(() => expect(save).toHaveBeenCalledWith({ power: { enabled: false } }));
    fireEvent.click(host);
    await waitFor(() => expect(save).toHaveBeenCalledWith({ host_metrics: false }));
    unmount();
    window.matchMedia = ((q: string) => ({
      matches: q.includes("max-width"), media: q, onchange: null, addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }) as MediaQueryList);
    wrap(<Settings role="agent" />);
    expect(screen.queryByRole("navigation", { name: "设置" })).toBeNull();
    expect(screen.getByLabelText("设置")).toBeInTheDocument();
    delete (window as { matchMedia?: unknown }).matchMedia;
  });

  it("a machine-name change offers the pending restart and keeps it across a remount (Codex r6 CX13)", async () => {
    inShell();
    let pending = false;
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], machine: "m" } as never);
    vi.spyOn(api, "restartState").mockImplementation(async () => (
      { required: pending, fields: pending ? ["machine"] : [], blocked: null, available: true }));
    vi.spyOn(api, "updateConfig").mockImplementation(async () => { pending = true; return { ok: true, restart_required: true }; });
    const first = wrap(<Settings role="agent" />);
    fireEvent.change(await screen.findByLabelText(/机器名称|Machine name/), { target: { value: "renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("status")).toHaveTextContent("已保存，重启 TaskPaw 后生效（机器名称）");
    expect(screen.getByRole("button", { name: "立即重启" })).toBeInTheDocument();
    first.unmount();
    wrap(<Settings role="agent" />);
    expect(await screen.findByRole("status")).toHaveTextContent("已保存，重启 TaskPaw 后生效（机器名称）");
    expect(screen.getByRole("button", { name: "立即重启" })).toBeInTheDocument();
  });

  it("no visible copy names a config file", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], machine: "m" } as never);
    wrap(<Settings role="agent" />);
    await screen.findByLabelText(/机器名称|Machine name/);
    expect(document.body.textContent).not.toMatch(/agent\.yaml|hub\.yaml/);
  });
});

function settingsState(): WorkflowSettingsState {
  const view = {
    enabled: true, role: "controller" as const, inbox_root: "E:\\TODO", data_dir: "D:\\TaskpawWork", ffprobe_path: null,
    jasna_exe_path: null, whisperjav_exe_path: null, ltx_gpu: "auto", mdcx_python: null, mdcx_root: null, mdcx_profile: null,
    filmpaw_data_dir: null, remote_workers: false, worker_api_port: 5682, staging_dir: null, staging_cap_gb: 500,
  };
  return {
    saved: view, running: view, restart_required: false, busy: { films: 0, remote: 0, unknown: false },
    presets: [{ id: "auto", label: "自动" }], ffprobe_detected: null, paths: {}, miko: "off",
  };
}

describe("设置 · 网络与访问 token mode (Codex r5 CX12)", () => {
  async function openNetwork(role: "off" | "worker" | "controller") {
    vi.spyOn(api, "network").mockResolvedValue(network());
    const base = settingsState();
    vi.spyOn(api, "workflowSettings").mockResolvedValue({ ...base, saved: { ...base.saved, role }, running: { ...base.running, role } });
    const gen = vi.spyOn(api, "networkToken").mockResolvedValue({ token: "tp_Generated0Generated0Generated012" });
    wrap(<Settings role="agent" section="network" />);
    return gen;
  }

  it.each(["off", "worker"] as const)("a %s machine pastes the household token and never generates its own", async (role) => {
    const gen = await openNetwork(role);
    const field = await screen.findByLabelText("访问令牌");
    expect(field).not.toHaveAttribute("readonly");
    await new Promise((r) => setTimeout(r, 50));
    expect(gen).not.toHaveBeenCalled();
  });

  it("the controller generates the household token", async () => {
    const gen = await openNetwork("controller");
    await waitFor(() => expect(gen).toHaveBeenCalledWith("generate"));
  });
});

describe("媒体工作流 off Windows (§12 operator decision)", () => {
  it("offers only 不参与 with the Windows-only note", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue({
      ...settingsState(), saved: { ...settingsState().saved, enabled: false, role: "off" }, media_supported: false,
    });
    vi.spyOn(api, "workflowDetect").mockResolvedValue(DETECTED);
    wrap(<WorkflowSettingsSection />);
    expect(await screen.findByText("媒体主控和 GPU 工作机仅支持 Windows；这台电脑只能选「不参与」。")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByLabelText("本机角色"));
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual(["不参与（普通 Agent，仅监控）"]);
  });
});

describe("媒体工作流 detection (R13)", () => {
  it("lists each tool, fills a found path, re-detects and shows directory facts", async () => {
    vi.spyOn(api, "workflowSettings").mockResolvedValue(settingsState());
    const detect = vi.spyOn(api, "workflowDetect").mockResolvedValue(DETECTED);
    vi.spyOn(api, "inspectDirectory").mockImplementation(async (path: string) => (
      path.startsWith("E:") ? { exists: false, free_bytes: null, volume: "E:", local: true }
        : { exists: true, free_bytes: 1.2 * 1024 ** 4, volume: "D:", local: true }));
    wrap(<WorkflowSettingsSection />);
    expect(await screen.findByText("4 / 4 已找到")).toBeInTheDocument();
    expect(screen.getByText("已找到 · 版本 0.11.3")).toBeInTheDocument();
    expect(screen.getByText("已找到 · 在 PATH 找到 · 版本 8.0.1")).toBeInTheDocument();
    expect(screen.getByText("已找到 · 三项齐全")).toBeInTheDocument();
    expect(await screen.findByText("已存在 · 1.2 TB 可用")).toBeInTheDocument();
    expect(await screen.findByText("不存在，保存时创建")).toBeInTheDocument();
    expect(screen.queryByText(/数据目录（队列与记录）/)).toBeNull(); // D3
    const jasnaRow = screen.getByText("Jasna", { selector: "h6, .MuiTypography-subtitle2" }).closest("div")!.parentElement!.parentElement!;
    fireEvent.click(within(jasnaRow).getByRole("button", { name: "使用" }));
    expect(screen.getByLabelText("Jasna")).toHaveValue("C:\\Jasna\\jasna.exe");
    fireEvent.click(screen.getByRole("button", { name: "重新检测" }));
    await waitFor(() => expect(detect).toHaveBeenCalledWith(true));
  });
});
