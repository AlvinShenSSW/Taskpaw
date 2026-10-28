import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HubDashboard } from "../views/HubDashboard";
import { machineMedia, mediaIndex, mediaSource, selfReport } from "../views/hubMedia.helpers";
import type { HubServer } from "../api";
import { theme } from "../theme";
import i18n from "../i18n";

// #359: the 机群 tab shows media-center work under each machine.

const OK = { state: "ok", age_seconds: 2 };
const LEGACY = {
  LADA: { state: "stopped", type_id: "lada", enabled: false },
  Jasna: { state: "stopped", type_id: "jasna", enabled: false },
  Translate: { state: "stopped", type_id: "avsubs", enabled: false },
  Disk: { state: "stopped", type_id: "folder", enabled: false },
};
const LTX = {
  stage_index: 2, stage_total: 4, stage_name: "encode", percent: 52, mode: "restore",
  percent_text: "52", current: "156", total: "300", rate_value: "5.62", rate_unit: "frame/s",
  elapsed: "00:27", eta: "00:25", run_eta: "12:12",
};
const gpu = { name: "NVIDIA GeForce RTX 4090", util: 98, used_mib: 20070, vram_mib: 24564, power_w: 351.5, hold: "workflow" };
const node = (name: string, state = "online", extra: Record<string, unknown> = {}) => ({
  name, state, heartbeat_age_s: 2, paused: false, admission: "enabled", gpu, current: null, staged: 0,
  transfers: [], ...extra,
});

function workflow(extra: Record<string, unknown> = {}) {
  return {
    counts: { planning: 0, active: 8, archiving: 1, done: 57 },
    current: { film: "LOCAL-001", stage: "asr" },
    currents: [
      { film: "LOCAL-001", stage: "asr", progress: "识别日语：Generating scene 365/377", machine: "本机" },
      { film: "FILM-107", stage: "restore", progress: "修复 · 阶段 2/4 encode · 52% · 剩余 12:12", ltx: LTX,
        machine: "Blacksilverpig" },
      { film: "FILM-027", stage: "restore", progress: "修复 · 阶段 4/4 compose · 7% · 剩余 1:32:49",
        ltx: { stage_index: 4, stage_total: 4, stage_name: "compose", percent: 7, mode: "restore" }, machine: "Bushpig" },
    ],
    currents_total: 3, currents_more: 0,
    dispatch: { state: "running", paused: false, reason: "" },
    workers: [
      node("Blacksilverpig", "online", { current: { film: "FILM-107", stage: "restore", phase: "running", ltx: LTX } }),
      node("Bushpig", "online", { current: { film: "FILM-027", stage: "restore", phase: "running",
        progress: "修复 · 阶段 4/4 compose · 7% · 剩余 1:32:49" }, staged: 2 }),
      node("darkbluepig", "lost", { heartbeat_age_s: 95 }),
    ],
    workers_total: 3, workers_more: 0,
    sample_age_seconds: 1.2,
    ...extra,
  };
}

function server(id: number, name: string, snapshot: Record<string, unknown> | null, extra: Partial<HubServer> = {}): HubServer {
  return {
    id, name, ip: `10.0.0.${id}`, port: 5681, enabled: 1, online: true, last_seen: "2026-10-28T08:00:00Z",
    status_health: OK, snapshot: snapshot as HubServer["snapshot"], ...extra,
  };
}

function fleet(flow: unknown = workflow(), controller: Partial<HubServer> = {}) {
  return [
    server(1, "BlackGoldPig", { machine: "BlackGoldPig", version: "3.11.4", monitors: LEGACY, workflow: flow }, controller),
    server(2, "BlackSilverPig", { machine: "BlackSilverPig", version: "3.11.4", monitors: LEGACY, workflow: null,
      workflow_worker: { state: "running", connected: true, current: { film_id: "x", stage: "restore", phase: "running" }, busy: true } }),
    server(3, "DarkbluePig", { machine: "DarkbluePig", version: "3.11.4", monitors: LEGACY }),
    server(4, "Render", { machine: "Render", version: "3.11.4", monitors: LEGACY }),
  ];
}

