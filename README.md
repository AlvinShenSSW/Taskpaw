# 🐾 TaskPaw

TaskPaw watches the AI tasks and services running on your machines — **LADA**
video restore, **ComfyUI**, download folders, processes — and surfaces their
status, progress, and events in one place, optionally notifying your **OpenClaw**
assistant when work completes.

**V3** is a cross-platform desktop app (Tauri + React) with a two-role design:

- **Agent** — runs on each machine; watches that machine's monitors and exposes a
  small local API. A native console lets you add/edit/start/stop monitors and read
  the local task log (「日志」), retained for 30 days, without touching config files.
- **Hub** — a headless aggregator that polls your agents, keeps durable event +
  status history, and forwards completions to OpenClaw. A dashboard view shows the
  whole fleet and its event log.

The UI ships **Simplified Chinese (default) and English**, a Settings tab
(language · agent config · about), a live status dashboard, and native
file/folder pickers for path fields.

> **V2 is frozen.** The original single-file Windows app (`taskpaw.py`) still
> works but is no longer developed. All new work lives under
> [`taskpaw_v3/`](taskpaw_v3/).

## What it monitors

| Monitor | Watches |
|---------|---------|
| `lada` | LADA video restore — managed (TaskPaw launches `lada-cli`, parses progress) or passive (detect an external run); file queue, GPU/VRAM, CPU/RAM |
| `avsubs` | Standalone「AV 翻译」— walk a library folder (recursive by default) and give every MP4 that has no subtitles yet a `<name>.srt` (zh, global LLM API) next to it, via a `<name>.ja.srt` transcript (WhisperJAV) that is deleted once the `.srt` is written (kept as the resume point when translation does not finish); translation is resumable line by line (a Stop or an outage loses at most the request in flight), lines the primary model refuses go to up to two fallback models, a line every model refuses keeps its Japanese text, and a video with no translation service for 2 h is paused and continued at the next Start; a video already has subtitles when its folder holds `<name>.srt` (or `.ass`/`.ssa`/`.vtt`), a `<name>.<tag>.srt` with a non-Japanese tag (e.g. `.chs.srt`), or it is the folder's only video and there is any non-Japanese subtitle — an unreadable folder skips the video for the run and an existing subtitle is never overwritten; takes turns on the GPU with Jasna per file (in-process GPU lease) |
| `jasna` | Jasna video restore — managed (TaskPaw runs one `jasna.exe` per video: skip/resume, per-resolution `unet-4x`, retry + degrade for BasicVSR++ launches) or passive; file queue with failures, GPU/VRAM, CPU/RAM. Output `<name>-破解.mp4`; a legacy `<name>_restored.mp4` still counts as restored. Optional「AV 翻译」: after each restore, WhisperJAV (ja ASR) + the global LLM API write `<name>-破解.srt` (zh) next to the restored video — always named like that video (`<name>_restored.srt` for a legacy file); the `.ja.srt` transcript is deleted once the `.srt` is written; translation is resumable and uses the fallback models, as in `avsubs`. An「8K VR」tick launches every file of the task with the 8K SBS VR profile and leaves the task's own settings untouched (see Jasna「8K VR」below). 「1080p 档：使用 LTX」/「4K 档：使用 LTX」ticks restore that tier's files with Jasna 0.11.1's LTX model, with fast mode and large canvas chosen from the GPU; the other tier and every other setting stay as they are (see Jasna LTX below) |
| `comfyui` | ComfyUI queue (idle = complete) + error diagnostics from its log |
| `folder` | A downloads dir — a file is "done" once its size is stable |
| `process` | Any process by name/pattern (running ↔ exited) |
| `custom_cmd` | Runs a command on a schedule; exit code = status |
| `tcp_check` | A host:port is listening |
| `jellyfin` | A Jellyfin media server is up and healthy — `/health` plus the public server info (version, server name); no credentials |
| `heartbeat` / `state_file` | A status/heartbeat JSON file stays fresh |
| `host_metrics` | The machine's own CPU/mem/GPU/net (auto-on baseline) |

## Install & run

### Desktop app (recommended)

Download the installer for your OS from the project's **Releases** (Windows `.msi`
/ NSIS `.exe`, macOS `.dmg`). Launch **TaskPaw Agent** on each machine you want to
watch; it self-creates a default config on first run.

