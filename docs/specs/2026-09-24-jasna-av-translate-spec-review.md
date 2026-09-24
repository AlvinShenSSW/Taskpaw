# 「AV 翻译」—— V3 字幕功能重设计 spec review 报告（三个 issue）

Date: 2026-09-24（issue 创建前的 spec review；AFK 运行时每个 issue 再按 `/afk-spec-planner` 出正式 design doc）
Status: 三个 issue 已建（#178 → #177 → #179，见 §5）；Codex Astra High 设计审阅三轮（§11）；第 3–4 轮只剩 P1-1 的收尾（取消覆盖不了 DNS/connect/慢速读 → 改为可终止的 LLM worker 子进程；再补响应队列唤醒契约与 worker 孤儿防护）；4 轮后按 codex-review 收敛规则停止
Owner: Alvin Shen
参考来源:
- 业内大佬的口述方案（2026-09-24，owner 转述；**本报告的首要参考**）
- Astra 的建议：large-v3 作基线，日语专用 Kotoba-Whisper 作对照；有公开模型，但无依据保证它在本类素材上更好
- 旧脚本 `macsubs.py`（Gemini 生成，MLX Whisper + OpenRouter Grok；因幻听严重弃用）
- 原型机实测（本机，下文 §3）
- Codex（`gpt-6-astra`，reasoning high）设计审阅（§11）

## 0. 一句话

三件事，三个 issue，按依赖顺序：

- **#178 全局 LLM API 设置**（agent 级，Settings 页）：base URL / model / key 填一次，默认 Grok，
  各处直接调用；key 走环境变量优先、agent.yaml 次之，界面上永远打码。
- **#177 Jasna「AV 翻译」勾选框**：勾上后每部影片转码发布后自动 VAD → 日语 ASR → LLM 日译中，
  同目录输出同名 `<原名>_restored.srt`（旁边留 `.ja.srt`）。字幕引擎放进共享包，供 #179 复用。
- **#179 独立「AV 翻译」任务类型**（`avsubs`，平行于 Lada/Jasna）：递归扫描一个目录下所有 MP4，
  没有同名 `.srt` 的就生成一个；附带进程内 GPU 租约，让它和 Jasna 不会同时占 8 GB 显存。

## 1. 旧方案为什么会「幻听」

`macsubs.py`（V2，已在 V3 监控里退役）的做法：`mlx_whisper.transcribe(..., language="ja",
condition_on_previous_text=False)` 用 `whisper-large-v3-turbo` **整段音频直喂**，然后用一个正则
`is_hallucination()` 事后过滤（重复片段、拟声词、字符多样性）。

根因：没有 **VAD 前置**。JAV 音频信噪比低、大段无对白（喘息/呻吟/背景乐），这些非语音段的频谱
经常像日语音节（如「ふ」），Whisper 的解码器在没有语音的地方会「编」出字幕（典型：
「ご視聴ありがとうございました」一类训练数据残留）。事后正则只能抓最明显的重复，抓不住语义通顺
的假句子。翻译阶段（Grok 4.1 fast，SRT 分块）owner 反馈「效果还行」，不是问题所在。

结论：**重设计的重点在 ASR 前置与引擎选型，不在翻译。** 这与大佬方案一致。

## 2. 参考意见 → 本设计的对应关系

大佬的方案分两半：**硬字幕 OCR 提取**（NVDEC + Zero-Disk + Spatial-Clustering + PP-OCRv5 SVTR，
GPU Tensor Transition Detection 定位画面变化）和 **AI 字幕**（Silero VAD 定位人声 → Whisper 拿文本
→ 翻译）。JAV 源片没有内嵌字幕，所以本功能只落 AI 字幕这一半；OCR 那一半是另一类输入
（有硬字幕的片源），列为非目标（§8）。大佬自己也把两者分开说了：「AI字幕基本上都是whisper拿到
文本然后翻译就行了」。

| # | 大佬原话 | 本设计的落实 |
|---|---------|-------------|
| E1 | 「用 Silero VAD (CUDA) 快速定位有人声区域的潜在字幕的时间戳」 | ASR 引擎内置 Silero VAD v6.2（`balanced`/`fast` 等模式）或 TEN VAD（ChronosJAV 模式），只对有人声区间解码；这是防幻听的第一道闸，也是旧脚本缺的那一环。 |
| E2 | 「whisper 拿到文本然后翻译就行了」 | 流水线固定为 VAD → Whisper 系 ASR → LLM 翻译，不做对齐/说话人分离等额外环节。 |
| E3 | 「可能会有一些针对 JAV 有 finetune 的 whisper，但我还没去查」 | 查到了：**WhisperJAV**（meizhong986，MIT，v1.9.3）是专为 JAV 做的 Whisper 流水线；其 `anime-whisper` 模式用 litagin/anime-whisper（kotoba-whisper-v2.0 在 5,300 h 日语 galgame/动画语音上微调，对喘息等非语言发声专门训练）。§4 采用它作 ASR 引擎，模式可切换。 |
| E4 | 「github 上排雷那个 vse 就行了，很难用，架构很混乱」 | 不用 video-subtitle-extractor；TaskPaw 只做**编排**（和管 `jasna.exe` 一样管一个子进程），ASR 引擎作为外部工具按路径调用，随时可换。 |
| E5 | 「识别不要用 VideOCR / RapidOCR」 | 本功能不含 OCR。 |
| E6 | 「日中翻译模型以前自己搞，现在没必要，token 便宜」 | 翻译走 OpenAI 兼容的云 API（默认 OpenRouter + `x-ai/grok-4.1-fast`，即旧脚本用过、owner 认可的组合；可改 DeepSeek 等），不跑本地 LLM（8 GB 显存也放不下 Whisper + LLM）。设置只填一次（#178）。 |
| E7 | 「2 小时的影片大概 1 分钟就能扫完」（OCR 定位） | AI 字幕做不到这个量级：WhisperJAV 官方数据 RTX 上 **每小时片长 5–10 分钟**，2 h 影片约 10–20 min GPU 时间。相对 Jasna 在 5060 上一部片的转码时长是小头；§4.3 用 GPU 串行吸收。 |
| A1 | Astra：large-v3 基线 + Kotoba-Whisper 对照，无依据保证更好 | 引擎做成预设下拉：`large-v3`（faster-whisper large-v3 = 基线）与 `anime-whisper`（Kotoba 谱系微调）都在。原型机合成日语语音实测（§12）anime-whisper 明显更好（逐句 cue、标点、时间轴准确、显存更低），faster-whisper 两种都把句子并成长 cue 且时间轴漂移 → **默认 `anime-whisper`**，真片 bake-off（§9.3）复核。 |

## 3. 原型机基线（本机实测 2026-09-24；owner：所有目标机器同配置，以本机为准）

