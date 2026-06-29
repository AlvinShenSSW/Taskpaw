import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, Stack, Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import CheckIcon from "@mui/icons-material/Check";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type PluginInfo, type PresetInfo } from "../api";
import { SchemaForm } from "../components/SchemaForm";
import { ServiceIcon } from "../components/ServiceIcon";

// A selectable service is either a plugin type or a preset bundle (e.g. moomoo).
type Service =
  | { kind: "plugin"; id: string; plugin: PluginInfo }
  | { kind: "preset"; id: string; preset: PresetInfo };

type Mode = "add" | "edit";

// Step-by-step "Add monitor" wizard (#93), replacing the old one-dialog
// select-then-form. Adding a service (e.g. Lada) is guided: choose → configure →
// review. A preset (moomoo) creates several monitors at once. Edit mode jumps
// straight to the config step with the type locked.
export function MonitorWizard({
  mode, name, existingConfig, existingType, plugins, presets, onClose, onDone, onError,
}: {
  mode: Mode;
  name?: string;
  existingConfig?: Record<string, unknown>;
  existingType?: string;
  plugins: PluginInfo[];
  presets: PresetInfo[];
  onClose: () => void;
  onDone: (savedName?: string) => void;
  onError: (e: unknown) => void;
}) {
  const { t } = useTranslation();

  const services: Service[] = useMemo(() => [
    ...plugins.filter((p) => !p.system).map((p) => ({ kind: "plugin" as const, id: p.type_id, plugin: p })),
    ...presets.map((p) => ({ kind: "preset" as const, id: `preset:${p.id}`, preset: p })),
  ], [plugins, presets]);

  // Edit mode starts on the config step with the existing type pre-selected.
  const editService = mode === "edit"
    ? services.find((s) => s.kind === "plugin" && s.plugin.type_id === existingType)
    : undefined;
  const [step, setStep] = useState<1 | 2 | 3>(mode === "edit" ? 2 : 1);
  const [selectedId, setSelectedId] = useState<string | null>(editService?.id ?? null);
  const [formData, setFormData] = useState<Record<string, unknown>>(existingConfig ?? {});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);

  // Edit can open before /control/plugins resolves (services empty → editService
  // undefined → selectedId stays null, leaving step 2 blank). Sync the selection
  // once the catalog arrives (Codex). The useState initializer alone can't.
  useEffect(() => {
    if (mode === "edit" && selectedId === null && editService) setSelectedId(editService.id);
  }, [mode, selectedId, editService]);

  const selected = services.find((s) => s.id === selectedId) ?? null;
  const serviceName = (s: Service) =>
    s.kind === "plugin" ? (s.plugin.display_name || s.plugin.type_id) : s.preset.display_name;
  const serviceDesc = (s: Service) =>
    s.kind === "preset"
      ? (s.preset.description ?? "")
      : t(`services.${s.plugin.type_id}`, { defaultValue: s.plugin.category ?? "" });

  // Adding from scratch is "dirty" once a service is picked → confirm before
  // discarding. Edit mode (already-saved) closes without a prompt.
  const isDirty = mode === "add" && selectedId !== null;
  const requestClose = () => (isDirty ? setConfirmClose(true) : onClose());

  // Secret keys (masked in review) — from the plugin's password widgets, plus a
  // name-based fallback for token/secret fields.
  const secretKeys = useMemo(() => {
    const keys = new Set<string>();
    if (selected?.kind === "plugin") {
      const ui = selected.plugin.ui_schema as Record<string, any>;
      for (const [k, v] of Object.entries(ui ?? {})) {
        if (v?.["ui:widget"] === "password") keys.add(k);
      }
    }
    return keys;
  }, [selected]);
  const mask = (k: string, v: unknown) =>
    secretKeys.has(k) || /token|password|secret/i.test(k) ? "••••••••" : String(v);

  const formUiSchema = useMemo(() => {
    if (selected?.kind !== "plugin") return {};
    const base = (selected.plugin.ui_schema as Record<string, unknown>) ?? {};
    return {
      ...base,
      "ui:submitButtonOptions": {
        submitText: mode === "edit" ? t("wizard.saveBtn") : t("wizard.review"),
      },
      ...(mode === "edit" ? { name: { ...(base.name as object), "ui:readonly": true } } : {}),
    };
  }, [selected, mode, t]);

  // ── submit ──────────────────────────────────────────────────────────────
  const finish = async (data: Record<string, unknown>) => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === "edit") {
        await api.updateMonitor(name as string, { config: data });
        onDone(name);
      } else if (selected.kind === "plugin") {
        await api.addMonitor({ type_id: selected.plugin.type_id, config: data });
        onDone(String(data.name ?? "") || undefined);
      } else {
        // Preset: create every bundled monitor; surface the first failure but
        // keep what succeeded (the operator can retry — already-added names error
        // as duplicates, which is safe).
        let firstName: string | undefined;
        for (const m of selected.preset.monitors) {
          await api.addMonitor({ type_id: m.type_id, config: m.config });
          firstName ??= m.name;
        }
        onDone(firstName);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  // Plugin config form submit: edit saves immediately; add captures + advances.
  const onFormSubmit = (data: unknown) => {
    const d = data as Record<string, unknown>;
    setFormData(d);
    if (mode === "edit") finish(d);
    else setStep(3);
  };

  const title = mode === "edit"
    ? t("agent.editMonitor", { name })
    : selected ? `${t(`wizard.s${step}`)} · ${serviceName(selected)}` : t("wizard.add");

  return (
    <Dialog open onClose={requestClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Box sx={{ flex: 1 }}>{title}</Box>
        <IconButton aria-label="close" size="small" onClick={requestClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>

      {/* Step indicator (hidden in edit mode — single step). */}
      {mode === "add" && <WizardSteps step={step} />}

      <DialogContent dividers>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {step === 1 && (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {t("wizard.s1desc")}
            </Typography>
            {services.length === 0 ? (
              <Typography color="text.secondary">{t("agent.noSelectableTypes")}</Typography>
            ) : (
              <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" }, gap: 1.5 }}>
                {services.map((s) => {
                  const sel = s.id === selectedId;
                  return (
                    <Box
                      key={s.id}
                      component="button"
                      type="button"
                      aria-pressed={sel}
                      onClick={() => setSelectedId(s.id)}
                      sx={{
                        textAlign: "left",
                        cursor: "pointer",
                        font: "inherit",
                        color: "text.primary",
                        bgcolor: "background.paper",
                        border: "1px solid",
                        borderColor: sel ? "primary.main" : "divider",
                        boxShadow: sel ? "0 0 0 1px rgba(34,197,94,.35), 0 0 18px -4px rgba(34,197,94,.5)" : "none",
                        borderRadius: "12px",
                        p: 1.75,
                        transition: "0.16s",
                        "&:hover": { borderColor: "rgba(34,197,94,.5)" },
                      }}
                    >
                      <ServiceIcon id={s.kind === "preset" ? s.preset.id : s.plugin.type_id} />
                      <Typography sx={{ fontWeight: 600, fontSize: 14, mt: 1.25 }}>{serviceName(s)}</Typography>
                      <Typography sx={{ fontSize: 11.5, color: "text.secondary", mt: 0.4, lineHeight: 1.5 }}>
                        {serviceDesc(s)}
                      </Typography>
                    </Box>
                  );
                })}
              </Box>
            )}
          </>
        )}

        {step === 2 && selected?.kind === "plugin" && (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {t("wizard.adapt", { name: serviceName(selected) })}
            </Typography>
            <SchemaForm
              schema={selected.plugin.json_schema}
              uiSchema={formUiSchema}
              formData={mode === "edit" ? existingConfig : formData}
              onSubmit={onFormSubmit}
            />
          </>
        )}

        {step === 2 && selected?.kind === "preset" && (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              {t("wizard.presetCreates", { count: selected.preset.monitors.length })}
            </Typography>
            <Stack spacing={1}>
              {selected.preset.monitors.map((m) => (
                <Box key={m.name} sx={{ display: "flex", alignItems: "center", gap: 1.5,
                  border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1 }}>
                  <ServiceIcon id={m.type_id} />
                  <Box>
                    <Typography sx={{ fontWeight: 600, fontSize: 14 }}>{m.name}</Typography>
                    <Typography variant="body2" color="text.secondary">{m.type_id}</Typography>
                  </Box>
                </Box>
              ))}
            </Stack>
          </>
        )}

        {step === 3 && selected && (
          <>
            <Box sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, overflow: "hidden" }}>
              <ReviewRow k={t("wizard.svctype")} v={selected.kind === "preset" ? selected.preset.id : selected.plugin.type_id} />
              {selected.kind === "plugin"
                ? Object.entries(formData)
                    .filter(([, v]) => typeof v !== "boolean")
                    .map(([k, v]) => <ReviewRow key={k} k={k} v={mask(k, v)} />)
                : selected.preset.monitors.map((m) => <ReviewRow key={m.name} k={m.name} v={m.type_id} />)}
            </Box>
            <Typography variant="caption" color="text.secondary" sx={{ mt: 2, display: "block" }}>
              {t("wizard.recap")}
            </Typography>
          </>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 1.5 }}>
        {step > 1 && mode === "add" && (
          <Button color="inherit" onClick={() => setStep((step - 1) as 1 | 2 | 3)} disabled={busy}>
            {t("wizard.back")}
          </Button>
        )}
        <Box sx={{ flex: 1 }} />
        <Button color="inherit" onClick={requestClose} disabled={busy}>{t("common.cancel")}</Button>
        {/* Step 1 → continue; preset step 2 → review; review → add. The plugin
            config form (step 2) submits via its own in-form button. */}
        {step === 1 && (
          <Button variant="contained" disabled={!selected} onClick={() => setStep(2)}>
            {t("wizard.continue")}
          </Button>
        )}
        {step === 2 && selected?.kind === "preset" && (
          <Button variant="contained" onClick={() => setStep(3)}>{t("wizard.review")}</Button>
        )}
        {step === 3 && (
          <Button variant="contained" startIcon={<CheckIcon />} disabled={busy}
            onClick={() => finish(formData)}>
            {t("wizard.addBtn")}
          </Button>
        )}
      </DialogActions>

      {/* Discard confirmation when closing a partly-filled add flow. */}
      <Dialog open={confirmClose} onClose={() => setConfirmClose(false)}>
        <DialogTitle>{t("wizard.closeTitle")}</DialogTitle>
        <DialogContent><Typography>{t("wizard.closeBody")}</Typography></DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmClose(false)}>{t("common.cancel")}</Button>
          <Button color="error" onClick={() => { setConfirmClose(false); onClose(); }}>
            {t("wizard.discard")}
          </Button>
        </DialogActions>
      </Dialog>
    </Dialog>
  );
}

