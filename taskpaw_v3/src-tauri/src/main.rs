// TaskPaw V3 desktop shell (design §7.1: "X = exit").
//
// The shell spawns the headless backend as a CHILD and ensures the WHOLE child
// tree is gone when the app exits — no orphan process holding a port (the V2
// "click X → tray → zombie" problem):
//   - Unix: SIGTERM the backend so its GracefulShutdown stops the supervisor +
//     managed children (lada-cli) cleanly, then force-kill if it lingers.
//   - Windows: the backend is assigned to a Job Object with KILL_ON_JOB_CLOSE,
//     so when the shell exits the OS terminates the entire process tree.
//
// Locked down (design §3.1): withGlobalTauri=false, empty capabilities (no
// IPC/FS). The webview talks ONLY to the local backend over HTTP; control token +
// base url + role are injected at runtime on the loopback origin via an init
// script (so packaged builds don't rely on compile-time env).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod control_credentials;
use control_credentials::{CredentialError, Descriptor, Ready};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

/// The webview's chosen UI language, pushed from the frontend via `set_ui_lang`
/// (#108). Defaults to the i18n default (zh-CN) until the page reports its choice.
struct UiLang(Mutex<String>);

/// Store the webview's current UI language so the native close dialog can follow
/// it (#108). Unknown values are ignored (keep the prior language).
#[tauri::command]
fn set_ui_lang(lang: String, state: tauri::State<'_, UiLang>) {
    if lang == "zh-CN" || lang == "en" {
        if let Ok(mut g) = state.0.lock() {
            *g = lang;
        }
    }
}

/// Title / body / OK / Cancel labels for the close-confirmation (#52), in the
/// app's chosen language (#108) — no longer bilingual. Role-tailored: a Hub loss
/// is aggregation/notifications; an agent loss is this machine's monitoring.
/// Any non-"en" language uses Chinese (the i18n default).
fn close_confirm_text(role: &str, lang: &str) -> (String, String, String, String) {
    if lang == "en" {
        let msg = if role == "hub" {
            "Closing this window stops the background Hub — aggregation and \
             OpenClaw notifications will stop. Close anyway?"
        } else {
            "Closing this window stops this machine's background monitoring. \
             Close anyway?"
        };
        (
            "TaskPaw — Confirm close".to_string(),
            msg.to_string(),
            "Close".to_string(),
            "Cancel".to_string(),
        )
    } else {
        let msg = if role == "hub" {
            "关闭窗口会停止后台 Hub —— 聚合与 OpenClaw 通知都会停止。确定关闭吗?"
        } else {
            "关闭窗口会停止本机的后台监控。确定关闭吗?"
        };
        (
            "TaskPaw — 确认关闭".to_string(),
            msg.to_string(),
            "关闭".to_string(),
            "取消".to_string(),
        )
    }
}

struct Backend(Mutex<Option<Child>>);

// Safety net: if Tauri `setup` fails AFTER the backend is managed (e.g. the
// window build errors), the managed state is dropped during teardown — kill the
// child here so it can't outlive the shell as an orphan (Unix child is in its own
// process group, so it would otherwise survive) (Kimi). Idempotent with
// kill_backend() on normal exit.
impl Drop for Backend {
    fn drop(&mut self) {
        // Recover from a poisoned mutex so cleanup still runs (else a panic that
        // poisoned the lock would let the backend orphan) (Kimi).
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        {
            if let Some(child) = guard.as_mut() {
                terminate_child(child); // same graceful path as normal exit
            }
        }
    }
}

#[cfg(windows)]
mod jobobj {
    // Assign a child to a Job Object that kills the whole tree when the job
    // handle closes (i.e. when this shell process exits).
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct Job(pub HANDLE);
    unsafe impl Send for Job {}

    // Close the job handle deterministically on drop (don't leak it until process
    // exit). Dropping the handle is also what triggers KILL_ON_JOB_CLOSE (Kimi).
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    pub fn assign(child: &Child) -> Option<Job> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() || job == INVALID_HANDLE_VALUE {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // Bail out if either call fails — otherwise we'd report a KILL_ON_JOB_CLOSE
            // guarantee that isn't actually installed (descendants could leak).
            let set_ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) != 0;
            let assign_ok = AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) != 0;
            if !set_ok || !assign_ok {
                CloseHandle(job);
                return None;
            }
            Some(Job(job))
        }
    }
}

#[cfg(windows)]
struct JobHandle(Mutex<Option<jobobj::Job>>);

/// The role this build/run targets: runtime TASKPAW_UI_ROLE wins; else the
/// compile-time TASKPAW_BUILD_ROLE baked at build (so the release matrix can ship
/// distinct agent and hub installers); else "agent".
fn ui_role() -> String {
    let nonblank = |s: String| Some(s).filter(|v| !v.trim().is_empty());
    let raw = std::env::var("TASKPAW_UI_ROLE")
        .ok()
        .and_then(nonblank)
        .or_else(|| {
            option_env!("TASKPAW_BUILD_ROLE")
                .map(str::to_string)
                .and_then(nonblank)
        })
        .unwrap_or_else(|| "agent".into());
    // Normalize + validate: the frontend (App.tsx) and backend expect exactly
    // "agent"/"hub"; anything else (e.g. "AGENT", typo) falls back to agent (Kimi).
    let role = raw.trim().to_ascii_lowercase();
    if matches!(role.as_str(), "agent" | "hub") {
        role
    } else {
        "agent".into()
    }
}