describe("hubMedia helpers", () => {
  it("only a current controller source is available", () => {
    expect(mediaSource(fleet(), true)?.unavailable).toBeNull();
    expect(mediaSource(fleet(), false)?.unavailable).toEqual({ code: "stale" });
    expect(mediaSource(fleet(workflow(), { online: false }), true)?.unavailable).toEqual({ code: "offline" });
    expect(mediaSource(fleet(workflow(), { status_health: { state: "stale" } }), true)?.unavailable).toEqual({ code: "stale" });
    expect(mediaSource(fleet(workflow({ sample_age_seconds: 40 })), true)?.unavailable).toEqual({ code: "expired" });
    expect(mediaSource(fleet(null), true)?.unavailable).toEqual({ code: "notRunning" });
    expect(mediaSource(fleet("garbage"), true)?.unavailable).toEqual({ code: "notRunning" });
    expect(mediaSource([server(9, "x", { machine: "x", monitors: {} })], true)).toBeNull();
  });

  it("indexes work by case-insensitive machine name with 本机 as the controller", () => {
    const servers = fleet();
    const index = mediaIndex(mediaSource(servers, true));
    const gold = machineMedia(index, servers[0]);
    expect(gold?.controller).toBe(true);
    expect(gold?.tasks.map(t => t.film)).toEqual(["LOCAL-001"]);
    const silver = machineMedia(index, servers[1]);
    expect(silver?.tasks.map(t => t.film)).toEqual(["FILM-107"]);
    expect(silver?.worker?.name).toBe("Blacksilverpig");
    expect(machineMedia(index, servers[2])?.worker?.state).toBe("lost");
    expect(machineMedia(index, servers[3])).toBeNull();
  });

  it("falls back to the worker heartbeat when bounded currents leave a machine out", () => {
    const servers = fleet(workflow({ currents: [], currents_more: 17 }));
    const silver = machineMedia(mediaIndex(mediaSource(servers, true)), servers[1]);
    expect(silver?.tasks.map(t => t.film)).toEqual(["FILM-107"]);
  });

  it("a GPU worker's null workflow never becomes the source (review P1-1)", () => {
    const servers = fleet();
    const workerFirst = [servers[1], servers[0], servers[2]];
    expect(mediaSource(workerFirst, true)?.server.name).toBe("BlackGoldPig");
    expect(mediaSource(workerFirst, true)?.unavailable).toBeNull();
    // the stale controller explains; a worker alone is no source at all
    expect(mediaSource(fleet(workflow(), { online: false }).slice(0, 2).reverse(), true)?.server.name).toBe("BlackGoldPig");
    expect(mediaSource([servers[1]], true)).toBeNull();
  });

  it("a lost node's retained heartbeat is not live work (review P1-2)", () => {
    const flow = workflow({ currents: [], workers: [node("BlackSilverPig", "lost", { heartbeat_age_s: 300,
      current: { film: "FILM-1", stage: "restore", phase: "running", ltx: LTX } })] });
    const servers = fleet(flow);
    const silver = machineMedia(mediaIndex(mediaSource(servers, true)), servers[1]);
    expect(silver?.tasks).toEqual([]);
    expect(silver?.worker?.state).toBe("lost");
  });

  it("drops out-of-range GPU samples and transfer percents", () => {
    const servers = fleet(workflow({ workers: [node("BlackSilverPig", "online", {
      gpu: { util: 140, used_mib: -1, vram_mib: 0, power_w: 300 },
      transfers: [{ film: "F", dir: "down", percent: 250 }] })] }));
    const row = mediaSource(servers, true)?.summary?.workers?.[0];
    expect(row?.gpu).toEqual({ util: undefined, used_mib: undefined, vram_mib: undefined, power_w: 300 });
    expect(row?.transfers).toEqual([{ film: "F", dir: "down", percent: undefined }]);
  });

  it("a stale controller indexes nothing", () => {
    expect(mediaIndex(mediaSource(fleet(), false)).size).toBe(0);
  });

  it("reads a worker's self-report; running with no current is idle", () => {
    expect(selfReport(fleet()[1])).toEqual({ state: "running", connected: true, paused: false, busy: true,
      current: { stage: "restore", phase: "running" } });
    const idle = server(5, "w", { machine: "w", monitors: {}, workflow_worker: { state: "running", connected: true, current: null, busy: false } });
    expect(selfReport(idle)?.state).toBe("idle");
    expect(selfReport(fleet()[3])).toBeNull();
  });
});