| 项 | 实测值 |
|----|-------|
| GPU | NVIDIA GeForce RTX 5060，8151 MiB（Blackwell，sm_120） |
| 驱动 | 610.47 |
| OS | Windows 11 Pro 10.0.26200.9168 |
| Python / uv | 3.12.9 / 0.11.21 |
| ffmpeg / ffprobe | 8.0.1（winget，在 PATH；`C:\Jasna\tools\` 里也有一份） |
| Jasna | 0.10.0，`C:\Jasna\jasna.exe`（#173） |
| WhisperJAV | **1.9.3 已装**：`C:\WhisperJAV\Scripts\whisperjav.exe`（conda 式独立环境，Python 3.10.18，6.5 GB，自带 ffmpeg；torch 2.11.0+cu128 含 sm_120，ctranslate2 4.8.1，faster-whisper 1.2.1，transformers 4.57.6；模型缓存在 `~/.cache/huggingface/hub`，每个大模型约 2.9 GB） |
| Codex CLI | 0.155.0，已登录（设计审阅用 `gpt-6-astra`，reasoning high） |
| TaskPaw V3 | 3.2.1 |

**Blackwell 兼容性是本机最大的环境风险**：faster-whisper 依赖的 CTranslate2 只有 **4.8.2
（2026-08-31）起**才支持 sm_120（PTX JIT），且需要 CUDA 12.x（12.8）运行库，CUDA 13 不行；老版本
在 RTX 50 上报 `CUBLAS_STATUS_NOT_SUPPORTED`（int8 尤甚，float16 可绕）。`anime-whisper`/
`transformers` 模式走 PyTorch，需要 2.7+ 的 cu128 wheel。WhisperJAV 1.9.3 的 Windows 安装器
**已验证可用**（§12）：torch cu128 带 sm_120；ctranslate2 4.8.1 配合 WhisperJAV 自己的 Blackwell 检测
（自动改用 `auto` compute type，其 issue #414）；三种配置都 rc 0、无 CUBLAS 错误。

## 4. 方案选型

### 4.1 ASR 引擎：WhisperJAV（外部工具）而不是自研 faster-whisper + Silero

| | 自研（faster-whisper + Silero VAD + 自写过滤） | **WhisperJAV v1.9.3（选定）** |
|---|---|---|
| 大佬 E1/E2/E3 | 要自己实现 VAD 接入、分段、幻觉/重复过滤 | 内置 Silero v6.2 / TEN VAD；JAV 专用后处理（按终助词/相槌/方言重组句子、幻觉与重复删除、纯拟声行删除、时间轴修复、场景边界重叠处理） |
| Astra A1 对照 | 要再接 transformers 跑 Kotoba/anime-whisper | `--mode balanced`（large-v3）/`anime-whisper`/ChronosJAV Kotoba v2.0/v2.1/`qwen`（Qwen3-ASR）/`--ensemble` 双引擎合并，全是 CLI 开关 |
| 环境 | TaskPaw 要自己管一个带 torch/CT2 的 Python 环境（约 3 GB），与 AGENTS.md 的依赖纪律冲突 | 独立安装器（自带 Python 与 ffmpeg），TaskPaw 只要一个 exe 路径，和 Jasna 同一模式 |
| 代码量 | 400–600 行 + 模型下载/缓存 | argv 构造 + 子进程管理 |
| 可换性 | 绑死 | 引擎换了只改 argv 构造函数 |

CLI 事实（**本机 1.9.3 实测**，与网页文档有出入，以实测为准，§12）：
- `--mode {fidelity,balanced,fast,faster,transformers,qwen,crispasr}`，**没有** `anime-whisper` 模式；
  anime-whisper = `--mode qwen --qwen-generator anime-whisper`（qwen 流水线，默认分段器 whisperseg，
  可 `--qwen-segmenter silero-v6.2|ten|…`）；`balanced` 的默认模型是 **large-v2**，large-v3 基线要
  `--model large-v3`；`balanced` 的内置 VAD 用 `--vad-version 3.1|4.0|6.2`（默认 4.0）。
- `--language {japanese,korean,chinese,english}`（不是 `ja`），默认 japanese。
- 输出到 `--output-dir` 的是 **`<stem>.ja.whisperjav.srt`**（不是 `<stem>.srt`），旁边有机器可读的
  **`whisperjav_run.json`**（每文件 `state ∈ done|empty|suspect|failed|skipped`、`subtitle_count`、
  `output` 路径、`error`）；qwen 模式另有 `<stem>.ja.whisperjav.analytics.json`，balanced 有 `raw_subs/`。
- 默认在字幕末尾追加一条 **签名 cue**（`WhisperJAV 1.9.3 | …`）→ 必须传 `--no-signature`。
- argparse **接受无歧义前缀缩写**（`--output-di` 生效）→ 拥有 flag 校验必须拒绝前缀。
- `--fail-on empty,suspect` 存在但 TaskPaw 不用（直接读 manifest）；`--temp-dir` 存在（默认
  `%TEMP%\whisperjav\<stem>_extracted.wav`，按 stem 命名，成功后自清，被杀则残留）；`--skip-existing`
  存在但 TaskPaw 不用（跳过由 TaskPaw 按已发布的 `.ja.srt` 判断）。
- 启动器 `whisperjav.exe` → `python.exe` 主进程 → `python.exe` ASR worker **三层进程树**；只杀启动器时
  worker 存活并继续占 GPU（实测）→ `terminate_tree()` 必须 `taskkill /T /F`。

### 4.2 翻译：TaskPaw 自有的翻译步骤 + 全局 LLM 设置，而不是 `whisperjav-translate`

`whisperjav-translate` 功能够用（`--target-language Chinese`、`--tone` 成人向、DeepSeek/
OpenRouter/自定义 OpenAI 兼容端点、断点续翻），但 API key 通过 **`--api-key` 命令行参数**传入
—— 违反宪法 §2「secrets 不进 argv/日志」；各 provider 的环境变量名文档未写明。

因此翻译由 TaskPaw 自己做（标准库 `urllib`，不加运行时依赖；HTTP 调用跑在一个**可终止的
LLM worker 子进程**里，见下），端点/模型/key 来自 **#178 的全局 LLM 设置**（`taskpaw_v3/core/llm.py`），
Jasna 勾选框和独立任务都不各自存 key：
- OpenAI 兼容 `POST {llm_api_base}/chat/completions`；key 优先环境变量 `TASKPAW_LLM_API_KEY`，
  其次 agent.yaml（gitignored）里的 `llm_api_key`；GET 一律打码 `***`，PATCH 空/`***` 保留原值
  （复用 #94 对 `api_token` 的机制）。每次请求前读 `get_llm_settings()`（live-apply）。
- **`chat()` 的契约（#178）**：同步、阻塞：`chat(settings, messages, *, temperature, max_tokens,
  json_mode, timeout=30) -> ChatResult(content, finish_reason, model, latency_ms)`（`urllib`，
  `timeout` 是 socket 级超时）。**完整 envelope 校验在 `chat()` 内**：只有 `finish_reason == "stop"`
  且 content 非空才返回；否则抛 `LLMError(kind)`：`auth`(401/403)、`rate_limit`(429)、`network`
  (连接/超时)、`bad_response`(非 2xx 其他、JSON 不合法、结构缺失、`finish_reason == "length"`)、
  `refusal`(`message.refusal` 非空、content 为空、`finish_reason == "content_filter"`)。`chat()` 本身
  **不可取消**，只在两个地方直接调用：`llm_test`（控制 API 的同步请求线程，20 s 有界）和 worker。
- **LLM worker 子进程（#178 提供，#177/#179 使用）**：`taskpaw_v3/core/llm_worker.py`（开发态
  `python -m taskpaw_v3.core.llm_worker`，打包态 `taskpaw-backend llm-worker` 角色，与现有 agent|hub
  角色分发同一入口）。协议：stdin 每行一个 JSON 请求 `{id, messages, temperature, max_tokens,
  json_mode, timeout}` → stdout 每行一个 JSON 响应 `{id, ok:true, content, finish_reason, model,
  latency_ms}` 或 `{id, ok:false, kind, status, message}`；端点/模型/key 由父进程放进**子进程的
  环境变量**（`TASKPAW_LLM_API_BASE/MODEL/API_KEY`，绝不进 argv）；**孤儿防护**：worker 起一条 stdin 监视线程，stdin EOF（父进程死亡、或父进程 `stop()` 关闭
  管道）→ 立刻 `os._exit(0)`，即使主线程正阻塞在 HTTP 里；顺序循环不能兼任这件事，因为它在 HTTP
  阻塞期间读不到 stdin。Windows 上父进程再用自己持有的 kill-on-close Job Object（`ctypes`，与 Tauri
  shell 持有的那个无关，开发态也生效）把 worker 关进去作兜底。不记录 key/prompt/正文。**取消 =
  终止进程**：`terminate_tree()` 覆盖 DNS、connect、TLS、慢速读取等所有阻塞阶段。
- **JSON 协议**（不是让模型输出整份 SRT）：请求 `{"cues":[{"id":n,"ja":"…"}…],"context":[…]}`，
  40 条/批，前 5 条只作 `context` 不重译；`json_mode`；响应 `{"<id>":"<zh>"}`。Translator 侧校验
  （内容层）：用 `object_pairs_hook` 拒绝重复键、键集合与 id 集合**精确相等**、每个值是非空 `str`；
  序号与时间轴永远由本地原始 cue 重建。
- **统一的重试策略**：内容层失败（JSON 不合法、键不符、空串/非字符串）与 `LLMError.kind ∈
  {rate_limit, network, bad_response}` → 半批各重试一次；`auth`、`refusal` → 该文件**立即失败**，
  不重试。任何最终失败 → 该文件**不发布**中文 srt（不产出日中混排文件），日文 srt 保留，下次 Start
  只重译不重新识别；原始响应正文不进日志/事件。
- 提示词沿用 `macsubs.py` 的**意图**（成人内容直译、口语短句、不加解释），改写成上述 JSON 协议，
  补「`○` 打码词按上下文还原」。
- 翻译只吃网络和 CPU，放在**每个监控实例一个后台单线程队列**里跑（该线程只是 worker 子进程的
  客户端：写请求行、从响应队列 `get(timeout=deadline)`），和下一部影片的 GPU 工作并行；线程
  **只算不写**：返回译好的 cue 列表，发布由监控 worker 线程做（§4.4 结算规则）。

### 4.3 调度：GPU 串行，翻译并行，进程内 GPU 租约

Jasna 1080p 档（clip 90 + unet-4x）按 #173 的推算约 6.6 GB；anime-whisper fp16 权重 1.6 GB +
激活，`balanced` 的 large-v3 fp16 约 3 GB → 与 Jasna 同时跑会超 8 GB。所以：
- Jasna 每部影片的顺序固定为 **restore → publish → subs(ASR, GPU) → 下一部**；
- 独立任务 #179 与 Jasna 可能被 owner 同时点 Start → #179 引入 `taskpaw_v3/core/gpu_lease.py`
  （进程内、以 `RunId = (instance_id, generation)` 为身份的非阻塞租约 + 公平交棒 + 撤销）：
  **任何 GPU 子进程**（restore 或 ASR，含 Jasna 的 subs-only 项）启动前必须持有本次运行的有效租约；
  纯翻译不占；拿不到就 `idle` + detail「waiting for GPU (held by …)」，下个 poll 再试；jasna 在
  同一 issue 里接入。跨进程（两个 agent）不管。

### 4.4 共享代码：`taskpaw_v3/monitors/subs/` 包（调用协议是契约的一部分）

#177 把字幕引擎写成独立包，#179 直接复用，不复制。**不**从 `lada.py` 导入 reader/terminate
（那是绑定 `LadaInstance` 的实例方法，`lada.py:416/438`）；包内自带：

- `child.py` — `ChildProcess`：`Popen`（list argv，`shell=False`，`CREATE_NO_WINDOW`，stdout+stderr
  → 有界环形缓冲的 byte reader 线程）、`poll()`、`tail()`、`terminate_tree(timeout)`（terminate →
  wait → Windows 上 `taskkill /PID <pid> /T /F` 兜底，因 WhisperJAV 可能有 worker 子进程，§9.1
  预检确认）、`join_reader(timeout)`。
- `whisperjav.py` — **引擎预设** `Engine = Literal["anime-whisper", "large-v3", "large-v2", "qwen3", "custom"]`
  → argv 片段：`anime-whisper` = `--mode qwen --qwen-generator anime-whisper`；`large-v3` = `--mode balanced
  --model large-v3`；`large-v2` = `--mode balanced`；`qwen3` = `--mode qwen`；`custom` = 不传
  mode/model/generator，由 extra args 指定。`build_argv(exe, source, out_dir, tmp_dir, engine, extra)` 纯函数 =
  `[exe, source, *preset, "--language", "japanese", "--output-dir", out_dir, "--output-format", "srt",
  "--temp-dir", tmp_dir, "--no-signature", *extra]`（§12 已用这组 argv 实测通过）。`owned_flags_in(extra)`：
  拥有 `--output-dir --output-format --language --temp-dir --no-signature` 加上预设占用的 `--mode --model
  --qwen-generator`（`custom` 预设放行这三个），按精确 token、`--flag=`、以及 **argparse 无歧义前缀**
  （如 `--out`、`--lang`）三种形式拒绝。`AsrOutcome ∈ {succeeded, no_speech, failed}` 以
  **`out_dir/whisperjav_run.json`** 为准：`state ∈ {done, suspect}` 且 `out_dir/<stem>.ja.whisperjav.srt`
  能被 `srt.py` 严格解析出 ≥ 1 条 → `succeeded`（`suspect` 写进 detail）；`state == empty`（或 rc 0 且
  0 条）→ `no_speech`；`state ∈ {failed, skipped}`、manifest 缺失/不合法、rc ≠ 0 → `failed`（带有界尾巴）。
  **每次尝试用独立的 `out_dir`**（`<staging_root>/<sha1(relpath)[:12]>/attempt-N/`，启动前删除），
  `--temp-dir` 指向 `<staging_root>/tmp`（避免默认 `%TEMP%\whisperjav` 按 stem 撞名），staging 根可随时
  整体删除，里面的任何文件**永远不作为产物复用**；唯一可复用的是已经**发布**的 `.ja.srt`（按存在判断；
  owner 删掉它即强制重新识别 —— 有意为之的简单规则）。
- `srt.py` — 严格 parse（`Cue(index, start_ms, end_ms, text)`）/serialize/round-trip；0 条也是合法文件。
- `translate.py` — `Translator`（每实例一条 daemon 线程 + 请求队列 + 结果队列 + 每次运行一个
  `cancel: Event`，以及**一个 LLM worker 子进程**（§4.2）与其 stdout reader 线程/响应队列）；
  §4.2 的批次/校验/重试策略；每批之间检查 `cancel`；每次请求前读 `get_llm_settings()`（base/model
  随请求传给 worker；key 变化 → 用新环境重新 spawn worker）；每次请求 `get(timeout=deadline=60 s)`，
  超时 → terminate worker、按 `network` 处理、下一批重新 spawn；**唤醒契约**：stdout reader 线程在
  EOF/异常时向响应队列投递 `EOF` sentinel，`stop()`/abort 在 terminate worker 前先投递 `CANCEL`
  sentinel；translator 收到任一 sentinel 即退出当前等待，取消路径**禁止 respawn**；**只返回** `TranslateResult(run:
  RunId, job_id, outcome, zh_cues | detail)`，**不写任何文件、不改任何计数**。
- `job.py` — `SubsJob(run: RunId, source, ja_target, zh_target, staging_dir, cancel, exe, mode, extra,
  source_identity)`；显式协议：`start_asr() -> bool`、`poll_asr() -> Optional[AsrOutcome]`、
  `publish_ja() -> Optional[str]`（严格解析后 `.tmp` → `os.replace`，由 worker 线程调用）、
  `needs_translation()`；向插件暴露**两个不同的结果**：`asr_done`（GPU 已空出）与 `subtitle_done`
  （来自结果队列）。job **没有**重试策略、没有计数 —— 重试/降级/中止是插件策略。
- **运行身份**：`generation` 由**进程级分配器** `taskpaw_v3/core/generation.py`（`next_generation()`，
  锁保护、单调、进程内永不复用）分配，不是实例内计数：UI 的 Stop/Start 走 `admin.set_enabled` →
  `unregister/register`（`supervisor.register` 每次 `plugin.create()` 新对象），`reconfigure` 也重建
  实例，实例内计数会重复。`RunId = (instance_id, generation)` 是结果、租约、等待者、临时文件名的
  统一身份；测试必须经过真实 `unregister/register` 与 `reconfigure`，不能只在同一对象上连续 `start()`。
- **结算规则（两个插件共用）**：每个规划出来的字幕 job 必须到达且只到达一次**终态**
  `completed | failed | skipped(reason)`，reason ∈ `restore_failed | no_llm_key | unstable |
  collision | cancelled | disabled | no_exe`。翻译线程只提交结果；监控 worker 线程在 `check()` 里
  `_settle_results()`：校验 `RunId`（不匹配丢弃）、在 `_launch_lock` 内且 `not _stopping` 时**由
  worker 发布** zh（写 `<zh>.<generation>.tmp` → `os.replace`），然后更新计数、连续失败与事件 ——
  worker 是**唯一写者**。无结果路径的终态：restore 最终失败 → 该文件的字幕 job `skipped(restore_failed)`；
  字幕被关闭/批次 abort → 未开始的 job、`_subs_only` 全部、排队与在途的翻译一律 `skipped(cancelled)`
  并移出待办；缺 exe → 全部 `skipped(no_exe)`。**`done` 条件** = restore 侧照旧完成 且 **所有规划 job
  都已终态** 且 无存活子进程（restore/ASR）且 无在途翻译 且 结果队列已排空；Stop/abort 永不发正常
  `done`。连续失败按**结算顺序**计；只有 `completed`（或 `skipped(no_llm_key)`，即 ja 已交付）才清零，
  ASR 成功本身不清零。
- **停止（必须 join，不留线程）**：`stop()`：置 `cancel`，向响应队列投递 `CANCEL` sentinel，**terminate LLM worker 子进程树**
  （reader 收到 EOF 再投递 `EOF` sentinel；翻译线程无论阻塞在 `Queue.get` 还是刚要发请求都会立即
  返回，任何 DNS/connect/TLS/读取阶段都被覆盖）；terminate 存活的 GPU
  子进程树；在**一个 monotonic deadline** 内依次 join reader 与 translator，join 时不持任何锁；`start()`（`generation` 换新）先无条件清理旧 job/reader/
  translator、清扫本实例目标的 `*.<old generation>.tmp`，再重置全部字段。发布与 Stop 互斥
  （同一 `_launch_lock`，`stop()` 用 timed acquire，与今天 `jasna.stop()` 一致），所以不存在
  「门控之后、`os.replace` 之前被 Stop」的窗口。测试：「请求挂起时 Stop」（分别在建连阶段挂起、慢速持续响应两种假服务器下）→ worker 被
  终止、线程在预算内退出并被 join，「随后立即 Start」不死锁、旧结果被丢弃。「本次运行关闭字幕」或 abort 后，排队与在途翻译
  一律**取消**（不排空）。
- **abort / 关闭字幕时 GPU 子进程仍在跑**：避免异步结算把还被占用的 GPU 租约放掉。avsubs 三连败
  abort：先置「不再启动」→ terminate 存活 ASR 树并确认退出（bounded，兜底 `taskkill`）→ 该 job
  `skipped(cancelled)` → 才 `release`；`degraded` 短路只在没有存活子进程后生效（今天
  `jasna._check_managed` 的 abort 短路发生在 poll 子进程之前，新流水线不能沿用）。Jasna 关闭字幕
  时：正在 restore 的文件**继续持有**租约到该文件 GPU 工作结束；若此时 ASR 子进程存活 → terminate、
  job `skipped(cancelled)`、走正常 ASR 退出路径释放。
- **源文件身份**：规划时记录 `(size, mtime_ns)`，`start_asr()` 前与 ASR 退出后各核对一次；变了 →
  `skipped(unstable)`，告警一次。排除 `*.tmp.*` 与 `*_restored.tmp.*` 命名。
- **无对白**：`no_speech` → 发布 0 字节 `.ja.srt` 与 0 字节 `.srt`，结算为 `completed`
  （detail「no speech」），不是失败；默认**不**传 `--fail-on`。§9.1 预检用静音片段确认 WhisperJAV 行为。
- **缺 LLM key**：在每个 job 到翻译步骤时判断（live-apply）：以 `.ja.srt` 结束，结算为
  `skipped(no_llm_key)`（每次运行告警一次）；运行中补上 key，后续文件立即生效。
- **目标路径冲突**：规划时对每个文件预留规范化后（`normcase(realpath)`）的 ja 与 zh 两个目标路径；
  后来者与先来者的预留集合有交集（含跨角色：`a.ja.mp4` 的 zh 目标 == `a.mp4` 的 ja 目标；
  `a.mp4` 与 `a.mkv` 同目录）→ 排除、告警一次、`skipped(collision)` 并计入失败数。多个活动任务
  不得指向重叠的目录/目标（operator 规则，非目标）。

## 5. 三个 issue 的契约（正文各自自包含；此处是摘要与依赖）

### 5.A 全局 LLM API 设置（#178，版本 3.2.1 → 3.3.0）

- `AgentConfig` 新增 `llm_api_base`（默认 `https://openrouter.ai/api/v1`）、`llm_model`（默认
  `x-ai/grok-4.1-fast`）、`llm_api_key`（默认空）；`agent.example.yaml` 加占位。
