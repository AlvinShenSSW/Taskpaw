import { useState } from "react";
import type { WorkflowStep } from "../../api";
import { archiveLive } from "./mediaModel";

// Samples come from board polls. Elapsed time alone never invents transfer speed.
export function useArchiveProgress(step: WorkflowStep | undefined) {
  const live = archiveLive(step);
  const bytes = live ? live.copied_bytes + live.verified_bytes : 0;
  const [sample, setSample] = useState(() => ({ bytes, at: Date.now(), start: live?.started_at, speed: 0 }));
  let current = sample;
  if (bytes !== sample.bytes || live?.started_at !== sample.start) {
    const at = Date.now();
    const speed = live?.started_at === sample.start && bytes > sample.bytes && at > sample.at
      ? (bytes - sample.bytes) * 1000 / (at - sample.at) : 0;
    current = { bytes, at, start: live?.started_at, speed };
    setSample(current);
  }
  return { live, speed: current.speed,
    eta: live && current.speed > 0 ? Math.ceil(Math.max(0, 2 * live.total_bytes - bytes) / current.speed) : null };
}
