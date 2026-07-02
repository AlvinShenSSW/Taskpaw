import {
  Alert, Box, Card, CardContent, Chip, Divider, LinearProgress, MenuItem,
  Stack, Tab, Tabs, TextField, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type HubServer, type MonitorSnapshot } from "../api";
import { StatusDot } from "../components/StatusDot";
import { EventLog } from "../components/EventLog";
import { MonitorMetrics, utilTint } from "../components/MonitorMetrics";
import { AiBadge } from "../components/AiActivity";
import { isAiMetrics } from "../components/aiActivity.helpers";
import { HubAgentManager } from "../components/HubAgentManager";
import { Settings } from "./Settings";

// ── fleet health (design pages/hub-dashboard.md "Fleet health") ──────────────
// Derived from #96's per-server `online` + `snapshot` (NOT `acks`, which is an
// event-id cursor, not heartbeat freshness — see the #95 design doc).
type Health = "ok" | "degraded" | "offline";
const HEALTH_STATE: Record<Health, string> = { ok: "ok", degraded: "degraded", offline: "stopped" };

// Any monitor failure state (theme.statusColors) counts as degraded — not just
// "alert". Plugins (Lada/ComfyUI/…) emit "error"/"degraded" on service failures,
// and the worker can be alive while the monitored service is down (Codex 外门).
const PROBLEM_STATES = new Set(["alert", "error", "degraded"]);
function monitorProblem(m: MonitorSnapshot): boolean {
  return m.alive === false || m.degraded === true || PROBLEM_STATES.has(m.state);
}
function serverHealth(s: HubServer): Health {
  if (!s.online) return "offline";
  const mons = s.snapshot?.monitors ?? {};
  return Object.values(mons).some(monitorProblem) ? "degraded" : "ok";
}

// Per-machine CPU/MEM from the agent's host_metrics monitor in its snapshot (#113).
// Select by type_id === "host_metrics", NOT by scanning for cpu_pct/mem_pct — other
// plugins (e.g. lada) emit those same keys, so a key-scan would mis-attribute the
// Lada worker's sample to the host or show bars when host_metrics is off (Kimi).
// Only legacy agents that report no type_id at all fall back to the key-scan.
function hostMetrics(s: HubServer): { cpu?: number; mem?: number } | null {
  const mons = Object.values(s.snapshot?.monitors ?? {});
  // Number.isFinite, not just typeof number — a malformed NaN metric must not slip
  // through and render as "NaN%" (Kimi).
  const pct = (met: Record<string, unknown> | undefined, k: string) =>
    typeof met?.[k] === "number" && Number.isFinite(met[k]) ? (met[k] as number) : undefined;
  const hasMetric = (m: MonitorSnapshot) => {
    const met = m.metrics as Record<string, unknown> | undefined;
    return pct(met, "cpu_pct") !== undefined || pct(met, "mem_pct") !== undefined;
  };
  const host =
    mons.find((m) => m.type_id === "host_metrics") ??
    (mons.every((m) => m.type_id == null) ? mons.find(hasMetric) : undefined);
  const met = host?.metrics as Record<string, unknown> | undefined;
  if (!met) return null;
  const cpu = pct(met, "cpu_pct");
  const mem = pct(met, "mem_pct");
  return cpu !== undefined || mem !== undefined ? { cpu, mem } : null;
}