- `core/llm.py`：`LLMSettings`、`resolve_llm_settings(config)`（env 覆盖）、进程级
  `set_/get_llm_settings()`、`ChatResult`、同步 `chat()`（§4.2 契约：`urllib`、envelope 校验、
  `LLMError` 五种 kind），日志只记 kind/status/耗时；`core/llm_worker.py` + `backend_main` 的
  `llm-worker` 角色（§4.2 协议；stdin 关闭即退出；设置只从环境变量读）。
- **启动顺序**：launcher 在 `supervisor.start()` **之前** `set_llm_settings(resolve_llm_settings(config))`
  （今天 `launcher.py:143` 先起 supervisor，`MonitorAdmin` 在其后才建）。
- 控制 API：`_LIVE_CONFIG = (api_token, llm_api_base, llm_model, llm_api_key)`，
  `_NON_LIVE_CONFIG` = 可编辑集合减去它；保存成功后才更新 desired、运行时 `_config` 三字段与
  holder 的不可变快照；env key **不写回** YAML；GET 打码在路由层（`app.py` get_config，与
  `api_token` 同处）+ `llm_api_key_source: env|config|none`；PATCH 空/`***` 不覆盖；
  `llm_test` 接受可选候选 `{llm_api_base, llm_model, llm_api_key}`（空/`***` 回退到当前生效值），
  **不持久化**，`max_tokens=8`，20 s，返回 `{ok, model, latency_ms}` 或 `{ok:false, error}`，绝不回显 key。
