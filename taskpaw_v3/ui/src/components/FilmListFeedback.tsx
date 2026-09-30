import { Box, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { FilmList } from "./FilmList";
import type { FilmFallback } from "./pipelineProgress.helpers";
import type { FilmNoteKey } from "./filmSource.helpers";

export function FilmListFeedback({ fallback, noteKeys, compact = true }: {
  fallback?: FilmFallback; noteKeys: readonly FilmNoteKey[]; compact?: boolean;
}) {
  const { t } = useTranslation();
  return <Box sx={{ minWidth: 0 }}>
    <Box role="status">
      {noteKeys.map(key => <Typography key={key} variant="body2" color="text.secondary">{t(key)}</Typography>)}
      {compact && !fallback?.films.length && <Typography variant="body2" color="text.secondary">{t("hub.films.noSnapshot")}</Typography>}
    </Box>
    {compact && !!fallback?.films.length && <FilmList films={fallback.films}
      filmsMore={fallback.filmsMore} focus={fallback.film} showSingle />}
  </Box>;
}
