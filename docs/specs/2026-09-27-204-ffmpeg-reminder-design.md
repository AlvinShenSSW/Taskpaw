# #204 — AV 翻译 FFmpeg: use WhisperJAV's bundled FFmpeg automatically, remind and give a setup script otherwise; version 3.9.4

Date: 2026-09-27 (design v4, FROZEN — debate rounds 1–4: F204-1…F204-16, N-1…N-8, N3-1…N3-5; round 4 CLEAN; M4-1 wording applied; owner chose "自动 + 提醒")
Issue: #204. Owner: "在添加jasna av字幕和独立翻译任务的时候，都应该有提醒要设置这个，并在某个地方给出脚本，这样用户可以运行不会漏掉"; and, when asked, "自动 + 提醒".
Driver: `/afk` — Claude leads; implementation Codex gpt-6-astra (high); outer gate Claude; final gate DeepSeek flash (afk-skills 1.2.3).
Merge when AFK merge-ready, then release 3.9.4.

## Spec review

How the three tools find FFmpeg:

- **WhisperJAV** finds it only through PATH (`audio_extraction.py`: `shutil.which("ffmpeg")` → "FFmpeg not found").
  - Its install ships `<root>\Library\bin\ffmpeg.exe`, where `whisperjav.exe` lives in `<root>\Scripts`.
  - Its own GUI adds `Library\bin` to PATH before it runs.
  - The installer's default root is `%LOCALAPPDATA%\WhisperJAV`; the owner used `C:\WhisperJAV`.
- **Jasna and Lada** call their own bundled copies.

How TaskPaw starts it:

- TaskPaw launches WhisperJAV with its own environment: `os.environ` minus `TASKPAW_LLM_*` (`subs/child.py`, `asr_env`).
- V3 has **no tray icon**. Closing the window exits the whole app (`src-tauri/src/main.rs`).
- Add and edit both render `MonitorWizard` step 2 → `SchemaForm`. `SchemaForm` has no `onChange` today.

## Frozen issue contract

### AC1 Automatic: WhisperJAV's bundled FFmpeg (owner decision F204-16)

When avsubs or Jasna launches WhisperJAV, the child's environment is built by `taskpaw_v3/monitors/subs/child.py` `asr_env(base=None, whisperjav_exe="")` (N-7). This is NOT `core.llm.without_llm_env`, which the llm-worker also uses. Both plugins' `_default_spawn(argv)` pass `argv[0]` as `whisperjav_exe`.

The bundled `ffmpeg.exe` is located by ONE shared helper, `bundled_ffmpeg(exe) -> str | None` (N-6), used by both AC1 and AC2. It:

- strips the path;
- rejects a path that is not drive-absolute, or that contains a control character, `;` or `"`;
- applies `normpath`, then `dirname(exe)\..\Library\bin\ffmpeg.exe`;
- returns the path only when `os.path.isfile` is true.

The environment handles FFmpeg like this:

- **Condition.** It acts only when `shutil.which("ffmpeg", path=<child PATH>)` is None AND `<bundled dir>\ffmpeg.exe` exists.
- **What it does.** It APPENDS the bundled dir to the child's PATH (N-7):
  - The PATH key is looked up case-insensitively and its value replaced in place, so there is never a second `PATH`/`Path` key. With no PATH key at all, `PATH` is added.
  - A trailing `;` is stripped first.
- **Bundled dir.** This is `<dirname(whisperjav_exe)>\..\Library\bin`, normalised. The configured `whisperjav.exe` must be drive-absolute (`^[A-Za-z]:[\\/]`).
- **Scope.** Only that child's environment changes. The agent's own environment and the system PATH are never modified.
- **Not a no-op.** An ffmpeg already on PATH keeps priority; nothing else changes.
- **Logging.** It logs at INFO once per process per folder (a module-level set behind a lock, N-8): `whisperjav: using the bundled FFmpeg (<dir>)`.
- **Per-plugin test (N-7b).** One test per plugin checks that `_default_spawn`'s env contains the folder. A builder-only test cannot catch a plugin that fails to pass `argv[0]`.
- **Tests:**
  - it is added when missing;
  - it is not added when ffmpeg is already on PATH;
  - it is not added when the bundled file is absent;
  - it is not added for a non-drive-absolute or UNC exe path;
  - the agent's `os.environ` is untouched.

### AC2 Status check