function WizardSteps({ step }: { step: 1 | 2 | 3 }) {
  const { t } = useTranslation();
  const labels = [t("wizard.s1"), t("wizard.s2"), t("wizard.s3")];
  return (
    <Stack direction="row" alignItems="center" spacing={1.25} sx={{ px: 3, pb: 1.5 }}>
      {labels.map((label, i) => {
        const n = (i + 1) as 1 | 2 | 3;
        const active = n === step;
        const done = n < step;
        return (
          <Stack key={label} direction="row" alignItems="center" spacing={1.25} sx={{ flex: i < 2 ? 1 : "0 0 auto" }}>
            <Stack direction="row" alignItems="center" spacing={1}>
              <Box
                sx={{
                  width: 24, height: 24, borderRadius: "50%", display: "flex",
                  alignItems: "center", justifyContent: "center", fontSize: 12,
                  fontFamily: '"Fira Code",ui-monospace,monospace',
                  border: "1.5px solid",
                  borderColor: active || done ? "primary.main" : "divider",
                  bgcolor: done ? "primary.main" : "transparent",
                  color: done ? "primary.contrastText" : active ? "primary.main" : "text.secondary",
                  boxShadow: active ? "0 0 12px -2px rgba(34,197,94,.7)" : "none",
                }}
              >
                {done ? <CheckIcon sx={{ fontSize: 15 }} /> : n}
              </Box>
              <Typography variant="body2" sx={{ fontWeight: 600, color: active ? "text.primary" : "text.secondary" }}>
                {label}
              </Typography>
            </Stack>
            {i < 2 && <Box sx={{ flex: 1, height: "1px", bgcolor: "divider", maxWidth: 48 }} />}
          </Stack>
        );
      })}
    </Stack>
  );
}

function ReviewRow({ k, v }: { k: string; v: string }) {
  return (
    <Box sx={{ display: "flex", justifyContent: "space-between", gap: 2, px: 1.5, py: 1,
      borderBottom: "1px solid", borderColor: "divider", "&:last-of-type": { borderBottom: 0 } }}>
      <Typography variant="body2" color="text.secondary">{k}</Typography>
      <Typography variant="body2" sx={{ textAlign: "right", wordBreak: "break-all" }}>{v}</Typography>
    </Box>
  );
}
