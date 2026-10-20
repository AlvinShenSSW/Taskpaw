# T06-P1 设计：「媒体入库」页（阶段 1 单机闭环的操作界面）

- Issue：Taskpaw#277（T06），父 EPIC #271；阶段 1 子集（验收 1–5、13、15、16 的单机部分）。
- 基线：Taskpaw `f1814f3`（F02-P1 实现 + 内审修复）。分支 `afk/claude-t06-277`。
- 设计原则 P0：个人家用、单用户、提效。一页、一个抽屉、一次点击；复用已有组件与轮询方式。

## 1. 目标

操作者在 Taskpaw 桌面应用里看到每部影片走到哪一步、为什么在等；整理完成的影片点开看 Miko 的成果，
选好 NAS 演员目录，点一次「确认入库」，之后归档与清理自动完成。失败的本地阶段可一键重试，可打开文件夹
查看。

## 2. 冻结契约

- **U1 列表**：AgentConsole 新增「媒体入库」页签，列出当前修订的影片：名称（成品目录名）、当前阶段与状态、
  等待原因（来自 dispatcher / finalizer 的 waiting）。分组：待确认入库 / 处理中 / 需处理（失败）/ 已入库
  待清理 / 已完成（最近 20 部）。5 秒轮询，与现有页签一致。流水线未启用或未启动时显示原因，不报错。
- **U2 详情抽屉**：点影片打开抽屉：封面（final 的 poster）、标题与演员（解析 final 的 NFO：`<title>`、
  `<actor><name>`）、片号（成品目录名）、分卷与全部文件（角色、大小）、成果标识（修复 / 中文字幕 / 4K）、
  将清理的来源（INBOX 原片清单、工作副本数量）。
- **U3 选目标**：抽屉内「入库到」：按演员名预填搜索 Filmpaw 演员（含别名），列出候选（名称、所属库、UNC
  路径、头像），操作者手动单选；不自动按名字选择（issue 要求）。Filmpaw 未运行 → 显示「Filmpaw 未运行」，
  确认按钮禁用。
- **U4 一次确认**：「确认入库」按钮单击即提交（无复选框、无二次确认）；成功后抽屉显示「已确认，正在归档」；
  被拒绝时显示后端给出的原因（ConfirmError 文本），不改变任何状态。
- **U5 需处理**：失败的本地阶段（restore / asr / translate / return / scrape）显示失败原因与「重试」按钮
  （`retry_stage`）；归档失败显示原因，可重新选目标并确认（即 U3/U4）。所有影片可「打开文件夹」：
  INBOX 包目录、或成品目录（存在时），用资源管理器打开。
- **U6 安全**：所有新接口只在本机控制 API（loopback + 控制令牌）上；网络 API 不暴露（与 `/control/ffmpeg`
  同样的测试断言）。后端从不接受客户端给出的文件路径：封面、文件夹都按 film_id 由后端解析。

## 3. 后端（agent 控制 API）

`launcher.py` 在控制 app 创建时传入一个后绑定的 `workflow_provider: Callable[[], LocalWorkflowHost | None]`
（宿主在 announce_ready 之后才创建）。`create_control_app` 新增参数并挂载 `workflow_routes.py`：

- `GET  /control/workflow/films` → `{state, reason, films: [FilmRow]}`；FilmRow =
  `{film_id, name, stage, status, waiting, group, can_confirm, can_retry, retry_stage}`。数据：`list_jobs`
  （各状态 × 全部阶段）汇总每部影片的当前阶段 + `film_workflow` + host.status() 的 waiting。
- `GET  /control/workflow/films/{film_id}` → 详情：final manifest 条目（role/path/size/volume）、标记、
  NFO 解析出的 title / actors、input 成员清单（相对路径、大小）、归档/清理状态与逐成员结果。
- `GET  /control/workflow/films/{film_id}/poster` → final 的 poster 文件（image/jpeg）；无 → 404。
- `GET  /control/workflow/performers?q=` → 代理 Filmpaw `GET /api/performers`（FilmpawClient 新增
  `performers(q)`）；Filmpaw 不可用 → 503 `{detail}`。
- `GET  /control/workflow/performers/{id}/thumb` → 代理 Filmpaw 头像。
- `POST /control/workflow/films/{film_id}/confirm` `{performer_id}` → `{approval_id}`；ConfirmError → 409
  `{detail}`。
- `POST /control/workflow/films/{film_id}/retry` `{stage}` → `retry_stage`（只允许失败的本地阶段）。
- `POST /control/workflow/films/{film_id}/open` `{what: "inbox" | "output"}` → `os.startfile`（Windows）；
  路径由后端解析；不存在 → 404。

## 4. 前端

`views/MediaWorkflow.tsx`（列表 + 分组）、`components/FilmDrawer.tsx`（详情 + 选目标 + 确认 + 重试/打开），
`api.ts` 新增对应方法，i18n 中文/英文键。沿用 MUI + react-query（轮询 5 秒，确认后 invalidate）。

## 5. 测试

- 后端：`test_workflow_control_routes.py`：用 F02 的 e2e World（真实 runtime + 假 Filmpaw）跑到 scrape
  成功；断言列表分组与 can_confirm、详情含文件与 NFO 标题、poster 返回字节、performers 代理、confirm
  成功/拒绝 409、retry 只对失败本地阶段、网络 app 上这些路由 404、未启动时 state 提示。