A new module, `taskpaw_v3/monitors/subs/ffmpeg.py`, provides `ffmpeg_status(whisperjav_exe: str = "") -> dict`. It never raises, has no side effects, and imports `winreg` lazily.

- **Input safety (F204-7).** `whisperjav_exe` is used only when it is drive-absolute (`^[A-Za-z]:[\\/]`). UNC (`\\`, `//`, `\\?\`), relative paths and control characters are ignored. Paths are normalised with `os.path.normpath` only, never `resolve()`.
- **Fields:**
  - `on_path`: `shutil.which("ffmpeg")` in the agent's environment, or null.
  - `bundled`: the bundled `ffmpeg.exe` path for the configured `whisperjav.exe` (AC1), if the file exists, else null.
  - `effective`: `on_path`, else `bundled`, else null. This is the one WhisperJAV will use.
  - `saved_path_ok` (Windows; else null, F204-4/F204-5): `shutil.which("ffmpeg", path=<HKLM Path + HKCU Path>)`. Each value is expanded only when it is REG_EXPAND_SZ, using `winreg.ExpandEnvironmentStrings` per entry (N-4: `ntpath.expandvars` skips text after a `'`). Empty and `"quoted"` entries are cleaned. This answers "would a freshly launched TaskPaw find ffmpeg on PATH".
  - `bundled` is `bundled_ffmpeg(whisperjav_exe)`, the SAME helper as AC1 (N-6), so the reminder and the runtime always agree.
  - `exe_ok`: `bundled_ffmpeg_dir(whisperjav_exe) is not None` (N3-5).
    - `bundled_ffmpeg_dir(exe)` applies the input rules and derives the folder, without checking that the file exists.
    - `bundled_ffmpeg(exe)` adds the `isfile` check. `candidates[0]` uses the dir helper.
  - `pending_restart`: `effective is None and saved_path_ok is not None`.
  - `candidates`: the fallback folders, deduplicated, in order, each with `exists` (the folder contains `ffmpeg.exe`):
    1. the bundled dir (when derivable);
    2. `%LOCALAPPDATA%\WhisperJAV\Library\bin` (F204-6);
    3. `C:\WhisperJAV\Library\bin`;
    4. `C:\Jasna\tools`;
    5. `C:\Lada\_internal\bin`.
  - `platform`: `"windows"` or `"other"`.
  - `error`: false, or true when the check itself failed internally (F204-10).

### AC3 Setup script (fallback only)

`setup_script(extra_dirs=()) -> str` returns ONE PowerShell script, safe to paste into a normal PowerShell 5.1 window.

- **Structure (F204-3).** The whole script is a single `& { … }` block.
  - It branches with `if` / `elseif` / `else`, with `} else {` on the same line.
  - No `exit`, no tabs, spaces only.
  - It prints `$env:USERNAME` (the account being changed, F204-15).
- **Check the SAVED PATH (F204-4, M4-1).** It tests the saved PATH entries for `ffmpeg.exe`, not the current window's PATH: the user PATH comes from the same `$key` it writes (`$key.GetValue('Path','')`), and the machine PATH from ONE expression, `[Environment]::GetEnvironmentVariable('Path','Machine')`, which the test hook swaps (see AC8).
  - It always uses `Test-Path -LiteralPath` (N-5: `[` `]` are wildcards otherwise).
  - It skips empty entries and strips `"` before testing and comparing.
  - Already there → print 「已找到 FFmpeg：<path>」.
  - Else it takes the first candidate folder that contains `ffmpeg.exe`, in this order: extra_dirs, then `$env:LOCALAPPDATA\WhisperJAV\Library\bin`, then the fixed candidates.
  - It appends that folder to the **user** PATH, only if the folder isn't already there (compared case-insensitively, expanded, ignoring a trailing `\`).
  - If no candidate has `ffmpeg.exe` → print the `winget install Gyan.FFmpeg` hint. Never run it.
- **Writing PATH without breaking it (F204-2).**
  - Read the raw value via `Microsoft.Win32.Registry`: `HKCU\Environment` → `GetValue('Path', '', 'DoNotExpandEnvironmentNames')`.
  - Write it back with `SetValue('Path', $new, 'ExpandString')`, so `%USERPROFILE%`-style entries survive.
  - Then broadcast the change with `[Environment]::SetEnvironmentVariable('TASKPAW_PATH_REFRESH', $null, 'User')`, so a Start-menu relaunch sees it.
  - Never use `setx`. Never use `SetEnvironmentVariable('Path', …)`.
- **Literals (F204-8).** `extra_dirs` are emitted as single-quoted PowerShell literals, with `'` and the typographic quotes U+2018–U+201B doubled. A dir with a control character is dropped.
- **Ending (F204-1).** The script always ends with 「完成后请关闭 TaskPaw 窗口（会完全退出），再从开始菜单重新打开」. There is no tray wording anywhere.
- No admin rights and no ExecutionPolicy change are needed; pasting into a console needs neither.

### AC4 API

`GET /control/ffmpeg?whisperjav=<path>` is served on the loopback control app only.

- It returns `ffmpeg_status(...)` plus `script`: `setup_script([bundled dir])` on Windows, null on other platforms.
- The parameter is optional, and input is filtered as in AC2.
- It is read-only and never returns 5xx; on an internal failure it answers 200 with `error: true`.

### AC5 UI reminder (`FfmpegReminder`)

- **Where.** `MonitorWizard` step 2 renders it **above the form's submit button** (F204-9).
  - rjsf's `Form` REPLACES its submit button with `children` (N-1: `Form.js:661`), so passing the reminder as the Form's children would remove Save/Review.
  - Instead, `SchemaForm` passes the reminder to a custom `ButtonTemplates.SubmitButton`. That template renders the reminder first, then the theme's default submit button (`Templates.ButtonTemplates.SubmitButton` from `@rjsf/mui`), with its text from `getSubmitButtonOptions(uiSchema)`.
  - **Channel (N3-1).** The reminder reaches the template ONLY through a React context. `SchemaForm` renders a Provider around `<Form>`, and a **module-level**, stable `SubmitButton` template reads it with `useContext`. It must NEVER go through `formContext` or any other non-function Form prop, and never through a template recreated per render: rjsf deep-compares Form props and rebuilds its state from `props.formData`. A critic experiment showed that typed edits revert and the OLD value gets saved.
  - **Required UI test.** In edit mode, type a new `whisperjav_exe_path`, wait past the debounce and the status update, then submit. The NEW value must be saved.
  - The copy button is `type="button"`. Tests: the submit button is still present and still submits; clicking 「复制脚本」 does NOT submit.
  - `typeId == "avsubs"`: always.
  - `typeId == "jasna"`: only while the live `av_translate` is true.
  - `SchemaForm` gains an optional `onChange` passthrough. The wizard seeds its live data from the form's input `formData` (edit mode: `existingConfig`) and resets it when the service changes (F204-13).
- **Query.** `api.ffmpeg(<live whisperjav_exe_path>)`, debounced by about 400 ms. The newest response wins (F204-12).
- **States:**

| State | Severity / role | Content |
|---|---|---|
| `effective` = `on_path` | success, `role="status"` | 「已找到 FFmpeg：<path>」 |
| `effective` = `bundled` | success, `role="status"` | 「将自动使用 WhisperJAV 自带的 FFmpeg：<path>」 |
| `pending_restart` | info, `role="status"` | 「FFmpeg 已加入 PATH，关闭 TaskPaw 窗口并从开始菜单重新打开后生效（仍提示则注销并重新登录 Windows）」 |
| Windows only: `exe_ok` false (blank or invalid live exe), and `on_path` null (N-2, N3-4, N3-5) | neutral, `role="status"` | 「填写 whisperjav.exe 的完整路径后，TaskPaw 会自动使用它自带的 FFmpeg」; no script |
| none, Windows | warning, `role="alert"` | see below |
| none, other platform (F204-11) | warning | status text only: no script, no copy button |
| `error` / request failed (F204-10) | neutral | 「无法检查 FFmpeg」 plus a pointer to the README 「FFmpeg」 section; no client-side copy of the script |

  - **Row precedence (N3-3).** Rows are evaluated in this order and the first match wins: `error` → `effective` = `on_path` → `effective` = `bundled` → `pending_restart` → the Windows neutral row → the Windows warning → the other-platform warning. On other platforms a null `on_path` goes straight to the other-platform warning; the neutral row never appears there (N3-4).
  - **The UI never re-implements the exe rules (N3-5).** It branches on the API's `exe_ok` (the verdict of `bundled_ffmpeg_dir`'s input rules), not on its own check.
  - **The Windows warning** appears ONLY when `exe_ok` is true and neither `on_path` nor `bundled` exists (N-2). It says:
    - AV 翻译 recognition (WhisperJAV) needs FFmpeg, and none was found: not on PATH, and not next to the configured `whisperjav.exe`.
    - It lists any candidate folder that has `ffmpeg.exe` (「已在 … 找到 ffmpeg.exe，脚本会把它加入 PATH」).
    - It shows the script in a monospace, scrollable, read-only block, with a 「复制脚本」 button (≥ 40 px, labelled; confirms 「已复制」; a rejected clipboard write shows 「复制失败，请手动选中复制」).
    - The instruction: 「粘贴到普通 PowerShell 窗口运行（不要另存为 .ps1）；只修改当前 Windows 用户的设置」 (F204-15).