- Settings 页「LLM API」卡片：base URL、model、key（password，placeholder `***`，env 来源时禁用并
  提示）、「测试连接」测试**当前表单值**（未保存也能测）；zh+en。
- 非目标：按监控覆盖、多套 provider、Hub 侧 LLM、任何实际用途（在 #177/#179）。

### 5.B Jasna「AV 翻译」勾选框（#177，依赖 #178，版本 3.3.0 → 3.4.0）

- `JasnaConfig` 新增：`av_translate: bool = False`（标题「AV 翻译」）、`whisperjav_exe_path`
  （开时必填）、`whisperjav_engine`（§4.4 五种预设，默认 **`anime-whisper`**，依据 §12）、
  `whisperjav_extra_args`（§4.4 的拥有 flag 规则）。**不**含任何 LLM 字段（来自 #178）。
- **规划分两层**：restore 层完全不变（`plan_queue`：已有 `_restored.mp4` 的文件从一开始就
  计入 `queue_completed`）；字幕层单独规划：`subs_total = len(pending restores) + len(subs_only)`，
  `subs_only` = 已有 mp4 但无 `<stem>_restored.srt` 的文件，**在所有 pending restore 之后**处理
  （restore 是主产品）；`subs_completed/failed/skipped`，`subs_remaining` 由此推得。
