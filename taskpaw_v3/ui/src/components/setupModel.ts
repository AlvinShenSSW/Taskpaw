// #299: the non-component helpers of the setup UI (detection, directory facts, the network
// step, 立即重启) — kept apart from the components for fast refresh.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, controlBootId, HttpError, restartSupported, type ControlRole, type Detection, type DirectoryFacts,
  type FirewallResult, type MikoFound, type ToolFound } from "../api";

export const DETECT_KEY = ["workflowDetect"];

export function useDetect() {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: DETECT_KEY, queryFn: () => api.workflowDetect(false), retry: false, staleTime: 60_000 });
  const refresh = async () => {
    const fresh = await api.workflowDetect(true);
    qc.setQueryData(DETECT_KEY, fresh);
  };
  return { ...query, refresh };
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

const SOURCE: Record<string, string> = {
  saved: "已保存的路径", monitor: "来自监控项", default: "默认安装位置", path: "在 PATH 找到", bundled: "WhisperJAV 自带",
};

export function toolLine(tool: ToolFound): string {
  if (!tool.found) return "未找到";
  const parts = ["已找到"];
  if (tool.source && tool.source !== "saved" && tool.source !== "default") parts.push(SOURCE[tool.source]);
  if (tool.version) parts.push(`版本 ${tool.version}`);
  return parts.join(" · ");
}

export function mikoLine(miko: MikoFound): string {
  if (miko.status === "complete") return "已找到 · 三项齐全";
  if (miko.status === "off") return "未找到";
  const gone = [!miko.root && "引擎", !miko.python && "Python", !miko.profile && "配置"].filter(Boolean);
  return `缺少 ${gone.join("、")}`;
}

/** A directory's facts for a typed absolute path (POST keeps it out of access logs). */
export function useDirFacts(path: string) {
  const text = path.trim();
  const absolute = /^[A-Za-z]:[\\/]/.test(text) || text.startsWith("/");
  return useQuery({
    queryKey: ["dirFacts", text], queryFn: () => api.inspectDirectory(text), enabled: absolute, retry: false, staleTime: 15_000,
  });
}

export function dirLine(facts: DirectoryFacts | undefined, sameDriveAs?: DirectoryFacts): string | null {
  if (!facts) return null;
  if (!facts.local) return "网络位置（不检查）";
  const free = facts.free_bytes != null ? `${formatBytes(facts.free_bytes)} 可用` : null;
  const same = sameDriveAs && sameDriveAs.local && sameDriveAs.volume && sameDriveAs.volume === facts.volume
    ? `与 INBOX 同在 ${facts.volume.replace(/[\\/]$/, "")} 盘` : null;
  return [facts.exists ? "已存在" : "不存在，保存时创建", same ?? free].filter(Boolean).join(" · ");
}

export function foundCount(d: Detection | undefined, names: Array<keyof Detection["tools"]>): string | null {
  if (!d) return null;
  const n = names.filter((k) => (k === "miko" ? d.tools.miko.status === "complete" : (d.tools[k] as ToolFound).found)).length;
  return `${n} / ${names.length} 已找到`;
}

export const NETWORK_KEY = ["network"];
export const RESTART_KEY = ["restartState"];
export const FIREWALL_DECLINED = "未添加防火墙规则，其他设备可能连不上本机；可在 设置 → 网络与访问 重试";
export const MAC_FIREWALL = "首次监听时 macOS 会询问是否允许传入连接，请点“允许”";

export function firewallText(result: FirewallResult): string | null {
  if (result === "added") return "已添加入站规则（仅专用网络）";
  if (result === "unsupported") return null;
  return FIREWALL_DECLINED;
}

export const RESTART_WAIT_MS = 45_000;
export const OUTSIDE = "请退出并重新打开 TaskPaw";

export function useRestart(role: ControlRole) {
  const [state, setState] = useState<"idle" | "sending" | "waiting" | "failed">("idle");
  const [refused, setRefused] = useState<string | null>(null);
  useEffect(() => {
    if (state !== "waiting") return;
    const timer = setTimeout(() => setState("failed"), RESTART_WAIT_MS);
    return () => clearTimeout(timer);
  }, [state]);
  const supported = restartSupported(role);
  const start = async () => {
    const boot = controlBootId(role);
    if (!supported || !boot) { setRefused(OUTSIDE); return; }
    setRefused(null); setState("sending");
    try {
      await api.restart(role, boot);
      setState("waiting");
    } catch (e) {
      setState("idle");
      setRefused(e instanceof HttpError ? e.message : e instanceof Error ? e.message : String(e));
    }
  };
  return { supported, state, refused, start };
}

const RESTART_LABELS: Record<string, string> = {
  machine: "机器名称", lan_access: "局域网访问", bind_host: "本机地址", bind_port: "局域网端口",
  control_host: "本机控制地址", control_port: "本机控制端口", host_metrics: "主机监控",
};
export function restartText(fields: string[], app = "TaskPaw"): string {
  const names = [...new Set(fields.map((f) => (f.startsWith("workflow.") ? "媒体工作流" : RESTART_LABELS[f] ?? f)))];
  return `已保存，重启 ${app} 后生效${names.length ? `（${names.join("、")}）` : ""}`;
}
