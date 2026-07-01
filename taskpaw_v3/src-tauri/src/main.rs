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
// IPC/FS). The webview talks ONLY to the local backend over HTTP; the api key +
// base url + role are injected at runtime on the loopback origin via an init
// script (so packaged builds don't rely on compile-time env).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

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
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
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
        .or_else(|| option_env!("TASKPAW_BUILD_ROLE").map(str::to_string).and_then(nonblank))
        .unwrap_or_else(|| "agent".into());
    // Normalize + validate: the frontend (App.tsx) and backend expect exactly
    // "agent"/"hub"; anything else (e.g. "AGENT", typo) falls back to agent (Kimi).
    let role = raw.trim().to_ascii_lowercase();
    if matches!(role.as_str(), "agent" | "hub") { role } else { "agent".into() }
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
        dir.to_path_buf(),                       // next to the app binary (release)
        dir.join("../Resources"),                // macOS .app resources fallback
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
        match backend_log_path() {
            Some(p) if p.exists() => p.display().to_string(),
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
    if std::fs::metadata(path).map(|m| m.len() > max_bytes).unwrap_or(false) {
        let mut rotated = path.as_os_str().to_owned();
        rotated.push(".1");
        let rotated = std::path::PathBuf::from(rotated);
        // A stale .1 blocks rename on Windows, so remove it first. NotFound is the
        // normal case (no prior backup) — only warn on a real removal failure.
        if let Err(e) = std::fs::remove_file(&rotated) {
            if e.kind() != std::io::ErrorKind::NotFound {
                eprintln!("taskpaw: cannot remove stale log backup {rotated:?}: {e}; rotation may stall");
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
#[cfg(any(target_os = "macos", windows))]
fn open_backend_log() -> Option<std::fs::File> {
    let path = backend_log_path()?;
    if let Some(dir) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            eprintln!("taskpaw: cannot create backend log dir {dir:?}: {e}; backend stderr discarded");
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
        Ok(f) => Some(f),
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
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong, 0, 0, 0);
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
            command.stderr(open_backend_log().map(Stdio::from).unwrap_or_else(Stdio::null));
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
            command.stderr(open_backend_log().map(Stdio::from).unwrap_or_else(Stdio::null));
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

/// Read the backend's stdout until the §3.1 readiness line (#48) and return its
/// `base_url`, or None on timeout. A reader thread parses each line as JSON,
/// sends the base_url from the first `{"taskpaw_ready":true,...}` line, then keeps
/// draining stdout to EOF so a full pipe can never block the backend. We wait on
/// the channel with a timeout so a backend that never reports ready can't hang the
/// shell (the caller fails loud instead of opening a UI that can't reach the API).
fn read_readiness(stdout: std::process::ChildStdout, timeout: Duration) -> Option<String> {
    use std::io::{BufRead, BufReader};
    use std::sync::mpsc;
    let (tx, rx) = mpsc::channel::<String>();
    std::thread::spawn(move || {
        let mut found = false;
        for line in BufReader::new(stdout).lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break, // pipe closed (backend exited)
            };
            if !found {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) {
                    if v.get("taskpaw_ready").and_then(|b| b.as_bool()) == Some(true) {
                        if let Some(b) = v.get("base_url").and_then(|b| b.as_str()) {
                            let _ = tx.send(b.to_string());
                            found = true;
                            continue;
                        }
                    }
                }
                eprintln!("[backend] {line}"); // stray pre-readiness stdout
            }
            // After readiness: keep reading (and discarding) to drain the pipe.
        }
    });
    rx.recv_timeout(timeout).ok()
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
    // Backslash first, then quotes, then newlines → an AppleScript string literal
    // can't span physical lines, so a message with "\n\n" (the readiness-timeout
    // text) must render them as the two-char \n escape or osascript won't compile
    // and the dialog is silently dropped on a fatal path (Kimi).
    s.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

/// Fatal startup failure: show a best-effort native error dialog, then exit
/// cleanly with code 1. We deliberately do NOT return Err from the setup hook for
/// these — Tauri `.expect()`s a setup Err into a panic, and on macOS that panic
/// can't unwind across the ObjC `did_finish_launching` callback, so it aborts with
/// SIGABRT and a crash report (e.g. on a mere port conflict). A clean exit shows a
/// friendly message instead. NOTE: process::exit skips Drop, so the managed
/// Backend's kill-on-drop never runs — callers that already spawned a backend MUST
/// kill_backend() first, or it orphans.
fn fatal_startup(message: &str) -> ! {
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
            let deadline = Instant::now() + Duration::from_secs(120);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,                 // user dismissed
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

/// Accept only a loopback base URL (design §3.1: the shell loopback-validates the
/// backend base_url before injecting it). A tampered/non-local TASKPAW_UI_BASE is
/// dropped so the frontend can't be pointed at a remote backend with the api key.
/// Empty string (the common bundled case) is allowed → frontend uses its safe
/// per-role loopback defaults.
fn loopback_base(raw: &str) -> String {
    use std::net::{Ipv4Addr, Ipv6Addr};
    use url::{Host, Url};

    let v = raw.trim();
    if v.is_empty() {
        return String::new();
    }
    // Add a scheme for scheme-less input ("127.0.0.1:5681") so a full URL parses;
    // a real "http(s)://…" is left untouched. Use a real parser (#54) — robust to
    // IDN/punycode, percent-encoding, and weird authorities, with browser parity.
    let candidate = if v.contains("://") { v.to_string() } else { format!("http://{v}") };
    let url = match Url::parse(&candidate) {
        Ok(u) => u,
        Err(_) => {
            eprintln!("TASKPAW_UI_BASE {v:?} is not a valid URL — ignoring");
            return String::new();
        }
    };
    // Reject ANY credentials: "http://127.0.0.1:8000@evil.com" parses with host
    // evil.com (a browser would hit evil.com) — refuse rather than strip, so the
    // injected api key can't leak to a remote origin (Codex/Kimi P1, #54).
    if !url.username().is_empty() || url.password().is_some() {
        eprintln!("TASKPAW_UI_BASE {v:?} carries credentials — ignoring");
        return String::new();
    }
    // Only http(s); reject e.g. ftp:// (the frontend speaks HTTP) (Kimi).
    if !matches!(url.scheme(), "http" | "https") {
        eprintln!("TASKPAW_UI_BASE {v:?} has a non-http(s) scheme — ignoring");
        return String::new();
    }
    // CANONICAL loopback only — in exact lockstep with the init-script origin guard
    // AND the CSP connect-src (localhost / 127.0.0.1 / ::1). The parser normalizes
    // browser forms (e.g. "127.1" → 127.0.0.1), so this matches what the webview
    // would actually request.
    let is_loopback = match url.host() {
        Some(Host::Domain(d)) => d.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(ip)) => ip == Ipv4Addr::LOCALHOST,
        Some(Host::Ipv6(ip)) => ip == Ipv6Addr::LOCALHOST,
        None => false,
    };
    if !is_loopback {
        eprintln!("TASKPAW_UI_BASE {v:?} is not loopback — ignoring");
        return String::new();
    }
    // Reconstruct a clean scheme://host[:port]path (host_str() brackets IPv6); drop
    // a normalized empty path "/" so the result matches the bare input, and drop
    // any query/fragment (a base URL carries none).
    let host = url.host_str().unwrap_or("");
    let port = url.port().map(|p| format!(":{p}")).unwrap_or_default();
    let path = url.path();
    let path = if path == "/" { "" } else { path };
    format!("{}://{}{}{}", url.scheme(), host, port, path)
}

/// Runtime config injected on the loopback origin (design §3.1) — packaged
/// builds can't use compile-time Vite env, so the shell injects it here.
fn init_script(base_url: &str) -> String {
    // Prefer the backend-reported base_url from the readiness handshake (#48) — it
    // reflects the ACTUAL (possibly custom) port. Fall back to TASKPAW_UI_BASE for
    // dev (Vite) when empty. Both are loopback-validated before injection.
    let raw = if base_url.is_empty() {
        std::env::var("TASKPAW_UI_BASE").unwrap_or_default()
    } else {
        base_url.to_string()
    };
    let base = loopback_base(&raw);
    let token = std::env::var("TASKPAW_UI_TOKEN").unwrap_or_default();
    let role = ui_role();
    // serde_json escapes the values safely.
    let cfg = serde_json::json!({ "baseUrl": base, "apiKey": token, "role": role });
    // Guard by origin so the api key is never exposed if the webview ever
    // navigates away from the local frontend to a non-loopback page (Kimi).
    // Allowed origins: loopback hosts, the macOS tauri: protocol, AND the
    // Exact canonical loopback set — in lockstep with loopback_base() and the CSP
    // connect-src. Plus the packaged webview host tauri.localhost (Windows
    // https://tauri.localhost, per core/cors.py) and the macOS tauri: protocol —
    // else the injected config is dropped in packaged builds (Codex).
    format!(
        "{{ const h = location.hostname; \
         if (h==='localhost'||h==='127.0.0.1'||h==='[::1]'||h==='::1'||h==='tauri.localhost'|| \
             location.protocol==='tauri:') \
           {{ window.__TASKPAW__ = {cfg}; }} }}"
    )
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
            // Release bundled mode (no dev override): a missing/failed sidecar
            // means the UI would open with no backend — fail LOUD rather than
            // silently broken (Kimi). Skip in debug so `cargo tauri dev` works
            // without a sidecar; dev (explicit TASKPAW_BACKEND_CMD) stays lenient.
            if !cfg!(debug_assertions)
                && child.is_none()
                && std::env::var_os("TASKPAW_BACKEND_CMD").is_none()
            {
                // Abort launch instead of opening a UI with no backend (Kimi) —
                // but exit cleanly, not via a setup-Err panic (see fatal_startup).
                // No child spawned here, so nothing to kill.
                fatal_startup(
                    "TaskPaw's bundled backend 'taskpaw-backend' was not found or \
                     failed to start, so the app cannot reach its local API. Try \
                     reinstalling the app.",
                );
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
            // §3.1 readiness handshake (#48): wait for the backend to report its
            // base_url on stdout BEFORE loading the webview — so the UI never races
            // the backend and a CUSTOM control/bind port is reflected. If it never
            // arrives, fail loud rather than open a UI that can't reach the API
            // (the managed Backend's Drop terminates the child on this Err → no
            // orphan). With no piped stdout (dev/no-backend) we fall back to the
            // TASKPAW_UI_BASE / loopback defaults.
            let base_url = match backend_stdout {
                Some(out) => match read_readiness(out, Duration::from_secs(30)) {
                    Some(b) => b,
                    None => {
                        // The backend was spawned but never reported readiness —
                        // most often its local API port is already taken (another
                        // instance, a V2 agent, or another service). Kill it first
                        // (process::exit skips the managed Backend's Drop → else it
                        // orphans), then exit cleanly with a friendly dialog rather
                        // than a setup-Err panic/abort (see fatal_startup).
                        let hint = backend_log_hint();
                        kill_backend(app.handle());
                        fatal_startup(&format!(
                            "TaskPaw's backend did not start within 30s. Its local \
                             API port may be in use by another TaskPaw instance, a \
                             V2 agent, or another service.\n\nDetails: {hint}"
                        ));
                    }
                },
                None => String::new(), // dev: Vite + TASKPAW_UI_BASE fallback
            };
            // Build the window in code so we can inject the runtime config script
            // (the validated base_url) BEFORE the page loads, only on the
            // loopback-served origin.
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("TaskPaw")
                .inner_size(1100.0, 720.0)
                .min_inner_size(720.0, 480.0)
                .initialization_script(&init_script(&base_url))
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
        assert_eq!(applescript_escape("line1\n\nline2"), "line1\\n\\nline2");
        assert!(!applescript_escape("a\nb").contains('\n')); // no raw newline survives
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
        std::fs::write(&log, vec![b'x'; 11]).unwrap();         // 11 bytes > max 10
        roll_log_if_oversized(&log, 10);
        assert!(!log.exists(), "oversized live log should be rolled away");
        assert_eq!(std::fs::read(&backup).unwrap(), vec![b'x'; 11], "stale .1 overwritten by the rolled log");

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
        assert!(agent.to_string_lossy().ends_with("taskpaw-backend-agent.log"));
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
        assert_eq!(loopback_base("http://127.0.0.1:5681"), "http://127.0.0.1:5681");
        assert_eq!(loopback_base("http://localhost:5690"), "http://localhost:5690");
        assert_eq!(loopback_base("http://[::1]:5681"), "http://[::1]:5681");
        assert_eq!(loopback_base("http://[::1]"), "http://[::1]");
        assert_eq!(loopback_base(""), "");
        // scheme-less → default http:// so the frontend sees an absolute origin
        assert_eq!(loopback_base("127.0.0.1:5681"), "http://127.0.0.1:5681");
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
        assert_eq!(loopback_base("http://127.1:8000"), "http://127.0.0.1:8000");
        assert_eq!(loopback_base("http://127.0.0.1/x"), "http://127.0.0.1/x");
    }

    #[test]
    fn ui_role_validates() {
        // (env-independent) — invalid/blank handled by the matches! guard; this
        // documents the accepted set.
        for r in ["agent", "hub"] {
            assert!(matches!(r, "agent" | "hub"));
        }
    }
}
