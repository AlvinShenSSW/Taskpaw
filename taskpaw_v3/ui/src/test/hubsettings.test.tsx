// #299 R14/R15 (board Hub-Settings): OpenClaw, 主控连接, 高级 and the Hub's 立即重启.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, HttpError, type HubSettingsState, type HubSettingsValues } from "../api";
import { theme } from "../theme";
import { Settings } from "../views/Settings";

const BOOT = "fedcba9876543210fedcba9876543210";
function values(over: Partial<HubSettingsValues> = {}): HubSettingsValues {
  return {
    openclaw_enabled: true, openclaw_url: "http://127.0.0.1:18789/hooks/wake", openclaw_token: "***", polling_token: "***",
    polling_token_hint: "7Q2f", poll_interval: 30, write_status_md: true, status_md_path: null,
    status_md_default: "~/.taskpaw-hub/status.md", status_log_retention_days: 14, lan_access: false, bind_host: "127.0.0.1",
    api_token: "", ...over,
  };
}
function state(over: Partial<HubSettingsState> = {}): HubSettingsState {
  return {
    saved: values(), running: values(), restart_required: false, fields: [], addresses: [], restart_available: true,
    controller_server_id: 1, ...over,
  };
}
function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><Settings role="hub" /></QueryClientProvider></ThemeProvider>);
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline"))));
  vi.spyOn(api, "hubStatus").mockResolvedValue({
    machine: "hub", acks: {}, self: {},
    servers: [
      { id: 1, name: "BlackGoldPig", ip: "192.168.1.20", port: 5680, enabled: 1, online: true, last_seen: new Date(Date.now() - 12000).toISOString() },
      { id: 2, name: "NAS-agent", ip: "192.168.1.5", port: 5680, enabled: 1, online: true, last_seen: null },
    ],
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Hub settings", () => {
  it("shows OpenClaw, 主控连接 and 高级 with every token masked", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state());
    wrap();
    expect(await screen.findByLabelText("地址")).toHaveValue("http://127.0.0.1:18789/hooks/wake");
    expect(screen.getByLabelText("令牌")).toHaveValue("");
    expect(screen.getByLabelText("令牌")).toHaveAttribute("placeholder", "***");
    expect(screen.getByText("家庭访问令牌 tp_••••••7Q2f")).toBeInTheDocument();
    expect(screen.getByText(/Hub 用这一个令牌轮询家里所有 TaskPaw/)).toBeInTheDocument();
    expect(await screen.findByText(/已连接 · 1\d 秒前/)).toBeInTheDocument();
    expect(screen.getByText("地址 192.168.1.20:5680")).toBeInTheDocument();
    expect(screen.getByLabelText("status.md 位置")).toHaveAttribute("placeholder", "~/.taskpaw-hub/status.md");
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  });

  it("saves only what changed and shows the restart banner with its fields", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValueOnce(state())
      .mockResolvedValue(state({ restart_required: true, fields: ["poll_interval"] }));
    const save = vi.spyOn(api, "hubSaveSettings").mockResolvedValue({ ok: true, restart_required: true, fields: ["poll_interval"] });
    wrap();
    fireEvent.change(await screen.findByLabelText("日志保留天数"), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("令牌"), { target: { value: "ocw_new-token" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ openclaw_token: "ocw_new-token", status_log_retention_days: 30 }));
    expect(await screen.findByRole("status")).toHaveTextContent("已保存，重启 TaskPaw Hub 后生效（轮询间隔）");
  });

  it("测试发送 reports each outcome", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state());
    const testSend = vi.spyOn(api, "hubOpenclawTest")
      .mockResolvedValueOnce({ ok: true, at: "14:02", elapsed_s: 0.4 })
      .mockResolvedValueOnce({ ok: false, error: "token", status: 401 })
      .mockResolvedValueOnce({ ok: false, error: "http", status: 502 })
      .mockResolvedValueOnce({ ok: false, error: "timeout" })
      .mockResolvedValueOnce({ ok: false, error: "unreachable" });
    wrap();
    const button = await screen.findByRole("button", { name: "测试发送" });
    for (const text of ["已送达 · 14:02 · 用时 0.4 秒", "令牌错误", "HTTP 502", "超时", "无法连接"]) {
      fireEvent.click(button);
      expect(await screen.findByText(text)).toBeInTheDocument();
    }
    expect(testSend).toHaveBeenCalledWith("http://127.0.0.1:18789/hooks/wake", "");
  });

  it("shows per-field refusals", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state());
    vi.spyOn(api, "hubSaveSettings").mockRejectedValue(new HttpError("设置有误", 422, undefined, { api_token: "开启局域网只读接口需要访问令牌" }));
    wrap();
    fireEvent.click((await screen.findAllByRole("switch")).at(-1)!); // 局域网只读接口
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("只读接口令牌：开启局域网只读接口需要访问令牌");
  });

  it("更换主控 binds the address's own server row — never edits the polled one (review IR2, Codex r4)", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state({ controller_server_id: 2 }));
    const update = vi.spyOn(api, "hubUpdateServer");
    const replace = vi.spyOn(api, "hubReplaceController").mockResolvedValue({ ok: true, id: 9, created: true });
    vi.spyOn(api, "hubSaveSettings").mockResolvedValue({ ok: true, restart_required: false, fields: [] });
    wrap();
    expect(await screen.findByText("地址 192.168.1.5:5680")).toBeInTheDocument();
    expect(screen.getByText("主控 NAS-agent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "更换主控" }));
    fireEvent.change(screen.getByLabelText("主控地址"), { target: { value: "192.168.1.6" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("192.168.1.6", 5680));
    expect(update).not.toHaveBeenCalled();
  });

  it("with no 主控 chosen it asks which polled TaskPaw is the 主控", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state({ controller_server_id: null }));
    const save = vi.spyOn(api, "hubSaveSettings").mockResolvedValue({ ok: true, restart_required: false, fields: [] });
    const update = vi.spyOn(api, "hubUpdateServer");
    wrap();
    expect(await screen.findByText("还没有选择主控")).toBeInTheDocument();
    expect(screen.queryByText("地址 192.168.1.20:5680")).toBeNull();
    fireEvent.mouseDown(await screen.findByLabelText("选择主控"));
    fireEvent.click(await screen.findByRole("option", { name: "NAS-agent · 192.168.1.5:5680" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ controller_server_id: 2 }));
    expect(update).not.toHaveBeenCalled();
  });

  it("添加主控 adds the server and names it the 主控", async () => {
    vi.spyOn(api, "hubStatus").mockResolvedValue({ machine: "hub", acks: {}, self: {}, servers: [] });
    vi.spyOn(api, "hubSettings").mockResolvedValue(state({ controller_server_id: null }));
    const replace = vi.spyOn(api, "hubReplaceController").mockResolvedValue({ ok: true, id: 7, created: true });
    wrap();
    fireEvent.click(await screen.findByRole("button", { name: "添加主控" }));
    fireEvent.change(screen.getByLabelText("主控地址"), { target: { value: "192.168.1.20" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("192.168.1.20", 5680));
  });

  it("更换主控 binds the new address and saves the polling token", async () => {
    vi.spyOn(api, "hubSettings").mockResolvedValue(state());
    const replace = vi.spyOn(api, "hubReplaceController").mockResolvedValue({ ok: true, id: 3, created: true });
    const save = vi.spyOn(api, "hubSaveSettings").mockResolvedValue({ ok: true, restart_required: false, fields: [] });
    wrap();
    fireEvent.click(await screen.findByRole("button", { name: "更换主控" }));
    fireEvent.change(screen.getByLabelText("主控地址"), { target: { value: "192.168.1.21" } });
    fireEvent.change(screen.getByLabelText("家庭访问令牌"), { target: { value: "tp_new" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("192.168.1.21", 5680));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ polling_token: "tp_new" }));
  });

  it("restarts the Hub through the shell", async () => {
    window.__TASKPAW__ = { baseUrl: "http://127.0.0.1:5691", controlToken: "hub-shell", role: "hub", bootId: BOOT, restart: true };
    vi.spyOn(api, "hubSettings").mockResolvedValue(state({ restart_required: true, fields: ["poll_interval"] }));
    const restart = vi.spyOn(api, "restart").mockResolvedValue({ restarting: true });
    wrap();
    fireEvent.click(await screen.findByRole("button", { name: "立即重启" }));
    await waitFor(() => expect(restart).toHaveBeenCalledWith("hub", BOOT));
    expect(await screen.findByText("正在重启 TaskPaw Hub…")).toBeInTheDocument();
  });
});
