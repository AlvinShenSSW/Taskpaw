# M01-P1 接口：`postprocess_one` 的 request.json / result.json（schema 1）

配合 [M01-P1 设计 v3](2026-10-20-media-workflow-m01-p1-design.md)。两边都按此实现；字段增减须同时改两边并提升 schema。

## 目录约定（均由 Taskpaw 在启动前准备）

```text
work/scrape/<film>-r<rev>/<attempt_id>/
  config/                 MDCX_CONFIG_DIR
    user-config.json      用户 Miko 配置的副本（Taskpaw 复制）
    userdata/             用户 userdata 中的映射文件与 watermark/（Taskpaw 复制）
  in/                     staging 输入：视频硬链接 + 中文字幕 <视频名>.srt
  out/                    Miko 成功输出根
  failed/                 Miko 失败输出根
  request.json
  result.json             CLI 最后原子写入
```

CLI 读取 `config/user-config.json`，按强制配置表（版本 `FORCED_TABLE_VERSION = 1`）覆盖后写成 `config/config.json`，然后才 import 并运行 Miko。CLI 自身版本 `CLI_VERSION = "postprocess_one/1"`。

## request.json

```json
{
  "schema": 1,
  "attempt_id": "<uuid>",
  "staging": {"in": "<abs>", "out": "<abs>", "failed": "<abs>"},
  "videos": [
    {"file": "<staging in 中的文件名>", "volume": 1, "restored": true, "zh_subtitle": true}
  ]
}
```

- `volume`：单视频为 null；多卷为 1..N（文件名已是 `<回传视频名>-cd<N><ext>`）。
- `restored`：T04 修复为 applied/reused。
- `zh_subtitle`：该视频有非空中文字幕（in 目录中有同名 `.srt`）。

## result.json

```json
{
  "schema": 1,
  "status": "succeeded",
  "error_class": null,
  "message": "",
  "number": "ABC-123",
  "actors": [{"name": "…", "source": "<site 或 null>"}],
  "film_dir": "<相对 out 的影片目录，POSIX>",
  "files": [
    {"path": "<相对 out，POSIX>", "role": "main_video", "video": "<request 中的 file>", "sha256": "…", "size": 123}
  ],
  "expected_marks": [
    {"video": "<request 中的 file>", "has_sub": true, "mosaic": "无码破解", "definition": "1080P"}
  ]
}
```

- `status`：`succeeded` 或 `failed`；失败时 `error_class` 为 `needs_attention` 或 `transient`，`message` 为简短中文原因。失败时其余字段尽量填写，可为空。
- `files[].role`：`main_video`、`nfo`、`poster`、`thumb`、`fanart`、`folder_cover`、`subtitle`。`video` 对主视频与字幕必填，其余为 null。
- 失败判定（任一即 failed，见设计 M3）：失败目录非空、成功数 ≠ 视频数、多个片号、缺少非空 poster 或 NFO、白名单外文件、NFO 缺少应有的中文字幕或 mosaic 标签（繁体 無碼破解 等同 无码破解）。
- 退出码：成功 0；失败非 0，但都写 result.json。result.json 缺失或无法解析 → Taskpaw 视为 `transient`。
