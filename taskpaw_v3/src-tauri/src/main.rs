// TaskPaw V3 desktop shell (design §7.1: "X = exit").
//
// The shell spawns the headless backend as a CHILD process and kills it when the
// app exits (closing the last window → ExitRequested), so there are no orphan
// processes holding ports — the V2 "click X → tray → zombie" problem is gone.
//
// Locked down (design §3.1): withGlobalTauri=false, empty capabilities (no
// IPC/FS), CSP restricts the webview to the loopback backend. The UI is pure web
// talking to the local backend over HTTP; it uses no Tauri commands.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

/// Holds the spawned backend child so we can terminate it on exit.
struct Backend(Mutex<Option<Child>>);

/// Spawn the headless backend DIRECTLY (no shell wrapper) so the stored Child is
/// the real long-running backend — closing the app then actually terminates it
/// (a `sh -c`/`cmd /C` wrapper could leave the backend orphaned holding its
/// port), and we don't violate the repo's no-shell invariant.
///
/// `TASKPAW_BACKEND_CMD` is the executable path; `TASKPAW_BACKEND_ARGS` (optional)
/// is whitespace-separated argv. A packaged build points these at the bundled
/// service binary. If unset, the UI connects to an already-running backend.
fn spawn_backend() -> Option<Child> {
    let program = std::env::var("TASKPAW_BACKEND_CMD").ok()?;
    if program.trim().is_empty() {
        return None;
    }
    let mut command = Command::new(&program);
    if let Ok(args) = std::env::var("TASKPAW_BACKEND_ARGS") {
        for arg in args.split_whitespace() {
            command.arg(arg);
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

/// Ask the backend to shut down GRACEFULLY (SIGTERM) so its GracefulShutdown
/// handler stops the supervisor + terminates managed child processes (e.g.
/// lada-cli) and releases ports. On Unix only — Windows has no SIGTERM.
#[cfg(unix)]
fn request_graceful(child: &Child) {
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
}
#[cfg(not(unix))]
fn request_graceful(_child: &Child) {
    // Windows: no SIGTERM. A future hardening is a Job Object so the whole child
    // tree is terminated; for now we fall back to kill() below.
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                // Graceful first → let the backend tear down its supervisor +
                // managed children, then force-kill only if it doesn't exit.
                request_graceful(child);
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    match child.try_wait() {
                        Ok(Some(_)) => return,            // exited cleanly
                        Ok(None) if Instant::now() < deadline => {
                            std::thread::sleep(Duration::from_millis(100));
                        }
                        _ => break,                       // timed out or error
                    }
                }
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            app.manage(Backend(Mutex::new(spawn_backend())));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building TaskPaw")
        .run(|app, event| {
            // Closing the last window raises ExitRequested → tear the backend down.
            if let RunEvent::ExitRequested { .. } = event {
                kill_backend(app);
            }
        });
}
