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
}

export interface AgentStatus {
  machine: string;
  server_id?: string;
  os?: string;
  monitors: Record<string, MonitorSnapshot>;
}

export interface HubStatus {
  machine: string;
  servers: Array<{ id: number; name: string; ip: string; port: number; enabled: number }>;
  acks: Record<string, number>;
  self: Record<string, MonitorSnapshot>;
}

declare global {
  interface Window {
    __TASKPAW__?: { baseUrl?: string; apiKey?: string };
  }
}

// Default backend ports differ by role: agent 5680, Hub 5690. An injected
// baseUrl (shell) or VITE_TASKPAW_BASE (browser) overrides both.
const DEFAULT_PORT = { agent: 5680, hub: 5690 } as const;

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

export const api = {
  agentStatus: () => get<AgentStatus>("agent", "/status"),
  hubStatus: () => get<HubStatus>("hub", "/status"),
};
