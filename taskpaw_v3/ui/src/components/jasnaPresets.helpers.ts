type Values = Record<string, unknown>;
export type JasnaProfile = { model: string; gpu: string; standard: Values; vr: Values };
export type JasnaPresets = { version: 1; gpus: { id: string; label: string }[]; profiles: JasnaProfile[] };

export function readJasnaPresets(ui: Values | undefined): JasnaPresets | undefined {
  const options = ui?.["ui:options"] as { jasnaPresets?: JasnaPresets } | undefined;
  const c = options?.jasnaPresets;
  return c?.version === 1 && Array.isArray(c.gpus) && Array.isArray(c.profiles) ? c : undefined;
}

export function applyJasnaPreset(c: JasnaPresets, model: string, gpu: string, data: Values, vr = data.vr_8k === true): Values | undefined {
  const profile = c.profiles.find(p => p.model === model && p.gpu === gpu);
  return profile ? { ...data, ...(vr ? profile.vr : profile.standard) } : undefined;
}