describe("HubDashboard 机群 media (#359)", () => {
  const clients: QueryClient[] = [];
  beforeEach(() => { void i18n.changeLanguage("zh-CN"); });
  afterEach(() => { clients.splice(0).forEach(qc => qc.clear()); vi.unstubAllGlobals(); });

  async function show(servers: HubServer[]) {
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(Response.json(url.includes("/status")
      ? { machine: "SunnyPig", servers, acks: {}, self: {} } : { events: [] }))));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    clients.push(qc);
    render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><HubDashboard /></QueryClientProvider></ThemeProvider>);
    await screen.findByText("BlackGoldPig");
  }
  const card = (name: string) => screen.getByText(name, { selector: "h6" }).closest(".MuiCard-root") as HTMLElement;

  it("shows a GPU Agent's Jasna/LTX progress and node state under its row", async () => {
    await show(fleet());
    const silver = within(card("BlackSilverPig"));
    const media = within(silver.getByTestId("hub-machine-media"));
    expect(media.getByText("FILM-107")).toBeInTheDocument();
    expect(media.getByText(/LTX 2\/4 encode/)).toBeInTheDocument();
    expect(media.getByText("52%")).toBeInTheDocument();
    expect(media.getByText(/156\/300 帧 · 5.62 frame\/s · 本阶段已用 00:27 · 本阶段剩余 00:25 · 本轮 LTX 剩余 12:12/)).toBeInTheDocument();
    expect(media.getByText(/媒体节点：在线 · GPU 98% · 显存 19.6\/24.0GB · 352W/)).toBeInTheDocument();
    expect(media.getByRole("progressbar", { name: /LTX 2\/4 encode/ })).toHaveAttribute("aria-valuenow", "52");
    // the disabled legacy Jasna monitor is explained, the folder monitor is not
    expect(silver.getAllByText("旧版独立监控未使用 · 媒体任务见「媒体处理」")).toHaveLength(3);
  });

  it("the controller row lists dispatch, counts and every GPU node, also unpolled ones", async () => {
    await show(fleet());
    const gold = within(card("BlackGoldPig"));
    const media = within(gold.getByTestId("hub-machine-media"));
    expect(media.getByText("LOCAL-001")).toBeInTheDocument();
    expect(media.getByText("识别日语：Generating scene 365/377")).toBeInTheDocument();
    expect(media.getByText("媒体主控 · 调度 running")).toBeInTheDocument();
    expect(media.getByText("GPU 节点 3/3")).toBeInTheDocument();
    const nodes = media.getAllByTestId("hub-media-node");
    expect(nodes.map(n => within(n).getAllByText(/pig$/i)[0].textContent)).toEqual(["Blacksilverpig", "Bushpig", "darkbluepig"]);
    // Bushpig is not a Hub server: its task (progress text from an older field set) is still shown
    expect(within(nodes[1]).getByText("FILM-027")).toBeInTheDocument();
    expect(within(nodes[1]).getByText(/已预取 2 部/)).toBeInTheDocument();
    expect(within(nodes[1]).getByText("修复 · 阶段 4/4 compose · 7% · 剩余 1:32:49")).toBeInTheDocument();
    // a lost node: no stale GPU sample
    expect(within(nodes[2]).getByText("失联 · 心跳 95s 前")).toBeInTheDocument();
  });

  it("keeps the volume in the progress text and marks an LTX speed test (Codex)", async () => {
    const flow = workflow({ currents: [
      { film: "FILM-107", stage: "restore", progress: "修复 第 1/2 卷 · 阶段 2/4 encode · 52% · 剩余 12:12", ltx: LTX,
        machine: "Blacksilverpig" },
      { film: "FILM-027", stage: "restore", ltx: { ...LTX, mode: "speed_test" }, machine: "Bushpig" }],
    workers: [node("Blacksilverpig"), node("Bushpig", "online", { current: { film: "FILM-027", stage: "restore",
      phase: "running", ltx: { ...LTX, mode: "speed_test" } } })] });
    await show(fleet(flow));
    const silver = within(within(card("BlackSilverPig")).getByTestId("hub-machine-media"));
    expect(silver.getByText("修复 第 1/2 卷 · 阶段 2/4 encode · 52% · 剩余 12:12")).toBeInTheDocument();
    expect(silver.queryByText("LTX 测速")).not.toBeInTheDocument();
    const bush = within(card("BlackGoldPig")).getAllByTestId("hub-media-node")[1];
    expect(within(bush).getByText("LTX 测速")).toBeInTheDocument();
  });

  it("a lost node's own row says so instead of idle", async () => {
    await show(fleet());
    const dark = within(within(card("DarkbluePig")).getByTestId("hub-machine-media"));
    expect(dark.getByText("媒体节点：失联 · 心跳 95s 前")).toBeInTheDocument();
    expect(dark.queryByText(/空闲/)).not.toBeInTheDocument();
    expect(within(card("Render")).queryByTestId("hub-machine-media")).not.toBeInTheDocument();
  });

  it("a lost node shows neither its last task nor its last prefetch and transfers", async () => {
    const flow = workflow({ currents: [], workers: [node("DarkbluePig", "lost", { heartbeat_age_s: 300, staged: 2,
      current: { film: "FILM-1", stage: "restore", phase: "running", ltx: LTX },
      transfers: [{ film: "FILM-9", dir: "down", percent: 40 }] })] });
    await show(fleet(flow));
    const dark = within(within(card("DarkbluePig")).getByTestId("hub-machine-media"));
    expect(dark.getByText("媒体节点：失联 · 心跳 300s 前")).toBeInTheDocument();
    expect(dark.queryByText("FILM-1")).not.toBeInTheDocument();
    expect(dark.queryByText(/已预取|下载 FILM-9/)).not.toBeInTheDocument();
    expect(dark.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("an online node's transfer without a percent omits it; prefetch work reads 下载", async () => {
    const flow = workflow({ currents: [{ film: "FILM-5", stage: "fetch", machine: "Blacksilverpig" }],
      workers: [node("Blacksilverpig", "online", { transfers: [{ film: "FILM-5", dir: "down" }] })] });
    await show(fleet(flow));
    const silver = within(within(card("BlackSilverPig")).getByTestId("hub-machine-media"));
    expect(silver.getByText(/· 下载 FILM-5$/)).toBeInTheDocument();
    expect(silver.queryByText(/—%/)).not.toBeInTheDocument();
    expect(silver.getByText("下载")).toBeInTheDocument();
  });

  it("a busy worker without a current stage is not reported as having no task", async () => {
    const servers = fleet(workflow({ sample_age_seconds: 40 }));
    servers[1] = server(2, "BlackSilverPig", { machine: "BlackSilverPig", monitors: {}, workflow: null,
      workflow_worker: { state: "running", connected: true, current: null, busy: true } });
    await show(servers);
    expect(within(card("BlackSilverPig")).getByText("运行中 · 本机自报 · 已连接主控")).toBeInTheDocument();
  });

  it("a stale controller makes no live claims; workers fall back to their self-report", async () => {
    await show(fleet(workflow({ sample_age_seconds: 40 })));
    expect(within(card("BlackGoldPig")).getByText("媒体采样已过期")).toBeInTheDocument();
    expect(screen.queryByText("FILM-107")).not.toBeInTheDocument();
    expect(within(card("BlackSilverPig")).getByText("运行中 · 本机自报 · 已连接主控 · 修复 running")).toBeInTheDocument();
  });

  it("a 3.11.1 summary without workers still shows remote work under the controller", async () => {
    const old = workflow();
    delete (old as Record<string, unknown>).workers;
    for (const item of old.currents) delete (item as Record<string, unknown>).ltx;
    await show(fleet(old));
    const gold = within(within(card("BlackGoldPig")).getByTestId("hub-machine-media"));
    expect(gold.getByText("Bushpig")).toBeInTheDocument();
    expect(gold.getByText("修复 · 阶段 4/4 compose · 7% · 剩余 1:32:49")).toBeInTheDocument();
    const silver = within(within(card("BlackSilverPig")).getByTestId("hub-machine-media"));
    expect(silver.getByText("修复 · 阶段 2/4 encode · 52% · 剩余 12:12")).toBeInTheDocument();
  });
});
