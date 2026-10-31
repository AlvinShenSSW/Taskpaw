// The presentational pieces of the Jasna console progress cards (#269 LtxProgress), shared so the
// pre-LTX phases (U1c: mosaic scan, BasicVSR++) and the workflow's 修复 step read exactly like it.
// Data-free: every value arrives already validated by the callers' readers.
import { useId, useState, type ReactNode } from "react";
import { Box, Button, LinearProgress, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ScheduleIcon from "@mui/icons-material/Schedule";
import { useTranslation } from "react-i18next";

export const MONO = '"Fira Code","Noto Sans SC","Microsoft YaHei","PingFang SC",ui-monospace,monospace';
export const DATA = { fontFamily: MONO, fontVariantNumeric: "tabular-nums", overflowWrap: "anywhere" } as const;

export function ProgressFrame({ testId, label, title, position, children }: {
  testId: string; label: string; title: ReactNode; position?: ReactNode; children: ReactNode;
}) {
  return (
    <Box component="section" data-testid={testId} aria-label={label}
      sx={{ border: "1px solid", borderColor: "divider", borderRadius: 2, minWidth: 0, overflow: "hidden",
        bgcolor: "background.paper" }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center"
        sx={{ px: { xs: 2, sm: 3 }, py: 2, gap: 1, flexWrap: "wrap", borderBottom: "1px solid", borderColor: "divider" }}>
        <Typography fontWeight={600} sx={{ fontSize: 14 }}>{title}</Typography>
        {position !== undefined && <Typography variant="caption" color="text.secondary" sx={DATA}>{position}</Typography>}
      </Stack>
      {children}
    </Box>
  );
}

// The big stage name + percent, its bar and the n / total counter row.
export function ProgressHeadline({ caption, name, label, percent, percentText, percentCaption, barLabel,
  current, total, countUnit, countsHelp }: {
  caption: string; name: string; label?: string; percent?: number; percentText?: string; percentCaption: string;
  barLabel: string; current?: string; total?: string; countUnit: string; countsHelp: string;
}) {
  return (
    <>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end" sx={{ gap: 2 }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>{caption}</Typography>
          {/* "&&": outranks host heading styles (the 媒体处理中心's `.mw h3`) so both places match */}
          <Typography component="h3" sx={{ "&&": { ...DATA, fontSize: { xs: 25, sm: 31 }, fontWeight: 500, lineHeight: 1.3 } }}>
            {name}{label && <Box component="span" sx={{ display: { xs: "block", sm: "inline" },
              ml: { xs: 0, sm: 1.5 }, fontFamily: theme => theme.typography.fontFamily, fontSize: { xs: 14, sm: 18 }, color: "text.secondary" }}> {label}</Box>}
          </Typography>
        </Box>
        <Box sx={{ textAlign: "right", flexShrink: 0, maxWidth: "50%", minWidth: 0 }}>
          <Typography sx={{ ...DATA, fontSize: { xs: 38, sm: 48 }, lineHeight: 1.1, color: "success.light" }}>
            {percentText === undefined ? "—" : `${percentText}%`}
          </Typography>
          <Typography variant="caption" color="text.secondary">{percentCaption}</Typography>
        </Box>
      </Stack>
      <LinearProgress aria-label={barLabel}
        variant={percent === undefined ? "indeterminate" : "determinate"} value={percent}
        sx={{ mt: 2.5, height: 10, borderRadius: 5, bgcolor: theme => alpha(theme.palette.text.secondary, 0.15),
          "& .MuiLinearProgress-bar": { borderRadius: 5 },
          "@media (prefers-reduced-motion: reduce)": { "&, & *": { animation: "none", transition: "none" } } }} />
      <Stack direction="row" justifyContent="space-between" sx={{ gap: 1, flexWrap: "wrap", mt: 1.25 }}>
        <Typography variant="caption" color="text.secondary">
          <Box component="span" sx={{ ...DATA, color: "text.primary" }}>{current ?? "—"} / {total ?? "—"}</Box> {countUnit}
        </Typography>
        <Typography variant="caption" color="text.secondary">{countsHelp}</Typography>
      </Stack>
    </>
  );
}

export type GridMetric = { label: string; value?: string; unit?: string; help: string };

// Rate · elapsed · remaining, one column each; a missing value reads 「—」, never a made-up zero.
export function MetricGrid({ values }: { values: GridMetric[] }) {
  return (
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "1.2fr 1fr 1fr" },
      gap: { xs: 2, sm: 3 }, mt: 3, pt: 2.5, borderTop: "1px solid", borderColor: "divider" }}>
      {values.map((metric, index) => <Box key={metric.label} sx={{ minWidth: 0,
        gridColumn: { xs: index === 0 ? "1 / -1" : "auto", sm: "auto" },
        borderLeft: { xs: index === 2 ? "1px solid" : "none", sm: index > 0 ? "1px solid" : "none" },
        borderColor: "divider", pl: { xs: index === 2 ? 2 : 0, sm: index > 0 ? 3 : 0 } }}>
        <Typography variant="caption" color="text.secondary">{metric.label}</Typography>
        <Typography sx={{ ...DATA, mt: 0.5, fontSize: { xs: 24, sm: 27 }, lineHeight: 1.4 }}>
          {metric.value ?? "—"}{metric.unit && <Box component="span" sx={{ ml: 0.75, fontSize: 14, color: "text.secondary" }}> {metric.unit}</Box>}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>{metric.help}</Typography>
      </Box>)}
    </Box>
  );
}

