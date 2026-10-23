import type { GpuFound, SetupRole, SetupStep, WorkflowRole } from "../../api";

// #299 §6: the wizard's paths, titles and shared step props.
export const SETUP_KEY = ["setupState"];

export interface StepProps {
  role: SetupRole;
  /** Record `next` for `as` (PUT /control/setup), then show it. */
  advance: (next: SetupStep, as: SetupRole) => Promise<void>;
  /** Show a step without writing (上一步 / 修改). */
  go: (step: SetupStep) => void;
}

export const ROLE_NAME: Record<SetupRole, string> = { agent: "普通 Agent（仅监控）", controller: "媒体主控", worker: "GPU 工作机" };
export const WORKFLOW_ROLE: Record<SetupRole, WorkflowRole> = { agent: "off", controller: "controller", worker: "worker" };

/** The stepper: [steps it covers, label]; 普通 Agent has three, the others four. */
export function stepsOf(role: SetupRole | null): Array<[SetupStep[], string]> {
  if (role === "controller") {
    return [[["role"], "选择角色"], [["network"], "网络与访问"], [["controller"], "主控设置"], [["done"], "完成并重启"]];
  }
  if (role === "worker") {
    return [[["role"], "选择角色"], [["network"], "网络与访问"], [["worker_fields", "worker_join"], "工作机设置"], [["done"], "完成"]];
  }
  return [[["role"], "选择角色"], [["network"], "网络与访问"], [["done"], "完成"]];
}

/** The step after the network step for a role. */
export function afterNetwork(role: SetupRole): SetupStep {
  return role === "controller" ? "controller" : role === "worker" ? "worker_fields" : "done";
}

export function titleOf(step: SetupStep, role: SetupRole | null): string {
  if (step === "role") return "首次启动 · 选择角色";
  if (step === "network") return "网络与访问";
  if (step === "controller") return "主控设置";
  if (step === "worker_fields" || step === "worker_join") return "GPU 工作机设置";
  return role === "controller" ? "完成并重启" : "完成";
}

/** 「RTX 5070 Ti 16 GB」 from nvidia-smi's name and MiB. */
export function gpuShort(gpu: GpuFound): string {
  const name = gpu.name.replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
  return `${name} ${Math.round(gpu.vram_mib / 1024)} GB`;
}

export const looksAbsolute = (path: string) => /^[A-Za-z]:[\\/]/.test(path.trim()) || path.trim().startsWith("/");

// R9: the local check before any request (the code itself is never shown back)
export const MALFORMED = "配对码格式不对，应以 TP1: 开头";
