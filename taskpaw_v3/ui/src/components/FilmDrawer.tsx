import {
  Alert, Avatar, Box, Button, Chip, Divider, Drawer, IconButton, List, ListItem, ListItemAvatar,
  ListItemButton, ListItemText, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, HttpError, type FilmDetail, type Performer } from "../api";
import { LifecycleStepper } from "./LifecycleSteps";
import { SkeletonRows } from "./SkeletonRows";
import {
  fmtBytes, lifecycle, MEDIA_FILMS_KEY, mediaFilmKey, openErrorText, useMediaLabels,
} from "./mediaWorkflow.helpers";

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Film detail drawer (T06-P1 U2–U5): Miko's result, the NAS target picker, the
// single-click 「确认入库」, retry of a failed local stage and 「打开文件夹」; H2 R5 adds the
// lifecycle stepper with each step's state, note and progress.
export function FilmDrawer({ filmId, onClose }: { filmId: string | null; onClose: () => void }) {
  return (
    <Drawer anchor="right" open={filmId !== null} onClose={onClose}
      slotProps={{ paper: { sx: { width: { xs: "100%", sm: 560 }, p: 2 } } }}>
      {filmId !== null && <FilmDetailPane key={filmId} filmId={filmId} onClose={onClose} />}
    </Drawer>
  );
}

