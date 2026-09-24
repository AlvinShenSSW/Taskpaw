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

// WhisperJAV field help shared by jasna (AV 翻译 tickbox, #177) and avsubs (#179).
const WHISPERJAV_ENGINE_ZH =
  "WhisperJAV 语音识别预设：anime-whisper（默认，--mode qwen --qwen-generator anime-whisper；原型机实测胜出：逐句断句、时间轴准确、显存更低）、large-v3（--mode balanced --model large-v3）、large-v2（--mode balanced）、qwen3（--mode qwen）、custom（不传预设参数，--mode / --model / --qwen-generator 交给下方的额外参数自行指定）。";
const WHISPERJAV_EXTRA_ARGS_ZH =
  "原样追加到 WhisperJAV 命令行末尾的额外参数。TaskPaw 已经控制的参数会被拒绝：--output-dir --output-format --language --temp-dir --no-signature，以及引擎不是 custom 时的 --mode --model --qwen-generator；4 个字符及以上的 argparse 前缀缩写（如 --out）同样会被拒绝。--translate* 系列参数一律拒绝，因为它们会把 API 密钥写到命令行上（翻译由 TaskPaw 用「设置」里的 LLM API 完成）。";

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
  jasna: {
    jasna_exe_path: {
      title: "jasna.exe 路径",
      description:
        "jasna.exe 可执行文件的完整路径（如 C:\\Jasna\\jasna.exe）——不是文件夹。填写 → 托管模式（TaskPaw 逐个文件启动 jasna.exe，需要下方的输入/输出文件夹）。留空 → 被动模式（仅监视已在运行的 jasna）。托管模式不会随 TaskPaw 开机自启，需手动点「启动」。",
    },
    process_name: {
      title: "进程名",
      description: "仅被动模式：要检测的进程。带不带结尾的 '.exe' 都能匹配（Windows：jasna.exe）。",
    },
    jasna_input_folder: {
      title: "输入文件夹",
      description:
        "要处理的视频所在文件夹（不递归扫描子文件夹）。托管模式必填，且必须与输出文件夹不同。",
    },
    jasna_output_folder: {
      title: "输出文件夹",
      description:
        "结果写入的文件夹，每个文件输出 <原名>_restored.mp4。托管模式必填；已存在最终输出的文件会被跳过（断点续跑），也用于统计队列数量与完成通知。",
    },
    unet4x_1080p: {
      title: "1080p 档：使用 unet-4x 二次修复",
      description:
        "默认开。1080p 档指像素数 ≤ 1920×1080×1.5 的视频（1920×1200、2560×1080 仍属 1080p 档；2560×1440 及以上归为 4K 档）。unet-4x 是 Jasna 的赞助者功能，需先在 Jasna 图形界面激活授权；未激活或显存不足时该文件会自动改用不带 unet-4x 重跑，并告警一次。",
    },
    unet4x_4k: {
      title: "4K 档：使用 unet-4x 二次修复",
      description:
        "默认关：8 GB 显存放不下 4K 的 unet-4x。4K 档指像素数 > 1920×1080×1.5（约 3.1 MP，即 2560×1440 及以上）的视频。显存更大时可以打开。",
    },
    av_translate: {
      title: "AV 翻译",
      description:
        "默认关。开：每个文件修复完成后，用 WhisperJAV 识别日语语音，再用「设置」里的 LLM API 翻译成简体中文，在修复后的视频旁边生成 <原名>_restored.ja.srt（日语）和 <原名>_restored.srt（简体中文）。输出文件夹里已修复但还没有字幕的影片，会在待修复文件全部处理完之后补做；已存在的 .ja.srt 会直接复用（只做翻译）。会在输出文件夹里创建 .avsubs/ 工作文件夹。需要先在「设置」里配置 LLM API。已有的 .ja.srt / .srt 会原样复用：如果同名替换了源视频，请先删掉旧的 .srt 文件。",
    },
    whisperjav_exe_path: {
      title: "whisperjav.exe 路径",
      description:
        "WhisperJAV 安装目录中 whisperjav.exe 的完整路径（如 C:\\WhisperJAV\\whisperjav.exe）——不是文件夹。勾选「AV 翻译」时必填。",
    },
    whisperjav_engine: { title: "识别引擎", description: WHISPERJAV_ENGINE_ZH },
    whisperjav_extra_args: { title: "WhisperJAV 额外参数", description: WHISPERJAV_EXTRA_ARGS_ZH },
    clip_size_1080p: {
      title: "1080p 档片段长度",
      description:
        "1080p 档每次送进模型的帧数（--max-clip-size）。越大越稳定但更吃显存；8 GB 显存建议 90。",
    },
    clip_size_4k: {
      title: "4K 档片段长度",
      description:
        "4K 档每次送进模型的帧数（--max-clip-size）。越大越稳定但更吃显存；8 GB 显存建议 60。",
    },
    temporal_overlap: {
      title: "时序重叠帧数",
      description:
        "相邻片段之间重叠的帧数（--temporal-overlap），减少接缝闪烁。建议 8–20；Jasna 要求 2×重叠 必须小于片段长度（两个档位都要满足）。",
    },
    codec: {
      title: "编码器",
      description: "输出视频的编码格式（--codec）。默认 hevc。",
    },
    cq: {
      title: "画质 CQ",
      description:
        "恒定质量参数（--cq，0–63）。数值越小画质越好、文件越大；hevc 建议 24。",
    },
    detection_model: {
      title: "检测模型",
      description:
        "马赛克检测模型（--detection-model）。保持默认 rfdetr-v6 时，4K 档会在 model_weights 里存在 rfdetr-v6-large.onnx 的情况下自动升级为 rfdetr-v6-large；手动指定则原样使用。",
    },
    jasna_extra_args: {
      title: "额外参数",
      description:
        "原样追加到命令行末尾的额外参数，例如 --device cuda:1。不要在这里重复 TaskPaw 已经控制的参数（--input --output --max-clip-size --temporal-overlap --codec --cq --detection-model）。特例：在这里写 --secondary-restoration（如 tvai / rtx-super-res / none）会对每个文件覆盖上面两个勾选框，并关闭自动的 unet-4x 降级重试。另外 --encoder-settings cq= 与上面的 CQ 冲突，Jasna 自己会报错。",
    },
    jasna_gpu_monitor: {
      title: "GPU 监控",
      description: "通过 nvidia-smi 报告 GPU%/显存（没有 NVIDIA GPU 的机器请关闭）。",
    },
    jasna_capture_progress: {
      title: "捕获进度",
      description:
        "高级。关（默认）：每个文件的 jasna.exe 各自开一个控制台窗口显示进度条（一个文件一个窗口）。开：把 Jasna 的输出捕获进 TaskPaw（不另开窗口），在状态面板显示 文件/%/fps/ETA。注意：「正在编译引擎」提示只依据 model_weights/*.engine 是否存在，与本开关无关。",
    },
  },
  // Standalone「AV 翻译 (subtitles)」task (#179).
  avsubs: {
    avsubs_root_folder: {
      title: "片库文件夹",
      description:
        "要扫描的视频片库文件夹（必填）。每个还没有同名 .srt 的视频，会在旁边生成 <原名>.ja.srt（日语）和 <原名>.srt（简体中文）；已有 .srt 的视频会跳过，已存在的 .ja.srt 会直接复用（只做翻译）。隐藏文件夹和链接文件夹会被跳过。会在该文件夹下创建 .avsubs/ 工作文件夹。与 Jasna 共用 GPU：两者同时运行时按文件轮流使用，等待的一方显示「waiting for GPU (held by …)」。需要先在「设置」里配置 LLM API。不会随 TaskPaw 开机自启，需手动点「启动」。注意：不要让两个运行中的任务覆盖相互重叠的文件夹，也不要指向已开启「AV 翻译」的 Jasna 任务的输出文件夹。",
    },
    avsubs_recursive: {
      title: "扫描子文件夹",
      description: "默认开：同时扫描所有子文件夹。关：只处理片库文件夹本身里的视频。",
    },
    avsubs_extensions: {
      title: "扩展名",
      description: '要处理的视频扩展名，不带点、不区分大小写，默认 ["mp4"]；例如再加上 mkv。',
    },
    whisperjav_exe_path: {
      title: "whisperjav.exe 路径",
      description:
        "whisperjav.exe 的完整路径（如 C:\\WhisperJAV\\Scripts\\whisperjav.exe）——不是文件夹。必填。",
    },
    whisperjav_engine: { title: "识别引擎", description: WHISPERJAV_ENGINE_ZH },
    whisperjav_extra_args: {
      title: "WhisperJAV 额外参数",
      description: WHISPERJAV_EXTRA_ARGS_ZH + "Windows 路径请加引号。",
    },
    avsubs_gpu_monitor: {
      title: "GPU 监控",
      description: "通过 nvidia-smi 报告 GPU%/显存（没有 NVIDIA GPU 的机器请关闭）。",
    },
  },
};

