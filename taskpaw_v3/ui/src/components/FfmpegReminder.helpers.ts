import type { FfmpegStatus } from "../api";

// Frozen AC5 precedence. Path validity belongs to the agent's exe_ok verdict.
export function ffmpegState(status: FfmpegStatus) {
  if (status.error) return "error";
  if (status.effective && status.effective === status.on_path) return "path";
  if (status.effective && status.effective === status.bundled) return "bundled";
  if (status.pending_restart) return "restart";
  if (status.platform === "windows") {
    if (!status.exe_ok && status.on_path === null) return "neutral";
    return "windows";
  }
  return "other";
}