- **阶段与所有权**：`_process` 只属于 restore，`_subs_job.child` 只属于 ASR；`_handle_exit`
  保持 restore-only；新的 `_poll_subs()` 处理 ASR 结果（`succeeded` → `publish_ja` → 入翻译队列或
  `skipped(no_llm_key)`；`no_speech` → 发布空文件、结算 completed；`failed` → 新 attempt 目录重试
  一次 → 告警、failed），任何 ASR 结果都不进 restore 的发布/unet 降级/重试分支；restore 最终失败
  → 该文件字幕 job `skipped(restore_failed)`；每个阶段结束都 `_advance()`。`stop()`：terminate 任一
  存活子进程树；ASR 已 rc 0 但尚未 poll → 发布 `.ja.srt`（C-4 同义）但不入翻译队列；`start()`
  无条件先清理旧 job/reader/translator 再重置全部字段（含 subs 计数、`_settled`），`generation` 换新。
- 降级：一次运行里**结算顺序**上连续 3 次 subs 失败 → 告警一次「AV 翻译已对本次运行关闭」，取消
  翻译队列，未开始的 job 与 `_subs_only` 全部 `skipped(cancelled)`，之后只转码；**不**计入现有
  3 连败中止；正在 restore 的文件继续持有 GPU 租约，存活的 ASR 子进程被 terminate。Start 预检：
  exe 不存在 → 告警一次、全部 `skipped(no_exe)`、只转码。
- metrics 新增 `phase`、`subs_total/completed/failed/skipped/remaining`、`subs_translating`；
  `queue_*` 语义不变；`done` 按 §4.4 条件；`status_md` 加 `subs S/T`。
- GPU 租约：本 issue 只留 hook（restore 与 ASR 启动点、含 subs-only；所有退出点），#179 接入。

### 5.C 独立「AV 翻译」任务类型（#179，依赖 #178 与 #177，版本 3.4.0 → 3.5.0）

- 插件 `avsubs`（display「AV 翻译 (subtitles)」，category `task`），无被动模式。
- 配置：`avsubs_root_folder`（dir picker，必填）、`avsubs_recursive: bool = True`、
  `avsubs_extensions: list[str] = ["mp4"]`、`whisperjav_exe_path`（必填）、`whisperjav_engine`
  （同 #177 的预设，默认 `anime-whisper`）、`whisperjav_extra_args`、`avsubs_gpu_monitor: bool = True`。
- `plan_tree` 纯函数 → 每项 `(source, relpath, ja_target, zh_target, kind ∈ {full, translate_only},
  size, mtime_ns)` + `collisions`；跳过 `.` 开头目录、`os.path.islink()` **与** `os.path.isjunction()`
  的目录、`*.tmp.*`/`*_restored.tmp.*` 文件；扩展名不分大小写；按规范化相对路径排序；
  目标路径冲突按 §4.4 处理。输出与视频**同目录同名**：`<stem>.srt`（中文）与 `<stem>.ja.srt`。
- 逐文件用 `subs/job.py` + §4.4 结算规则；重试一次后告警跳过；连续 3 个文件失败中止（`degraded`）：
  先停止启动 → terminate 存活 ASR 树并确认退出 → `skipped(cancelled)` → 释放租约 → 取消翻译；
  `done` 按 §4.4 条件：`AV 翻译 complete | Queue: X/Y done, F failed, K skipped`。
- **GPU 租约** `core/gpu_lease.py`：`try_acquire(run: RunId, poll_interval) -> bool`、
  `release(run)`（身份完全匹配才释放；否则 warning 不抛）、`withdraw(run)`（撤销等待/保留：
  `stop()`、abort、不再需要 GPU 时调用）、`holder()`。**公平交棒**：失败的 `try_acquire` 把
  `(run, poll_interval, 最近一次尝试时刻)` 登记为等待者；释放时若存在非释放者的等待者，租约为最早
  等待者保留 `max(30 s, 2 × 它的 poll_interval)`，期间只有它能拿到；拿到即移出；保留逾期 → 保留失效
  且该等待者被剔除；任何等待者若超过 `3 × 它的 poll_interval` 没再尝试也被剔除。租约锁是叶子锁
  （O(1) 非阻塞，持有时不取任何其他锁）。**释放点**：GPU 子进程退出（任何 rc）、`Popen` 失败、
  `stop()`、abort（**先确认子进程退出**）、字幕步骤被跳过/关闭。**jasna 接入**：任何 GPU 子进程
  启动前拿（restore 或 ASR，含 subs-only），同一文件 restore→ASR 之间不放，该文件 GPU 工作结束即放；
  迟到的旧 `RunId` 释放不能解除新运行的租约。
- `status_md.py` 与 openclaw 指南把 `avsubs` 加进 lada/jasna 的队列渲染块；ServiceIcon、i18n、
  wizard 目录、About、README。
- 非目标：持续监视目录（空闲时自动重扫）—— 后续 issue；跨进程 GPU 协调；多语种（源固定 ja，
  目标固定 zh）；Lada；OCR；多个活动任务写同一目标（operator 规则）。

## 6. 假设清单（未验证的都在这里；§9.1 在原型机逐条验证）