// last_seen ISO → locale time, or empty.
function fmtSeen(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

// Multi-machine observability (design pages/hub-dashboard.md): fleet health
// summary + grid of drill-down machine cards + the Hub's own host-health
// self-monitor, and an aggregated event log (#44). No marketing hero/CTA.
export function HubDashboard() {
  const { t } = useTranslation();
  const { data, error, isLoading } = useQuery({
    queryKey: ["hubStatus"], queryFn: api.hubStatus,
    refetchInterval: 5000, // #95: auto-refresh like the agent console.
  });
  const [tab, setTab] = useState<"fleet" | "manage" | "events" | "settings">("fleet");
  const [level, setLevel] = useState<string>("");
  const [serverFilter, setServerFilter] = useState<string>(""); // "" = all servers
  // Aggregated durable history from all polled agents; only poll while open.
  const events = useQuery({
    queryKey: ["hubEvents", level, serverFilter],
    queryFn: () => {
      const id = serverFilter ? Number(serverFilter) : NaN;
      return api.hubEvents({
        level: level || undefined,
        server: Number.isFinite(id) ? id : undefined, // never ?server=NaN (Kimi)
      });
    },
    refetchInterval: 5000, enabled: tab === "events",
  });

  // No early return on loading/error — Settings (language/about) must stay
  // reachable even when the Hub is unreachable (#87/Codex).
  // useMemo keeps a stable reference so the serverFilter effect below only re-runs
  // when the fleet actually changes, not on every render (react-hooks/exhaustive-deps).
  const servers = useMemo(() => data?.servers ?? [], [data]);
  const self = data?.self ?? {};

  // Reset the events server filter if the selected server is removed, so the
  // Select can't hold a stale id that yields an empty feed (Kimi #133).
  useEffect(() => {
    if (serverFilter && !servers.some((s) => String(s.id) === serverFilter)) {
      setServerFilter("");
    }
  }, [servers, serverFilter]);

  const counts = { ok: 0, degraded: 0, offline: 0 };
  for (const s of servers) counts[serverHealth(s)] += 1;

  return (
    <Stack spacing={1.5}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 0 }}>
        <Tab value="fleet" label={t("hub.fleet")} />
        <Tab value="manage" label={t("hub.manage")} />
        <Tab value="events" label={t("hub.events")} />
        <Tab value="settings" label={t("settings.title")} />
      </Tabs>

      {tab === "settings" ? (
        <Settings role="hub" />
      ) : tab === "manage" ? (
        // Managing agents is separate from observing (design: 4 tabs). Like
        // Settings, it stays reachable when the Hub is unreachable — the agent
        // list may be empty but the add form still shows (#87 rationale).
        <HubAgentManager servers={servers} />
      ) : isLoading ? (
        <Typography>{t("common.loading")}</Typography>
      ) : error ? (
        <Alert severity="error">{t("hub.unreachable", { error: String(error) })}</Alert>
      ) : tab === "events" ? (
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
              <Typography variant="overline" color="text.secondary">{t("hub.eventHistory")}</Typography>
              <Stack direction="row" spacing={1}>
                <TextField select size="small" label={t("hub.server")} value={serverFilter}
                  onChange={(e) => setServerFilter(e.target.value)} sx={{ minWidth: 140 }}>
                  <MenuItem value="">{t("hub.allServers")}</MenuItem>
                  {servers.map((s) => (
                    <MenuItem key={s.id} value={String(s.id)}>{s.name}</MenuItem>
                  ))}
                </TextField>
                <TextField select size="small" label={t("common.level")} value={level}
                  onChange={(e) => setLevel(e.target.value)} sx={{ minWidth: 140 }}>
                  <MenuItem value="">{t("common.allLevels")}</MenuItem>
                  {["info", "done", "warn", "alert"].map((l) => (
                    <MenuItem key={l} value={l}>{t(`state.${l}`, { defaultValue: l })}</MenuItem>
                  ))}
                </TextField>
              </Stack>
            </Stack>
            <EventLog events={events.data?.events} />
          </CardContent>
        </Card>
      ) : (
        <Stack spacing={2}>
          <Stack direction="row" alignItems="center" spacing={2} sx={{ flexWrap: "wrap" }}>
            <Typography variant="overline" color="text.secondary">
              {t("hub.fleetTitle", {
                machine: data?.machine,
                count: servers.length,
                unit: t(servers.length === 1 ? "hub.agent" : "hub.agents"),
              })}
            </Typography>
            {servers.length > 0 && (
              <Stack direction="row" spacing={2} alignItems="center" aria-label={t("hub.fleetHealth")}>
                <HealthCount health="ok" label={t("hub.healthOk")} n={counts.ok} />
                <HealthCount health="degraded" label={t("hub.healthDegraded")} n={counts.degraded} />
                <HealthCount health="offline" label={t("hub.healthOffline")} n={counts.offline} />
              </Stack>
            )}
          </Stack>

          <Stack spacing={1.5}>
            {servers.map((s) => <MachineRow key={s.id} server={s} />)}
            {servers.length === 0 && (
              <Typography color="text.secondary">{t("hub.noAgents")}</Typography>
            )}
          </Stack>

          {Object.keys(self).length > 0 && (
            <Card>
              <CardContent>
                <Typography variant="overline" color="text.secondary">{t("hub.selfMonitor")}</Typography>
                {Object.entries(self).map(([name, snap]) => (
                  <Box key={name} sx={{ mt: 1 }}>
                    <Stack direction="row" alignItems="center" spacing={1}>
                      <StatusDot state={snap.state} />
                      <Typography variant="body2">{name}</Typography>
                    </Stack>
                    {/* #95: metric tiles instead of a raw JSON <pre>. */}
                    <MonitorMetrics metrics={snap.metrics} />
                  </Box>
                ))}
              </CardContent>
            </Card>
          )}
        </Stack>
      )}
    </Stack>
  );
}

// Labelled health tally (dot + word + count — never color alone, a11y §1).
function HealthCount({ health, label, n }: { health: Health; label: string; n: number }) {
  return (
    <Stack direction="row" alignItems="center" spacing={0.5} sx={{ opacity: n === 0 ? 0.4 : 1 }}>
      <StatusDot state={HEALTH_STATE[health]} live={false} />
      <Typography variant="body2" sx={{ fontVariantNumeric: "tabular-nums" }}>
        <Box component="span" sx={{ fontWeight: 700 }}>{n}</Box> {label}
      </Typography>
    </Stack>
  );
}