function zhField(typeId: string | undefined, field: string): FieldT | undefined {
  return (typeId ? BY_TYPE[typeId]?.[field] : undefined) ?? BASE[field];
}

// A display label for a config field key (used by the wizard review step, #121):
// the zh title when the UI is Chinese and we have a translation, else the schema's
// own English `title` (so it matches what the FORM shows — localizeSchema uses the
// same fallback), else the raw key.
export function fieldLabel(field: string, typeId?: string, lang?: string, englishTitle?: string): string {
  const zh = zhField(typeId, field);
  if (lang && lang.startsWith("zh") && zh) return zh.title;
  return englishTitle || field;
}

// Overlay Chinese title/description onto a plugin json_schema's properties when the
// UI language is Chinese. Returns a NEW schema (never mutates the input); leaves the
// English schema untouched for `en`, and keeps the English title/description for any
// field we haven't translated (never blanks a field). Only the top-level `properties`
// are localized — the plugin schemas are flat (no nested objects).
export function localizeSchema(schema: RJSFSchema, typeId?: string, lang?: string): RJSFSchema {
  if (!lang || !lang.startsWith("zh") || typeof schema !== "object" || !schema) return schema;
  const props = (schema as { properties?: Record<string, unknown> }).properties;
  // Only a plain object of properties is localizable — guard a malformed schema
  // (properties as a primitive/array) rather than throwing on Object.entries (Kimi).
  if (!props || typeof props !== "object" || Array.isArray(props)) return schema;
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
  // nextProps mirrors the input property shapes (open-ended JSON Schema); assert
  // back to RJSFSchema rather than widen every field to JSONSchema7Definition.
  return { ...schema, properties: nextProps } as RJSFSchema;
}
