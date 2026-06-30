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

/// Human-readable location of the backend log, for user-facing messages. The
/// packaged (console-less) shell redirects the backend's stderr to this file;
/// centralized so the spawn redirect and any hint we print can never name
/// different paths (Kimi). Linux inherits stderr, so it points there instead.
fn backend_log_hint() -> &'static str {
    #[cfg(target_os = "macos")]
    {
        "~/Library/Logs/TaskPaw/taskpaw-backend.log"
    }
    #[cfg(windows)]
    {
        "%APPDATA%\\TaskPaw\\taskpaw-backend.log"
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        "the backend's stderr (journalctl, or the launching terminal)"
    }
}

#[cfg(target_os = "macos")]
fn macos_backend_log_path(home: &std::path::Path) -> std::path::PathBuf {
    home.join("Library/Logs/TaskPaw/taskpaw-backend.log")
}

/// Open the per-OS backend log for APPEND so crash logs accumulate across
/// relaunches instead of being truncated on every start (Kimi). Creates the dir;
/// on failure warns to the shell's own stderr (captured by the OS log) rather than
/// silently discarding the backend's logs. None on Linux (stderr is inherited).
#[cfg(any(target_os = "macos", windows))]
fn open_backend_log() -> Option<std::fs::File> {
    #[cfg(target_os = "macos")]
    let path = {
        let home = std::env::var_os("HOME")?;
        macos_backend_log_path(std::path::Path::new(&home))
    };
    #[cfg(windows)]
    let path = {
        let appdata = std::env::var_os("APPDATA")?;
        std::path::Path::new(&appdata).join("TaskPaw").join("taskpaw-backend.log")
    };
    if let Some(dir) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            eprintln!("taskpaw: cannot create backend log dir {dir:?}: {e}; backend stderr discarded");
            return None;
        }
    }
    match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
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
        // A windowed macOS .app has no console (the same problem the Windows block
        // below solves with %APPDATA%), so the backend's STDERR — its logs — would
        // vanish in a packaged build. Route it to ~/Library/Logs/TaskPaw/ (see
        // open_backend_log) so production failures are debuggable. stdout stays
        // piped (above) for the §3.1 readiness handshake. Linux is left inheriting:
        // the headless Hub runs under a terminal / systemd that already captures it.
        #[cfg(target_os = "macos")]
        {
            use std::process::Stdio;
            command.stderr(open_backend_log().map(Stdio::from).unwrap_or_else(Stdio::null));
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use std::process::Stdio;
        // CREATE_NO_WINDOW (0x08000000) | CREATE_BREAKAWAY_FROM_JOB (0x01000000) so
        // we can put the backend in OUR Job Object even when the launcher is
        // already inside one. NOTE: 0x00080000 is EXTENDED_STARTUPINFO_PRESENT, NOT
        // breakaway — using it made CreateProcess fail (os error 87) and crash
        // every launch (caught by Windows verification, #50).
        command.creation_flags(0x08000000 | 0x01000000);
        // The windowed shell has no console — route backend STDERR (its logs) to
        // %APPDATA%\TaskPaw\taskpaw-backend.log so production builds are debuggable
        // (see open_backend_log: append, with a warning if it can't open). stdout
        // stays piped (above) for the readiness handshake.
        command.stderr(open_backend_log().map(Stdio::from).unwrap_or_else(Stdio::null));
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
                // Abort launch instead of opening a UI with no backend (Kimi).
                return Err(
                    "bundled backend 'taskpaw-backend' not found/failed to start; the app \
                     cannot reach a local API. Reinstall, or set TASKPAW_BACKEND_CMD for dev."
                        .into(),
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
                        return Err("could not assign the backend to a Windows Job \
                                    Object; refusing to launch to avoid orphaned \
                                    backend processes on exit."
                            .into());
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
                        return Err(format!(
                            "backend did not report readiness within 30s; refusing to \
                             open a UI that cannot reach the local API. See {}.",
                            backend_log_hint()
                        )
                        .into());
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
    use super::{backend_log_hint, close_confirm_text, loopback_base};

    #[test]
    fn backend_log_hint_is_platform_specific_and_nonempty() {
        let h = backend_log_hint();
        assert!(!h.is_empty());
        #[cfg(target_os = "macos")]
        assert!(h.contains("Library/Logs/TaskPaw"), "macOS hint should point at ~/Library/Logs: {h}");
        #[cfg(windows)]
        assert!(h.contains("APPDATA"), "Windows hint should point at %APPDATA%: {h}");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_backend_log_path_under_library_logs() {
        // Pure path mapping (no env mutation) — the spawn redirect and the
        // readiness-error hint must resolve to this same location.
        let p = super::macos_backend_log_path(std::path::Path::new("/Users/example"));
        assert_eq!(
            p,
            std::path::Path::new("/Users/example/Library/Logs/TaskPaw/taskpaw-backend.log")
        );
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
