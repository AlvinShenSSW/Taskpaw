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
| `jasna` | Jasna video restore — managed (TaskPaw runs one `jasna.exe` per video: skip/resume, per-resolution `unet-4x`, retry + degrade) or passive; file queue with failures, GPU/VRAM, CPU/RAM. Output `<name>-破解.mp4`; a legacy `<name>_restored.mp4` still counts as restored. Optional「AV 翻译」: after each restore, WhisperJAV (ja ASR) + the global LLM API write `<name>-破解.srt` (zh) next to the restored video — always named like that video (`<name>_restored.srt` for a legacy file); the `.ja.srt` transcript is deleted once the `.srt` is written; translation is resumable and uses the fallback models, as in `avsubs`. An「8K VR」tick launches every file of the task with the 8K SBS VR profile and leaves the task's own settings untouched (see Jasna「8K VR」below). A「1080p 档：使用 LTX」tick restores the 1080p-tier files with Jasna 0.11.1's LTX model; 4K files and every other setting stay as they are (see Jasna LTX below) |
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

### Jasna LTX（1080p 档）

Jasna 0.11.1 起有赞助者模型 **LTX**：细节比 BasicVSR++ 多，但慢很多，只支持 NVIDIA RTX 显卡。
在 Jasna 任务的表单里勾选「1080p 档：使用 LTX」后：

- **只有 1080p 档**的文件用 LTX；**4K 档的文件和其他所有设置都按原来的配置**（4K 档继续用 BasicVSR++
  和「4K 档：使用 unet-4x 二次修复」）。8 GB 显卡上 LTX 做不完 4K 文件（见下面的实测）。
- 「8K VR」和「1080p 档：使用 unet-4x 二次修复」变灰、启动时不使用：LTX 不支持 VR 左右并排视频，也不支持
  二次修复。**你保存的值不会被改写**，取消勾选后恢复。
- 需要先在 Jasna 里激活含 LTX 的 License code（0.11 起用新的激活方式，0.10 的邮箱 + key 不再有效）。

| 选项 | 默认 | 作用 |
|---|---|---|
| LTX 快速模式 `--ltx-fast` | 开 | 快约 1.4 倍、细节略少、下载更小；**仅 RTX 50 系**。标准 LTX 模型在 8 GB 显卡上加载就会显存不足。 |
| LTX 仅用于大马赛克（%） `--ltx-auto-split` | 12 | 不小于视频高度这个比例的马赛克用 LTX，更小的用 BasicVSR++（不带 unet-4x）；0 = 整片用 LTX。影片没有合适切点时 Jasna 会跳过拆分、整片用 LTX。 |
| LTX 最佳模型 `ltx-undistilled` | 关 | 细节略多，慢约 1.5–1.8 倍，需要单独下载模型。 |

**第一次用 LTX**：模型不在 `model_weights\ltx-restore` 里时，Jasna 会在它的窗口里询问是否下载
（快速模式约 9 GB，不开快速约 15 GB），输入 `y`。开着「捕获进度」时 TaskPaw 会为每次运行的第一个这样的文件
单独打开一个 Jasna 窗口（仅 Windows），并告警一次；也可以先在 Jasna 自己的程序里选 LTX 下载好。

**RTX 5060 8 GB 实测**（10 秒 1080p 测试片，Jasna 0.11.1 试速模式，桌面程序占约 1.3 GB 显存）：

| 配置 | 结果 |
|---|---|
| LTX 标准模型 | 加载时显存不足（两次都是） |
| LTX 快速模式 | 541–614 秒（约 54–61 倍实时；首次运行另需一次约 11 分钟的测速，内核缓存冷时 740 秒） |
| LTX 最佳 + 快速 | 968 秒 |
| LTX 快速模式，4K 测试片 | 最后阶段失败：`Cannot fit a DiffVAE decode tile under the memory budget` |
| BasicVSR++ + unet-4x，4K 测试片 | 约 7 秒，显存峰值 6.4 GB |

- LTX 的耗时主要看马赛克窗口数，不看分辨率；但 4K 帧缓冲占掉显存后，合成阶段放不下，所以 4K 只用 BasicVSR++。
- 关掉占显存的程序（浏览器、视频播放器等）会让更多 LTX 模块常驻显存，速度更快。Jasna 也建议在 NVIDIA
  控制面板把「CUDA - 系统内存回退策略」设为「首选无系统内存回退」。
- 勾选后，「额外参数」里一律不能写 LTX 参数（`--ltx-*`、`--restoration-model-name`），也不能写
  `--secondary-restoration`、`--denoise`、`--no-compile-basicvsrpp`、`--stream`、`--vr-mode`、
  `--restoration-model-path`、`--working-directory`（开「LTX 仅用于大马赛克」时还有 `--fmp4`、`--segments`）：
  额外参数会用到每次启动，LTX 启动不接受它们。所以这些参数也没法只给 4K 档用。
- LTX 的临时文件放在输出文件夹的 `.jasna-work` 里；中途停止留下的残留会在下次「启动」时清理。
- 开「捕获进度」时状态行会显示 LTX 的阶段（如 `LTX 2/4 encode`）、该阶段的百分比和整部影片的剩余时间。
- 在运行中的任务上保存会停止当前的 Jasna，并从头重跑当前影片。

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
