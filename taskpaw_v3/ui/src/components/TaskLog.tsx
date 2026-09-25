import { Fragment, useEffect, useId, useRef, useState } from "react";
import { Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Stack, TextField, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { api, type LogEntry, type LogDays, type LogParams } from "../api";
import { compareLogIds, formatLogDay, localLogDay, logDetails, logSafeText, logText, logTime, mergeLogEntries, renderLogSentence } from "./TaskLog.helpers";

function LogRow({ entry, compact }: { entry: LogEntry; compact: boolean }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const mirrored = entry.kind === "event.mirrored";
  const details = logDetails(entry, t);
  return (
    <Box component="li" data-testid={`log-row-${entry.id}`} data-compact={compact || mirrored}
      sx={{ listStyle: "none", py: mirrored || compact ? 0.5 : 1, borderBottom: 1, borderColor: "divider", overflowWrap: "anywhere" }}>
      <Stack direction="row" alignItems="center" useFlexGap flexWrap="wrap" gap={1}>
        <Typography component="time" dateTime={entry.ts} variant="body2" color="text.secondary" sx={{ fontVariantNumeric: "tabular-nums" }}>{logTime(entry)}</Typography>
        <Chip size="small" variant="outlined" color={entry.severity === "warn" ? "warning" : entry.severity === "error" ? "error" : "info"} label={t(`logs.${entry.severity}`)} />
        <Typography variant="caption" color="text.secondary">{logSafeText(entry.task)} · {logSafeText(entry.task_type)}</Typography>
        {details.length > 0 && <Button size="small" aria-expanded={expanded} aria-controls={`details-${entry.id}`}
          aria-label={`${t("logs.details")} ${entry.id}`} onClick={() => setExpanded(!expanded)} sx={{ ml: "auto", minHeight: 40 }}>{t("logs.details")}</Button>}
      </Stack>
      <Typography variant={mirrored || compact ? "caption" : "body1"} component="p" color={mirrored ? "text.secondary" : "text.primary"}>
        {renderLogSentence(entry, t)}
      </Typography>
      {entry.film && <Typography variant="caption" color="text.secondary">{logSafeText(entry.film)}</Typography>}
      {expanded && <Box component="dl" id={`details-${entry.id}`} sx={{ m: 0, mt: 1, p: 1, bgcolor: "background.default" }}>
        {details.map(([label, value]) => <Box key={label} sx={{ display: "flex", flexWrap: "wrap", gap: 1, py: 0.25 }}>
          <Typography component="dt" variant="caption" color="text.secondary">{label}</Typography>
          <Typography component="dd" variant="body2" sx={{ m: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", minWidth: 0 }}>{value}</Typography>
        </Box>)}
      </Box>}
    </Box>
  );
}

export function TaskLogRows({ entries = [], compact = false }: { entries?: LogEntry[]; compact?: boolean }) {
  const { t } = useTranslation();
  const rows = mergeLogEntries(entries);
  if (!rows.length) return <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>{t("logs.empty")}</Typography>;
  return <Box component="ul" sx={{ p: 0, m: 0 }}>
    {rows.map((entry, index) => <Fragment key={entry.id}>
      {(index === 0 || entry.ts.slice(0, 10) !== rows[index - 1].ts.slice(0, 10)) &&
        <Typography component="li" variant="overline" sx={{ listStyle: "none", pt: 1 }}>{entry.ts.slice(0, 10)}</Typography>}
      <LogRow entry={entry} compact={compact} />
    </Fragment>)}
  </Box>;
}

type Session = {
  day: string;
  boot: string | null;
  cursor: string;
  before: string | null;
  busy: boolean;
  active: boolean;
  load: (older?: boolean) => Promise<void>;
};

// The mounted tab owns one session. Filter changes invalidate it; late responses
// cannot overwrite the next selection. Paging and polling are serialized.
export function TaskLog({ tasks = [] }: { tasks?: string[] }) {
  const { t, i18n } = useTranslation();
  const [today, setToday] = useState(() => localLogDay());
  const yesterday = localLogDay(-1);
  const taskListId = useId();
  const [selection, setSelection] = useState("today");
  const day = selection === "today" ? today : selection;
  const [taskText, setTaskText] = useState("");
  const [task, setTask] = useState("");
  const [severity, setSeverity] = useState("");
  const [searchText, setSearchText] = useState("");
  const [q, setQ] = useState("");
  const [revision, setRevision] = useState(0);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [days, setDays] = useState<LogDays["days"]>([]);
  const [before, setBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState("");
  const session = useRef<Session | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => { setTask(taskText); setQ(searchText); }, 300);
    return () => window.clearTimeout(timer);
  }, [taskText, searchText]);

  useEffect(() => {
    const live = selection === "today";
    const day = live ? localLogDay() : selection;
    const current: Session = { day, boot: null, cursor: `${day}-0`, before: null, busy: false, active: true, load: async () => {} };
    session.current = current;
    const filters = { task, severity, q };
    const restart = () => { if (current.active) setRevision(n => n + 1); };
    async function readDays() {
      const result = await api.logDays();
      if (!current.active) return;
      if (current.boot && current.boot !== result.boot) { restart(); return; }
      setDays(result.days);
    }
    current.load = async (older = false) => {
      if (!current.active || current.busy) return;
      current.busy = true; setBusy(true); setError(false);
      if (!older) { setLoading(true); setEntries([]); setBefore(null); }
      try {
        const result = await api.logs({ day, ...filters, limit: 100, ...(older && current.before ? { before: current.before } : {}) });
        if (!current.active) return;
        if (current.boot && result.boot !== current.boot) { restart(); return; }
        current.boot = result.boot;
        current.before = result.next_before;
        setBefore(result.next_before);
        const rows = mergeLogEntries(result.entries);
        if (!older) current.cursor = rows[0]?.id ?? `${day}-0`;
        setEntries(previous => older ? mergeLogEntries(previous, rows) : rows);
        await readDays();
      } catch { if (current.active) setError(true); }
      finally { current.busy = false; if (current.active) { setBusy(false); setLoading(false); } }
    };
    async function poll() {
      const nowDay = localLogDay();
      if (current.active) setToday(nowDay);
      if (!current.active || current.busy) return;
      if (!current.boot) { await current.load(); return; }
      current.busy = true; setBusy(true);
      try {
        // Catch up full pages without skipping rows. Bound each tick so a busy
        // producer cannot monopolize the tab; the next tick resumes this cursor.
        for (let page = 0; live && page < 20 && current.active; page++) {
          const result = await api.logs({ ...filters, after: current.cursor, limit: 500 });
          if (!current.active) return;
          if (result.boot !== current.boot) { restart(); return; }
          const newest = mergeLogEntries(result.entries)[0]?.id;
          if (!newest || compareLogIds(newest, current.cursor) <= 0) break;
          current.cursor = newest;
          // Today's live timeline carries forward over midnight with date
          // headers; an explicitly selected historical day stays day-scoped.
          const visible = result.entries.filter(e => e.id.slice(0, 8) >= day);
          setEntries(previous => mergeLogEntries(previous, visible));
          if (result.entries.length < 500) break;
        }
        await readDays();
        if (current.active) setError(false);
      } catch { if (current.active) setError(true); }
      finally { current.busy = false; if (current.active) setBusy(false); }
    }
    void current.load();
    const timer = window.setInterval(() => { void poll(); }, 5000);
    return () => { current.active = false; window.clearInterval(timer); };
  }, [selection, task, severity, q, revision]);

  async function exportDay() {
    const current = session.current;
    if (!current?.boot || exporting) return;
    setExporting(true); setNotice("");
    const translate = i18n.getFixedT(i18n.language);
    const exportDay = selection === "today" ? localLogDay() : day;
    const params: LogParams = { day: exportDay, task, severity, q, limit: 500 };
    const all = new Map<string, LogEntry>();
    const cursors = new Set<string>();
    let truncated = false;
    try {
      do {
        const result = await api.logs(params);
        if (!current.active) return;
        if (result.boot !== current.boot) {
          setNotice("exportRestart"); setRevision(n => n + 1); return;
        }
        for (const e of result.entries) { if (all.size < 20000) all.set(e.id, e); }
        if (all.size >= 20000) { truncated = Boolean(result.next_before); break; }
        if (!result.next_before) break;
        if (cursors.has(result.next_before)) throw new Error("Non-advancing log page");
        cursors.add(result.next_before);
        params.before = result.next_before;
      } while (current.active);
      const blob = new Blob([logText([...all.values()], translate), truncated ? `\n${translate("logs.exportCap")}\n` : ""], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement("a");
        link.href = url; link.download = `taskpaw-${exportDay}.txt`;
        document.body.appendChild(link);
        try { link.click(); } finally { link.remove(); }
      } finally {
        // Give the WebView download flow time to acquire the blob.
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      if (truncated) setNotice("exportCap");
    } catch { if (current.active) setNotice("exportFailed"); }
    finally { setExporting(false); }
  }

  const taskNames = [...new Set([...tasks, ...entries.map(e => e.task), ...(task ? [task] : [])].filter(Boolean))].sort();
  return <Card><CardContent>
    <Stack spacing={1.5}>
      <Stack direction="row" useFlexGap flexWrap="wrap" gap={1}>
        <TextField select SelectProps={{ native: true }} size="small" label={t("logs.day")} value={day} onChange={e => setSelection(e.target.value === today ? "today" : e.target.value)} sx={{ minWidth: 155 }}>
          <option value={today}>{t("logs.today")}</option><option value={yesterday}>{t("logs.yesterday")}</option>
          {days.filter(d => d.day !== today && d.day !== yesterday).map(d => <option key={d.day} value={d.day}>{formatLogDay(d.day)} ({d.count})</option>)}
        </TextField>
        <TextField size="small" label={t("logs.task")} placeholder={t("logs.allTasks")} value={taskText} onChange={e => setTaskText(e.target.value)} inputProps={{ list: taskListId }} sx={{ minWidth: 150, maxWidth: "100%" }} />
        <datalist id={taskListId}>{taskNames.map(name => <option key={name} value={name}>{logSafeText(name)}</option>)}</datalist>
        <TextField select SelectProps={{ native: true }} size="small" label={t("logs.severity")} value={severity} onChange={e => setSeverity(e.target.value)} sx={{ minWidth: 160 }}>
          <option value="">{t("logs.allSeverities")}</option>
          {["info", "warn", "error"].map(s => <option key={s} value={s}>{t(`logs.${s}`)}</option>)}
          <option value="warn,error">{t("logs.warningsErrors")}</option>
        </TextField>
        <TextField size="small" label={t("logs.search")} value={searchText} onChange={e => setSearchText(e.target.value)} sx={{ flex: "1 1 220px" }} />
        <Button variant="outlined" disabled={loading || exporting || !session.current?.boot} onClick={() => { void exportDay(); }}>{t(exporting ? "logs.exporting" : "logs.export")}</Button>
      </Stack>
      {notice && <Alert severity={notice === "exportCap" ? "info" : "warning"}>{t(`logs.${notice}`)}</Alert>}
      {error && <Alert severity="error" action={<Button color="inherit" disabled={busy} onClick={() => setRevision(n => n + 1)}>{t("logs.retry")}</Button>}>{t("logs.failed")}</Alert>}
      <Box role="status" sx={{ minHeight: 20 }}>
        {busy && <Typography variant="caption" color="text.secondary">{t(loading ? "common.loading" : "common.updating")}</Typography>}
      </Box>
      {loading ? <CircularProgress size={24} aria-label={t("common.loading")} /> : <TaskLogRows key={`${revision}-${selection}-${task}-${severity}-${q}`} entries={entries} />}
      {before && <Button disabled={busy} onClick={() => { void session.current?.load(true); }}>{t("logs.earlier")}</Button>}
    </Stack>
  </CardContent></Card>;
}
