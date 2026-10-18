import { Alert, Box, Button, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import type { JasnaPresets as Catalogue } from "./jasnaPresets.helpers";

export function JasnaPresets({ catalogue, model, gpu, disabled, onSelect, onApply }: {
  catalogue: Catalogue; model: string; gpu: string; disabled?: boolean;
  onSelect: (model: string, gpu: string) => void; onApply: () => void;
}) {
  const { t } = useTranslation();
  return <Box sx={{ border: "1px solid", borderColor: "divider", borderRadius: 2, p: 2, mb: 2 }}>
    <Typography fontWeight={600} sx={{ mb: 1.5 }}>{t("jasna.presets")}</Typography>
    <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
      <TextField select fullWidth size="small" label={t("jasna.model")} value={model} disabled={disabled}
        onChange={e => onSelect(e.target.value, gpu)}>
        {["plain", "unet", "ltx"].map(m => <MenuItem key={m} value={m}>{t(`jasna.${m}`)}</MenuItem>)}
      </TextField>
      <TextField select fullWidth size="small" label={t("jasna.gpu")} value={gpu} disabled={disabled}
        onChange={e => onSelect(model, e.target.value)}>
        {catalogue.gpus.map(g => <MenuItem key={g.id} value={g.id}>{g.label}</MenuItem>)}
      </TextField>
    </Stack>
    <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>{t("jasna.presetHelp")}</Typography>
    {model === "ltx" && <Alert severity={gpu === "5060-8" ? "warning" : "info"} sx={{ mt: 1 }}>
      {t(gpu === "5060-8" ? "jasna.smallGpu" : "jasna.canvasHelp")}
    </Alert>}
    <Button size="small" disabled={disabled || !model || !gpu} onClick={onApply} sx={{ mt: 1 }}>{t("jasna.reapply")}</Button>
  </Box>;
}