- Paths wrap (`overflowWrap: anywhere`); there is no page-level horizontal scroll at 375 px. zh + en strings.

### AC6 Docs

- README gets a new heading **「FFmpeg（AV 翻译识别需要）」** under the AV 翻译 bullet list (around :53-62). It says:
  - TaskPaw uses WhisperJAV's bundled FFmpeg automatically;
  - when a manual setup is needed, how to run the generic script (`setup_script()`, no extra dirs) in a code block, pasted into a normal PowerShell window.
- A test pins the README block to the generator output: it takes the FIRST fenced block under the new 「FFmpeg（AV 翻译识别需要）」 heading, reads it with `read_text` (LF newlines), and compares it with the LF-only generator output (F204-14).
- A one-line note goes in `design-system/taskpaw-v3/pages/agent-console.md`.

### AC7 Unchanged

Scheduling, settlement, publishing, translation and status payloads are unchanged. The only runtime behaviour change is AC1: the WhisperJAV child's PATH.

### AC8 Version and tests

- 3.9.4 in the six version files; CHANGELOG 3.9.4 (Chinese).
- **AC1 tests:** as listed in AC1.
- **Status tests:**
  - found on PATH;
  - bundled only;
  - `saved_path_ok` / `pending_restart` with the registry mocked, including REG_EXPAND_SZ values with `%VARS%` and a machine-PATH-only folder;
  - not found;
  - candidates and dedupe, including `%LOCALAPPDATA%`;
  - UNC / relative / control-character input ignored;
  - never raises;
  - non-Windows.