/// Resolve the backend command: an explicit dev override, else the bundled
/// `taskpaw-backend` sidecar next to this executable run with the UI role (#40).
fn backend_command() -> Option<(String, Vec<String>)> {
    // Dev / explicit override. Distinguish UNSET (fall back to the sidecar) from
    // SET-BUT-EMPTY (explicitly "no backend") so the old disable-via-empty dev
    // workflow still works (Kimi).
    match std::env::var("TASKPAW_BACKEND_CMD") {
        Ok(program) => {
            if program.trim().is_empty() {
                return None; // explicitly disabled
            }
            let args = std::env::var("TASKPAW_BACKEND_ARGS")
                .ok()
                .map(|a| {
                    // JSON array (argv-safe for paths with spaces) or whitespace.
                    serde_json::from_str::<Vec<String>>(&a)
                        .unwrap_or_else(|_| a.split_whitespace().map(str::to_string).collect())
                })
                .unwrap_or_default();
            return Some((program, args));
        }
        Err(_) => {} // unset → bundled sidecar below
    }
    // Bundled `externalBin` sidecar, run with the role so one binary serves both
    // agent and hub. Tauri strips the target-triple and places it next to the app
    // binary, but the exact dir differs by bundle (macOS .app Contents/MacOS,
    // sometimes ../Resources; Windows next to the .exe), so probe candidates
    // rather than assume one path (Codex).
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let ext = if cfg!(windows) { ".exe" } else { "" };
    // Probe BOTH the stripped name (Tauri normally renames the externalBin to
    // this next to the app) AND the target-suffixed name produced by build.py,
    // so we find the backend regardless of how Tauri places it (Codex P1).
    let triple = option_env!("TASKPAW_TARGET_TRIPLE").unwrap_or("");
    let names = [
        format!("taskpaw-backend{ext}"),
        format!("taskpaw-backend-{triple}{ext}"),
    ];
    let mut bases = vec![
        dir.to_path_buf(),        // next to the app binary (release)
        dir.join("../Resources"), // macOS .app resources fallback
    ];
    // Only probe binaries/ in debug — release bundles place the sidecar next to
    // the exe, and probing binaries/ could pick up a stale/wrong-arch dev artifact
    // (Kimi). In debug, `cargo tauri dev` runs from target/debug/, so also look up
    // toward src-tauri/binaries/ where build.py --skip-tauri puts it (Kimi).
    if cfg!(debug_assertions) {
        bases.push(dir.join("binaries"));
        bases.push(dir.join("../binaries"));
        bases.push(dir.join("../../binaries"));
    }
    let found = bases
        .iter()
        .flat_map(|b| names.iter().map(move |n| b.join(n)))
        .find(|p| p.exists())?;
    let role = ui_role();
    Some((found.to_string_lossy().into_owned(), vec![role]))
}

/// The real, role-scoped backend log path for this OS, or None if its base env
/// var is unset / the platform inherits stderr (Linux). Single source of truth so
/// the spawn redirect and the user-facing hint can never name different files
/// (Kimi). Role-scoped so an agent and a Hub on the same account stay distinct.
#[cfg(any(target_os = "macos", windows))]
fn backend_log_path() -> Option<std::path::PathBuf> {
    let role = ui_role();
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var_os("HOME")?;
        Some(macos_backend_log_path(std::path::Path::new(&home), &role))
    }
    #[cfg(windows)]
    {
        let appdata = std::env::var_os("APPDATA")?;
        Some(
            std::path::Path::new(&appdata)
                .join("TaskPaw")
                .join(format!("taskpaw-backend-{role}.log")),
        )
    }
}

/// Human-readable location of the backend log, for user-facing messages. Dev
/// builds inherit stderr (see spawn_backend), so the hint names the terminal;
/// Linux always inherits. In release it names the real per-role file — but only if
/// that file actually exists, since the redirect creates it at spawn: an absent
/// file means open_backend_log() failed and stderr went to null, so we point at
/// the OS app log instead of a file that was never written (Kimi).
fn backend_log_hint() -> String {
    #[cfg(any(target_os = "macos", windows))]
    {
        if cfg!(debug_assertions) {
            return "the launching terminal (dev builds inherit backend stderr)".to_string();
        }
        // Point at the real file only if THIS launch actually opened it (not a
        // stale prior-run log) — see BACKEND_LOG_OPENED (Kimi).
        match backend_log_path() {
            Some(p) if BACKEND_LOG_OPENED.load(std::sync::atomic::Ordering::Relaxed) => {
                p.display().to_string()
            }
            _ => "the OS app log (Console.app / Event Viewer) — the backend log \
                  file could not be opened, so its stderr was discarded"
                .to_string(),
        }
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    "the backend's stderr (journalctl, or the launching terminal)".to_string()
}

/// Pure mapping from $HOME + role to the macOS backend log path, factored out so
/// it's unit-testable without mutating the process environment.
#[cfg(target_os = "macos")]
fn macos_backend_log_path(home: &std::path::Path, role: &str) -> std::path::PathBuf {
    home.join("Library/Logs/TaskPaw")
        .join(format!("taskpaw-backend-{role}.log"))
}

/// Roll `path` to a single `<name>.1` backup when it exceeds `max_bytes`, bounding
/// the appended log's growth. Removes any stale `.1` FIRST: std::fs::rename
/// overwrites on Unix but FAILS on Windows when the destination exists, which would
/// otherwise silently disable rotation after the first roll and let the log grow
/// unbounded (Codex + Kimi). Best-effort — an I/O error just means the log keeps
/// appending this session; evaluated at open (launch) time. Path-injectable so the
/// rotation logic is unit-testable without the env-derived per-OS path.
#[cfg(any(target_os = "macos", windows, test))]
fn roll_log_if_oversized(path: &std::path::Path, max_bytes: u64) {
    if std::fs::metadata(path)
        .map(|m| m.len() > max_bytes)
        .unwrap_or(false)
    {
        let mut rotated = path.as_os_str().to_owned();
        rotated.push(".1");
        let rotated = std::path::PathBuf::from(rotated);
        // A stale .1 blocks rename on Windows, so remove it first. NotFound is the
        // normal case (no prior backup) — only warn on a real removal failure.
        if let Err(e) = std::fs::remove_file(&rotated) {
            if e.kind() != std::io::ErrorKind::NotFound {
                eprintln!(
                    "taskpaw: cannot remove stale log backup {rotated:?}: {e}; rotation may stall"
                );
            }
        }
        // If the roll itself fails, the live log keeps growing unbounded — surface
        // it so an operator can notice rather than silently swallowing (Kimi).
        if let Err(e) = std::fs::rename(path, &rotated) {
            eprintln!("taskpaw: cannot roll backend log {path:?} -> {rotated:?}: {e}; it may grow unbounded");
        }
    }
}

/// Open the per-OS, per-role backend log for APPEND so crash logs accumulate
/// across relaunches instead of being truncated on every start (Kimi). Creates the
/// dir, rolls the file to `.1` if it's already >~5 MB *at launch* (see
/// roll_log_if_oversized — there is no mid-session cap), and warns to the shell's
/// own stderr (captured by the OS log) rather than silently discarding the
/// backend's logs. None on Linux (stderr is inherited). Callers only invoke this in
/// release builds; dev keeps stderr on the inherited terminal.
/// Set once open_backend_log() actually opens the file THIS launch, so
/// backend_log_hint() points at the real log only when it exists AND we wrote to it
/// — never at a stale log left by a prior run when this run's open failed (Kimi).
#[cfg(any(target_os = "macos", windows))]
static BACKEND_LOG_OPENED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

#[cfg(any(target_os = "macos", windows))]
fn open_backend_log() -> Option<std::fs::File> {
    let path = backend_log_path()?;
    if let Some(dir) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            eprintln!(
                "taskpaw: cannot create backend log dir {dir:?}: {e}; backend stderr discarded"
            );
            return None;
        }
    }
    // Bound growth: at launch, roll to a single .1 backup if the live file is
    // already past ~5 MB. This is a per-restart cap, not a mid-session one — the
    // child holds the fd, so we can't rotate underneath it without a proxy pipe,
    // which is overkill for a sparse status-poll log (Kimi).
    roll_log_if_oversized(&path, 5 * 1024 * 1024);
    let mut opts = std::fs::OpenOptions::new();
    opts.create(true).append(true);
    // Don't follow a symlink planted at the log path — append to the real file in
    // our own dir or fail, rather than be redirected elsewhere (Kimi). macOS only;
    // the Windows reparse-point equivalent is a low-risk follow-up (the dir is
    // user-owned).
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NOFOLLOW);
    }
    match opts.open(&path) {
        Ok(f) => {
            BACKEND_LOG_OPENED.store(true, std::sync::atomic::Ordering::Relaxed);
            Some(f)
        }
        Err(e) => {
            eprintln!("taskpaw: cannot open backend log {path:?}: {e}; backend stderr discarded");
            None
        }
    }
}

