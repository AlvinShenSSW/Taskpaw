# M01-P1 设计：本机 headless Miko 完整后处理（v3）

日期：2026-10-20。Issue：[MDCX304#296](https://github.com/AlvinShenSSW/MDCX304/issues/296)（阶段 1）。
涉及两个仓库：
- **MDCX304**：分支 `afk/claude-m01-296` @ `fade135`，新增一次性后处理命令行。
- **Taskpaw**：分支 `afk/claude-m01-296` @ `e7cf6c8`（T04-P1 之上，T04 合并点更新后再 rebase），新增 scrape 步骤。

设计原则 P0：家庭内网、单用户、提效。不做常驻托管服务、原生出生计数、句柄/ACL 校验、两段式发布日志之类的机制。

v2 修正辩论第 1 轮的 D01–D12，v3 修正第 2 轮的 D13–D16（处置见文末附录）。

## 1. 目标

T04 的回传包（`work/return/<film>-r<rev>/` + `return-manifest.json`）就绪后，Taskpaw 的 scrape 阶段做三件事：
1. 在私有 staging 中调用 Miko，对这部影片执行完整后处理：刮削元数据、NFO、封面/背景图/缩略图、标识、目录封面、命名整理。
2. 核对产物。
3. 以 scrape_adapter 身份写入 T01 的 FinalManifest 并完成该阶段。

不在本阶段：远端节点、真实刮削站点的产物质量（待实机）、保留用户原有的手选封面。

## 2. 冻结契约（M01-P1）

| ID | 要求 | 对应 #296 验收 |
|---|---|---|
| M1 | 每部影片每个 attempt 起一个新的 MDCX304 子进程（MDCX304 自己的 Python 环境），经 Taskpaw 的 kill-on-close Job 启动，配置目录独立；单线程调度串行执行，因此 Miko 的全局 Flags 和配置从不被两次运行同时修改 | 1 |
| M2 | 只写 `work/scrape/<film>-r<rev>/<attempt>/` 下的 staging 和配置目录：回传包、收件工作副本、用户原始 input、用户的 Miko 配置与片库的 hash 都不变。由 §3.1 的强制配置表保证，测试逐行断言 | 2 |
| M3 | 结果以 `result.json` 返回，包括真实最终路径、各文件 hash、片号、演员（含来源），以及每个视频**应有**的标识（has_sub、mosaic、definition）。下列任一情况为失败：失败目录非空、成功数量与视频数不符、出现多个片号、缺少非空的 poster 或 NFO、出现 §4.3 白名单以外的文件、NFO 中的中文字幕与有码/无码/破解标签与应有标识不符（标签由 §3.1 强制写入）。文件名后缀依用户设置而定，不作判定；标识的像素效果待实机验证 | 3、6 |
| M4 | 重启时，上一 attempt 按"中断"核销并重试：attempt 目录先删除，再完整重跑。只重新联网刮削到私有目录，不会重跑 GPU（P0 下放宽"无证据不重刮"的要求）；新的 final revision 取已有最大值 + 1 | 4 |
| M5 | 只用 Miko 后端，不启动桌面壳。预检 MDCX304 的 Python 能否运行、用户配置能否读取、有站点需要浏览器时 Chrome 是否已安装；失败时返回可读的错误（`needs_attention`） | 5、7 |
| M6 | 已有的 NFO 和图片不进入 staging，每次完整处理、从干净图源重建：纠正旧图的缺标、错标，且不重复叠印（多卷时共用一张 poster，见 §3.1）。必需的 poster 下载失败就是失败，不让 IGNORE_PIC_FAIL 吞掉 | 8、9 |
| M7 | 标识只看 T04 的真实成果：修复为 applied/reused → 破解；有非空中文字幕 → 中文字幕；4K 以 Miko 按实际视频测出的 definition 为准（4K/UHD/8K）。Taskpaw 的 FinalManifest 只有在自己的探测也满足"宽 ≥ 3840"时才声明 4k 标识，否则不声明，但不判为失败 | 8 |
| M8 | 多卷影片：staging 中把视频命名为 `<回传视频名>-cd<卷序><扩展名>`（Taskpaw 不知道片号；Miko 会正确解析出片号与 cd 段），让 Miko 按卷序整理；`folder.jpg` 必须存在，且与本次 poster 字节一致，列入 manifest。单视频不要求 folder.jpg。同一影片中出现不同片号 → `needs_attention` | 10 |
| M9 | 通过核对的结果写成 T01 FinalManifest（新的 final revision，旧的批准随之失效），以 scrape 身份完成阶段，再由 operator 核销 | 11 |
| M10 | 测试：MDCX304 侧用离线的假爬虫数据跑真实后处理流程；Taskpaw 侧用假的命令行（真实子进程）覆盖成功、各类失败、重启重跑和 FinalManifest 校验 | 5 |

待实机（交 T09）：
- 真实刮削站点与 Chrome；
- 用户真实的 Miko 配置；
- 标识的像素效果与封面质量。

## 3. MDCX304 侧：`mdcx/cmd/postprocess_one.py`

### 3.1 强制配置表

用户的 Miko 配置复制一份，再按下表覆盖；表本身有版本号，测试会对每一行做断言：

| 项 | 值 | 原因 |
|---|---|---|
| `main_mode` | 1（刮削模式） | 2 是只整理模式，不生成 NFO 和图片 |
| `media_path` / `success_output_folder` / `failed_output_folder` | staging 的 in / out / failed（绝对路径） | 写入边界（M2） |
| `success_file_move` / `success_file_rename` / `failed_file_move` | 开 | 不开的话产物留在 in 目录，失败也不可见 |
| `soft_link`、`auto_link` | 关 | 会往用户的片库建链接或复制文件（D01） |
| `extrafanart_folder` | `"extrafanart_copy"`（相对路径），`download_files` 中不含它 | 避免删除 staging 以外的目录（D01） |
| `subtitle_add` | 关；`subtitle_add_chs` 开；`sub_type` 包含 `.srt` | 不从用户的字幕目录拷文件（D01） |
| `emby_on=[]`、`actor_photo_kodi_auto=False`、`use_database=False` | — | 演员头像、Emby、数据库同步会写到 staging 以外（D01） |
| `keep_files` | `[]` | 每次重建（M6） |
| `download_files` | 精确列表：poster、thumb、fanart、nfo；不含 IGNORE_PIC_FAIL、预告片、剧照 | 防止出现白名单外的文件（M3） |
| `mark_type` 包含 SUB/UMR/HD；`poster_mark=1`、`thumb_mark=1` | — | 标识（M7） |
| `hd_get` | `"video"` | 分辨率按实际视频测量，不从文件名取 |
| `pic_simple_name` | 开；用户的 `folder_name` 为空时强制为 `"number"` | 多卷共用一张 poster，不会重复叠印（D03、D13：该选项只在有影片目录时生效） |
| `nfo_tag_include` 包含 CNWORD、MOSAIC；`nfo_include_new` 包含 TAG | — | M3 能可靠核对 NFO 中的标识标签（D14） |
| `create_folder_jpg` | 开 | M8 |
| REST_SCRAPE、AUTO_EXIT、`thread_time` | 关 / 0 | 只会增加等待 |
| `thread_number` | 1 | 串行 |

用户的 `userdata/` 映射文件复制到新配置目录。

### 3.2 运行

- **调用**：`python -m mdcx.cmd.postprocess_one --request request.json --result result.json`，环境变量 `MDCX_CONFIG_DIR=<attempt>/config`，必须在任何 `mdcx` import 之前设好。
- **request.json**：staging 目录、视频列表（staging 中的文件名、卷序）、修复状态、是否有非空中文字幕、用户配置文件路径。
- **标识注入**：`Scraper` 增加一个可选的 `file_info_hook`，只在 `get_file_info_v2` 之后调用一次，由请求中的事实设定以下字段：
  - destroyed、leak、mosaic、has_sub、c_word、file_show_name；
  - 未修复时保留 wuma，因为它决定使用哪些无码站点。

  `res.mosaic` 由这些字段推导，NFO 与 add_mark 都读它，缓存片号的路径也使用各卷挂钩后的 file_info。
- **执行**：用 `executor.run(Scraper(..., task_id=<attempt>).run(FileMode.Default, videos))`，`HeadlessSignals` 子类收集逐项结果。
- **result.json**：结束后遍历输出目录，按 M3 判定后原子写入。退出码 0 表示成功；其他值也都附带 result.json，写明失败原因与类别（`needs_attention` / `transient`）。

## 4. Taskpaw 侧：`workflow/scrape_step.py`

### 4.1 必须一并修改的位置

- `read_store._LOCAL_STAGES` 加入 scrape。
- `PendingComputeJob.stage` 的取值范围加入 scrape。
- 调度的 stage 列表：领取顺序为 compute → return → scrape；`reconcile_retries` 也覆盖 scrape。
- `LocalIdentity` 增加 `scrape_component_id`，补写方式同 receiver。
- `_grant` 改成按 role 查表，不能再用 if/else 默认落到 receiver。
- `_ours` 增加本运行时 scrape 身份的 assignment。

### 4.2 运行

- **领取**：每个 boot 授予一个 scrape_adapter 身份，领取方式同 return（不带 worker/resource）。
- **配置摘要**：领取前计算 `config_digest = sha256(用户 Miko 配置文件字节 + userdata 映射文件字节 + 强制配置表版本 + 命令行版本)`，这些在领取前都已知。
- **staging**：
  - 先重新计算 `return-manifest.json` 的 sha256，与 return 阶段回执中的 `output_manifest_hash` 比对，不一致 → `needs_attention`（D16）。
  - 视频从回传包硬链接过来；多卷时按 M8 命名。
  - 中文字幕复制为 `<staging 视频名>.srt`。
  - 日语字幕结束后按最终命名放进输出目录。
  - 旧 NFO 和图片不进入 staging。
- **启动**：经 `OwnedJob.launch([mdcx_python, "-m", "mdcx.cmd.postprocess_one", ...], env={MDCX_CONFIG_DIR: ...})` 启动，子进程身份记入 attempt 文件，重启时据此核对旧进程是否还在。

### 4.3 组装 FinalManifest

- **文件白名单**：main_video、nfo、poster、thumb、fanart、folder_cover、source/generated_subtitle。出现任何其他文件 → `needs_attention`。
- **字幕**：
  - 中文字幕语言固定写 `zh`，不取文件名里的 `chs`；
  - 日语字幕作为 item 列出（语言 ja，带 video_item_id），但不进 `subtitles.item_ids`；
  - 多卷时 `parent_context` 按目录填写。
- **item id 映射**：`restoration.video_item_ids` 与 `subtitles.item_ids` 必须是最终产物的 item id。回传包中的 id 先经 staging 硬链接对应到最终路径，再取 `uuid5(film, 最终相对路径)`（D16）。
- **证据**：restoration 和 subtitles 的事实来自 `return-manifest.json`（已由 T04 回执的 digest 担保）；scrape 身份签署的证据中，`payload_digest` 为 return-manifest 的 sha256。postprocess 的证据由 scrape 身份签署 result.json 的 digest。
- **探测**：用现有 ffprobe 逻辑重新探测最终视频，填 `media_probes`；4k 标识按 M7 处理。
- **提交**：`put_final_manifest` → `finish_attempt`（scrape 身份签证据），再由 operator 核销。

### 4.4 配置项

`WorkflowSettings` 增加 `mdcx_python`、`mdcx_root`、`mdcx_profile`。缺少任何一项都不启用 scrape，影片停在"待刮削"并显示原因。

## 5. 不做的事

- 不常驻 Miko 服务，不走 HTTP。
- 不做原生出生计数、句柄/ACL 校验、浏览器版本钉死。
- 不保留旧封面；重启后不复用旧 attempt 的产物。

## 6. 测试计划

| 测试 | 覆盖 |
|---|---|
| MDCX304：强制配置表逐行断言；用户配置文件与 `userdata` 源不被修改；配置中的绝对 extrafanart 目录、auto_link 均被中和 | M1、M2 |
| MDCX304：离线假爬虫 + 真实后处理：单视频、两卷（cd 命名、folder.jpg 与 poster 字节一致、poster 只叠印一次——与单次叠印的参考图比对）、无中文字幕、修复标识、请求中的事实覆盖文件名里的 `-C` 和旧 NFO 标签 | M3、M6、M7、M8 |
| MDCX304：poster 下载失败 → 失败；失败目录非空 → 失败；多个片号 → needs_attention；出现白名单外文件 → 失败 | M3、M6、M8 |
| Taskpaw：假命令行成功 → FinalManifest 写入并通过 T01 校验，scrape 阶段成功，回传包不变 | M2、M9 |
| Taskpaw：命令行失败类别映射；预检失败 → needs_attention；缺少配置项 → 不启用 scrape 并显示原因 | M3、M5 |
| Taskpaw：重启 → 旧 attempt 被核销、目录被删除、重跑成功，revision 递增 | M4 |
| Taskpaw：4K 规则：Miko 判为 4K 但宽 < 3840 → 不声明 4k，仍成功 | M7 |

## 附录：辩论第 1 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| D01 强制配置项不全，会写到 staging 以外 | P1 | §3.1 强制配置表，测试逐行断言 |
| D02 其他强制值（主模式等） | P2 | 写入 §3.1 |
| D03 多卷 poster 被叠印两次 | P2 | `pic_simple_name` 开；测试与单次叠印参考比对 |
| D04 4K 规则不一致 | P2 | M7 改写 |
| D05 无法从文件判定"标识未加上" | P2 | result.json 记录应有标识；核对 NFO 标签与后缀；像素效果待实机 |
| D06 task_id、executor、浏览器 | minor | 写入 §3.2、M5 |
| D07 卷序未被使用 | P2 | staging 按 cd 命名（M8） |
| D08 第二个挂钩点多余 | minor | 只保留一个挂钩点，并写明字段（§3.2） |
| D09 Taskpaw 侧需要修改的位置 | P2 | §4.1 |
| D10 config_digest 的来源 | P2 | 领取前可算出的组合（§4.2） |
| D11 FinalManifest 组装细节 | P2 | §4.3 |
| D12 重启复用过度设计 | P2 | 改为删除后重跑（M4） |
| D13 `pic_simple_name` 需要影片目录 | minor | `folder_name` 为空时强制为 `"number"`（§3.1） |
| D14 NFO 标签与后缀核对依赖用户设置 | P2 | 强制 NFO 标签设置；不核对后缀（M3、§3.1） |
| D15 Taskpaw 没有片号 | P2 | staging 用 `<回传视频名>-cd<N>`（M8） |
| D16 item id 映射与回传 manifest 核对 | minor | §4.2、§4.3 |
