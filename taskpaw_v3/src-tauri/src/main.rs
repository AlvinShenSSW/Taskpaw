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
    let name = if cfg!(windows) { "taskpaw-backend.exe" } else { "taskpaw-backend" };
    let candidates = [
        dir.join(name),                              // next to the app binary
        dir.join("../Resources").join(name),         // macOS .app resources fallback
        dir.join("binaries").join(name),
    ];
    let found = candidates.iter().find(|p| p.exists())?;
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
    // Don't pop a console window for the console-subsystem backend exe on Windows
    // (the Tauri shell is a windowed app) (Kimi).
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
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

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                // Graceful first → the backend's GracefulShutdown stops the
                // supervisor + managed children.
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
                // Force-kill the whole group (Unix) / the process (Windows; the
                // Job Object reaps the rest of the tree).
                #[cfg(unix)]
                signal_group(child, libc::SIGKILL);
                #[cfg(not(unix))]
                let _ = child.kill();
                let _ = child.wait();
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
        r.split(']').next().unwrap_or("")
    } else {
        host_port.rsplit_once(':').map(|(h, _)| h).unwrap_or(host_port)
    };
    let host = host.to_ascii_lowercase();
    // Parse the IPv4 form so a *hostname* like "127.0.0.1.evil.com" (which a
    // browser resolves to a remote IP) is NOT accepted — a prefix check would
    // leak the api key (Codex/Kimi P1). Accept localhost + IPv6 ::1 + 127.0.0.0/8.
    let is_loopback = host == "localhost"
        || host == "::1"
        || host.parse::<std::net::Ipv4Addr>().map(|ip| ip.is_loopback()).unwrap_or(false);
    if !is_loopback {
        eprintln!("TASKPAW_UI_BASE {v:?} is not loopback — ignoring");
        return String::new();
    }
    // Reconstruct WITHOUT credentials (host_port already excludes userinfo).
    if scheme.is_empty() {
        format!("{host_port}{path}")
    } else {
        format!("{scheme}://{host_port}{path}")
    }
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
    format!(
        "if (['localhost','127.0.0.1','[::1]','::1','tauri.localhost'].includes(location.hostname) \
         || location.protocol === 'tauri:') {{ window.__TASKPAW__ = {cfg}; }}"
    )
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let mut child = spawn_backend();
            // Bundled mode (no dev override): a missing/failed sidecar means the
            // UI would open with no backend — fail LOUD rather than silently
            // broken (Kimi). Dev (explicit TASKPAW_BACKEND_CMD) stays lenient.
            if child.is_none() && std::env::var_os("TASKPAW_BACKEND_CMD").is_none() {
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
