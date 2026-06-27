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
use tauri::{Manager, RunEvent};

/// Holds the spawned backend child so we can terminate it on exit.
struct Backend(Mutex<Option<Child>>);

/// Spawn the headless backend. The command is provided via TASKPAW_BACKEND_CMD
/// (a packaged build points this at the bundled Python/service binary). If unset,
/// the shell still runs and the UI connects to an already-running backend.
fn spawn_backend() -> Option<Child> {
    let cmd = std::env::var("TASKPAW_BACKEND_CMD").ok()?;
    if cmd.trim().is_empty() {
        return None;
    }
    #[cfg(windows)]
    let child = Command::new("cmd").args(["/C", &cmd]).spawn();
    #[cfg(not(windows))]
    let child = Command::new("sh").args(["-c", &cmd]).spawn();
    match child {
        Ok(c) => Some(c),
        Err(e) => {
            eprintln!("failed to spawn backend: {e}");
            None
        }
    }
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
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
