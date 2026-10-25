import {
  Alert, Box, Card, CardContent, Chip, Divider, LinearProgress, MenuItem,
  Stack, Tab, Tabs, TextField, Typography,
} from "@mui/material";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type HubServer, type MonitorSnapshot } from "../api";
import { StatusDot } from "../components/StatusDot";
import { EventLog } from "../components/EventLog";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { utilTint } from "../components/monitorMetrics.helpers";
import { AiBadge } from "../components/AiActivity";
import { isAiMetrics } from "../components/aiActivity.helpers";
import { HubAgentManager } from "../components/HubAgentManager";
import { Settings } from "./Settings";
import { ServiceIcon } from "../components/ServiceIcon";
import { compareSemver, hubFilmKind } from "./hubDashboard.helpers";
import { requestFailed } from "../components/requestEvidence";
import { hubServerHealth, useHubResponseAge, type HubHealth } from "../hubHealth";
import { HubSampleEvidence, HubNotificationEvidence } from "../components/HubHealth";
import { MediaWorkspace } from "../components/media/MediaWorkspace";
import { MEDIA_FILMS_KEY, mediaSourceKey } from "../components/media/workspace";

const HEALTH_STATE: Record<HubHealth, string> = { ok: "ok", degraded: "degraded", offline: "stopped", stale: "unknown", disabled: "disabled" };

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
  const { t, i18n } = useTranslation();
  const status = useQuery({
    queryKey: ["hubStatus"], queryFn: api.hubStatus,
    refetchInterval: 5000, // #95: auto-refresh like the agent console.
  });
  const { data, error, isLoading } = status;
  const { elapsed, outdated } = useHubResponseAge(status.dataUpdatedAt);
  const failed = requestFailed(status.failureCount, status.errorUpdatedAt, status.dataUpdatedAt, error);
  const current = !!data && !failed && !outdated;
  const [tab, setTab] = useState<"fleet" | "manage" | "events" | "settings" | "media">("fleet");
  const [level, setLevel] = useState<string>("");
  const [serverFilter, setServerFilter] = useState<string>(""); // "" = all servers
  // Aggregated durable history from all polled agents; only poll while open.
  const events = useQuery({
    queryKey: ["hubEvents", level, serverFilter],
    queryFn: ({ signal }) => {
      const id = serverFilter ? Number(serverFilter) : NaN;
      return api.hubEvents({
        level: level || undefined,
        server: Number.isFinite(id) ? id : undefined, // never ?server=NaN (Kimi)
      }, signal);
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
    if (status.isSuccess && serverFilter && !servers.some((s) => String(s.id) === serverFilter)) {
      setServerFilter("");
    }
  }, [servers, serverFilter, status.isSuccess]);

  const counts = { ok: 0, degraded: 0, offline: 0, stale: 0, disabled: 0 };
  for (const s of servers) counts[hubServerHealth(s, current)] += 1;

  return (
    <Stack spacing={1.5}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" scrollButtons="auto" sx={{ minHeight: 0 }}>
        <Tab value="fleet" label={t("hub.fleet")} />
        <Tab value="manage" label={t("hub.manage")} />
        <Tab value="events" label={t("hub.events")} />
        <Tab value="settings" label={t("settings.title")} />
        <Tab value="media" label="媒体处理" />
      </Tabs>

      {tab === "media" ? (
        <HubMediaWorkspace servers={servers} loading={isLoading} />
      ) : tab === "settings" ? (
        <Settings role="hub" />
      ) : tab === "manage" ? (
        // Managing agents is separate from observing (design: 4 tabs). Like
        // Settings, it stays reachable when the Hub is unreachable — the agent
        // list may be empty but the add form still shows (#87 rationale).
        <HubAgentManager servers={servers} />
      ) : tab === "events" ? (
        <Card>
          <CardContent>
            {(isLoading || error) && <Alert severity="warning">{t("events.statusUnavailable")}</Alert>}
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
            <EventLog events={events.data?.events} hasData={events.data !== undefined}
              fetching={events.isFetching}
              failed={requestFailed(events.failureCount, events.errorUpdatedAt, events.dataUpdatedAt, events.error)}
              lastSuccessAt={events.dataUpdatedAt}
              onRetry={() => { void events.refetch({ cancelRefetch: false }); }} />
          </CardContent>
        </Card>
      ) : isLoading ? (
        <Typography>{t("common.loading")}</Typography>
      ) : error && !data ? (
        <Alert severity="error">{t("hub.unreachable", { error: String(error) })}</Alert>
      ) : (
        <Stack spacing={2}>
          {failed ? <Alert severity="error">{t("hub.unreachable", { error: String(error ?? "request failed") })}</Alert>
            : outdated && <Alert severity="warning">{i18n.language.startsWith("en") ? "Hub response is outdated; showing last known data" : "Hub 响应已过期，正在显示上次记录"}</Alert>}
          <HubNotificationEvidence health={data?.notifications} elapsed={elapsed} current={current} />
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
                {counts.stale > 0 && <HealthCount health="stale" label={i18n.language.startsWith("en") ? "Stale" : "过期 / 未知"} n={counts.stale} />}
                {counts.disabled > 0 && <HealthCount health="disabled" label={t("state.disabled")} n={counts.disabled} />}
              </Stack>
            )}
          </Stack>

          <Stack spacing={1.5}>
            {servers.map((s) => <MachineRow key={s.id} server={s} current={current} elapsed={elapsed} />)}
            {servers.length === 0 && (
              <Typography color="text.secondary">{t("hub.noAgents")}</Typography>
            )}
          </Stack>

          {current && Object.keys(self).length > 0 && (
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

// Phase 1 has one workflow controller. Discover it through the existing registered Agents;
// this component only lives while the media tab is open and shares its film query with the view.
function HubMediaWorkspace({ servers, loading }: { servers: HubServer[]; loading: boolean }) {
  const enabled = servers.filter(s => s.enabled);
  const probes = useQueries({ queries: enabled.map(server => ({
    queryKey: mediaSourceKey(MEDIA_FILMS_KEY, server.id),
    queryFn: () => api.hubWorkflowFilms(server.id), refetchInterval: 5000, retry: false,
  })) });
  const index = probes.findIndex(probe => probe.isSuccess && probe.data);
  if (index >= 0) return <MediaWorkspace readOnly server={enabled[index]} />;
  return <Stack spacing={1.5}>
    <Alert severity="info">只读监控 · 操作请在主控电脑上进行</Alert>
    <Typography color="text.secondary">{loading || probes.some(probe => probe.isPending)
      ? "正在查找媒体工作流主控…" : "没有可用的媒体工作流主控。"}</Typography>
  </Stack>;
}

// Labelled health tally (dot + word + count — never color alone, a11y §1).
function HealthCount({ health, label, n }: { health: HubHealth; label: string; n: number }) {
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
function MachineRow({ server: s, current, elapsed }: { server: HubServer; current: boolean; elapsed: number }) {
  const { t, i18n } = useTranslation();
  const health = hubServerHealth(s, current);
  const online = health === "ok" || health === "degraded";
  const disabled = !s.enabled;
  const metrics = online ? hostMetrics(s) : null;
  const monitors = online ? (s.snapshot?.monitors ?? {}) : {};
  const monitorNames = Object.keys(monitors);
  const version = typeof s.snapshot?.version === "string" && s.snapshot.version ? s.snapshot.version : undefined;
  const newer = compareSemver(version, __APP_VERSION__) === 1;
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
          <Typography variant="subtitle1" sx={{ minWidth: 0, overflowWrap: "anywhere" }}>{s.name}</Typography>
          <Typography variant="body2" color="text.secondary"
            sx={{ fontFamily: '"Fira Code", monospace', minWidth: 0, overflowWrap: "anywhere" }}>{s.ip}:{s.port}</Typography>
          {version && <Typography variant="caption" aria-label={t("hub.agentVersion", { version })}
            sx={{ minWidth: 0, overflowWrap: "anywhere" }}>v{version}</Typography>}
          {/* A disabled server is forced offline by the backend; label it disabled
              (not just offline) so the two are distinguishable (Kimi). */}
          <Chip size="small"
            label={disabled ? t("state.disabled") : online ? t("hub.online") : health === "stale" ? (i18n.language.startsWith("en") ? "Stale / unknown" : "过期 / 未知") : t("hub.offline")}
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

        <HubSampleEvidence server={s} elapsed={elapsed} current={current} />

        {newer && <Alert severity="warning" sx={{ mt: 1, minWidth: 0, overflowWrap: "anywhere" }}>
          {t("hub.versionSkew", { hub: __APP_VERSION__, agent: version })}
        </Alert>}

        {/* Monitors flush beneath — no indent, thin dividers between (online only). */}
        {online && monitorNames.length > 0 && (
          <Stack sx={{ mt: 1.5 }} divider={<Divider flexItem />} spacing={1}>
            {monitorNames.map((name) => {
              const m = monitors[name];
              const typeId = typeof m.type_id === "string" && m.type_id ? m.type_id : undefined;
              const filmKind = hubFilmKind(typeId, m.metrics);
              return (
                <Box key={name}>
                  <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: "wrap", rowGap: 0.5 }}>
                    <StatusDot state={m.state} />
                    {typeId && <ServiceIcon id={typeId} />}
                    <Typography variant="body2" sx={{ flex: "1 1 120px", minWidth: 0, overflowWrap: "anywhere" }}>{name}</Typography>
                    {typeId && <Chip size="small" label={t(`monitorType.${typeId}`, { defaultValue: typeId })}
                      sx={{ maxWidth: "100%", height: "auto", "& .MuiChip-label": { whiteSpace: "normal", overflowWrap: "anywhere" } }} />}
                    {m.detail && (
                      <Typography variant="caption" color="text.secondary"
                        sx={{ textAlign: "right", wordBreak: "break-all" }}>{m.detail}</Typography>
                    )}
                  </Stack>
                  {/* Full metric gauges (CPU/GPU/VRAM/queue/fps), flush (no indent). */}
                  <MonitorMetrics metrics={m.metrics} taskType={typeId}
                    taskName={filmKind ? name : undefined}
                    filmSource={filmKind ? { kind: "hub", serverId: s.id } : undefined} />
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
