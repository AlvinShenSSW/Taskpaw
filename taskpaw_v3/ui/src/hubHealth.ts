import { useEffect, useMemo, useState } from "react";
import type { HubServer } from "./api";
import { RESPONSE_FRESH_MS } from "./components/requestEvidence";

// Additive Hub evidence; missing fields remain compatible with older Hubs.
export interface HubStatusHealth {
  state: string;
  error_code?: string | null;
  attempted_at?: string | null;
  last_good_at?: string | null;
  age_seconds?: number | null;
}
export interface HubEventChannel {
  state: string;
  reason?: string | null;
  recovery_hint?: string | null;
}
export interface HubNotificationHealth {
  state: string;
  enabled?: boolean;
  configured?: boolean;
  worker_running?: boolean;
  pending_count?: number;
  failed_count?: number;
  dead_letter_count?: number;
  quarantined_count?: number;
  oldest_pending_at?: string | null;
  oldest_pending_age_seconds?: number | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_failure_reason?: string | null;
}

// One display clock per dashboard. UTC elapsed time catches macOS sleep;
// monotonic elapsed time prevents a backward wall-clock adjustment reducing age.
// A clock before the response timestamp cannot establish current health.
export function useHubResponseAge(receivedAt: number) {
  const [observed, setObserved] = useState(() => ({ receivedAt, wall: Date.now() }));
  const clock = useMemo(() => ({ receivedAt, monotonic: performance.now() }), [receivedAt]);
  useEffect(() => {
    if (!receivedAt) return;
    const update = () => setObserved(previous => ({
      receivedAt, wall: Math.max(previous.receivedAt === receivedAt ? previous.wall : 0, Date.now()),
    }));
    const timer = setInterval(update, 1000);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [receivedAt]);
  const wall = Math.max(observed.receivedAt === receivedAt ? observed.wall : 0, Date.now()) - receivedAt;
  const elapsed = Math.max(wall, performance.now() - clock.monotonic) / 1000;
  return { elapsed, outdated: !receivedAt || Date.now() < receivedAt || elapsed * 1000 >= RESPONSE_FRESH_MS };
}

export type HubHealth = "ok" | "degraded" | "offline" | "stale" | "disabled";
const PROBLEM_STATES = new Set(["alert", "error", "degraded"]);
export function hubServerHealth(server: HubServer, responseCurrent: boolean): HubHealth {
  if (!server.enabled) return "disabled";
  if (!responseCurrent) return "stale";
  const sample = server.status_health;
  if (sample && ["stale", "never", "recovering"].includes(sample.state)) return "stale";
  if (!server.online || (sample && sample.state !== "ok")) return "offline";
  return Object.values(server.snapshot?.monitors ?? {}).some(m => m.alive === false || m.degraded === true || PROBLEM_STATES.has(m.state))
    ? "degraded" : "ok";
}

export function hubAge(age: number | null | undefined, elapsed: number): number | null {
  return typeof age === "number" && Number.isFinite(age) && age >= 0 && Number.isFinite(elapsed)
    ? age + Math.max(0, elapsed) : null;
}
export function hubDuration(seconds: number | null, unknown: string): string {
  if (seconds == null) return unknown;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}