| # | 假设 | 依据 | 错了怎么办 |
|---|------|------|-----------|
| A1 | ✅ **已验证**（§12）：安装器自带 torch cu128（sm_120）+ ctranslate2 4.8.1，三种配置 rc 0、无 CUBLAS 错误 | 实测 | — |
| A2 | ❌ **已修正**：输出是 `<stem>.ja.whisperjav.srt` + `whisperjav_run.json`；成功 rc 0，`empty`/`suspect` 也是 rc 0，只有 `failed` 非 0 | 实测 | 判据改为读 manifest（§4.4） |
| A3 | ✅ `C:\WhisperJAV\Scripts\whisperjav.exe` | 实测 | — |
| A4 | ✅ `--temp-dir`、`--fail-on empty,suspect`、`--skip-existing`、`--no-signature` 都存在；**前缀缩写被接受**；`--language` 取 `japanese` | 实测 `--help` 与 `--output-di` | 拥有 flag 拒绝前缀；argv 用 `--language japanese`（§4.4） |
| A5 | 每小时片长 5–10 min GPU 时间（RTX 泛指）。实测 40.7 s 合成片：anime-whisper 首次 52 s（含下载与 35 s 模型加载），缓存后 **11 s**；large-v2 21 s；large-v3 46 s（含下载） | 官方 README + §12 | 真片吞吐 bake-off 实测 |
| A6 | Grok 4.1 fast 经 OpenRouter 不拒绝成人向日译中 | 旧脚本实际用过 | `llm_model` 换 DeepSeek 等；拒绝按翻译失败处理 |
| A7 | Tauri 拉起的 backend sidecar 继承用户级环境变量（读得到 `TASKPAW_LLM_API_KEY`） | Windows 子进程默认继承 | #178 已有 agent.yaml 次选路径，不依赖 env |
| A8 | Jasna 输出与源文件时间轴一致（字幕对源文件识别、给转码文件用） | Jasna 逐帧修复不改时长 | 若 Jasna 会掉帧/改帧率，则改为对 `_restored.mp4` 识别 |
| A9 | anime-whisper 的 `○` 打码词与句末无句号不影响翻译可用性 | 模型卡列出的已知弱点 | 提示词已要求还原；bake-off 看样本 |
| A10 | 一个 agent 进程里同时只会有 jasna/avsubs 各一个实例在跑 GPU 工作 | owner 用法 | 租约按 `RunId` 排他 + 公平交棒，多实例也正确，只是排队 |
| A11 | ❌ **已修正**：进程树是 启动器 → python 主进程 → python ASR worker；只杀启动器时 worker 存活并继续占 GPU（实测 pid 出现在 `nvidia-smi` 计算进程列表） | 实测 | `terminate_tree()` 必须 `taskkill /PID <pid> /T /F`（§4.4） |
| A12 | ✅ 无语音输入 → rc 0、0 字节 `.ja.whisperjav.srt`、manifest `state: empty`（默认不触发非零退出） | 实测（纯音调 30 s 片段） | — |
| A13 | 打包态 `taskpaw-backend llm-worker` 子进程启动 ≤ 2 s（PyInstaller onefile 解包）；每次运行只 spawn 一次，可忽略 | 现有 sidecar 启动经验 | 若过慢，改 onedir 或复用已解包目录；不影响正确性 |

## 7. 风险

| 风险 | 可能性 | 影响 | 缓解 |
|------|--------|------|------|
| Blackwell 兼容（A1） | 中 | subs 步骤全失败 | §9.1 第一步验证；3 连败自动关闭，不拖累转码 |
| 幻听仍在（尤其 `balanced`） | 中 | 字幕质量 | VAD 前置 + JAV 后处理 + bake-off 选默认模式；`--sensitivity conservative` 可调 |
| 8 GB 显存：两个 GPU 任务撞车 | 中（#179 之后） | OOM | Jasna 内部串行；#179 的进程内 GPU 租约；abort 先确认子进程退出再释放 |
| API 拒答/限流 | 中 | 中文 srt 缺 | 严格 JSON 校验 + 统一重试策略 + 只重译续跑；`llm_test` 先测；模型可换 |
| 翻译线程在 Stop 预算内退不出 | 低（取消 = 终止 worker 进程） | 违反 §4 | worker 子进程被 terminate 后管道关闭，线程立即返回并被 join；测试覆盖建连挂起与慢速响应 |
| 吞吐下降（每部 +10–20 min） | 确定 | 批次更慢 | 翻译并行；ASR 串行是显存约束下的必要成本 |
| 秘密泄露 | 低 | 高 | env/agent.yaml + 打码；测试断言 argv/日志/事件里没有 key |
| 三个 issue 的顺序被打乱 | 低 | 返工 | 每个 issue 正文写明依赖；#178 不含用途，#179 只复用 |

## 8. 非目标

- 硬字幕 OCR（大佬方案的 NVDEC / PP-OCRv5 / Tensor Transition Detection 一半）—— 另一类片源，另开 issue。
- 自研或本地部署翻译模型；本地 LLM 的安装/管理。
- 字幕编辑/校对 UI、说话人分离、双语字幕、烧录字幕进视频。
- 改动 `lada` 插件；给 `lada` 加字幕。
- 把 WhisperJAV 或模型打进 TaskPaw 安装包。
- 独立任务（#179）的持续监视模式（后续）；跨进程 GPU 协调；多个活动任务写同一目标。
- `.ja.srt` 的源文件变更失效检测（按存在复用，owner 删文件即重跑 —— 有意的简单规则）。

## 9. 验收与测试

### 9.1 原型机预检（owner，装完 WhisperJAV 后，先于 #177 实现；结果写进 design doc 假设表）

**2026-09-24 已在本机完成第 1–5 步，结果见 §12；剩余第 6 步（#178 合并后测连接）与真片 bake-off（§9.3）。**

1. 安装 WhisperJAV 1.9.3 Windows 安装器；记下 `whisperjav.exe` 路径（A3）。
2. 取一段短片段：`whisperjav clip.mp4 --mode balanced --model large-v3 --language japanese --output-dir .\o`
   与 `--mode qwen --qwen-generator anime-whisper` 各跑一次；看是否报 `CUBLAS_STATUS_NOT_SUPPORTED`/CUDA 错误、
   `nvidia-smi` 峰值显存、产出文件名与退出码（A1、A2）。
3. `whisperjav --help` 全文存档；确认前缀缩写是否被 argparse 接受（A4）。
4. 运行中用任务管理器看 `whisperjav.exe` 的**进程树**；中途 kill 父进程，确认 worker 是否残留（A11）。
5. 用一段静音片段跑一次，记录 rc 与输出（A12）。
6. #178 合并后：Settings 里填 Grok，点「测试连接」通过（A6/A7）。

### 9.2 自动化测试（各 issue 正文有完整列表）

- #178：配置默认/校验；打码与 `llm_api_key_source`（路由层）；PATCH 保留；env 优先且不写回 YAML；
  holder 在 supervisor 之前初始化、保存失败不更新；`chat()` 请求形状/`max_tokens`/envelope 校验/
  错误映射（含 `refusal`、`length`、`content_filter`）/`ChatResult`；`llm_worker` 协议（真实子进程 +
  本地假 HTTP 服务器：请求/响应行、错误行、stdin 关闭即退出、环境变量取设置、argv 无 key、
  被 terminate 时不留孤儿；**HTTP 挂起期间杀死父进程且不 terminate worker → worker ≤ 1 s 内自行
  退出**）；caplog 无 key/正文；
  `llm_test` 候选值与回退、不持久化；UI 卡片渲染、打码、env 禁用、测试当前表单值。