- **Script tests:**
  - starts with `& {`;
  - no tab, no `exit`, no `setx`, no `SetEnvironmentVariable('Path'`;
  - contains `DoNotExpandEnvironmentNames`, `ExpandString` and the refresh broadcast;
  - an O'Brien and a ’-quote path parse cleanly (escaped);
  - no admin or ExecutionPolicy text;
  - the ending text;
  - if a PowerShell is available in CI/Windows, `[System.Management.Automation.Language.Parser]::ParseInput` reports 0 errors;
  - **EXECUTED (N-3; Windows-only, skipped without `powershell`):**
    - **Script reads, fixed by N3-2.** For its "already there" check, the script reads the user PATH from the SAME `$key` it writes (`$key.GetValue('Path','')`, which expands REG_EXPAND_SZ). It reads the machine PATH through ONE separate expression.
    - Setup: the generator has a private test hook that swaps the `HKCU\Environment` `$key` for a private app-hive key (`RegLoadAppKey`), replaces the machine-PATH expression with `''`, and disables the broadcast. The test therefore never depends on the real machine's saved PATH. The hive starts with an ExpandString `Path` containing `%USERPROFILE%\x`, plus a candidate folder holding a dummy `ffmpeg.exe`.
    - Run the script TWICE and assert: the value kind stays ExpandString; `%USERPROFILE%` survives unexpanded; the folder is added exactly once; the second run changes nothing.
    - The real user PATH and registry are never touched.
- **API tests:** shape; `error` flag; control-app only; blank or UNC parameter.
- **UI tests:**
  - shown for avsubs, and for Jasna only with AV on, including edit with AV on and no interaction;
  - hidden for other types;
  - placed above the submit button;
  - each state in zh and en;
  - roles;
  - copy success and failure (mocked);
  - debounce and newest-wins;
  - other platform: no script;
  - error state.
- **Docs test:** the README matches the generator.

## Assumptions

- **A1** The fallback candidate folders cover the owner's installs and WhisperJAV's default. Other layouts get the winget hint.
- **A2** The script edits the current user's PATH only; the UI and README say so, and the script prints the user name.
- **A3** Automatic use (AC1) needs a WhisperJAV install that has `Library\bin\ffmpeg.exe` next to its `Scripts\whisperjav.exe`, which is the installer's layout.
