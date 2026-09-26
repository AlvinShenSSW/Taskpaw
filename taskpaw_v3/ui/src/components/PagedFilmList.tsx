import { useEffect, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { FilmList } from "./FilmList";
import type { Pipeline } from "./pipelineProgress.helpers";
import { type FilmPage, readFilmPage } from "./pagedFilmList.helpers";

export function PagedFilmList({ name, fallback }: { name: string; fallback?: Pipeline }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // undefined asks the server for its current focus page on every poll.
  const [page, setPage] = useState<number>();
  const [lastGood, setLastGood] = useState<{ data: FilmPage; following: boolean }>();
  const query = useQuery({
    queryKey: ["films", name, page],
    queryFn: async () => readFilmPage(await api.films(name, page, 10)),
    refetchInterval: 5000,
    placeholderData: keepPreviousData,
    gcTime: 0,
  });

  // gcTime: 0 is timer-based. Also discard immediately on task unmount so a
  // rapid reselect cannot reuse the previous run before that timer fires.
  useEffect(() => () => queryClient.removeQueries({ queryKey: ["films", name] }), [queryClient, name]);

  useEffect(() => {
    if (query.data && !query.isPlaceholderData && !query.isError) {
      if (query.data !== lastGood?.data) {
        const newRun = lastGood !== undefined && lastGood.data.run !== query.data.run;
        setLastGood({ data: query.data, following: newRun || page === undefined });
        if (newRun) setPage(undefined);
        // Normalize a manual request after the server clamps it, so the next
        // click cannot set the already-requested (but no longer shown) page.
        else if (page !== undefined && page !== query.data.page) setPage(query.data.page);
      }
    } else if (query.isError && lastGood) {
      // V3-1: recover the request cursor too, not just the visible rows.
      const restoredPage = lastGood.following ? undefined : lastGood.data.page;
      if (page !== restoredPage) {
        // This task's retained response makes recovery a background refresh,
        // not another placeholder transition that would disable the pager.
        queryClient.setQueryData(["films", name, restoredPage], lastGood.data);
        setPage(restoredPage);
      }
    }
  }, [query.data, query.isPlaceholderData, query.isError, lastGood, page, queryClient, name]);

  const data = query.data && !query.isPlaceholderData ? query.data : lastGood?.data;
  if (!data || (data.total === 0 && fallback && fallback.films.length > 0)) {
    return fallback ? <FilmList films={fallback.films} filmsMore={fallback.filmsMore} focus={fallback.film} /> : null;
  }
  if (data.total < 2) return null;
  const inFlight = query.isPlaceholderData;
  const buttonSx = { minHeight: 40, minWidth: 40,
    "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 2 } };
  return (
    <Box sx={{ minWidth: 0 }}>
      <FilmList films={data.films} focus={data.focus ?? undefined} total={data.total} />
      {data.total > 10 && (
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mt: 1 }}>
          <Button variant="outlined" sx={buttonSx} disabled={inFlight || data.page <= 1}
            onClick={() => setPage(data.page - 1)}>{t("pipeline.paging.previous")}</Button>
          <Typography variant="body2" sx={{ fontVariantNumeric: "tabular-nums" }}>
            {t("pipeline.paging.page", { page: data.page, pages: data.pages, total: data.total })}
          </Typography>
          <Button variant="outlined" sx={buttonSx} disabled={inFlight || data.page >= data.pages}
            onClick={() => setPage(data.page + 1)}>{t("pipeline.paging.next")}</Button>
          {page !== undefined && <Button sx={buttonSx} disabled={inFlight}
            onClick={() => setPage(undefined)}>{t("pipeline.paging.current")}</Button>}
        </Box>
      )}
    </Box>
  );
}