function FilmDetailPane({ filmId, onClose }: { filmId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const detail = useQuery({ queryKey: mediaFilmKey(filmId), queryFn: () => api.workflowFilm(filmId), refetchInterval: 5000 });
  const header = (
    <Stack direction="row" alignItems="center">
      <Typography variant="h6" sx={{ flex: 1, wordBreak: "break-all" }}>{detail.data?.name ?? ""}</Typography>
      <IconButton aria-label={t("media.close")} onClick={onClose}><CloseIcon /></IconButton>
    </Stack>
  );
  if (detail.isLoading) return <>{header}<SkeletonRows rows={6} /></>;
  if (detail.error || !detail.data) return <>{header}<Alert severity="error">{t("media.loadFailed")}</Alert></>;
  return <>{header}<FilmBody film={detail.data} /></>;
}

function Poster({ film }: { film: FilmDetail }) {
  const { t } = useTranslation();
  const poster = useQuery({
    queryKey: ["mediaPoster", film.film_id], queryFn: () => api.image(api.posterPath(film.film_id)),
    enabled: film.has_poster, staleTime: Infinity,
  });
  return film.has_poster && poster.data ? (
    <Box component="img" src={poster.data} alt={film.title ?? film.name}
      sx={{ width: 160, maxHeight: 240, objectFit: "cover", borderRadius: 1, flexShrink: 0 }} />
  ) : (
    <Box sx={{ width: 160, height: 220, borderRadius: 1, bgcolor: "action.hover", flexShrink: 0,
      display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Typography variant="caption" color="text.secondary">{t("media.noPoster")}</Typography>
    </Box>
  );
}

function FilmBody({ film }: { film: FilmDetail }) {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const qc = useQueryClient();
  const invalidate = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY }),
      qc.invalidateQueries({ queryKey: mediaFilmKey(film.film_id) }),
    ]);
  const retry = useMutation({
    mutationFn: (stage: string) => api.workflowRetry(film.film_id, stage), onSuccess: invalidate,
  });
  const open = useMutation({ mutationFn: (what: "inbox" | "output") => api.workflowOpen(film.film_id, what) });
  const openError = openErrorText(open.error, t);
  const [confirmed, setConfirmed] = useState(false);
  const marks = (["restored", "chinese_subtitles", "uhd"] as const).filter((m) => film.marks?.[m]);

  return (
    <Stack spacing={2} sx={{ mt: 1 }}>
      <Stack direction="row" spacing={2}>
        <Poster film={film} />
        <Stack spacing={0.5} sx={{ minWidth: 0 }}>
          {film.title && <Typography variant="subtitle1">{film.title}</Typography>}
          <Typography variant="body2">{t("media.number")}：{film.number ?? film.name}</Typography>
          {film.actors.length > 0 && (
            <Typography variant="body2">{t("media.actors")}：{film.actors.join("、")}</Typography>
          )}
          <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap", gap: 0.5 }}>
            {marks.map((m) => <Chip key={m} size="small" color="primary" label={t(`media.marks.${m}`)} />)}
          </Stack>
          {film.note && <Typography variant="body2" color="text.secondary">{film.note}</Typography>}
        </Stack>
      </Stack>

      <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
        {film.can_retry && film.retry_stage && (
          <Button size="small" variant="contained" color="warning" disabled={retry.isPending}
            onClick={() => retry.mutate(film.retry_stage as string)}>
            {t("media.retry", { stage: label.stage(film.retry_stage) })}
          </Button>
        )}
        <Button size="small" variant="outlined" disabled={open.isPending} onClick={() => open.mutate("inbox")}>
          {t("media.openInbox")}
        </Button>
        <Button size="small" variant="outlined" disabled={open.isPending} onClick={() => open.mutate("output")}>
          {t("media.openOutput")}
        </Button>
      </Stack>
      {retry.error && <Alert severity="error">{errorText(retry.error)}</Alert>}
      {openError && <Alert severity="error">{openError}</Alert>}

      {/* the success note lasts only while the film is not confirmable: a failed archive shows the picker again */}
      {confirmed && !film.can_confirm ? <Alert severity="success">{t("media.confirmed")}</Alert>
        : film.can_confirm && (
          <TargetPicker film={film} onConfirmed={() => { setConfirmed(true); return invalidate(); }} />
        )}

      <Divider />
      {(film.steps ?? []).length > 0 ? (
        <Box>
          <Typography variant="overline" color="text.secondary">{t("media.lifecycle")}</Typography>
          <LifecycleStepper steps={lifecycle(film.steps)} />
        </Box>
      ) : (
        <Box>
          <Typography variant="overline" color="text.secondary">{t("media.stages")}</Typography>
          <List dense disablePadding>
            {film.stages.map((s) => (
              <ListItem key={s.stage} disableGutters>
                <ListItemText primary={`${label.stage(s.stage)} · ${label.status(s.status)}`} secondary={s.note || undefined} />
              </ListItem>
            ))}
          </List>
        </Box>
      )}

      <Box>
        <Typography variant="overline" color="text.secondary">{t("media.files")}</Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t("media.role")}</TableCell><TableCell>{t("media.path")}</TableCell>
              <TableCell align="right">{t("media.size")}</TableCell><TableCell>{t("media.volume")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {film.files.map((f) => (
              <TableRow key={`${f.role}:${f.path}`}>
                <TableCell>{f.role}</TableCell>
                <TableCell sx={{ wordBreak: "break-all" }}>{f.path}</TableCell>
                <TableCell align="right">{fmtBytes(f.size)}</TableCell>
                <TableCell>{f.volume ?? ""}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>

      <Box>
        <Typography variant="overline" color="text.secondary">{t("media.inputs")}</Typography>
        <List dense disablePadding>
          {film.inputs.map((i) => (
            <ListItem key={i.path} disableGutters>
              <ListItemText primary={i.path} secondary={fmtBytes(i.size)} sx={{ wordBreak: "break-all" }} />
            </ListItem>
          ))}
        </List>
        <Typography variant="body2" color="text.secondary">{t("media.workcopies", { count: film.workcopy_count })}</Typography>
      </Box>

      <Box>
        <Typography variant="overline" color="text.secondary">{t("media.archiveTitle")}</Typography>
        {film.archive?.status && <Typography variant="body2">{t("media.archiveStatus", { status: label.status(film.archive.status) })}</Typography>}
        {film.archive?.target_path && <Typography variant="body2" sx={{ wordBreak: "break-all" }}>{t("media.target", { path: film.archive.target_path })}</Typography>}
        {film.archive?.error && <Alert severity="error" sx={{ mt: 0.5 }}>{film.archive.error}</Alert>}
        {film.cleanup?.input && <Typography variant="body2">{t("media.cleanupInput", { status: label.status(film.cleanup.input) })}</Typography>}
        {film.cleanup?.workcopy && <Typography variant="body2">{t("media.cleanupWorkcopy", { status: label.status(film.cleanup.workcopy) })}</Typography>}
        {(film.cleanup?.kept ?? []).length > 0 && (
          <>
            <Typography variant="body2" sx={{ mt: 0.5 }}>{t("media.kept")}</Typography>
            <List dense disablePadding>
              {film.cleanup.kept.map((k) => (
                <ListItem key={k.path} disableGutters>
                  <ListItemText primary={k.path} secondary={label.kept(k.status)} sx={{ wordBreak: "break-all" }} />
                </ListItem>
              ))}
            </List>
          </>
        )}
      </Box>
    </Stack>
  );
}

function PerformerAvatar({ performer }: { performer: Performer }) {
  const thumb = useQuery({
    queryKey: ["performerThumb", performer.id], queryFn: () => api.image(api.thumbPath(performer.id)),
    enabled: performer.has_thumb, staleTime: Infinity,
  });
  return <Avatar src={performer.has_thumb ? thumb.data : undefined} alt={performer.name}>{performer.name.slice(0, 1)}</Avatar>;
}

// U3/U4: search Filmpaw performers (prefilled with the first actor), pick one by
// hand — nothing is auto-selected — then a single click confirms the intake.
function TargetPicker({ film, onConfirmed }: { film: FilmDetail; onConfirmed: () => Promise<unknown> }) {
  const { t } = useTranslation();
  const initial = film.actors[0] ?? film.number ?? "";
  const [text, setText] = useState(initial);
  const [query, setQuery] = useState(initial);
  const [selected, setSelected] = useState<Performer | null>(null);
  useEffect(() => {
    const h = setTimeout(() => setQuery(text.trim()), 300);
    return () => clearTimeout(h);
  }, [text]);
  const performers = useQuery({
    queryKey: ["mediaPerformers", query], queryFn: () => api.performers(query),
    enabled: query.trim() !== "", retry: false,
  });
  const filmpawDown = performers.error instanceof HttpError && performers.error.status === 503;
  // The mutation stays pending until the refreshed detail has arrived, then resets: whatever the
  // backend now says (archiving, or already failed and confirmable) decides — never a stale
  // success, however quickly or identically an archive fails (Codex T06 r1/r2).
  const confirm = useMutation({
    mutationFn: (performer: Performer) => api.workflowConfirm(film.film_id, performer.id),
    onSuccess: async () => {
      await onConfirmed();
      confirm.reset();
    },
  });
  const canClick = film.can_confirm && selected !== null && !filmpawDown && !confirm.isPending;
  const items = performers.data?.items ?? [];

  return (
    <Box>
      <Typography variant="overline" color="text.secondary">{t("media.targetTitle")}</Typography>
      <TextField fullWidth size="small" label={t("media.search")} value={text}
        onChange={(e) => setText(e.target.value)} />
      {filmpawDown ? (
        <Alert severity="warning" sx={{ mt: 1 }}>{t("media.filmpawDown")}</Alert>
      ) : performers.error ? (
        <Alert severity="error" sx={{ mt: 1 }}>{t("media.searchFailed", { error: errorText(performers.error) })}</Alert>
      ) : performers.isFetching && items.length === 0 ? (
        <SkeletonRows rows={2} />
      ) : performers.data && items.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{t("media.noResults")}</Typography>
      ) : (
        <List dense sx={{ maxHeight: 280, overflow: "auto" }}>
          {items.map((p) => (
            <ListItemButton key={p.id} selected={selected?.id === p.id} onClick={() => setSelected(p)}
              disabled={confirm.isPending}>
              <ListItemAvatar><PerformerAvatar performer={p} /></ListItemAvatar>
              <ListItemText
                primary={p.source_label ? `${p.name} · ${p.source_label}` : p.name}
                secondary={
                  <>
                    <span style={{ wordBreak: "break-all" }}>{p.unc_path ?? ""}</span>
                    {p.aliases.length > 0 && <><br /><span>{t("media.aliases", { names: p.aliases.join("、") })}</span></>}
                  </>
                }
              />
            </ListItemButton>
          ))}
        </List>
      )}
      {selected && (
        <Typography variant="body2" sx={{ mt: 1, wordBreak: "break-all" }}>
          {t("media.selected", { name: selected.unc_path ? `${selected.name}（${selected.unc_path}）` : selected.name })}
        </Typography>
      )}
      <Button sx={{ mt: 1 }} variant="contained" disabled={!canClick}
        onClick={() => { if (selected && canClick) confirm.mutate(selected); }}>
        {t("media.confirm")}
      </Button>
      {confirm.error && <Alert severity="error" sx={{ mt: 1 }}>{errorText(confirm.error)}</Alert>}
    </Box>
  );
}