// One machine = one full-width row (design pages/hub-dashboard.md): a single
// wrapping header line (health + name + addr + chip + CPU/MEM mini-bars + last-seen)
// with its monitors + full metric gauges rendered FLUSH directly beneath — no
// click-to-expand, no indent. Offline machines show the header only. Management and
// the events feed live on their own tabs (#132/#133).
function MachineRow({ server: s }: { server: HubServer }) {
  const { t } = useTranslation();
  const health = serverHealth(s);
  const online = !!s.online;
  const disabled = !s.enabled;
  const metrics = online ? hostMetrics(s) : null;
  const monitors = online ? (s.snapshot?.monitors ?? {}) : {};
  const monitorNames = Object.keys(monitors);
  // The dev_activity monitor's `ai` block, surfaced as a header badge (#154) so the
  // fleet view shows at a glance which machines are actively running AI.
  const aiMon = online
    ? Object.values(monitors).find((mm) => isAiMetrics(mm.metrics))
    : undefined;
  return (
    <Card>
      <CardContent>
        {/* Header — a single wrapping line. */}
        <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: "wrap", rowGap: 0.5 }}>
          <StatusDot state={HEALTH_STATE[health]} />
          <Typography variant="subtitle1">{s.name}</Typography>
          <Typography variant="body2" color="text.secondary"
            sx={{ fontFamily: '"Fira Code", monospace' }}>{s.ip}:{s.port}</Typography>
          {/* A disabled server is forced offline by the backend; label it disabled
              (not just offline) so the two are distinguishable (Kimi). */}
          <Chip size="small"
            label={disabled ? t("state.disabled") : online ? t("hub.online") : t("hub.offline")}
            color={!disabled && online ? "success" : "default"}
            variant={!disabled && online ? "filled" : "outlined"} />
          {aiMon && isAiMetrics(aiMon.metrics) && <AiBadge metrics={aiMon.metrics} />}
          <Box sx={{ flex: 1 }} />
          {metrics?.cpu !== undefined && (
            <Box sx={{ minWidth: 96 }}><MiniBar label={t("hub.cpu")} pct={metrics.cpu} /></Box>
          )}
          {metrics?.mem !== undefined && (
            <Box sx={{ minWidth: 96 }}><MiniBar label={t("hub.mem")} pct={metrics.mem} /></Box>
          )}
          <Typography variant="caption" color="text.secondary"
            sx={{ fontVariantNumeric: "tabular-nums" }}>
            {s.last_seen ? t("hub.lastSeen", { time: fmtSeen(s.last_seen) }) : t("hub.lastSeenNever")}
          </Typography>
        </Stack>

        {/* Monitors flush beneath — no indent, thin dividers between (online only). */}
        {online && monitorNames.length > 0 && (
          <Stack sx={{ mt: 1.5 }} divider={<Divider flexItem />} spacing={1}>
            {monitorNames.map((name) => {
              const m = monitors[name];
              return (
                <Box key={name}>
                  <Stack direction="row" alignItems="center" spacing={1}>
                    <StatusDot state={m.state} />
                    <Typography variant="body2" sx={{ flex: 1 }}>{name}</Typography>
                    {m.detail && (
                      <Typography variant="caption" color="text.secondary"
                        sx={{ textAlign: "right", wordBreak: "break-all" }}>{m.detail}</Typography>
                    )}
                  </Stack>
                  {/* Full metric gauges (CPU/GPU/VRAM/queue/fps), flush (no indent). */}
                  {m.metrics && Object.keys(m.metrics).length > 0 && (
                    <MonitorMetrics metrics={m.metrics} />
                  )}
                </Box>
              );
            })}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

// Compact CPU/MEM utilization bar for a machine card (#113): label + thin bar +
// %, coloured by the shared 70/90 ramp. Status is conveyed by the number too, not
// colour alone (a11y §1).
function MiniBar({ label, pct }: { label: string; pct: number }) {
  // One rounded value drives both the bar and the label so they can't disagree (Kimi).
  const v = Math.round(Math.max(0, Math.min(100, pct)));
  const tint = utilTint(v);
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.25 }}>
        <Typography variant="caption" color="text.secondary"
          sx={{ textTransform: "uppercase", letterSpacing: 0.5, fontSize: 10 }}>{label}</Typography>
        <Typography variant="caption" sx={{ fontFamily: '"Fira Code", monospace',
          fontVariantNumeric: "tabular-nums", fontSize: 11 }}>{v}%</Typography>
      </Stack>
      <LinearProgress variant="determinate" value={v}
        sx={{ height: 5, borderRadius: 3, bgcolor: "rgba(148,163,184,0.15)",
              "& .MuiLinearProgress-bar": { bgcolor: tint, borderRadius: 3 } }} />
    </Box>
  );
}

