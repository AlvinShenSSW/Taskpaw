import {
  Alert, Card, CardContent, Chip, List, ListItemButton, ListItemText, Stack, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type WorkflowCurrent } from "../api";
import { FilmDrawer } from "../components/FilmDrawer";
import { SkeletonRows } from "../components/SkeletonRows";
import { MEDIA_FILMS_KEY, MEDIA_GROUPS, rowNote, useMediaLabels } from "../components/mediaWorkflow.helpers";

// 「媒体入库」 tab (T06-P1 U1): the current films grouped by what they wait for,
// polled every 5 s like the rest of the console; a row opens the detail drawer.
export function MediaWorkflow() {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const films = useQuery({ queryKey: MEDIA_FILMS_KEY, queryFn: api.workflowFilms, refetchInterval: 5000 });
  const [open, setOpen] = useState<string | null>(null);

  if (films.isLoading) return <Card><CardContent><SkeletonRows rows={4} /></CardContent></Card>;
  if (films.error || !films.data) return <Alert severity="error">{t("media.failed")}</Alert>;
  const { state, reason, current } = films.data;
  if (state !== "running") {
    return <Alert severity="info">{t("media.unavailable", { reason: reason ?? state })}</Alert>;
  }
  const rows = films.data.films ?? [];
  const queued = (c: WorkflowCurrent) => t("media.queued", { film: c.film, stage: label.stage(c.stage) });

  return (
    <Stack spacing={1.5}>
      {rows.length === 0 && (
        <Card><CardContent><Typography color="text.secondary">{t("media.empty")}</Typography></CardContent></Card>
      )}
      {MEDIA_GROUPS.map((group) => {
        const members = rows.filter((r) => r.group === group);
        if (members.length === 0) return null;
        return (
          <Card key={group} component="section" aria-label={t(`media.groups.${group}`)}>
            <CardContent>
              <Stack direction="row" spacing={1} alignItems="center">
                <Typography variant="overline" color="text.secondary">{t(`media.groups.${group}`)}</Typography>
                <Chip size="small" label={members.length} />
              </Stack>
              <List dense disablePadding>
                {members.map((row) => {
                  const note = rowNote(row, current, queued);
                  return (
                    <ListItemButton key={row.film_id} onClick={() => setOpen(row.film_id)}>
                      <ListItemText
                        primary={row.name}
                        secondary={
                          <>
                            <span>{`${label.stage(row.stage)} · ${label.status(row.status)}`}</span>
                            {note && <><br /><span>{note}</span></>}
                          </>
                        }
                      />
                    </ListItemButton>
                  );
                })}
              </List>
            </CardContent>
          </Card>
        );
      })}
      <FilmDrawer filmId={open} onClose={() => setOpen(null)} />
    </Stack>
  );
}