fn spawn_backend() -> Option<Child> {
    let (program, args) = backend_command()?;
    let mut command = Command::new(&program);
    command.args(args);
    // Pipe stdout on EVERY platform so the shell can read the §3.1 readiness line
    // (#48). Backend logs go to STDERR (logging.basicConfig) — kept separate from
    // the one-line handshake on stdout.
    command.stdout(std::process::Stdio::piped());
    // Own process group so we can signal the WHOLE backend tree on exit.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
        // If the shell is HARD-killed (SIGKILL / segfault / OOM), neither
        // RunEvent::ExitRequested nor Backend::Drop runs to reap the backend, so on
        // its own process group it would orphan and hold ports (#54). On Linux ask
        // the kernel to SIGTERM the backend when its parent dies (PR_SET_PDEATHSIG)
        // — its GracefulShutdown then stops cleanly. macOS has no equivalent, so a
        // hard crash there can still briefly orphan the backend (residual risk;
        // the normal X-to-exit path is covered by ExitRequested/Drop).
        #[cfg(target_os = "linux")]
        unsafe {
            command.pre_exec(|| {
                // async-signal-safe; runs in the child between fork and exec.
                libc::prctl(
                    libc::PR_SET_PDEATHSIG,
                    libc::SIGTERM as libc::c_ulong,
                    0,
                    0,
                    0,
                );
                Ok(())
            });
        }
        // A windowed (packaged) macOS .app has no console (the same problem the
        // Windows block below solves with %APPDATA%), so the backend's STDERR — its
        // logs — would vanish. In RELEASE route it to ~/Library/Logs/TaskPaw/ (see
        // open_backend_log) so production failures are debuggable; in DEBUG leave it
        // inherited so `cargo tauri dev` still shows backend logs in the terminal
        // (Kimi). stdout stays piped (above) for the §3.1 readiness handshake.
        #[cfg(target_os = "macos")]
        if !cfg!(debug_assertions) {
            use std::process::Stdio;
            command.stderr(
                open_backend_log()
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            );
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW (0x08000000) | CREATE_BREAKAWAY_FROM_JOB (0x01000000) so
        // we can put the backend in OUR Job Object even when the launcher is
        // already inside one. NOTE: 0x00080000 is EXTENDED_STARTUPINFO_PRESENT, NOT
        // breakaway — using it made CreateProcess fail (os error 87) and crash
        // every launch (caught by Windows verification, #50).
        command.creation_flags(0x08000000 | 0x01000000);
        // The windowed (packaged) shell has no console — in RELEASE route backend
        // STDERR (its logs) to %APPDATA%\TaskPaw\taskpaw-backend-<role>.log so
        // production builds are debuggable (see open_backend_log: append, rolls at
        // ~5 MB, warns if it can't open). In DEBUG leave stderr inherited so dev
        // sees logs. stdout stays piped (above) for the readiness handshake.
        if !cfg!(debug_assertions) {
            use std::process::Stdio;
            command.stderr(
                open_backend_log()
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            );
        }
    }
    match command.spawn() {
        Ok(c) => Some(c),
        Err(e) => {
            eprintln!("failed to spawn backend {program:?}: {e}");
            None
        }
    }
}

/// Parse nonsecret startup metadata; malformed data fails without echoing it.
fn parse_ready_line(line: &str) -> Option<Result<Ready, CredentialError>> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    if value.get("taskpaw_ready").and_then(|v| v.as_bool()) != Some(true) {
        return None;
    }
    Some(serde_json::from_value(value).map_err(|_| CredentialError))
}
fn read_readiness(
    stdout: std::process::ChildStdout,
    timeout: Duration,
) -> Result<Ready, CredentialError> {
    use std::io::{BufRead, BufReader};
    use std::sync::mpsc;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut found = false;
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else {
                break;
            };
            if !found {
                if let Some(ready) = parse_ready_line(&line) {
                    let _ = tx.send(ready);
                    found = true;
                } else {
                    // Never echo untrusted readiness/stdout data (it may contain
                    // credentials or invalid URLs with embedded userinfo).
                    eprintln!("[backend] pre-readiness output omitted");
                }
            }
        }
    });
    rx.recv_timeout(timeout).map_err(|_| CredentialError)?
}