// The expandable, selectable and copyable console line.
export function ConsoleFold({ raw, title, help, unavailable, testIdPrefix = "ltx-console" }: {
  raw?: string; title?: string; help?: string; unavailable?: string; testIdPrefix?: string;
}) {
  const { t } = useTranslation();
  const rawId = useId();
  const [copy, setCopy] = useState<{ line: string; result: "ok" | "failed" }>();
  const feedback = copy && copy.line === raw ? copy.result : undefined;
  const heading = title ?? t("jasna.consoleTitle");
  const copyRaw = async () => {
    if (!raw) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(raw);
      setCopy({ line: raw, result: "ok" });
    } catch {
      setCopy({ line: raw, result: "failed" });
    }
  };
  return (
    <Box component="details" sx={{ borderTop: "1px solid", borderColor: "divider",
      "&[open] .ltx-disclosure-icon": { transform: "rotate(180deg)" } }}>
      <Box component="summary" data-testid={`${testIdPrefix}-toggle`} aria-controls={rawId}
        sx={{ minHeight: 48, px: { xs: 2, sm: 3 }, py: 1.5, display: "flex", alignItems: "center",
          justifyContent: "space-between", cursor: "pointer", listStyle: "none", fontSize: 12,
          "&::-webkit-details-marker": { display: "none" },
          "&:focus-visible": { outline: "2px solid", outlineColor: "info.main", outlineOffset: -3 } }}>
        {heading}<ExpandMoreIcon className="ltx-disclosure-icon" aria-hidden sx={{ fontSize: 20 }} />
      </Box>
      <Box sx={{ px: { xs: 2, sm: 3 }, pb: 2 }}>
        <Box component="pre" id={rawId} data-testid={`${testIdPrefix}-raw`} tabIndex={0} aria-label={heading}
          sx={{ ...DATA, m: 0, p: 1.5, fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word", userSelect: "text",
            bgcolor: "background.default", border: "1px solid", borderColor: "divider", borderRadius: 1,
            "&:focus-visible": { outline: "2px solid", outlineColor: "info.main" } }}>{raw ?? "—"}</Box>
        <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ gap: 1, flexWrap: "wrap", mt: 1 }}>
          <Typography variant="caption" color="text.secondary">
            {raw ? help ?? t("jasna.consoleHelp") : unavailable ?? t("jasna.consoleUnavailable")}
          </Typography>
          <Button size="small" startIcon={<ContentCopyIcon />} disabled={!raw} onClick={() => void copyRaw()}
            sx={{ minHeight: 40, fontSize: 12 }}>{t("jasna.copyLine")}</Button>
        </Stack>
        {feedback && <Typography role="status" variant="caption" color={feedback === "ok" ? "success.main" : "text.secondary"}>
          {t(feedback === "ok" ? "jasna.copyOk" : "jasna.copyFailed")}
        </Typography>}
      </Box>
    </Box>
  );
}

// The footer row under a progress card: an icon, a label and one value (LTX: the run ETA).
export function RunEtaRow({ label, value }: { label: string; value?: string }) {
  return (
    <Box sx={{ borderTop: "1px solid", borderColor: "divider", px: { xs: 2, sm: 3 }, py: 2,
      display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 1.5,
      bgcolor: theme => alpha(theme.palette.background.default, 0.4) }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <ScheduleIcon aria-hidden sx={{ color: "info.main", fontSize: 17 }} />{label}
      </Typography>
      <Typography sx={{ ...DATA, fontSize: 22, color: "info.light" }}>{value ?? "—"}</Typography>
    </Box>
  );
}

export function ScopeNote({ children }: { children: ReactNode }) {
  return (
    <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: { xs: 2, sm: 3 }, pb: 2 }}>
      {children}
    </Typography>
  );
}
