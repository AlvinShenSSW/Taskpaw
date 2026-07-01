import type { RJSFSchema } from "@rjsf/utils";

// Chinese labels + help for the backend plugin config schemas (#121). The plugin
// json_schema carries English `title`/`description` (Pydantic field metadata); when
// the UI language is Chinese we overlay these translations so a zh user sees a fully
// Chinese config form. Any field without a translation falls back to the schema's
// own English (so a new backend field is never blank). Keyed by type_id so
// same-named fields that mean different things per plugin (e.g. `host`/`port` in
// tcp_check vs comfyui, `path` in folder vs state_file) translate correctly.

type FieldT = { title: string; description?: string };

// Shared base config fields (BaseMonitorConfig) — present on every plugin form.
const BASE: Record<string, FieldT> = {
  name: { title: "名称", description: "该监控在本机上的唯一名称。" },
  poll_interval: { title: "轮询间隔", description: "检查频率（秒，最小 1）。" },
  timeout: { title: "超时", description: "每次检查的命令/HTTP 超时（秒）。" },
  max_events_per_minute: { title: "每分钟最大事件数" },
  max_line_bytes: { title: "单行最大字节" },
};

// Per-plugin field translations (type_id → field → zh title/description).
const BY_TYPE: Record<string, Record<string, FieldT>> = {
  process: {
    pattern: {
      title: "匹配模式",
      description:
        "用于匹配进程名（若下方启用则含命令行）的正则，例如 ^lada-cli$ 或 PM2.*God",
    },
    search_cmdline: { title: "匹配命令行", description: "同时匹配完整命令行，而不仅是进程名。" },
    category_label: { title: "类别标签", description: "该监控显示的标签（如 service、task）。" },
  },
  heartbeat: {
    path: { title: "状态文件路径" },
    status_field: { title: "状态字段" },
    due_field: { title: "到期字段" },
    grace_seconds: { title: "宽限秒数" },
    hibernating_states: { title: "休眠状态" },
  },
  tcp_check: {
    host: { title: "主机" },
    port: { title: "端口" },
  },
  host_metrics: {
    cpu_alert_pct: { title: "CPU 告警阈值(%)" },
    mem_alert_pct: { title: "内存告警阈值(%)" },
    disk_alert_pct: { title: "磁盘告警阈值(%)" },
    disk_path: { title: "磁盘路径" },
    cpu_sustained_cycles: { title: "CPU 持续周期" },
    collect_gpu: { title: "采集 GPU" },
  },
  folder: {
    path: { title: "路径", description: "要监视新文件的文件夹（如下载目录）。" },
    extensions: {
      title: "扩展名",
      description: '只监视这些扩展名，例如 ["mp4","mkv"]；留空 = 所有文件。',
    },
    stable_seconds: {
      title: "稳定秒数",
      description: "文件大小在这么多秒内没有变化即视为完成。",
    },
  },
  comfyui: {
    host: { title: "主机", description: "ComfyUI 主机/IP（运行 ComfyUI 的机器）。" },
    port: { title: "端口", description: "ComfyUI 端口。" },
    idle_confirm: {
      title: "空闲确认次数",
      description: "队列连续这么多次检查为空后再通知“完成”（消抖，避开两次任务之间的空档）。",
    },
    stall_confirm: {
      title: "停滞确认次数",
      description: "没有任务运行但仍有排队 prompt 持续这么多次检查时告警（prompt 出错卡住）。",
    },
    stuck_checks: {
      title: "卡住检查次数",
      description: "同一个 prompt 连续这么多次检查仍未完成时告警（0 = 关闭）。",
    },
    comfyui_log_path: {
      title: "ComfyUI 日志路径",
      description:
        "可选：停滞/卡住时要 tail 的 ComfyUI 日志文件，用于抓取真实错误（CUDA OOM / RuntimeError / Traceback）。",
    },
  },
  custom_cmd: {
    command: {
      title: "命令",
      description: "每个周期运行的命令；退出码 0 = 正常/空闲，非 0 = 忙碌/失败。",
    },
  },
  state_file: {
    path: { title: "状态文件路径" },
    state_field: { title: "状态字段" },
    ts_field: { title: "时间戳字段" },
    busy_states: { title: "忙碌状态" },
    waiting_states: { title: "等待状态" },
    idle_states: { title: "空闲状态" },
    busy_alert_seconds: { title: "忙碌告警秒数" },
    stale_seconds: { title: "过期秒数" },
    missing_is_idle: { title: "文件缺失视为空闲" },
  },
  lada: {
    lada_cli_path: {
      title: "lada-cli 路径",
      description:
        "lada-cli 可执行文件的完整路径（如 C:\\Lada\\lada-cli.exe）——不是文件夹。填写 → 托管模式（TaskPaw 启动 lada-cli，需要下方的输入/输出文件夹）。留空 → 被动模式（仅监视已在运行的 lada-cli）。",
    },
    process_name: {
      title: "进程名",
      description: "仅被动模式：要检测的进程。带不带结尾的 '.exe' 都能匹配（Windows：lada-cli.exe）。",
    },
    lada_input_folder: {
      title: "输入文件夹",
      description: "要处理的视频所在文件夹（lada-cli --input）。托管模式必填。",
    },
    lada_output_folder: {
      title: "输出文件夹",
      description: "lada-cli 写结果的文件夹（--output）。托管模式必填；也用于统计队列数量与完成通知。",
    },
    lada_extra_args: {
      title: "额外参数",
      description: "原样传给 lada-cli 的额外参数，例如 --device cuda:1 --encoder h264_nvenc",
    },
    lada_gpu_monitor: {
      title: "GPU 监控",
      description: "通过 nvidia-smi 报告 GPU%/显存（没有 NVIDIA GPU 的机器请关闭）。",
    },
    lada_capture_progress: {
      title: "捕获进度",
      description:
        "高级。关（默认）：lada-cli 自己开一个控制台窗口显示进度条。开：把 lada 的输出捕获进 TaskPaw（不另开窗口），在状态面板显示 文件/%/fps/ETA。",
    },
  },
};

function zhField(typeId: string | undefined, field: string): FieldT | undefined {
  return (typeId ? BY_TYPE[typeId]?.[field] : undefined) ?? BASE[field];
}

// A display label for a config field key (used by the wizard review step, #121):
// the zh title when the UI is Chinese and we have a translation, else the raw key.
export function fieldLabel(field: string, typeId?: string, lang?: string): string {
  const zh = zhField(typeId, field);
  return lang && lang.startsWith("zh") && zh ? zh.title : field;
}

// Overlay Chinese title/description onto a plugin json_schema's properties when the
// UI language is Chinese. Returns a NEW schema (never mutates the input); leaves the
// English schema untouched for `en`, and keeps the English title/description for any
// field we haven't translated (never blanks a field). Only the top-level `properties`
// are localized — the plugin schemas are flat (no nested objects).
export function localizeSchema(schema: RJSFSchema, typeId?: string, lang?: string): RJSFSchema {
  if (!lang || !lang.startsWith("zh") || typeof schema !== "object" || !schema) return schema;
  const props = (schema as { properties?: Record<string, unknown> }).properties;
  if (!props) return schema;
  const nextProps: Record<string, unknown> = {};
  for (const [field, spec] of Object.entries(props)) {
    const zh = zhField(typeId, field);
    if (zh && spec && typeof spec === "object") {
      nextProps[field] = {
        ...(spec as object),
        title: zh.title,
        ...(zh.description !== undefined ? { description: zh.description } : {}),
      };
    } else {
      nextProps[field] = spec;
    }
  }
  return { ...schema, properties: nextProps };
}