- Closing the window fully exits — no orphaned background process (#40).
- The agent's control API is loopback-only. The network API defaults to
  `127.0.0.1` (on-host only); **a fresh config has no token, so auth is disabled**
  — set an API token (Settings → Configuration) before binding it to a LAN address
  so the Hub reaches it over Bearer-authenticated HTTP.
- **LLM API** (Settings → LLM API, #178): one agent-level base URL / model / key
  (default xAI direct, `https://api.x.ai/v1` + `grok-4.3`) that features such as Jasna's
  AV 翻译 reuse. The key comes from the `TASKPAW_LLM_API_KEY` environment variable
  first, else `agent.yaml`; it is masked everywhere and never logged. Two optional
  **fallback models** (备用模型 1 / 2, #190/#192 — e.g. DeepSeek, then the MiMo Token
  Plan; keys from `TASKPAW_LLM_FALLBACK1_API_KEY` / `TASKPAW_LLM_FALLBACK2_API_KEY`
  first) translate the lines the primary refuses and, with「主模型不可用时改用备用模型」
  on (the default), the lines while it is unavailable. "Test connection" sends one
  real translation request with the current form values of that model, without
  saving them.
  「思考」/ Thinking: automatic disables thinking on DeepSeek and MiMo; choose「不发送」to use the service default, or「关闭思考」to send the parameter explicitly; slots differing only in this setting count as the same model (the first wins).

### FFmpeg（AV 翻译识别需要）

TaskPaw 会自动使用 WhisperJAV 安装目录中自带的 FFmpeg（Library\bin），
用于独立 AV 翻译和 Jasna 的 AV 翻译识别；已在 PATH 上的 FFmpeg 优先。
如果没有找到可用的 FFmpeg，将下面的脚本粘贴到普通 PowerShell 窗口运行
（不要另存为 .ps1）；只修改当前 Windows 用户的设置。
完成后请关闭 TaskPaw 窗口（会完全退出），再从开始菜单重新打开。

```powershell
& {
    Write-Host "当前 Windows 用户：$env:USERNAME"
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $userPath = [string]$key.GetValue('Path','')
        $machinePath = [Environment]::GetEnvironmentVariable('Path','Machine')
        $savedPath = [string]$machinePath + ';' + $userPath
        $found = $null
        foreach ($entry in ($savedPath -split ';')) {
            $folder = $entry.Trim().Trim('"').Trim()
            if ($folder) {
                $exe = ($folder.TrimEnd('\') + '\ffmpeg.exe')
                if (Test-Path -LiteralPath $exe -PathType Leaf -ErrorAction SilentlyContinue) {
                    $found = $exe
                    break
                }
            }
        }
        if ($found) {
            Write-Host "已找到 FFmpeg：$found"
        } else {
            $candidates = @(
                if ($env:LOCALAPPDATA) { ($env:LOCALAPPDATA + '\WhisperJAV\Library\bin') }
                'C:\WhisperJAV\Library\bin'
                'C:\Jasna\tools'
                'C:\Lada\_internal\bin'
            )
            $selected = $null
            foreach ($folder in $candidates) {
                if (Test-Path -LiteralPath ($folder.TrimEnd('\') + '\ffmpeg.exe') -PathType Leaf -ErrorAction SilentlyContinue) {
                    $selected = $folder
                    break
                }
            }
            if ($selected) {
                $raw = [string]$key.GetValue('Path','','DoNotExpandEnvironmentNames')
                $present = $false
                foreach ($entry in ($raw -split ';')) {
                    $folder = $entry.Trim().Trim('"').Trim()
                    if ($folder) {
                        $folder = [Environment]::ExpandEnvironmentVariables($folder)
                        if ($folder.TrimEnd('\') -ieq $selected.TrimEnd('\')) {
                            $present = $true
                        }
                    }
                }
                if ($present) {
                    Write-Host "FFmpeg 文件夹已在用户 PATH：$selected"
                } else {
                    $new = $raw.TrimEnd(';')
                    if ($new) { $new += ';' }
                    $new += $selected
                    $key.SetValue('Path',$new,'ExpandString')
                    [Environment]::SetEnvironmentVariable('TASKPAW_PATH_REFRESH',$null,'User')
                    Write-Host "已加入用户 PATH：$selected"
                }
            } else {
                Write-Host '未找到 FFmpeg。可运行：winget install Gyan.FFmpeg'
            }
        }
    } finally {
        $key.Close()
    }
    Write-Host '完成后请关闭 TaskPaw 窗口（会完全退出），再从开始菜单重新打开'
}
```

### Jasna「8K VR」

在 Jasna 任务的表单里勾选「8K VR」后，这个任务的**每个文件**都按 8K SBS VR（VR180 左右并排）处理，
每次启动 Jasna 时固定使用下面的配置：

| 参数 | 8K VR 使用的值 |
|---|---|
| 检测模型 `--detection-model` | `rfdetr-vr-v1`（Jasna 0.10.0 起自带；4K 档不再自动换成 `rfdetr-v6-large`） |
| `--vr-mode` | `sbs` |
| 时序重叠 `--temporal-overlap` | 8（所有档位） |
| 4K 档片段长度 `--max-clip-size` | 30（1080p 档仍用「1080p 档片段长度」） |
| 4K 档 unet-4x | 关（1080p 档仍按「1080p 档：使用 unet-4x 二次修复」的勾选） |

- 勾选后，表单里被覆盖的四项（4K 档 unet-4x、检测模型、4K 档片段长度、时序重叠）变灰并显示上面的值；
  **你保存的原值不会被改写**，取消勾选后下一次启动就恢复使用它们。
- 勾选后整个任务的所有文件都按 SBS VR 处理，**2D 影片请放到另一个任务**。
- 在运行中的任务上保存（包括勾选 / 取消勾选）会停止当前的 Jasna，并从头重跑当前影片。
- 显存更大、想在 4K 档也用 unet-4x：在「额外参数」里加 `--secondary-restoration unet-4x`
  （对这个任务的每个文件都生效，并关闭自动的 unet-4x 降级重试）；状态行会显示 `secondary via extra args`。
- 8K 的 **H.264** 片源：NVIDIA 硬件解码（NVDEC）的 H.264 最高只支持到 4K，8K H.264 会改用 CPU 解码，非常慢；
  请先转码成 HEVC（H.265）再放进输入文件夹。

### Jasna LTX（1080p 档 / 4K 档）

Jasna 0.11.1 起有赞助者模型 **LTX**：细节比 BasicVSR++ 多，但慢很多，只支持 NVIDIA RTX 显卡。Jasna 任务的表单里有
两个勾选框，按档位分别开启（档位按像素数划分，同 unet-4x）：

- 「1080p 档：使用 LTX」：1080p 档的文件用 LTX。勾选时「1080p 档：使用 unet-4x 二次修复」不使用。
- 「4K 档：使用 LTX」：4K 档的文件用 LTX。**需要大显存显卡**：8 GB 显卡上 Jasna 0.11.1 的 4K LTX 会在最后阶段失败（旧版本实测，0.11.2 未复测，
  TaskPaw 在第一个 4K LTX 文件启动时提醒一次）；24 GB 的 RTX 4090 足够。勾选时「4K 档：使用 unet-4x 二次修复」不使用。
- 任一勾选时「8K VR」不使用：LTX 不支持 VR 左右并排视频，也不支持二次修复。灰显字段**保存的值不会被改写**，
  取消勾选后恢复。没勾选的档位和其他所有设置都按原来的配置。
- 需要先在 Jasna 里激活含 LTX 的 License code（0.11 起用新的激活方式，0.10 的邮箱 + key 不再有效）。

| 选项 | 默认 | 作用 |
|---|---|---|
| LTX 快速模式 `--ltx-fast` | 开 | 快约 1.4 倍、细节略少、下载更小；**仅 RTX 50 系**。TaskPaw 每次「启动」用 nvidia-smi 检测显卡，没有 RTX 50 系（如 RTX 4090）就自动不用。0.11.1 实测 8 GB 显卡加载标准模型显存不足，预设因此使用 Fast；0.11.2 未复测。 |
| LTX 仅用于大马赛克（%） `--ltx-auto-split` | 12 | 不小于视频高度这个比例的马赛克用 LTX，更小的用 BasicVSR++（不带 unet-4x，4K 片上可能略糊）；0 = 整片用 LTX（画质优先）。影片没有合适切点时 Jasna 会跳过拆分、整片用 LTX。 |
| LTX 最佳模型 `ltx-undistilled` | 关 | 细节略多，慢约 1.5–1.8 倍，需要单独下载模型。 |
| LTX 大马赛克高清 `--ltx-large-canvas` | 开 | 大马赛克按 768 px 修复，特写和 4K 细节更多，这些片段慢约 3 倍。**只有每张 NVIDIA 显卡都有 16 GB 及以上显存时才传给 Jasna**：在 8 GB 卡上实测它连 1080p 都会让 Jasna 失败；机器上多插一张小显卡也会让它停用。 |

- **LTX 失败的影片不会马上重跑**（BasicVSR++ 的失败仍会重跑一次）：LTX 的失败通常可重复，而且 4K 可能在跑了几小时后才失败。
  下次「启动」时会再试；连续 3 个文件失败仍会停止这一批。
- **第一次用 LTX**：缺少模型时，在 TaskPaw 本地任务面板确认「下载并继续」。面板显示缺少文件、目标目录、下载大小与进度；「暂不下载」保留影片队列，稍后可直接继续。失败后显式重试，停止任务也会终止下载。等待和下载期间不占 GPU，不计影片失败。
  下载来自官方 Hugging Face 仓库及其文档中的 hf-mirror，固定版本的加密模型通过大小与 SHA256 校验后才发布，不覆盖已有文件；Jasna 仍负责授权与解密。Hub 只能查看状态。
- 「额外参数」里**一律**不能写 LTX 参数（`--ltx-*`、`--restoration-model-name`）。**勾选任一 LTX 时**，也不能写
  `--secondary-restoration`、`--denoise`、`--no-compile-basicvsrpp`、`--stream`、`--vr-mode`、`--restoration-model-path`、
  `--working-directory`（开「LTX 仅用于大马赛克」时还有 `--fmp4`、`--segments`）：额外参数会用到每次启动，LTX 启动不接受它们。
- LTX 的临时文件放在输出文件夹的 `.jasna-work` 里；中途停止留下的残留会在下次「启动」时清理。
- LTX 总是在 TaskPaw 内显示查找、准备、修复、保存四个阶段及当前阶段百分比（不是整片百分比），并显示上游报告的剩余时间；状态里的
  `LTX fast` / `large canvas` 表示实际传给 Jasna 的选项。
- 在运行中的任务上保存会停止当前的 Jasna，并从头重跑当前影片。

**配置页预设（3.9.16）**

选模型（普通 / unet-4x / LTX）和设备即可同时填入 1080p、4K 两档，适合混合批次；不再选择视频类型。预设只是未经本机基准测试的起点，所有调优值仍可手动修改。「重新填入预设」或切换模型 / 设备才会重填，打开旧任务不会自动改配置。路径、字幕、额外参数、CQ、编码器与帧率不随预设改动。

| 设备 | BasicVSR++ 片段长度 1080p / 4K | LTX 快速模式 | 选 LTX 后的 1080p / 4K |
|---|---|---|---|
| RTX 5060 8 GB（当前 64 GB 内存） | 90 / 45 | 开 | LTX / unet-4x |
| RTX 5060 Ti 16 GB（当前 32 GB 内存） | 180 / 90 | 开 | LTX / LTX |
| RTX 5070 Ti 16 GB（当前 32 GB 内存） | 180 / 90 | 开 | LTX / LTX |
| RTX 4090 24 GB | 180 / 180 | 关 | LTX / LTX |
| RTX 5090 32 GB | 180 / 180 | 开 | LTX / LTX |

各速度预设使用重叠 8、LTX 自动拆分 12%、Best 关、大马赛克关、LTX 减少重叠 auto。普通模式两档关闭 unet 和 LTX；unet 模式两档开启 unet、关闭 LTX。
8K VR 仍是独立开关：参考值为 4K 片段 30、重叠 8、4K unet 关、LTX 关。新预设开启「8K VR：使用可编辑参数」，允许随后手动调整；旧任务默认保留固定 VR 行为。检测器和 SBS 模式保持固定，2D 影片应单独建任务。

**Jasna 0.11.2 与大马赛克的取舍（2026-10-18 调研）**

- [0.11.2 发布说明](https://github.com/Kruk2/jasna/releases/tag/v0.11.2)称长视频 LTX 约快 13%，Windows 默认开启实验性的减少重叠。TaskPaw 的 `auto` 不传新参数，兼容旧版；显式 `on/off` 需要 0.11.2。原有片段长度 / 时序重叠只调整 BasicVSR++（包括自动拆分的小马赛克片段），不会直接加速 LTX 自身窗口。
- [作者的模型说明](https://github.com/Kruk2/jasna/blob/v0.11.2/docs/en/models.md#ltx-options)：大马赛克将相关区域的修复画布从 512 提到 768，主要改善大面积遮挡、近景和 4K 细节，相关区域约慢 3 倍。它不是「LTX 仅用于大马赛克 12%」那个筛选开关。旧配置仍保留原选项；速度预设默认关闭，可按画质需要重新开启。
- [社区 #502](https://github.com/Kruk2/jasna/issues/502)在 0.11.1、20 GB 显卡上报告开启后编码阶段显存溢出到共享内存、长时间卡在分页；关闭后该阶段峰值约 9 GB、正常完成。这是单个环境的报告，不能认定 16 / 24 GB 卡必然如此，也不能据此断言 0.11.2 已修复。公开材料不足以给出同片开关的可靠画质差距百分比；作者链接的 Discord 样例也不是已验证的严格 A/B。
- 操作者反馈的 4090 4K60 标准 LTX 约 40 多小时、5060 1080p 超过 40 小时未完成，均为界面勾选大马赛克。但 TaskPaw 3.9.15 已按显存屏蔽 8 GB 卡的大马赛克参数，所以 5060 的勾选状态不等于实际启用；应看任务日志里的 `large-canvas`。40 小时不能仅靠约 13% 的版本优化变成数小时，本文不再用官方 1080p 样例外推整片 4K 时间。
- 建议升级后，在同一代表片段上保持模型、seed、帧率及其他参数一致，只切换大马赛克，比较细节、闪烁、各阶段时间和专用 / 共享显存；首次 GPU 编译时间单列。测试内容至少包含近景与小马赛克，并用超过一分钟的片段评估新版长视频提速。尚未在这些机器上进行新版本实测。
- 模型与输出工作目录尽量放 SSD，避免机械盘读写瓶颈；不要让其他程序挤占显存。保留源 60p，本次预设不自动降帧。32 / 64 GB 系统内存不能代替显存吞吐。

**RTX 5060 8 GB 实测**（10 秒测试片，Jasna 0.11.1 试速模式）：

| 配置 | 结果 |
|---|---|
| LTX 标准模型 | 加载时显存不足（两次都是） |
| LTX 快速模式，1080p | 541–721 秒（约 54–72 倍实时，取决于桌面程序占用的显存） |
| LTX 最佳 + 快速，1080p | 968 秒 |
| LTX 快速 + 大马赛克高清，1080p | 失败（`Cannot fit a DiffVAE decode tile`），所以 TaskPaw 不会在 8 GB 卡上传这个选项 |
| LTX 快速模式，4K | 最后阶段失败：`Cannot fit a DiffVAE decode tile under the memory budget` |
| BasicVSR++ + unet-4x，4K | 约 7 秒，显存峰值 6.4 GB |

- LTX 耗时与马赛克窗口数、画布大小、帧率、显存及数据搬运有关；上表是旧版本短片试速，不能代表新版整片表现。
- 关掉占显存的程序（浏览器、视频播放器等）会让更多 LTX 模块常驻显存，速度更快。Jasna 也建议在 NVIDIA
  控制面板把「CUDA - 系统内存回退策略」设为「首选无系统内存回退」，以免显存溢出到内存时悄悄变慢。

### From source (dev)

Python 3.10+ via [`uv`](https://docs.astral.sh/uv/), Node 22, and the Rust
toolchain (for the Tauri shell).

```bash
# From the repo root:
uv sync --group dev          # Python backend + tests
uv run pytest                # backend test suite

# Headless backend, no GUI (still from the repo root):
uv run python -m taskpaw_v3.bootstrap agent --run     # an agent
uv run python -m taskpaw_v3.bootstrap hub --run       # the Hub

# Build the packaged desktop app (backend sidecar + Tauri bundle). The build
# extra provides PyInstaller for the sidecar:
uv sync --extra build --extra v3
uv run python scripts/build.py

# Frontend tests/build (in its own directory):
cd taskpaw_v3/ui && npm ci && npm test && npm run build
```

## Architecture

```
 Machine A ─ TaskPaw Agent ┐
 Machine B ─ TaskPaw Agent ┼─poll→ TaskPaw Hub ──HTTP POST──→ OpenClaw
 Machine C ─ TaskPaw Agent ┘        (history + status.md)        (Telegram/…)
```

Each agent monitors its own machine; the Hub aggregates them, keeps event/status
history, and notifies OpenClaw on completions. Run as many agents as you like.

## Docs

- **[docs/guides/deployment.md](docs/guides/deployment.md)** — deploy agents + a Hub.
- **[docs/guides/openclaw-integration.md](docs/guides/openclaw-integration.md)** — read fleet status (CPU/RAM/GPU/queue) from `hub.db` / `status.md`.
- **[Design spec](docs/specs/2026-06-27-taskpaw-v3-design.md)** — V3 architecture.
- **[AGENTS.md](AGENTS.md)** / **[docs/constitution.md](docs/constitution.md)** — repo guide + hard rules (for contributors/agents).
- **[CHANGELOG.md](CHANGELOG.md)** — release notes.

## Author & Copyright

TaskPaw was **initiated, designed, and is maintained by Alvin Shen (304)**.
Copyright © 2026 Alvin Shen (304).

## License

MIT — see [LICENSE](LICENSE).