- #177：配置；两层规划全组合；argv 与拥有 flag（token/`=`/前缀）；`srt.py` 严格解析；翻译内容层
  校验（重复键、缺 id、多 id、空串、非字符串）与 `LLMError` 各 kind 的重试/不重试策略；context
  不重译；结果只返回不写文件；worker 发布在 `_launch_lock` 内且 `_stopping` 后不发布；旧 `RunId`
  结果丢弃；`*.<generation>.tmp` 命名与 `start()` 清扫；FakePopen 按 argv[0] 区分 jasna/whisperjav
  的生命周期（restore→ASR→下一部；ASR 结果不进 restore 分支；`no_speech`；`unstable`；
  `restore_failed` → 字幕 job skipped；subs-only 在 restore 之后且不启动 jasna；已有 `.ja.srt` 只
  重译；结算顺序的 3 连败 → 取消翻译、未开始 job 与 `_subs_only` 全部 skipped(cancelled)、转码继续、
  存活 ASR 被 terminate、正在 restore 的租约不放；`done` 的各条件单独能阻止且「所有 job 终态」是
  必要条件；Stop 时 ASR 已 rc 0 → 发布 ja 不入队；「请求挂起时 Stop」（建连挂起、慢速响应两种假服务器）→ worker 被终止、线程在预算内被 join（含「已进入 `Queue.get` 等待、worker 无响应即被终止」
  的时序：sentinel 唤醒、不 respawn）、「立即 Start」不死锁；`get(timeout=deadline)` 超时 → terminate + 重新 spawn + `network`；key 变化 → 重新 spawn；`start()` 在只剩 translator 时也清理；**经真实 `unregister/register` 与
  `reconfigure` 重建实例后旧结果/旧释放不影响新运行**）；`status_md`；metrics 契约；版本。
- #179：`plan_tree`（递归/非递归、隐藏目录、symlink 与 junction、扩展名大小写、`*.tmp.*` 排除、
  三分类、跨扩展名与跨角色冲突、空格/中日文文件名/大小写路径、排序稳定）；源身份变化 → 跳过；
  生命周期；GPU 租约（拿/放/持有者/身份不匹配释放被拒/重复释放不抛/等待者登记/公平保留与逾期
  剔除/`withdraw`/停滞等待者剔除/窗口按 poll_interval 放大；`Popen` 失败与 `stop()` 都释放并
  `withdraw`；**翻译三连败结算时 GPU 子进程仍存活 → 先 terminate 确认退出再释放，期间 jasna 拿不到**；
  jasna 的 restore 与 subs-only 启动都需要租约；释放者不能在有等待者时立刻重拿）；`status_md`；
  wizard；版本。

### 9.3 Bake-off（owner，#177 合并后在原型机上，决定默认模式）

3 部有代表性的影片（各约 2 h），每部各跑预设 `anime-whisper`（默认，§12 合成语音胜出）与
`large-v3`（Astra 基线）；记录：GPU 时间、峰值显存、cue 数、随机抽 3 个 10 min 无对白窗口里的
假字幕条数、抽 3 个对白窗口里的漏句数。结果写回 design doc；`anime-whisper` 在真片上不劣于基线
则保持默认，否则改 `whisperjav_engine` 的默认值。同时用 1 份 `.ja.srt` 比较 `x-ai/grok-4.1-fast`
与 `deepseek-chat` 的拒答率与可读性，定 `llm_model` 默认值。

### 9.4 验收冒烟（owner）

- #177：Jasna 任务勾上「AV 翻译」，输入文件夹放一部短片，Start → 转码完成后 detail 进入
  `subtitling:`，随后出现 `<name>_restored.ja.srt`，再出现 `<name>_restored.srt`（中文），
  `done` 事件文案含 `Subs: 1/1 done`；再次 Start 全部跳过；把 `.srt` 删掉再 Start → 只重译不重识别。
- #179：建一个 AV 翻译任务指向一个含子目录的库，Start → 只有没 `.srt` 的 MP4 被处理，`.srt`/`.ja.srt`
  出现在各自视频旁边；同时 Start 一个 Jasna → 其中一个显示 `waiting for GPU (held by …)`，另一个
  当前文件的 GPU 工作结束后它才开始（公平保留），而不是原任务连跑整批。

## 10. 审阅流程

1. 本报告 + 三个 issue 正文 → **Codex Astra High 设计审阅**（`codex exec -m gpt-6-astra
   -c model_reasoning_effort=high`，只读），只看架构与真实风险；发现项回写（§11）。
2. 之后按 #178 → #177 → #179 交 `/afk`：Claude 实现 → 内审 → Codex 外门 → Kimi 终审；每个 issue 各出
   design doc。

## 11. Codex Astra High 审阅记录

### 第 1 轮（2026-09-24）：NEEDS-CHANGES，7×P1、5×P2、4 个问题

逐条核对了引用的代码行（`jasna.py:767/887/954/1105/1247`、`supervisor.py:124/194`、
`launcher.py:143`、`admin.py:221/284`、`lada.py:416/438`），全部成立，处理如下：

| 发现 | 处理（落到哪里） |
|------|------------------|
| P1-1 翻译请求不能在 stop 预算内退出，旧线程可能在重启后发布 | §4.4「停止」+ 第 2 轮再修（见下） |
| P1-2 `_phase` 未覆盖退出/停止/重启分支 | §5.B「阶段与所有权」 |
| P1-3 双队列缺统一终态结算 | §4.4「结算规则」+ 第 2 轮再修 |
| P1-4 三分法与 `queue_*` 语义冲突 | §5.B「规划分两层」 |
| P1-5 GPU 令牌漏掉 subs-only 与各释放点 | §4.3/§5.C |
| P1-6 staging 保留 + `--skip-existing` 会把残片当成功 | §4.1/§4.4 attempt 目录 |
| P1-7 staging 唯一 ≠ 最终路径唯一 | §4.4「目标路径冲突」 |
| P2-1 settings 启动顺序/live 集合/双 GET 路径/测试语义/`max_tokens` | §5.A |
| P2-2 共享包需要调用协议；lada helper 是实例方法 | §4.4 |
| P2-3 JSON 校验不足、提示词不能照搬 SRT 输出 | §4.2 + 第 2 轮再修 |
| P2-4 正在写入的视频、staging 视频、junction、文件名 | §4.4「源文件身份」、§5.C |
| P2-5 非阻塞令牌无公平性 | §5.C + 第 2 轮再修 |
| Q1–Q4 | §4.4 各条、§9.1 |

### 第 2 轮：NEEDS-CHANGES —— P1-2/4/5/6/7、P2-1/2/4、Q1–Q4 FIXED；以下回写

