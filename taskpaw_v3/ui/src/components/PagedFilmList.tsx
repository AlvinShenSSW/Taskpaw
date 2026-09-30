import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { FilmList } from "./FilmList";
import { FilmListFeedback } from "./FilmListFeedback";
import { AGENT_SOURCE, filmQueryPrefix, filmFailure, type FilmSource, type FilmFailure } from "./filmSource.helpers";
import type { FilmFallback } from "./pipelineProgress.helpers";
import { type FilmPage, readFilmPage } from "./pagedFilmList.helpers";

type Props = { name: string; fallback?: FilmFallback; source?: FilmSource; showSingle?: boolean; };

export function PagedFilmList(props: Props) {
  const source = props.source ?? AGENT_SOURCE;
  return <PagedFilmListContent key={JSON.stringify([source.kind, source.kind === "hub" ? source.serverId : null, props.name])} {...props} />;
}

function PagedFilmListContent({ name, fallback, source = AGENT_SOURCE, showSingle = false }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const hub = source.kind === "hub";
  const serverId = source.kind === "hub" ? source.serverId : undefined;
  const prefix = useMemo(() => filmQueryPrefix("films", name,
    serverId === undefined ? AGENT_SOURCE : { kind: "hub", serverId }), [name, serverId]);
  const [failure, setFailure] = useState<FilmFailure>();
  // undefined asks the server for its current focus page on every poll.
  const [page, setPage] = useState<number>();
  const [lastGood, setLastGood] = useState<{ data: FilmPage; following: boolean }>();
  const query = useQuery({
    queryKey: [...prefix, page],
    queryFn: async context => {
      const signal = hub ? context.signal : undefined;
      try {
        const data = readFilmPage(await (hub ? api.hubFilms(serverId!, name, page, 10) : api.films(name, page, 10)));
        if (hub && !signal?.aborted) setFailure(undefined);
        return data;
      } catch (error) {
        if (hub && !signal?.aborted) setFailure(filmFailure(error));
        throw error;
      }
    },
    ...(hub ? { retry: false } : {}),
    refetchInterval: 5000,
    placeholderData: keepPreviousData,
    gcTime: 0,
  });

  // Recovery seeds can inherit the client's longer gcTime. Once we leave a
  // page, discard its unobserved query before a later return can reuse it.
  useEffect(() => {
    queryClient.removeQueries({ queryKey: prefix, predicate: q => q.getObserversCount() === 0 });
  }, [page, queryClient, prefix]);

  // gcTime: 0 is timer-based. Also discard immediately on task unmount so a
  // rapid reselect cannot reuse the previous run before that timer fires.
  useEffect(() => () => queryClient.removeQueries({ queryKey: prefix }), [queryClient, prefix]);

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
        queryClient.setQueryData([...prefix, restoredPage], lastGood.data);
        setPage(restoredPage);
      }
    }
  }, [query.data, query.isPlaceholderData, query.isError, lastGood, page, queryClient, prefix]);

  const data = query.data && !query.isPlaceholderData && !query.isError ? query.data : lastGood?.data;
  if (hub && (failure?.definite || (failure && !lastGood))) {
    return <FilmListFeedback fallback={fallback} noteKeys={[failure.note]} />;
  }
  if (!data || (data.total === 0 && fallback && fallback.films.length > 0)) {
    if (hub) return <FilmListFeedback fallback={fallback} noteKeys={failure
      ? [failure.note, "hub.films.stale"] : [data ? "hub.films.resyncing" : "hub.films.loading"]} />;
    return fallback ? <FilmList films={fallback.films} filmsMore={fallback.filmsMore} focus={fallback.film} /> : null;
  }
  const feedback = hub && failure
    ? <FilmListFeedback compact={false} noteKeys={[failure.note, "hub.films.stale"]} /> : null;
  if (data.total === 0 && hub) return <>{feedback}<FilmListFeedback compact={false} noteKeys={["hub.films.empty"]} /></>;
  if (data.total < 2 && !(showSingle && data.total === 1)) return feedback;
  const inFlight = query.isPlaceholderData;
  const buttonSx = { minHeight: 40, minWidth: 40,
    "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 2 } };
  return (
    <Box sx={{ minWidth: 0 }}>
      {feedback}
      <FilmList films={data.films} focus={data.focus ?? undefined} total={data.total} showSingle={showSingle} />
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