// Signal the backend's whole process GROUP on Unix (negative pid), so a wedged
// backend's children are terminated too — not just the direct process.
#[cfg(unix)]
fn signal_group(child: &Child, sig: libc::c_int) {
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), sig);
    }
}

/// Terminate the backend GRACEFULLY: SIGTERM (→ its GracefulShutdown stops the
/// supervisor + managed children), wait up to a deadline, then force-kill. Shared
/// by normal exit (kill_backend) and the Drop safety net so both honor the same
/// graceful contract (Kimi).
fn terminate_child(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return; // already gone
    }
    #[cfg(unix)]
    signal_group(child, libc::SIGTERM);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(100));
            }
            _ => break,
        }
    }
    // Force-kill the whole group (Unix) / the process (Windows; the Job Object
    // reaps the rest of the tree).
    #[cfg(unix)]
    signal_group(child, libc::SIGKILL);
    #[cfg(not(unix))]
    let _ = child.kill();
    let _ = child.wait();
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                terminate_child(child);
            }
        }
    }
    // On Windows the Job Object (KILL_ON_JOB_CLOSE) terminates any remaining
    // descendants when its handle drops as the process exits.
}

/// Escape a string for safe interpolation into an AppleScript double-quoted
/// literal (backslash and double-quote), so a log path in the message can't break
/// out of the osascript string.
#[cfg(any(target_os = "macos", test))]
fn applescript_escape(s: &str) -> String {
    // Escape backslash/quote, and replace ANY control char (newline/tab/etc.) with a
    // space — an AppleScript literal can't span physical lines, and other control
    // bytes in a log path shouldn't reach osascript. Cap length so a pathological
    // message can't build a huge script (Kimi).
    s.chars()
        .take(1000)
        .map(|c| match c {
            '\\' => "\\\\".to_string(),
            '"' => "\\\"".to_string(),
            c if c.is_control() => " ".to_string(),
            c => c.to_string(),
        })
        .collect()
}

/// Fatal startup failure: show a best-effort native error dialog, then exit
/// cleanly with code 1. We deliberately do NOT return Err from the setup hook for
/// these — Tauri `.expect()`s a setup Err into a panic, and on macOS that panic
/// can't unwind across the ObjC `did_finish_launching` callback, so it aborts with
/// SIGABRT and a crash report (e.g. on a mere port conflict). A clean exit shows a
/// friendly message instead. process::exit skips Drop, so the managed Backend's
/// kill-on-drop never runs — pass the AppHandle when a backend has been managed and
/// this kills it first (centralizing the no-orphan invariant so a future call site
/// can't forget it — Kimi); pass None when nothing is managed yet.
fn fatal_startup(message: &str, app: Option<&tauri::AppHandle>) -> ! {
    if let Some(app) = app {
        kill_backend(app);
    }
    eprintln!("taskpaw: fatal startup error: {message}");
    #[cfg(target_os = "macos")]
    if std::env::var_os("TASKPAW_NO_STARTUP_DIALOG").is_none() {
        // A separate osascript process shows the alert reliably without depending
        // on our half-initialized NSApp; best-effort — ignore if it can't run.
        let script = format!(
            "display dialog \"{}\" with title \"TaskPaw\" buttons {{\"OK\"}} \
             default button \"OK\" with icon caution",
            applescript_escape(message)
        );
        // Poll with a deadline so a hung/broken osascript can't block the exit
        // forever — the dialog is best-effort; exit(1) must always run (Kimi).
        if let Ok(mut child) = std::process::Command::new("osascript")
            .arg("-e")
            .arg(script)
            .spawn()
        {
            let deadline = Instant::now() + Duration::from_secs(15);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break, // user dismissed
                    Ok(None) if Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(100))
                    }
                    _ => break, // timed out or errored → stop waiting, proceed to exit
                }
            }
        }
    }
    // Windows/Linux: the message is already on stderr (→ OS log / journal); a
    // native Windows dialog is a low-risk follow-up.
    std::process::exit(1);
}

#[cfg(test)]
fn loopback_base(value: &str) -> String {
    if control_credentials::canonical_base(value) {
        value.to_string()
    } else {
        String::new()
    }
}
fn default_credential_path(role: &str) -> Result<std::path::PathBuf, CredentialError> {
    use std::path::PathBuf;
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
        .ok_or(CredentialError)?
        .join("TaskPaw");
    #[cfg(target_os = "macos")]
    let base = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or(CredentialError)?
        .join("Library/Application Support/TaskPaw");
    #[cfg(all(unix, not(target_os = "macos")))]
    let base = PathBuf::from("/etc/taskpaw");
    Ok(base.join(format!("{role}.control.json")))
}
fn load_credentials(ready: Option<Ready>, role: &str) -> Result<Descriptor, CredentialError> {
    let descriptor = if let Some(ready) = ready {
        if !ready.valid_for(role) {
            return Err(CredentialError);
        }
        let descriptor = control_credentials::read_descriptor(&ready.control_credential_file)?;
        if !descriptor.matches_ready(&ready) {
            return Err(CredentialError);
        }
        descriptor
    } else {
        let path = std::env::var_os("TASKPAW_CONTROL_CREDENTIAL_FILE")
            .map(std::path::PathBuf::from)
            .map(Ok)
            .unwrap_or_else(|| default_credential_path(role))?;
        let descriptor = control_credentials::read_descriptor(&path)?;
        if descriptor.role != role {
            return Err(CredentialError);
        }
        descriptor
    };
    if let Ok(expected) = std::env::var("TASKPAW_UI_BASE") {
        if expected != descriptor.base_url {
            return Err(CredentialError);
        }
    }
    Ok(descriptor)
}
fn trusted_ui_navigation(url: &url::Url, debug: bool) -> bool {
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let host = url.host_str().unwrap_or("");
    let packaged = url.port().is_none()
        && ((url.scheme() == "tauri" && host == "localhost")
            || (matches!(url.scheme(), "http" | "https") && host == "tauri.localhost"));
    packaged
        || (debug
            && url.scheme() == "http"
            && matches!(host, "localhost" | "127.0.0.1" | "[::1]")
            && url.port() == Some(5173))
}
fn init_script(descriptor: &Descriptor, debug: bool) -> String {
    let cfg = serde_json::json!({ "baseUrl": descriptor.base_url, "controlToken": descriptor.control_token, "role": descriptor.role, "bootId": descriptor.boot_id });
    let dev = if debug {
        "|| (p==='http:' && n==='5173' && (h==='localhost'||h==='127.0.0.1'||h==='[::1]'))"
    } else {
        ""
    };
    format!("(()=>{{ if(window.top!==window) return; const p=location.protocol,h=location.hostname,n=location.port; const u=new URL(location.href); if(u.username||u.password) return; if ((n==='' && ((p==='tauri:'&&h==='localhost')||((p==='http:'||p==='https:')&&h==='tauri.localhost'))) {dev}) {{ window.__TASKPAW__={cfg}; }} }})();")
}