| 发现 | 处理（落到哪里） |
|------|------------------|
| P1-1 PARTIAL：门控后、`os.replace` 前被 Stop 仍可发布；daemon 线程遗留与 §4 join 要求不符；urllib 超时不能保证整次请求 ≤ 30 s | §4.2「`chat()` 契约」：`http.client` + `CancelToken.abort()` 关闭在途 socket + `timeout`/`deadline`；§4.4「结算规则」/「停止」：翻译线程**只返回结果**，发布由 worker 在 `_launch_lock` 内做，与 Stop 互斥，`*.<generation>.tmp` 按运行隔离；`stop()` 必须 join，不再有 draining 线程；A13 退路 |
| P1-3 PARTIAL：无结果路径（restore 失败、关闭字幕时的排队/未开始/`_subs_only`）没有终态 | §4.4「结算规则」：终态枚举 + reason；`skipped(restore_failed/cancelled/disabled/no_exe)` 由 worker 恰好结算一次；`done` 要求**所有规划 job 终态** |
| P1-8 NEW：实例重建（`admin.set_enabled` → unregister/register、reconfigure）让实例内 `run_id` 重复 | §4.4「运行身份」：进程级 `core/generation.py` 分配 `generation`，`RunId = (instance_id, generation)`；测试经真实 unregister/register 与 reconfigure |
| P1-9 NEW：翻译三连败 abort/关闭字幕时 GPU 子进程仍在跑，异步结算会放掉被占用的租约 | §4.4「abort / 关闭字幕时 GPU 子进程仍在跑」；§5.B/§5.C：先停止启动 → terminate 并确认退出 → 结算 → 才释放；正在 restore 的租约不放；跨插件测试 |
| P2-3 PARTIAL：`chat() -> str` 藏了 `finish_reason`；`length/refusal` 是重试还是立即失败两处不一致 | §4.2：`ChatResult`，envelope 校验归 `chat()`，统一重试策略（内容层 + rate_limit/network/bad_response 半批重试；auth/refusal 立即失败） |
| P2-5 PARTIAL：等待者只有成功才移除、无撤销、保留逾期无清理、固定 30 s 与 `poll_interval > 30` 不匹配 | §5.C：`withdraw(run)`、按 `RunId` 登记、窗口 `max(30 s, 2×poll_interval)`、逾期与停滞等待者剔除 |

### 第 3 轮：NEEDS-CHANGES —— P1-3/P1-8/P1-9、P2-3/P2-5 FIXED，无新 P1；只剩 P1-1 PARTIAL

| 发现 | 处理（落到哪里） |
|------|------------------|
| P1-1 PARTIAL：`CancelToken.abort()` 在 DNS/TCP connect 完成前没有 socket 可关；只在操作之间检查 `deadline` 约束不了持续少量数据的慢速读；A13 只验证阻塞读被打断 | 采纳其建议的子进程方案：§4.2「LLM worker 子进程」（#178 提供 `core/llm_worker.py` + `llm-worker` 角色，stdin/stdout JSON 行协议，设置走子进程环境变量）；§4.4 `translate.py`/「停止」：**取消 = terminate worker**，覆盖建连、TLS、读取所有阶段，管道关闭后线程立即返回并被 join；`get(timeout=deadline)` 兜住慢速响应；`chat()` 回归同步 `urllib`，只被 `llm_test` 与 worker 调用；A13 改为 worker 启动开销假设；测试加「建连挂起」「慢速持续响应」两种假服务器 |

### 第 4 轮：NEEDS-CHANGES —— P1-1 PARTIAL（只差响应队列唤醒契约）+ P1-10 NEW（父进程死亡时 worker 遗留）；无 P2

| 发现 | 处理（落到哪里） |
|------|------------------|
| P1-1 PARTIAL：translator 阻塞在 `Queue.get(timeout=60)`，terminate worker 只让 reader 收到 EOF，没有东西唤醒队列等待 | §4.4 `translate.py`/「停止」：`CANCEL`/`EOF` sentinel 唤醒契约，取消路径禁止 respawn；#177 item 1/7/12 |
| P1-10 NEW：HTTP 阻塞期间顺序循环读不到 stdin EOF，父进程死亡后无人 terminate；Tauri 的 Job Object 属于 shell，开发态无覆盖 | §4.2「孤儿防护」：worker 的 stdin 监视线程 EOF → `os._exit(0)`，父进程持有的 kill-on-close Job Object 兜底；#178 item 3 与测试 |

4 轮后按 `skill/codex-review` 的收敛规则停止：第 3、4 轮各只剩同一子系统（翻译线程的停止时序）的
一个实现细节，后续由 #177/#178 的测试契约在代码里兑现；owner 可决定是否再花一轮。

## 12. 原型机预检结果（2026-09-24，本机 RTX 5060 8 GB）

环境：`C:\WhisperJAV`（1.9.3，Python 3.10.18，torch 2.11.0+cu128 / sm_120，ctranslate2 4.8.1，
faster-whisper 1.2.1）。测试素材不用 owner 的片库：`ffmpeg` 合成的 30 s 纯音调片段（无语音）和
用 edge-tts（ja-JP-NanamiNeural）合成的 40.7 s 日语四句 + 4 s 静音间隔片段。显存峰值含桌面基线
约 1 GB（`nvidia-smi` 每秒采样）。

| 配置（argv） | rc | 耗时 | 峰值显存 | cue 数 | 观察 |
|---|---|---|---|---|---|
| `--mode balanced`（默认 = large-v2） | 0 | 21 s | 3300 MiB | 3 | 两句并成一条长 cue；「行って→言って」「明日→明後日」等错字；首条 cue 从 8.0 s 开始（实际 0 s 就有语音），时间轴漂移 |
| `--mode balanced --model large-v3`（Astra 基线） | 0 | 46 s（含 2.9 GB 下载） | 3331 MiB | 2 | 文本更准，但全部并成两条长 cue，时间轴同样漂移 |
| `--mode qwen --qwen-generator anime-whisper` | 0 | 52 s（含 2.9 GB 下载 + 35 s 模型加载）；缓存后 **11 s** | **2779 MiB** | **7** | **逐句一条 cue，文本与标点全对，时间轴与实际语音吻合**（0.0–4.5、9.2–14.6、19.0–25.9、30.4–36.5 s）；分段器 whisperseg（CPU），推理 fp16 |
| 纯音调 30 s，`--mode balanced` | 0 | 60 s（含下载） | — | 0 | 0 字节 srt，manifest `empty`，无幻觉 cue |
| 插件最终 argv（anime-whisper 预设 + `--language japanese --output-dir <attempt> --output-format srt --temp-dir <tmp> --no-signature`） | 0 | 11 s | — | 7 | 无签名 cue；manifest `done`，`subtitle_count 7`；输出名 `<stem>.ja.whisperjav.srt` |

其他实测事实：三种配置默认都会在末尾追加签名 cue「WhisperJAV 1.9.3 | Balanced/Aggressive」（`--no-signature`
去掉）；argparse 接受前缀缩写；进程树三层，只杀启动器会留下占 GPU 的 worker；默认临时目录
`%TEMP%\whisperjav\<stem>_extracted.wav`，成功后自清、被杀则残留，输出目录里不会留下假的 srt/manifest；
幻觉过滤词表首次从 gist 下载后缓存在 `~/.cache/whisperjav/hallucination_filters`；模型缓存
`~/.cache/huggingface/hub`（large-v2、large-v3、anime-whisper 各 2.9 GB，whisperseg 114 MB）。

**结论**：Blackwell 无问题；默认引擎改为 `anime-whisper`（合成语音上明显优于两种 faster-whisper 配置，
显存更低）；真片 bake-off 只需确认它在真实 JAV 音频上不退化。

## 参考链接

- WhisperJAV: https://github.com/meizhong986/WhisperJAV ；文档 https://meizhong986.github.io/WhisperJAV/
  （CLI 参考、ChronosJAV、翻译指南）
- litagin/anime-whisper: https://huggingface.co/litagin/anime-whisper
- kotoba-tech/kotoba-whisper-v2.0: https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0
- Silero VAD: https://github.com/snakers4/silero-vad
- CTranslate2 Blackwell（sm_120）支持与 `CUBLAS_STATUS_NOT_SUPPORTED`：
  https://github.com/SubtitleEdit/subtitleedit/issues/10180 ，
  https://github.com/m-bain/whisperX/issues/1211 ，
  https://github.com/bengizmo/voxint/issues/429
