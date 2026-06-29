import { Card, CardContent, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { LANGS, type Lang, currentLang, setLang } from "../i18n";

// Settings tab (#79): app/UX settings. First two sections — Language and About.
// Config editing (machine/ports/token/OpenClaw) is a separate section (#43) that
// can join this tab later.
export function Settings() {
  const { t } = useTranslation();
  return (
    <Stack spacing={2} sx={{ maxWidth: 640 }}>
      <Typography variant="overline" color="text.secondary">{t("settings.title")}</Typography>

      {/* Language */}
      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.language")}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t("settings.languageHint")}
          </Typography>
          <TextField select size="small" value={currentLang()} sx={{ minWidth: 200 }}
            onChange={(e) => setLang(e.target.value as Lang)}>
            {LANGS.map((l) => (
              <MenuItem key={l.value} value={l.value}>{l.label}</MenuItem>
            ))}
          </TextField>
        </CardContent>
      </Card>

      {/* About */}
      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ mb: 1 }}>{t("settings.about")}</Typography>
          <Stack direction="row" alignItems="baseline" spacing={1} sx={{ mb: 1 }}>
            <Typography variant="h6">🐾 TaskPaw</Typography>
            <Typography variant="caption" color="text.secondary">v3.0.0-dev</Typography>
          </Stack>
          <Typography variant="body2" sx={{ mb: 1.5 }}>{t("settings.aboutBody")}</Typography>
          <Typography variant="body2" color="text.secondary">{t("settings.author")}</Typography>
          <Typography variant="caption" color="text.secondary">{t("settings.copyright")}</Typography>
        </CardContent>
      </Card>
    </Stack>
  );
}
