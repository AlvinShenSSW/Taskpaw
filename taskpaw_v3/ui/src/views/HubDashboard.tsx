import {
  Alert, Box, Card, CardActionArea, CardContent, Chip, Collapse, LinearProgress, MenuItem,
  Stack, Tab, Tabs, TextField, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type HubServer, type MonitorSnapshot } from "../api";
import { StatusDot } from "../components/StatusDot";
import { EventLog } from "../components/EventLog";
import { MonitorMetrics, utilTint } from "../components/MonitorMetrics";
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
  const [tab, setTab] = useState<"fleet" | "events" | "settings">("fleet");
  const [level, setLevel] = useState<string>("");
  const [expanded, setExpanded] = useState<number | null>(null);
  // Aggregated durable history from all polled agents; only poll while open.
  const events = useQuery({
    queryKey: ["hubEvents", level],
    queryFn: () => api.hubEvents({ level: level || undefined }),
    refetchInterval: 5000, enabled: tab === "events",
  });

  // No early return on loading/error — Settings (language/about) must stay
  // reachable even when the Hub is unreachable (#87/Codex).
  const servers = data?.servers ?? [];
  const self = data?.self ?? {};

  const counts = { ok: 0, degraded: 0, offline: 0 };
  for (const s of servers) counts[serverHealth(s)] += 1;

  return (
    <Stack spacing={1.5}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 0 }}>
        <Tab value="fleet" label={t("hub.fleet")} />
        <Tab value="events" label={t("hub.events")} />
        <Tab value="settings" label={t("settings.title")} />
      </Tabs>

      {tab === "settings" ? (
        <Settings role="hub" />
      ) : isLoading ? (
        <Typography>{t("common.loading")}</Typography>
      ) : error ? (
        <Alert severity="error">{t("hub.unreachable", { error: String(error) })}</Alert>
      ) : tab === "events" ? (
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
              <Typography variant="overline" color="text.secondary">{t("hub.eventHistory")}</Typography>
              <TextField select size="small" label={t("common.level")} value={level}
                onChange={(e) => setLevel(e.target.value)} sx={{ minWidth: 140 }}>
                <MenuItem value="">{t("common.allLevels")}</MenuItem>
                {["info", "done", "warn", "alert"].map((l) => (
                  <MenuItem key={l} value={l}>{t(`state.${l}`, { defaultValue: l })}</MenuItem>
                ))}
              </TextField>
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

          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}>
            {servers.map((s) => (
              <MachineCard key={s.id} server={s} expanded={expanded === s.id}
                onToggle={() => setExpanded((cur) => (cur === s.id ? null : s.id))} />
            ))}
            {servers.length === 0 && (
              <Typography color="text.secondary">{t("hub.noAgents")}</Typography>
            )}
          </Box>

          {/* #124: add/edit/remove polled agents + set the polling token. */}
          <HubAgentManager servers={servers} />

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

// One machine: card face (health + name + addr + last-seen + online chip) that
// drills down into its monitors + recent events. Hover lift uses transform (no
// reflow / layout shift, #95 acceptance) and degrades under reduced motion.
function MachineCard({ server: s, expanded, onToggle }:
  { server: HubServer; expanded: boolean; onToggle: () => void }) {
  const { t } = useTranslation();
  const health = serverHealth(s);
  const online = !!s.online;
  const disabled = !s.enabled;
  // Mini CPU/MEM bars only for a live machine that actually reports host metrics.
  const metrics = online ? hostMetrics(s) : null;
  return (
    <Card
      sx={{
        width: { xs: "100%", sm: 300 }, alignSelf: "flex-start",
        // Hover lift via transform only → no layout shift / reflow (#95 acceptance);
        // degrades to no movement under reduced motion.
        transition: "transform .16s ease, box-shadow .16s ease",
        "&:hover": { transform: "translateY(-2px)", boxShadow: "0 8px 24px -10px rgba(0,0,0,.5)" },
        "@media (prefers-reduced-motion: reduce)": { "&:hover": { transform: "none" } },
      }}
    >
      {/* CardActionArea (not CardContent component="button") → the whole face is
          one focusable control without a <button> wrapping block elements (Kimi). */}
      <CardActionArea onClick={onToggle} aria-expanded={expanded}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1}>
            <StatusDot state={HEALTH_STATE[health]} />
            <Typography variant="subtitle1" sx={{ flex: 1 }}>{s.name}</Typography>
            {/* A disabled server is forced offline by the backend; label it as
                disabled (not just offline) so the two are distinguishable (Kimi). */}
            <Chip size="small"
              label={disabled ? t("state.disabled") : online ? t("hub.online") : t("hub.offline")}
              color={!disabled && online ? "success" : "default"}
              variant={!disabled && online ? "filled" : "outlined"} />
          </Stack>
          <Typography variant="body2" color="text.secondary">{s.ip}:{s.port}</Typography>
          <Typography variant="caption" color="text.secondary">
            {s.last_seen ? t("hub.lastSeen", { time: fmtSeen(s.last_seen) }) : t("hub.lastSeenNever")}
          </Typography>
          {metrics && (metrics.cpu !== undefined || metrics.mem !== undefined) && (
            <Stack spacing={0.75} sx={{ mt: 1.25 }}>
              {metrics.cpu !== undefined && <MiniBar label={t("hub.cpu")} pct={metrics.cpu} />}
              {metrics.mem !== undefined && <MiniBar label={t("hub.mem")} pct={metrics.mem} />}
            </Stack>
          )}
        </CardContent>
      </CardActionArea>

      <Collapse in={expanded} unmountOnExit>
        <Box sx={{ px: 2, pb: 2 }}>
          <MachineDetail server={s} />
        </Box>
      </Collapse>
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

// Drill-down: the machine's monitors (from its #96 snapshot) + recent events
// (fetched only while expanded via the existing per-server events filter).
function MachineDetail({ server: s }: { server: HubServer }) {
  const { t } = useTranslation();
  const monitors = s.snapshot?.monitors ?? {};
  const events = useQuery({
    queryKey: ["hubEvents", "server", s.id],
    queryFn: () => api.hubEvents({ server: s.id, limit: 5 }),
    refetchInterval: 5000,
  });
  return (
    <Stack spacing={1.5}>
      <Box>
        <Typography variant="overline" color="text.secondary">{t("hub.machineMonitors")}</Typography>
        {Object.keys(monitors).length === 0 ? (
          <Typography variant="body2" color="text.secondary">{t("hub.noMonitors")}</Typography>
        ) : (
          <Stack spacing={0.5} sx={{ mt: 0.5 }}>
            {Object.entries(monitors).map(([name, m]) => (
              <Stack key={name} direction="row" alignItems="center" spacing={1}>
                <StatusDot state={m.state} />
                <Typography variant="body2" sx={{ flex: 1 }}>{name}</Typography>
                {m.detail && (
                  <Typography variant="caption" color="text.secondary"
                    sx={{ textAlign: "right", wordBreak: "break-all" }}>{m.detail}</Typography>
                )}
              </Stack>
            ))}
          </Stack>
        )}
      </Box>
      <Box>
        <Typography variant="overline" color="text.secondary">{t("hub.machineEvents")}</Typography>
        <EventLog events={events.data?.events} />
      </Box>
    </Stack>
  );
}