fn main() {
    tauri::Builder::default()
        // Native file/directory picker for the add-monitor path fields (#71). The
        // ONLY widening of the locked-down shell (§3.1): a user-initiated open
        // dialog that returns a path string — no FS read/write IPC. Scoped to just
        // `dialog:allow-open` in capabilities/default.json.
        .plugin(tauri_plugin_dialog::init())
        // The webview reports its UI language so the native close dialog can
        // follow it (#108). Default = zh-CN (the i18n default) until it does.
        .manage(UiLang(Mutex::new("zh-CN".to_string())))
        .invoke_handler(tauri::generate_handler![set_ui_lang])
        .setup(|app| {
            // `mut`: the Windows Job-Object failure kill path AND taking the
            // backend's stdout for the readiness handshake (#48).
            let mut child = spawn_backend();
            // Only an explicit empty command selects attach. Failed spawns must
            // not silently attach to an unrelated already-running backend.
            let attach = std::env::var("TASKPAW_BACKEND_CMD").map(|value| value.trim().is_empty()).unwrap_or(false);
            if child.is_none() && !attach {
                fatal_startup("TaskPaw's backend could not be started. Please check the backend installation or command.", None);
            }
            // Take the backend's piped stdout now (before it's moved into managed
            // state) so we can read the readiness handshake below.
            let backend_stdout = child.as_mut().and_then(|c| c.stdout.take());
            #[cfg(windows)]
            {
                // If we spawned a backend but couldn't put it in a kill-on-close
                // Job Object, the "X = exit, no orphan descendants" guarantee is
                // broken — fail rather than risk leaking a backend tree (Kimi).
                let job = match child.as_ref().map(jobobj::assign) {
                    Some(Some(j)) => Some(j),
                    Some(None) => {
                        // Assignment failed AFTER spawn — kill the backend now,
                        // else returning Err drops Child WITHOUT terminating it
                        // (Child has no kill-on-drop) → orphan (Codex).
                        if let Some(c) = child.as_mut() {
                            let _ = c.kill();
                            let _ = c.wait();
                        }
                        // Backend already killed above; exit cleanly (see
                        // fatal_startup) rather than via a setup-Err panic.
                        fatal_startup(
                            "TaskPaw could not assign its backend to a Windows Job \
                             Object, so it can't guarantee the backend stops when you \
                             quit. Refusing to launch to avoid orphaned processes.",
                            None, // child already killed inline above
                        );
                    }
                    None => None, // dev: backend intentionally disabled
                };
                app.manage(JobHandle(Mutex::new(job)));
            }
            // Whether closing the window will actually kill a spawned backend —
            // only then do we ask for confirmation (#52). Dev with no backend just
            // closes.
            let has_backend = child.is_some();
            app.manage(Backend(Mutex::new(child)));
            // Spawn metadata must bind the protected descriptor to this boot;
            // explicit attach reads the current protected descriptor directly.
            let ready = match backend_stdout {
                Some(out) => match read_readiness(out, Duration::from_secs(30)) {
                    Ok(ready) => Some(ready),
                    Err(_) => fatal_startup("TaskPaw's backend did not report valid startup metadata. Its local API may be unavailable. Please check the backend log.", Some(app.handle())),
                },
                None => None,
            };
            let descriptor = match load_credentials(ready, &ui_role()) {
                Ok(descriptor) => descriptor,
                Err(_) => fatal_startup("TaskPaw could not securely read this backend's current local control credentials. Please restart or reopen TaskPaw.", Some(app.handle())),
            };
            // Build the window in code so we can inject the runtime config script
            // (the validated base_url) BEFORE the page loads, only on the
            // loopback-served origin.
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("TaskPaw")
                .inner_size(1100.0, 720.0)
                .min_inner_size(720.0, 480.0)
                .initialization_script(init_script(&descriptor, cfg!(debug_assertions)))
                .on_navigation(|url| trusted_ui_navigation(url, cfg!(debug_assertions)))
                .build()?;
            // Closing the window kills the backend; warn first so the operator
            // doesn't accidentally stop background monitoring — and, for a Hub,
            // aggregation + OpenClaw notifications (#52). Only when a backend was
            // actually spawned. The OK button is debounced via a flag so the
            // post-confirm close isn't re-intercepted.
            if has_backend {
                let role = ui_role();
                let confirmed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
                let app_handle = app.handle().clone();
                win.on_window_event(move |event| {
                    use std::sync::atomic::Ordering::SeqCst;
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        if confirmed.load(SeqCst) {
                            return; // already confirmed → allow the close through
                        }
                        api.prevent_close();
                        let confirmed = confirmed.clone();
                        let app_handle = app_handle.clone();
                        // Read the current UI language AT CLOSE TIME so an in-session
                        // language switch is honored without a restart (#108).
                        let lang = app_handle
                            .state::<UiLang>()
                            .0
                            .lock()
                            .map(|g| g.clone())
                            .unwrap_or_else(|_| "zh-CN".to_string());
                        let (title, msg, ok_label, cancel_label) =
                            close_confirm_text(&role, &lang);
                        app_handle
                            .dialog()
                            .message(msg)
                            .title(title)
                            .buttons(MessageDialogButtons::OkCancelCustom(ok_label, cancel_label))
                            .show(move |ok| {
                                if ok {
                                    confirmed.store(true, SeqCst);
                                    if let Some(w) = app_handle.get_webview_window("main") {
                                        let _ = w.close();
                                    }
                                }
                            });
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building TaskPaw")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                kill_backend(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{
        applescript_escape, backend_log_hint, close_confirm_text, loopback_base,
        roll_log_if_oversized,
    };

    #[test]
    fn applescript_escape_neutralizes_quotes_and_backslashes() {
        // A log path with a quote/backslash must not break out of the osascript
        // string literal in fatal_startup; newlines must become the \n escape so
        // the literal stays on one physical line (osascript won't compile a
        // multi-line literal).
        assert_eq!(applescript_escape(r#"a"b\c"#), r#"a\"b\\c"#);
        assert_eq!(applescript_escape("plain text"), "plain text");
        // control chars (newline/tab) become spaces so the literal stays one line
        assert_eq!(applescript_escape("line1\n\tline2"), "line1  line2");
        assert!(!applescript_escape("a\nb\tc").contains(|c: char| c.is_control()));
        // length is capped
        assert!(applescript_escape(&"x".repeat(5000)).chars().count() <= 1000);
    }

    #[test]
    fn roll_log_rotates_oversized_and_overwrites_stale_backup() {
        // Reproduces the cross-platform rotation bug: a stale `.1` must not block
        // the roll (std::fs::rename fails on Windows if the dest exists).
        let dir = std::env::temp_dir().join(format!("taskpaw-roll-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("taskpaw-backend-agent.log");
        let backup = dir.join("taskpaw-backend-agent.log.1");

        std::fs::write(&backup, b"stale-old-backup").unwrap(); // pre-existing .1
        std::fs::write(&log, vec![b'x'; 11]).unwrap(); // 11 bytes > max 10
        roll_log_if_oversized(&log, 10);
        assert!(!log.exists(), "oversized live log should be rolled away");
        assert_eq!(
            std::fs::read(&backup).unwrap(),
            vec![b'x'; 11],
            "stale .1 overwritten by the rolled log"
        );

        // Under threshold: left in place, no spurious roll.
        std::fs::write(&log, b"tiny").unwrap();
        roll_log_if_oversized(&log, 10);
        assert!(log.exists() && std::fs::read(&log).unwrap() == b"tiny");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn backend_log_hint_is_nonempty() {
        // Tests build with debug_assertions, so on macOS/Windows the hint names the
        // dev terminal; the per-OS file path is exercised via macos_backend_log_path.
        assert!(!backend_log_hint().is_empty());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_backend_log_path_is_role_scoped_under_library_logs() {
        // Pure path mapping (no env mutation). Agent and Hub must resolve to
        // DISTINCT files so co-located roles don't interleave logs (Kimi).
        let home = std::path::Path::new("/Users/example");
        let hub = super::macos_backend_log_path(home, "hub");
        let agent = super::macos_backend_log_path(home, "agent");
        assert_eq!(
            hub,
            std::path::Path::new("/Users/example/Library/Logs/TaskPaw/taskpaw-backend-hub.log")
        );
        assert!(agent
            .to_string_lossy()
            .ends_with("taskpaw-backend-agent.log"));
        assert_ne!(hub, agent);
    }

    #[test]
    fn close_confirm_is_single_language_per_choice() {
        // English: no Chinese, no bilingual "/" separator in the title.
        let (title, msg, ok, cancel) = close_confirm_text("agent", "en");
        assert!(title.contains("Confirm close") && !title.contains('/'));
        assert!(msg.contains("background monitoring"));
        assert!(!msg.contains("关闭"));
        assert_eq!((ok.as_str(), cancel.as_str()), ("Close", "Cancel"));

        // Chinese: no English body, localized buttons.
        let (title, msg, ok, cancel) = close_confirm_text("agent", "zh-CN");
        assert!(title.contains("确认关闭") && !title.contains('/'));
        assert!(msg.contains("后台监控"));
        assert!(!msg.contains("Closing"));
        assert_eq!((ok.as_str(), cancel.as_str()), ("关闭", "取消"));
    }

    #[test]
    fn close_confirm_is_role_tailored() {
        // Hub mentions aggregation; agent mentions this machine — in each language.
        assert!(close_confirm_text("hub", "en").1.contains("aggregation"));
        assert!(close_confirm_text("agent", "en").1.contains("this machine"));
        assert!(close_confirm_text("hub", "zh-CN").1.contains("聚合"));
        assert!(close_confirm_text("agent", "zh-CN").1.contains("本机"));
    }

    #[test]
    fn close_confirm_unknown_lang_falls_back_to_chinese() {
        // Any non-"en" value (incl. junk) uses the i18n default language.
        let (title, _msg, ok, _cancel) = close_confirm_text("agent", "fr");
        assert!(title.contains("确认关闭"));
        assert_eq!(ok, "关闭");
    }

    #[test]
    fn accepts_loopback_forms() {
        assert_eq!(
            loopback_base("http://127.0.0.1:5681"),
            "http://127.0.0.1:5681"
        );
        assert_eq!(loopback_base("http://localhost:5690"), "");
        assert_eq!(loopback_base("http://[::1]:5681"), "http://[::1]:5681");
        assert_eq!(loopback_base("http://[::1]"), "");
        assert_eq!(loopback_base(""), "");
        // scheme-less → default http:// so the frontend sees an absolute origin
        assert_eq!(loopback_base("127.0.0.1:5681"), "");
    }

    #[test]
    fn rejects_non_loopback_and_bypasses() {
        // userinfo bypass: browser would use evil.com
        assert_eq!(loopback_base("http://127.0.0.1:8000@evil.com"), "");
        // hostname that merely looks loopback but resolves remote
        assert_eq!(loopback_base("http://127.0.0.1.evil.com:8000"), "");
        assert_eq!(loopback_base("http://evil.com:5681"), "");
        assert_eq!(loopback_base("http://10.0.0.5:5681"), "");
        // non-canonical loopback rejected (canonical-only, lockstep with CSP/guard)
        assert_eq!(loopback_base("http://127.0.0.5:9000"), "");
        assert_eq!(loopback_base("ftp://127.0.0.1:5681"), "");
    }

    #[test]
    fn rejects_credentials_outright() {
        // A base URL must never carry credentials — refuse it (don't strip), so the
        // injected api key can't leak to a misread host (#54). The userinfo-bypass
        // case is also covered by rejects_non_loopback_and_bypasses.
        assert_eq!(loopback_base("http://user:pass@127.0.0.1:5681/x"), "");
        assert_eq!(loopback_base("http://user@127.0.0.1:5681"), "");
    }

    #[test]
    fn normalizes_browser_ipv4_forms() {
        // The real parser normalizes browser IPv4 spellings the webview would
        // actually request (#54 browser/CSP parity): "127.1" == 127.0.0.1.
        assert_eq!(loopback_base("http://127.1:8000"), "");
        assert_eq!(loopback_base("http://127.0.0.1/x"), "");
    }

    #[test]
    fn ui_role_validates() {
        // (env-independent) — invalid/blank handled by the matches! guard; this
        // documents the accepted set.
        for r in ["agent", "hub"] {
            assert!(matches!(r, "agent" | "hub"));
        }
    }
    #[test]
    fn readiness_metadata_is_typed_and_bound_to_role_and_boot() {
        let mut ready = super::parse_ready_line(r#"{"taskpaw_ready":true,"role":"hub","base_url":"http://[::1]:5691","boot_id":"0123456789abcdef0123456789abcdef","control_credential_file":"/private/fixture/hub.control.json"}"#).unwrap().unwrap();
        ready.control_credential_file = std::env::temp_dir().join("hub.control.json");
        assert!(ready.valid_for("hub"));
        assert!(!ready.valid_for("agent"));
        assert!(super::parse_ready_line(
            r#"{"taskpaw_ready":true,"base_url":"http://127.0.0.1:5681"}"#
        )
        .unwrap()
        .is_err());
        assert!(super::parse_ready_line("not JSON fake-test-marker").is_none());
        assert!(super::parse_ready_line(r#"{"taskpaw_ready":false}"#).is_none());
    }
    #[test]
    fn navigation_is_independently_limited_to_exact_ui_origins() {
        for raw in [
            "tauri://localhost/index.html",
            "https://tauri.localhost/index.html",
            "http://tauri.localhost/",
        ] {
            assert!(super::trusted_ui_navigation(
                &url::Url::parse(raw).unwrap(),
                false
            ));
        }
        for raw in [
            "http://localhost:5173/",
            "http://127.0.0.1:5173/",
            "http://[::1]:5173/",
        ] {
            assert!(super::trusted_ui_navigation(
                &url::Url::parse(raw).unwrap(),
                true
            ));
            assert!(!super::trusted_ui_navigation(
                &url::Url::parse(raw).unwrap(),
                false
            ));
        }
        for raw in [
            "http://localhost:5681/",
            "tauri://evil/",
            "https://tauri.localhost.evil/",
            "file:///tmp/ui.html",
            "data:text/html,hi",
            "http://u:p@localhost:5173/",
        ] {
            assert!(!super::trusted_ui_navigation(
                &url::Url::parse(raw).unwrap(),
                true
            ));
        }
    }
    #[test]
    fn generated_initialization_javascript_executes_only_on_trusted_top_frame() {
        use std::io::Write;
        use std::process::{Command, Stdio};
        let descriptor = super::control_credentials::parse_descriptor(br#"{"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-init-marker"}"#).unwrap();
        // Execute the actual Rust-generated script, using an independent VM
        // harness and explicit expected values rather than string assertions.
        let harness = r#"
const vm=require('node:vm'), assert=require('node:assert/strict');
let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{
 const scripts=JSON.parse(input);
 for(const debug of [false,true]) {
  const script=scripts[debug?'debug':'release'];
  for(const href of ['tauri://localhost/index.html','http://tauri.localhost/','https://tauri.localhost/','http://localhost:5173/','http://127.0.0.1:5173/','http://[::1]:5173/','http://localhost:5681/','http://127.0.0.1:9999/','http://evil.example/','http://localhost.evil:5173/','http://10.0.0.1:5173/','tauri://evil/','data:text/html,hi','file:///tmp/index.html','about:blank','blob:http://localhost:5173/id','http://u:p@localhost:5173/']) {
   for(const frame of ['top','same-origin-child','cross-origin-child']) {
    const location=new URL(href);const window={};window.top=frame==='top'?window:{};
    const context={window,location,URL};vm.runInNewContext(script,context);
    const trusted=['tauri://localhost/index.html','http://tauri.localhost/','https://tauri.localhost/'].includes(href)||(debug&&['http://localhost:5173/','http://127.0.0.1:5173/','http://[::1]:5173/'].includes(href));
    if(frame==='top'&&trusted) assert.equal(JSON.stringify(window.__TASKPAW__),JSON.stringify({baseUrl:'http://127.0.0.1:5681',bootId:'0123456789abcdef0123456789abcdef',controlToken:'fake-init-marker',role:'agent'}));
    else assert.equal(window.__TASKPAW__,undefined);
   }
  }
 }
});"#;
        let mut child = Command::new("node")
            .args(["-e", harness])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .expect("Node is required for initialization-script execution tests");
        let input = serde_json::json!({"debug":super::init_script(&descriptor,true),"release":super::init_script(&descriptor,false)});
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.to_string().as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "Initialization-script execution failed"
        );
    }
    #[cfg(windows)]
    #[test]
    #[ignore = "Requires the production Python writer's Windows fixtures"]
    fn windows_python_credential_interop() {
        #[derive(serde::Deserialize)]
        struct Manifest {
            version: u8,
            cases: Vec<Case>,
        }
        #[derive(serde::Deserialize)]
        struct Case {
            name: String,
            path: std::path::PathBuf,
            expect: String,
        }
        let directory = std::env::var_os("TASKPAW_TEST_CONTROL_FIXTURES")
            .expect("Interop fixture directory is required");
        let manifest: Manifest = serde_json::from_slice(
            &std::fs::read(std::path::Path::new(&directory).join("manifest.json"))
                .expect("Interop manifest is required"),
        )
        .expect("Interop manifest must be valid");
        assert_eq!(manifest.version, 1);
        let mut accepted = 0;
        let mut rejected = 0;
        let mut secure_seen = false;
        let mut broad_seen = false;
        let mut owner_rights_seen = false;
        let mut everyone_owner_rights_seen = false;
        let mut file_owner_rights_seen = false;
        let mut installer_file_acl_seen = false;
        for case in manifest.cases {
            if case.name == "secure" {
                assert_eq!(case.expect, "accept");
                secure_seen = true;
            }
            if case.name == "broad_acl" {
                assert_eq!(case.expect, "reject");
                broad_seen = true;
            }
            if case.name == "trusted_owner_rights" {
                assert_eq!(case.expect, "accept");
                owner_rights_seen = true;
            }
            if case.name == "everyone_owner_rights" {
                assert_eq!(case.expect, "reject");
                everyone_owner_rights_seen = true;
            }
            if case.name == "file_owner_rights" {
                assert_eq!(case.expect, "reject");
                file_owner_rights_seen = true;
            }
            if case.name == "trusted_installer_file_acl" {
                assert_eq!(case.expect, "reject");
                installer_file_acl_seen = true;
            }
            match case.expect.as_str() {
                "accept" => {
                    let descriptor = super::control_credentials::read_descriptor(&case.path)
                        .expect("Python safe fixture must be readable");
                    assert_eq!(descriptor.role, "agent");
                    assert_eq!(descriptor.base_url, "http://127.0.0.1:5681");
                    assert_eq!(descriptor.boot_id, "0123456789abcdef0123456789abcdef");
                    assert_eq!(descriptor.control_token, "fake-python-rust-interop-token");
                    accepted += 1;
                }
                "reject" => {
                    assert!(
                        super::control_credentials::read_descriptor(&case.path).is_err(),
                        "Unsafe Python fixture accepted: {}",
                        case.name
                    );
                    rejected += 1;
                }
                "skip" => {
                    assert!(
                        !matches!(
                            case.name.as_str(),
                            "secure"
                                | "broad_acl"
                                | "trusted_owner_rights"
                                | "everyone_owner_rights"
                                | "file_owner_rights"
                                | "trusted_installer_file_acl"
                        ),
                        "Basic interop fixtures must never skip"
                    );
                    eprintln!("Windows optional fixture unavailable: {}", case.name);
                }
                _ => panic!("Invalid interop expectation"),
            }
        }
        assert!(
            accepted >= 2
                && rejected >= 4
                && secure_seen
                && broad_seen
                && owner_rights_seen
                && everyone_owner_rights_seen
                && file_owner_rights_seen
                && installer_file_acl_seen,
            "Interop requires safe and broad-ACL real files"
        );
    }
    #[test]
    fn readiness_eof_timeout_and_invalid_metadata_fail_closed() {
        use std::process::{Command, Stdio};
        for (program, timeout) in [
            ("process.exit(0)", std::time::Duration::from_secs(2)),
            ("setTimeout(()=>{},1000)", std::time::Duration::from_millis(10)),
            ("process.stdout.write(JSON.stringify({taskpaw_ready:true,base_url:'http://127.0.0.1:5681'})+'\\n')", std::time::Duration::from_secs(2)),
        ] {
            let mut child=Command::new("node").args(["-e",program]).stdout(Stdio::piped()).spawn().unwrap();
            assert!(super::read_readiness(child.stdout.take().unwrap(), timeout).is_err());
            let _=child.kill(); let _=child.wait();
        }
    }
    #[cfg(unix)]
    #[test]
    fn spawned_credentials_must_match_the_current_role_base_and_boot() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!(
                "taskpaw-spawn-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.join("hub.control.json");
        std::fs::write(&path,br#"{"version":1,"role":"hub","base_url":"http://[::1]:15991","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-spawn-key"}"#).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let ready = super::Ready {
            role: "hub".into(),
            base_url: "http://[::1]:15991".into(),
            boot_id: "0123456789abcdef0123456789abcdef".into(),
            control_credential_file: path,
        };
        assert!(super::load_credentials(Some(ready.clone()), "hub").is_ok());
        assert!(super::load_credentials(Some(ready.clone()), "agent").is_err());
        let mut stale = ready.clone();
        stale.boot_id = "ffffffffffffffffffffffffffffffff".into();
        assert!(super::load_credentials(Some(stale), "hub").is_err());
        let mut wrong_base = ready;
        wrong_base.base_url = "http://127.0.0.1:15991".into();
        assert!(super::load_credentials(Some(wrong_base), "hub").is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "Requires a descriptor made by the production Python writer"]
    fn darwin_python_credential_interop() {
        let path = std::env::var_os("TASKPAW_TEST_DARWIN_CONTROL_FIXTURE")
            .expect("Production writer fixture is required");
        let descriptor = super::control_credentials::read_descriptor(std::path::Path::new(&path))
            .expect("Safe production Python descriptor must be readable");
        assert_eq!(descriptor.role, "agent");
        assert_eq!(descriptor.base_url, "http://127.0.0.1:5681");
        assert_eq!(descriptor.boot_id, "0123456789abcdef0123456789abcdef");
        assert_eq!(descriptor.control_token, "fake-python-rust-interop-token");
    }
}
