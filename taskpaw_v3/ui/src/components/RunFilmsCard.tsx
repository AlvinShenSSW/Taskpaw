import { Fragment, useEffect, useId, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Chip, Table, TableBody, TableCell, TableHead, TableRow, Typography, useMediaQuery } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import { visuallyHidden } from "@mui/utils";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { FilmList } from "./FilmList";
import { BOX, MONO, type Pipeline } from "./pipelineProgress.helpers";
import { toneSx, type Tone } from "./filmList.helpers";
import { TINT } from "./monitorMetrics.helpers";
import { RUN_FILTERS, RUN_TOTALS, readRunFilms, restoreLabel, translationLabel, runDuration, runFinishedAt,
  type RunFilm, type RunFilms, type RunFilter } from "./runFilmsCard.helpers";

const buttonSx = { minHeight: 40, minWidth: 40,
  "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 2 } };
const COLUMNS = ["name", "restore", "translate", "models", "duration", "finished"] as const;

function FilmModels({ models }: { models: RunFilm["models"] }) {
  const { t } = useTranslation();
  const id = useId();
  if (!models.length) return <>—</>;
  return <>{models.map(([label, lines], i) => <Box key={`${label}-${i}`}>
    <Box component="span" aria-describedby={`${id}-${i}`}>
      {t("runFilms.modelLines", { model: label.split(" · ")[0], n: lines })}
    </Box>
    <Box component="span" id={`${id}-${i}`} sx={visuallyHidden}>{label}</Box>
  </Box>)}</>;
}

function FilmRow({ row, focus, desktop }: { row: RunFilm; focus: boolean; desktop: boolean }) {
  const { t } = useTranslation();
  const failed = row.outcome === "failed" || row.outcome === "asr_failed" || row.outcome === "restore_failed";
  const tone: Tone = failed ? "crit" : row.outcome === "partial" ? "warn"
    : row.outcome === "translated" ? "ok" : "idle";
  const cells = [
    <Box component="span" sx={{ fontFamily: MONO, fontWeight: focus ? 600 : 400 }}>{row.name}</Box>,
    <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      {!row.restored_before && row.restore === "done" && <CheckIcon sx={{ fontSize: 16, color: "success.main" }} />}
      {row.restore === "failed" && <CloseIcon sx={{ fontSize: 16, color: "error.main" }} />}
      {restoreLabel(row, t)}
    </Box>,
    <Chip size="small" label={translationLabel(row, t)} sx={{ ...toneSx(tone), maxWidth: "100%", height: "auto",
      "& .MuiChip-label": { whiteSpace: "normal", py: 0.5, overflowWrap: "anywhere" } }} />,
    <FilmModels models={row.models} />,
    runDuration(row.duration_s, t),
    runFinishedAt(row.finished_at),
  ];
  const sx = { bgcolor: focus ? alpha(TINT.ok, 0.08) : "transparent", overflowWrap: "anywhere" } as const;
  if (desktop) return <TableRow data-testid="run-film-row" aria-current={focus ? "true" : undefined} sx={sx}>
    {cells.map((cell, i) => <TableCell key={COLUMNS[i]} sx={{ verticalAlign: "top", px: 1.5, py: 1,
      fontSize: 13, fontVariantNumeric: "tabular-nums" }}>{cell}</TableCell>)}
  </TableRow>;
  return <Box data-testid="run-film-row" aria-current={focus ? "true" : undefined}
    sx={{ ...sx, minWidth: 0, p: 2, borderTop: "1px solid", borderColor: "divider" }}>
    {cells[0]}
    <Box component="dl" sx={{ m: 0, mt: 1, display: "flex", flexWrap: "wrap", gap: 1 }}>
      {cells.slice(1).map((cell, i) => <Box key={COLUMNS[i + 1]} sx={{ display: "flex", flexWrap: "wrap", gap: 0.5,
        flex: "1 1 200px", minWidth: 0, maxWidth: "100%", alignItems: "baseline" }}>
        <Typography component="dt" variant="caption" color="text.secondary">
          {t("runFilms.stackedLabel", { label: t(i === 2 ? "runFilms.models" : `runFilms.column.${COLUMNS[i + 1]}`) })}
        </Typography>
        <Box component="dd" sx={{ m: 0, minWidth: 0, maxWidth: "100%", fontSize: 13 }}>{cell}</Box>
      </Box>)}
    </Box>
  </Box>;
}

// Mounted with key={taskName}; lastGood and query cursors belong to one task.
export function RunFilmsCard({ name, fallback }: { name: string; fallback?: Pipeline }) {
  const { t } = useTranslation();
  const headingId = useId();
  const desktop = useMediaQuery(useTheme().breakpoints.up("sm"));
  const queryClient = useQueryClient();
  const [request, setRequest] = useState<{ filter: RunFilter; page: number }>({ filter: "done", page: 1 });
  const { filter, page } = request;
  const [lastGood, setLastGood] = useState<{ data: RunFilms; request: typeof request }>();
  const query = useQuery({
    queryKey: ["runFilms", name, filter, page],
    queryFn: async () => readRunFilms(await api.runFilms(name, filter, page, 10)),
    refetchInterval: 5000,
    placeholderData: keepPreviousData,
    gcTime: 0,
  });

  // Recovery seeds can inherit a longer client gcTime; remove them immediately
  // on departure so a later filter/page visit never flashes abandoned data.
  useEffect(() => {
    queryClient.removeQueries({ queryKey: ["runFilms", name], predicate: q => q.getObserversCount() === 0 });
  }, [filter, page, queryClient, name]);
  useEffect(() => () => queryClient.removeQueries({ queryKey: ["runFilms", name] }), [queryClient, name]);

  useEffect(() => {
    if (query.data && !query.isPlaceholderData && !query.isError) {
      if (query.data !== lastGood?.data) {
        const newRun = lastGood !== undefined && lastGood.data.run !== query.data.run;
        const answered = { filter: query.data.filter, page: newRun ? 1 : query.data.page };
        setLastGood({ data: query.data, request: answered });
        if (filter !== answered.filter || page !== answered.page) setRequest(answered);
      }
    } else if (query.isError && lastGood) {
      const restored = lastGood.request;
      if (filter !== restored.filter || page !== restored.page) {
        // Seed recovery before changing the cursor: it is a background refresh,
        // so both filters and pager remain usable even if that refresh stalls.
        queryClient.setQueryData(["runFilms", name, restored.filter, restored.page], lastGood.data);
        setRequest(restored);
      }
    }
  }, [query.data, query.isPlaceholderData, query.isError, lastGood, filter, page, queryClient, name]);

  const data = query.data && !query.isPlaceholderData && !query.isError ? query.data : lastGood?.data;
  if (!data || (data.counts.all === 0 && fallback && fallback.films.length > 0)) {
    return fallback ? <FilmList films={fallback.films} filmsMore={fallback.filmsMore} focus={fallback.film} /> : null;
  }
  const inFlight = query.isPlaceholderData;
  return <Box component="section" role="region" aria-labelledby={headingId}
    sx={{ ...BOX, borderColor: "success.main", bgcolor: "background.paper", minWidth: 0, maxWidth: "100%" }}>
    <Box sx={{ p: 2 }}>
      <Typography id={headingId} component="h3" sx={{ fontFamily: MONO, fontWeight: 600 }}>{t("runFilms.title")}</Typography>
      <Typography variant="caption" color="text.secondary">{t(`runFilms.order.${data.filter}`)}</Typography>
      <Box role="group" aria-label={t("runFilms.filters")} sx={{ display: "flex", flexWrap: "wrap", gap: 0.5, my: 1.5 }}>
        {RUN_FILTERS.map(f => <Button key={f} sx={buttonSx} variant={data.filter === f ? "contained" : "outlined"}
          aria-pressed={data.filter === f} disabled={inFlight} onClick={() => setRequest({ filter: f, page: 1 })}>
          {t(`runFilms.filter.${f}`, { n: data.counts[f] })}
        </Button>)}
      </Box>
      <Box sx={{ display: "flex", flexWrap: "wrap", columnGap: 2, rowGap: 0.5 }}>
        {RUN_TOTALS.map(k => <Typography key={k} variant="caption" color="text.secondary">
          {t(`runFilms.totals.${k}`)} {data.totals[k]}
        </Typography>)}
      </Box>
    </Box>
    {data.films.length === 0 ? <Typography sx={{ p: 2 }} color="text.secondary">{t(`runFilms.empty.${data.filter}`)}</Typography>
      : desktop ? <Table size="small" aria-label={t("runFilms.title")} sx={{ tableLayout: "fixed", width: "100%" }}>
        <TableHead><TableRow>{COLUMNS.map(k => <TableCell key={k} scope="col" sx={{ px: 1.5, color: "text.secondary" }}>
          {t(`runFilms.column.${k}`)}
        </TableCell>)}</TableRow></TableHead>
        <TableBody>{data.films.map((row, i) => <FilmRow key={`${row.name}-${i}`} row={row} focus={row.name === data.focus} desktop />)}</TableBody>
      </Table> : <Fragment>{data.films.map((row, i) =>
        <FilmRow key={`${row.name}-${i}`} row={row} focus={row.name === data.focus} desktop={false} />)}</Fragment>}
    {data.total > 10 && <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, p: 2 }}>
      <Button variant="outlined" sx={buttonSx} disabled={inFlight || data.page <= 1}
        onClick={() => setRequest({ filter: data.filter, page: data.page - 1 })}>{t("pipeline.paging.previous")}</Button>
      <Typography variant="body2" sx={{ fontVariantNumeric: "tabular-nums" }}>
        {t("pipeline.paging.page", { page: data.page, pages: data.pages, total: data.total })}
      </Typography>
      <Button variant="outlined" sx={buttonSx} disabled={inFlight || data.page >= data.pages}
        onClick={() => setRequest({ filter: data.filter, page: data.page + 1 })}>{t("pipeline.paging.next")}</Button>
    </Box>}
  </Box>;
}
