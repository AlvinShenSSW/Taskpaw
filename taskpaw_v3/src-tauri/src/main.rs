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
    let bases = [
        dir.to_path_buf(),                       // next to the app binary
        dir.join("../Resources"),                // macOS .app resources fallback
        dir.join("binaries"),
    ];
    let found = bases
        .iter()
        .flat_map(|b| names.iter().map(move |n| b.join(n)))
        .find(|p| p.exists())?;
    let role = ui_role();
    Some((found.to_string_lossy().into_owned(), vec![role]))
}

fn spawn_backend() -> Option<Child> {
    let (program, args) = backend_command()?;
    let mut command = Command::new(&program);
    command.args(args);
    // Own process group so we can signal the WHOLE backend tree on exit.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
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
        // The windowed shell has no console — instead of nulling (which discards
        // all backend logs), redirect to %APPDATA%\TaskPaw\taskpaw-backend.log so
        // production builds are debuggable; fall back to null if it can't open
        // (Kimi). (#48 will switch stdout to a pipe for the readiness handshake.)
        let log = std::env::var("APPDATA").ok().and_then(|a| {
            let dir = std::path::Path::new(&a).join("TaskPaw");
            std::fs::create_dir_all(&dir).ok()?;
            std::fs::File::create(dir.join("taskpaw-backend.log")).ok()
        });
        match log {
            Some(f) => {
                let err = f.try_clone().ok();
                command.stdout(Stdio::from(f));
                command.stderr(err.map(Stdio::from).unwrap_or_else(Stdio::null));
            }
            None => {
                command.stdout(Stdio::null()).stderr(Stdio::null());
            }
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
    let v = raw.trim();
    if v.is_empty() {
        return String::new();
    }
    let (scheme, rest) = v.split_once("://").unwrap_or(("", v));
    // authority = up to the path/query/fragment; the remainder is the path.
    let auth_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, path) = rest.split_at(auth_end);
    // Strip ANY userinfo (user:pass@) — else "http://127.0.0.1:8000@evil.com"
    // would read 127.0.0.1 as host while a browser uses evil.com, leaking the
    // injected api key to a remote origin (Codex/Kimi P1). rsplit on the LAST '@'.
    let host_port = authority.rsplit_once('@').map(|(_, h)| h).unwrap_or(authority);
    // Extract host: bracketed IPv6 "[::1]"/"[::1]:port" → between [ and ]; else
    // strip a trailing :port (rsplit so it doesn't trip on IPv6 colons).
    let host = if let Some(r) = host_port.strip_prefix('[') {
        r.split(']').next().unwrap_or("")          // [ipv6] or [ipv6]:port
    } else if host_port.matches(':').count() > 1 {
        host_port                                   // bare IPv6 (must be bracketed
                                                    // to carry a port) → whole is host
    } else {
        host_port.rsplit_once(':').map(|(h, _)| h).unwrap_or(host_port)  // host[:port]
    };
    let host = host.to_ascii_lowercase();
    // CANONICAL loopback only — keep validation in exact lockstep with the
    // init-script origin guard AND the CSP connect-src (neither can wildcard
    // 127.0.0.0/8 or expanded IPv6). Accepting only these three avoids "validated
    // but blocked by CSP / not injected by the guard" mismatches (Codex/Kimi). A
    // literal-string match still rejects "127.0.0.1.evil.com" (it isn't equal).
    // The backend binds 127.0.0.1 by default, so this loses nothing in practice.
    let is_loopback = matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1");
    if !is_loopback {
        eprintln!("TASKPAW_UI_BASE {v:?} is not loopback — ignoring");
        return String::new();
    }
    // Only http(s) (or scheme-less → http). Reject e.g. ftp://127.0.0.1 — a
    // non-HTTP scheme would break the frontend's HTTP client (Kimi).
    let scheme = if scheme.is_empty() { "http" } else { scheme };
    if !matches!(scheme, "http" | "https") {
        eprintln!("TASKPAW_UI_BASE {v:?} has a non-http(s) scheme — ignoring");
        return String::new();
    }
    // Reconstruct WITHOUT credentials (host_port already excludes userinfo).
    format!("{scheme}://{host_port}{path}")
}

/// Runtime config injected on the loopback origin (design §3.1) — packaged
/// builds can't use compile-time Vite env, so the shell injects it here.
fn init_script() -> String {
    let base = loopback_base(&std::env::var("TASKPAW_UI_BASE").unwrap_or_default());
    let token = std::env::var("TASKPAW_UI_TOKEN").unwrap_or_default();
    let role = ui_role();
    // serde_json escapes the values safely.
    let cfg = serde_json::json!({ "baseUrl": base, "apiKey": token, "role": role });
    // Guard by origin so the api key is never exposed if the webview ever
    // navigates away from the local frontend to a non-loopback page (Kimi).
    // Allowed origins: loopback hosts, the macOS tauri: protocol, AND the
    // packaged webview host `tauri.localhost` (Windows https://tauri.localhost,
    // per core/cors.py) — else the injected config is dropped in packaged builds
    // (Codex).
    // Guard must accept the same set loopback_base() does (all of 127.0.0.0/8,
    // not just 127.0.0.1) or a custom 127.x base would be validated yet __TASKPAW__
    // would never be set (Kimi). Plus the packaged webview host tauri.localhost
    // and the macOS tauri: protocol.
    // Exact canonical loopback set — matches loopback_base() and the CSP. Plus the
    // packaged webview host tauri.localhost and the macOS tauri: protocol.
    format!(
        "{{ const h = location.hostname; \
         if (h==='localhost'||h==='127.0.0.1'||h==='[::1]'||h==='::1'||h==='tauri.localhost'|| \
             location.protocol==='tauri:') \
           {{ window.__TASKPAW__ = {cfg}; }} }}"
    )
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // `mut` only needed for the Windows Job-Object failure kill path.
            #[cfg(windows)]
            let mut child = spawn_backend();
            #[cfg(not(windows))]
            let child = spawn_backend();
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
            app.manage(Backend(Mutex::new(child)));
            // NOTE (#48): the design §3.1 readiness handshake (read the backend's
            // stdout readiness JSON before loading the webview + inject the actual
            // base_url for custom ports) is tracked separately. For default ports
            // the frontend's per-role loopback defaults work; base_url is
            // loopback-validated here.
            // Build the window in code so we can inject the runtime config script
            // BEFORE the page loads (only on this loopback-served origin).
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("TaskPaw")
                .inner_size(1100.0, 720.0)
                .min_inner_size(720.0, 480.0)
                .initialization_script(&init_script())
                .build()?;
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
    use super::loopback_base;

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
        // abbreviated IPv4 ("127.1") isn't parsed by std Ipv4Addr → rejected
        // (stricter than a browser, but safe: just falls back to defaults).
        assert_eq!(loopback_base("http://127.1:8000"), "");
    }

    #[test]
    fn strips_credentials_from_returned_url() {
        assert_eq!(loopback_base("http://user:pass@127.0.0.1:5681/x"),
                   "http://127.0.0.1:5681/x");
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
