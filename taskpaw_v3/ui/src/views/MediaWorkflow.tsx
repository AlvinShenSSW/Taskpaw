import {
  Alert, Box, Button, Card, CardContent, Chip, List, ListItem, ListItemButton, ListItemText, Stack, Typography,
} from "@mui/material";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type PackageRow, type WorkflowCurrent, type WorkflowIntake } from "../api";
import { FilmDrawer } from "../components/FilmDrawer";
import { StepLine } from "../components/LifecycleSteps";
import { SkeletonRows } from "../components/SkeletonRows";
import {
  lifecycle, MEDIA_FILMS_KEY, MEDIA_GROUPS, openErrorText, packageSteps, rowNote, useMediaLabels,
} from "../components/mediaWorkflow.helpers";

// 「媒体处理中心」 tab (T06-P1 U1, H2): the current films and the INBOX packages that are not films
// yet, grouped by what they wait for, polled every 5 s like the rest of the console. A film row
// opens the detail drawer; a package row only opens its folder (R2).
export function MediaWorkflow() {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const films = useQuery({ queryKey: MEDIA_FILMS_KEY, queryFn: api.workflowFilms, refetchInterval: 5000 });
  const [open, setOpen] = useState<string | null>(null);

  if (films.isLoading) return <Card><CardContent><SkeletonRows rows={4} /></CardContent></Card>;
  if (films.error || !films.data) return <Alert severity="error">{t("media.failed")}</Alert>;
  const { state, reason, current, intake } = films.data;
  if (state !== "running") {
    return <Alert severity="info">{t("media.unavailable", { reason: reason || state })}</Alert>;
  }
  const rows = films.data.films ?? [];
  const packages = films.data.packages ?? [];
  const queued = (c: WorkflowCurrent) => t("media.queued", { film: c.film, stage: label.stage(c.stage) });

  return (
    <Stack spacing={1.5}>
      {rows.length === 0 && packages.length === 0 && (
        <Card><CardContent><Typography color="text.secondary">{t("media.empty")}</Typography></CardContent></Card>
      )}
      {MEDIA_GROUPS.map((group) => {
        const members = rows.filter((r) => r.group === group);
        const pkgs = packages.filter((p) => p.group === group);
        if (members.length === 0 && pkgs.length === 0) return null;
        return (
          <Card key={group} component="section" aria-label={t(`media.groups.${group}`)}>
            <CardContent>
              <Stack direction="row" spacing={1} alignItems="center">
                <Typography variant="overline" color="text.secondary">{t(`media.groups.${group}`)}</Typography>
                <Chip size="small" label={members.length + pkgs.length} />
              </Stack>
              <List dense disablePadding>
                {members.map((row) => {
                  const note = rowNote(row, current, queued);
                  return (
                    <ListItemButton key={row.film_id} onClick={() => setOpen(row.film_id)}
                      sx={{ flexDirection: "column", alignItems: "stretch" }}>
                      <ListItemText
                        primary={row.name}
                        secondary={
                          <>
                            <span>{`${label.stage(row.stage)} · ${label.status(row.status)}`}</span>
                            {note && <><br /><span>{note}</span></>}
                          </>
                        }
                      />
                      {(row.steps ?? []).length > 0 && <StepLine steps={lifecycle(row.steps)} />}
                    </ListItemButton>
                  );
                })}
                {pkgs.map((pkg) => <PackageItem key={pkg.package_id} pkg={pkg} intake={intake} />)}
              </List>
            </CardContent>
          </Card>
        );
      })}
      <FilmDrawer filmId={open} onClose={() => setOpen(null)} />
    </Stack>
  );
}

// R2/R2a: an INBOX package that is not a film yet — its folder, readable state and offending
// files, the step line at 入库扫描, and 「打开文件夹」. It opens no drawer.
function PackageItem({ pkg, intake }: { pkg: PackageRow; intake: WorkflowIntake | null | undefined }) {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const open = useMutation({ mutationFn: () => api.workflowOpenPackage(pkg.package_id) });
  const openError = openErrorText(open.error, t);
  const problems = label.packageProblems(pkg);
  return (
    <ListItem sx={{ flexDirection: "column", alignItems: "stretch" }} data-package={pkg.package_id}>
      <Stack direction="row" spacing={1} alignItems="flex-start">
        <ListItemText
          sx={{ minWidth: 0, wordBreak: "break-all" }}
          primary={pkg.name}
          secondary={
            <>
              <span>{label.packageStatus(pkg)}</span>
              {problems.map((p) => <span key={p}><br />{p}</span>)}
            </>
          }
        />
        <Box sx={{ flexShrink: 0, pt: 0.5 }}>
          <Button size="small" variant="outlined" disabled={open.isPending} onClick={() => open.mutate()}>
            {t("media.openFolder")}
          </Button>
        </Box>
      </Stack>
      <StepLine steps={packageSteps(pkg, intake)} />
      {openError && <Alert severity="error" sx={{ mt: 0.5 }}>{openError}</Alert>}
    </ListItem>
  );
}
