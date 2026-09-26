import { Box, Chip, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import RemoveIcon from "@mui/icons-material/Remove";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { TINT } from "./monitorMetrics.helpers";
import { type FilmRow, type StepState, clock, etaText, stepLabel } from "./pipelineProgress.helpers";
import type { PageRow } from "./pagedFilmList.helpers";
import { type Tone, toneSx } from "./filmList.helpers";

const MONO = '"Fira Code", monospace';
const BOX = { borderRadius: 2, border: "1px solid", borderColor: "divider" } as const;
// ── batch list ──────────────────────────────────────────────────────────────
const ROW_ACTIVE = new Set(["restore", "asr", "translate"]);

function rowStatus(r: FilmRow | PageRow, t: TFunction): string {
  switch (r.status) {
    case undefined:
      return "";
    case "active": {
      const key = r.steps.find(([, s]) => s === "active")?.[0];
      return key && ROW_ACTIVE.has(key) ? t(`pipeline.row.${key}`) : t("pipeline.row.active");
    }
    case "pending":
      // A Jasna film restored before this run only needs its subtitles.
      return r.steps.some(([k, s]) => k === "restore" && s === "done")
        ? t("pipeline.row.subsOnly") : t("pipeline.row.pending");
    default:
      return t(`pipeline.row.${r.status}`);
  }
}

function rowTime(r: FilmRow | PageRow, t: TFunction): string {
  if (r.status === "done" && r.duration_s !== undefined) return clock(r.duration_s);
  if (r.status === "active" && r.eta_s !== undefined) {
    return t("pipeline.left", { d: etaText(r.eta_s, t) });
  }
  return "—";
}

function RowChip({ stepKey, state, percent }: { stepKey: string; state: StepState; percent?: number }) {
  const { t } = useTranslation();
  const label = state === "active" && percent !== undefined
    ? `${stepLabel(stepKey, t)} ${Math.round(percent)}%` : stepLabel(stepKey, t);
  const icon = state === "done" ? <CheckIcon />
    : state === "failed" ? <CloseIcon />
    : state === "skipped" ? <RemoveIcon /> : undefined;
  const tone: Tone = state === "done" ? "ok" : state === "failed" ? "crit"
    : state === "waiting_gpu" ? "warn" : "idle";
  return (
    <Chip size="small" label={label} icon={icon}
      variant={state === "active" ? "outlined" : "filled"}
      sx={state === "active"
        ? { borderColor: TINT.ok, bgcolor: alpha(TINT.ok, 0.08), color: "text.primary" }
        : { ...toneSx(tone), "& .MuiChip-icon": { color: "inherit", fontSize: 14 } }} />
  );
}

export function FilmList({ films, filmsMore = 0, focus: focusName, total = films.length + filmsMore }: {
  films: (FilmRow | PageRow)[]; filmsMore?: number; focus?: string; total?: number;
}) {
  const { t } = useTranslation();

  // A single film is already the header above — the list is for batches.
  if (total < 2 && filmsMore === 0) return null;
  return (
    <Box data-testid="pipeline-films" sx={{ ...BOX, py: 0.5 }}>
      <Typography variant="overline" color="text.secondary" sx={{ px: 2, display: "block" }}>
        {t("pipeline.films")}
      </Typography>
      {films.map((r, i) => {
        const focus = r.name === focusName;
        const activeKey = r.steps.find(([, s]) => s === "active")?.[0];
        return (
          <Box key={`${r.name}-${i}`} data-testid="film-row" aria-current={focus ? "true" : undefined} sx={{
            display: "flex", flexWrap: "wrap", alignItems: "center", columnGap: 2, rowGap: 0.75,
            px: 2, py: 1, borderTop: "1px solid", borderColor: "divider",
            bgcolor: focus ? alpha(TINT.ok, 0.05) : "transparent",
          }}>
            <Typography sx={{ fontFamily: MONO, fontSize: 13, fontWeight: focus ? 600 : 400,
              flex: "1 1 160px", minWidth: 0, wordBreak: "break-all" }}>{r.name}</Typography>
            <Stack direction="row" sx={{ flexWrap: "wrap", gap: 0.75 }}>
              {r.steps.map(([k, s]) => (
                <RowChip key={k} stepKey={k} state={s} percent={k === activeKey ? r.percent : undefined} />
              ))}
            </Stack>
            <Typography sx={{ fontSize: 13, flex: "1 1 140px", minWidth: 0,
              color: r.status === "failed" ? "error.main"
                : r.status === "active" || r.status === "done" ? "text.primary" : "text.secondary" }}>
              {rowStatus(r, t)}
            </Typography>
            <Typography sx={{ fontFamily: MONO, fontSize: 13, color: "text.secondary", minWidth: 72,
              textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{rowTime(r, t)}</Typography>
          </Box>
        );
      })}
      {filmsMore > 0 && (
        <Typography variant="caption" color="text.secondary"
          sx={{ px: 2, py: 1, display: "block", borderTop: "1px solid", borderColor: "divider" }}>
          {t("pipeline.more", { n: filmsMore })}
        </Typography>
      )}
    </Box>
  );
}