- 前端：vitest + Testing Library：列表渲染分组与等待原因；抽屉内搜索候选、未选目标时按钮禁用、单击确认
  调用一次 API 并显示结果、拒绝原因显示；Filmpaw 不可用提示。

## 6. 不在 P1

设备页、远程节点、安装/迁移、批量入库、「跳过影片」（T01 无对应命令；保留不确认即可）、Mac 控制。

## 7. v2 修订（辩论第 1 轮 D01–D15，优先于上文）

- **D01（P1）图片**：控制 API 要求 Bearer，打包后 CSP 为 `img-src 'self' data:`。前端 `api.image(path)` 带令牌
  fetch，FileReader 转 `data:` URL 后给 `<img>`；不改 CSP。
- **D02（P1）失败原因来源**：本地阶段失败 = 该阶段最近一次 attempt 的 `evidence[-1].summary.note`；归档失败 =
  最近归档操作的 `error_code`；等待原因 = host.status() 的 waiting；正在运行 = `dispatch.current`。
- **D03 一次读取**：新增 T01 读 `film_board(auth, limit)`：一条 SQL 取所有影片的当前修订阶段状态 + 每个阶段
  最近 attempt 的说明 + `film_created_at`；列表路由只调它一次（再加 host.status()）。不再用 list_jobs × 7。
- **D04 分组**：待确认入库（scrape 成功、archive pending/failed 且无未结归档）/ 处理中 / 需处理（failed、
  blocked → 可重试；unknown → 不可重试，「等待确认停止」）/ 已确认待归档与清理 / 已完成（按创建时间最近 20 部，
  D13）。
- **D05 重试与确认路由**：`POST …/retry` `{stage}`：后端由 `get_film(op, film_id).stages`（当前修订）取 job，
  只允许 STAGE_ROLES 中 failed/blocked 的阶段，`StageRetry(job_id, expected_version, reason="operator")`；
  `cas_conflict`/`precondition_failed` → 409。新增 `host.retry(film_id, stage)`（唤醒 dispatcher）。确认路由调
  `host.confirm`；未启动 → 503。
- **D06 长阶段**：等待原因缺失时前端显示「排队中（当前：<片名>·<阶段>）」；确认后显示「已确认，等待归档」。
- **D07 生命周期**：路由只在 host.state == "running" 时读写；WorkflowError → 503；provider 为 None 时按
  `config.workflow` 给原因（未启用 / 未设 INBOX / 启动失败见日志）。
- **D08 Filmpaw 代理**：FilmpawClient 新增 `performers(q)`（`include_missing=false`）与 `thumb(id) -> bytes`
  （原始字节调用）；Filmpaw 404 → 404，不可用 → 503。
- **D09 打开文件夹**：经模块级 `open_folder(path)`（Windows `os.startfile`，其他平台 501），测试替换它。
- **D10 NFO**：复用 `manifest.parse_nfo_hint` 的加固读取，扩展出 `parse_nfo_summary` 返回 title/actors；解析
  失败返回空，不 500。测试替身：假 MDCX 写 `<title>`/`<actor><name>`；FakeFilmpaw 增加 `/api/performers` 与
  thumb。
- **D11** workcopy 清理后详情只显示状态与 NAS 目标，封面占位；poster 的 media type 按后缀。
- **D12** 名称：有 final 用成品目录名，否则 `display_name`。
- **D14** provider 类型为小协议（status / confirm / retry / runtime），测试可包装 F02 的 World。
- **D15** 确认按钮在请求进行中禁用。

辩论第 2 轮：无新 P1；N01（P2）与 M1–M3 在实现中落实：`film_board` 每部影片另带 `approval_active`
（当前上下文修订的未撤销审批）、`archive_open`（未结归档操作）、`archive_error`（最近归档操作的 error_code）；
`limit` 只作用于「已完成」组；图片按 film/performer 缓存（staleTime ∞）；INBOX 路径按包内相对路径显示。设计冻结。

## 8. 接口形状（冻结，前后端共用）

```text
GET  /control/workflow/films
  200 {state, reason, current: {film_id, film, stage, progress} | null,
       films: [{film_id, name, group, stage, status, note, can_confirm, can_retry, retry_stage, created_at}]}
  group ∈ "confirm" | "active" | "attention" | "archiving" | "done"
  state ≠ "running" 时 films 为 []，reason 给出原因
GET  /control/workflow/films/{film_id}
  200 {film_id, name, number, title, actors: [str], marks: {restored, chinese_subtitles, uhd},
       files: [{role, path, size, volume}], inputs: [{path, size}], workcopy_count, has_poster,
       group, can_confirm, can_retry, retry_stage, note,
       stages: [{stage, status, note}],
       archive: {status, error, target_path},
       cleanup: {input, workcopy, kept: [{path, status}]}}
  404 未知影片
GET  /control/workflow/films/{film_id}/poster        200 image/* | 404
GET  /control/workflow/performers?q=                  200 {items: [{id, name, source_label, unc_path, has_thumb, aliases: [str]}]} | 503 {detail}
GET  /control/workflow/performers/{id}/thumb          200 image/* | 404 | 503
POST /control/workflow/films/{film_id}/confirm  {performer_id}   200 {approval_id} | 409 {detail} | 503 {detail}
POST /control/workflow/films/{film_id}/retry    {stage}          200 {ok: true}    | 409 {detail} | 503 {detail}
POST /control/workflow/films/{film_id}/open     {what: "inbox" | "output"}   204 | 404 | 501
```
