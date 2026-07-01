// Minimal REST client for the V3 backends.
//
// In the Tauri shell the backend base URL + api key are injected on the loopback
// origin (design §3.1); in the browser they come from Vite env / localStorage.
// The agent control API is loopback-only; the Hub API is read-only here.

export interface MonitorSnapshot {
  state: string;
  metrics?: Record<string, unknown>;
  detail?: string;
  alive?: boolean;
  degraded?: boolean;
  // Added by the agent status provider (#57): whether the monitor is enabled
  // (running) and its plugin type, so the console can toggle + label it.
  enabled?: boolean;
  type_id?: string | null;
}

export interface AgentStatus {
  machine: string;
  server_id?: string;
  os?: string;
  monitors: Record<string, MonitorSnapshot>;
}

export interface HubServer {
  id: number;
  name: string;
  ip: string;
  port: number;
  enabled: number;
  // Per-server poll snapshot (#96): live reachability, last good poll time, and
  // the agent's last parsed /status (null if never polled). A disabled server is
  // forced online=false. Optional so older Hub builds (pre-#96) still type-check.
  online?: boolean;
  last_seen?: string | null;
  snapshot?: AgentStatus | null;
}

export interface HubStatus {
  machine: string;
  servers: HubServer[];
  acks: Record<string, number>;
  self: Record<string, MonitorSnapshot>;
}

// A selectable monitor type from /control/plugins (#57): its form schema drives
// the add/edit dialog.
export interface PluginInfo {
  type_id: string;
  display_name: string;
  category: string;
  config_version: number;
  system: boolean;
  json_schema: Record<string, unknown>;
  ui_schema: Record<string, unknown>;
}

export interface PresetInfo {
  id: string;
  display_name: string;
  description?: string;
  monitors: Array<{ type_id: string; name: string; config: Record<string, unknown> }>;
}

export interface MonitorSpec {
  type_id: string;
  name?: string;
  config: Record<string, unknown>;
  enabled?: boolean;
}

// One row in the event log (#44). Agent-local events carry `time`/`machine`;
// Hub-aggregated events carry `received_at`/`server`. The renderer tolerates both.
export interface EventItem {
  id?: number;
  event_id?: number;
  server_id?: number; // Hub events: id is only unique WITH the server (key needs both)
  time?: string;
  received_at?: string;
  machine?: string;
  server?: string;
  monitor?: string;
  message?: string;
  level?: string;
}

declare global {
  interface Window {
    // Injected by the Tauri shell on the loopback origin (main.rs init_script).
    // `role` drives single-role navigation (App.tsx, #58).
    __TASKPAW__?: { baseUrl?: string; apiKey?: string; role?: "agent" | "hub" };
  }
}

// The agent console talks to the agent's loopback CONTROL API (5681) — the
// network API (5680) is the Hub-facing, CORS-free read surface. The Hub
// dashboard talks to the Hub API (5690). An injected baseUrl (shell) or
// VITE_TASKPAW_BASE (browser) overrides both.
const DEFAULT_PORT = { agent: 5681, hub: 5690 } as const;

function cfg(role: "agent" | "hub") {
  const injected = window.__TASKPAW__ || {};
  const baseUrl =
    injected.baseUrl ||
    (import.meta.env.VITE_TASKPAW_BASE as string) ||
    `http://127.0.0.1:${DEFAULT_PORT[role]}`;
  const apiKey = injected.apiKey || (import.meta.env.VITE_TASKPAW_TOKEN as string) || "";
  return { baseUrl, apiKey };
}

async function get<T>(role: "agent" | "hub", path: string): Promise<T> {
  const { baseUrl, apiKey } = cfg(role);
  const res = await fetch(`${baseUrl}${path}`, {
    headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

// Non-GET control calls (#57). On error, surface the backend's `detail` (the
// admin's ValueError message → 400) so the UI can show why an edit was rejected.
async function send<T>(
  role: "agent" | "hub",
  method: "POST" | "DELETE" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  const { baseUrl, apiKey } = cfg(role);
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers: {
      ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = String(j.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const q = (name: string) => `?name=${encodeURIComponent(name)}`;

export const api = {
  agentStatus: () => get<AgentStatus>("agent", "/control/status"),
  hubStatus: () => get<HubStatus>("hub", "/status"),
  plugins: () => get<{ plugins: PluginInfo[]; presets: PresetInfo[] }>("agent", "/control/plugins"),
  // Full agent config (secrets masked as "***") — used to pre-fill the edit form.
  config: () => get<{ monitors: MonitorSpec[] } & Record<string, unknown>>("agent", "/control/config"),
  // Edit top-level agent config from the Settings UI (#43). Returns
  // {ok, restart_required}. A blank/"***" api_token keeps the stored one.
  updateConfig: (patch: Record<string, unknown>) =>
    send<{ ok: boolean; restart_required: boolean }>("agent", "PATCH", "/control/config", patch),
  addMonitor: (spec: MonitorSpec) => send("agent", "POST", "/control/monitors", spec),
  removeMonitor: (name: string) => send("agent", "DELETE", `/control/monitors${q(name)}`),
  updateMonitor: (name: string, patch: { config?: Record<string, unknown>; enabled?: boolean }) =>
    send("agent", "PATCH", `/control/monitors${q(name)}`, patch),
  startMonitor: (name: string) => send("agent", "POST", `/control/monitors/start${q(name)}`),
  stopMonitor: (name: string) => send("agent", "POST", `/control/monitors/stop${q(name)}`),
  // Event log (#44): agent reads recent local events (non-destructive); the Hub
  // reads durable aggregated history, filterable by server id + level.
  agentEvents: (limit = 200) =>
    get<{ events: EventItem[] }>("agent", `/control/events?limit=${limit}`),
  hubEvents: (p: { server?: number; level?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (p.server != null) qs.set("server", String(p.server));
    if (p.level) qs.set("level", p.level);
    qs.set("limit", String(p.limit ?? 200));
    return get<{ events: EventItem[] }>("hub", `/events?${qs.toString()}`);
  },
  // Manage the agents the Hub polls, from the dashboard (#124). Bearer-gated like
  // the rest of the Hub API (#106); errors surface the backend `detail`.
  hubAddServer: (s: { name: string; ip: string; port: number }) =>
    send<HubServer & { ok: boolean }>("hub", "POST", "/servers", s),
  hubUpdateServer: (id: number, patch: { name?: string; ip?: string; port?: number; enabled?: boolean }) =>
    send<HubServer & { ok: boolean }>("hub", "PATCH", `/servers/${id}`, patch),
  hubRemoveServer: (id: number) => send<{ ok: boolean }>("hub", "DELETE", `/servers/${id}`),
  hubSetPollingToken: (polling_token: string) =>
    send<{ ok: boolean }>("hub", "PATCH", "/config", { polling_token }),
};
