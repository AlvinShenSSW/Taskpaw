//! Mac Hub desktop connects to the independent service; it never owns it.
use crate::control_credentials::{self, Descriptor};
use crate::{
    build_main_window, default_credential_path, restart_supervisor, trusted_ui_navigation,
    REBUILDING,
};
use std::path::PathBuf;
use std::sync::atomic::Ordering::SeqCst;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

pub const POLL_INTERVAL: Duration = Duration::from_secs(2);

pub fn effective_role(macos: bool, baked: Option<&str>, runtime: Option<&str>) -> String {
    // A packaged Mac Hub cannot reach the Agent's child-owning startup through
    // a runtime environment override. Preserve existing overrides elsewhere.
    if macos && baked.is_some_and(|value| value.trim().eq_ignore_ascii_case("hub")) {
        return "hub".into();
    }
    let raw = runtime
        .filter(|s| !s.trim().is_empty())
        .or_else(|| baked.filter(|s| !s.trim().is_empty()))
        .unwrap_or("agent");
    match raw.trim().to_ascii_lowercase().as_str() {
        "hub" => "hub".into(),
        _ => "agent".into(),
    }
}

pub fn connect_only(macos: bool, role: &str) -> bool {
    macos && role == "hub"
}

pub struct Input {
    pub path: Option<PathBuf>,
    pub expected_base: Option<String>,
}
impl Input {
    pub fn read(&self) -> Option<Descriptor> {
        // The shared reader bounds size, validates the same open file object,
        // and rejects symlinks/unsafe ancestors, ownership, modes and ACLs.
        let descriptor = control_credentials::read_descriptor(self.path.as_ref()?).ok()?;
        if descriptor.role != "hub"
            || self
                .expected_base
                .as_ref()
                .is_some_and(|base| *base != descriptor.base_url)
        {
            return None;
        }
        Some(descriptor)
    }
}

fn same(left: &Option<Descriptor>, right: &Option<Descriptor>) -> bool {
    match (left, right) {
        (None, None) => true,
        (Some(a), Some(b)) => {
            a.boot_id == b.boot_id && a.base_url == b.base_url && a.control_token == b.control_token
        }
        _ => false,
    }
}

#[derive(Clone, Default)]
pub struct Stop(Arc<(Mutex<bool>, Condvar)>);
impl Stop {
    pub fn stopped(&self) -> bool {
        *self.0 .0.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Interruptible monotonic wait; true means shutdown has been requested.
    pub fn wait(&self, duration: Duration) -> bool {
        let guard = self.0 .0.lock().unwrap_or_else(|e| e.into_inner());
        *self
            .0
             .1
            .wait_timeout_while(guard, duration, |stopped| !*stopped)
            .unwrap_or_else(|e| e.into_inner())
            .0
    }
    fn stop(&self) {
        *self.0 .0.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.0 .1.notify_all();
    }
}

/// One reader and at most one window replacement at a time. It has no backend
/// command, process handle or service API. GUI shutdown only cancels/joins it.
pub struct Reader {
    stop: Stop,
    thread: Option<JoinHandle<()>>,
}
impl Reader {
    pub fn start(
        input: Input,
        initial: Option<Descriptor>,
        apply: impl FnMut(Option<Descriptor>, &Stop) -> bool + Send + 'static,
    ) -> std::io::Result<Self> {
        Self::start_with(initial, POLL_INTERVAL, move || input.read(), apply)
    }

    fn start_with(
        mut applied: Option<Descriptor>,
        interval: Duration,
        mut read: impl FnMut() -> Option<Descriptor> + Send + 'static,
        mut apply: impl FnMut(Option<Descriptor>, &Stop) -> bool + Send + 'static,
    ) -> std::io::Result<Self> {
        let stop = Stop::default();
        let worker_stop = stop.clone();
        let thread = std::thread::Builder::new()
            .name("hub-connection".into())
            .spawn(move || {
                while !worker_stop.wait(interval) {
                    let next = read();
                    if !same(&applied, &next) {
                        if worker_stop.stopped() || !apply(next.clone(), &worker_stop) {
                            break;
                        }
                        applied = next;
                    }
                }
            })?;
        Ok(Self {
            stop,
            thread: Some(thread),
        })
    }

    pub fn stop(&mut self) {
        self.stop.stop();
        if let Some(thread) = self.thread.take() {
            if thread.join().is_err() {
                eprintln!("taskpaw: Hub connection reader stopped unexpectedly");
            }
        }
    }
}
impl Drop for Reader {
    fn drop(&mut self) {
        self.stop();
    }
}

/// The Mac Hub has no `Backend` state and no restart supervisor. Its only owned
/// resource is the descriptor reader, which is cancelled and joined on exit.
struct HubReader(Mutex<Option<Reader>>);

fn build_hub_window(
    app: &tauri::AppHandle,
    descriptor: Option<&Descriptor>,
    geometry: Option<restart_supervisor::Geometry>,
) -> tauri::Result<()> {
    if let Some(descriptor) = descriptor {
        return build_main_window(app, descriptor, false, false, geometry);
    }
    // No credentials reach the waiting page. It is a packaged asset, never a
    // remote setup URL, and cannot install/start/restart the independent service.
    let mut builder =
        WebviewWindowBuilder::new(app, "main", WebviewUrl::App("hub-waiting.html".into()))
            .title("TaskPaw Hub")
            .inner_size(1100.0, 720.0)
            .min_inner_size(
                restart_supervisor::MIN_WIDTH,
                restart_supervisor::MIN_HEIGHT,
            )
            .on_navigation(|url| trusted_ui_navigation(url, cfg!(debug_assertions)));
    if let Some(g) = geometry {
        builder = builder.inner_size(g.width, g.height).maximized(g.maximized);
        if let Some((x, y)) = g.position {
            builder = builder.position(x, y);
        }
    }
    builder.build()?;
    Ok(())
}

/// Waits cannot deadlock quit: the main thread may be joining this reader, so
/// every main-thread reply wait also observes cancellation at most 25 ms later.
fn reply<T>(rx: std::sync::mpsc::Receiver<T>, stop: &Stop) -> Result<T, ()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if stop.stopped() || Instant::now() >= deadline {
            return Err(());
        }
        match rx.recv_timeout(Duration::from_millis(25)) {
            Ok(value) => return Ok(value),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return Err(()),
        }
    }
}

fn rebuild_hub_window(
    app: &tauri::AppHandle,
    descriptor: Option<Descriptor>,
    stop: &Stop,
) -> Result<(), ()> {
    if stop.stopped() {
        return Err(());
    }
    // Destroying the old webview discards its in-memory credentials and React
    // query cache. No token is put in URLs, local storage, logs or IPC messages.
    REBUILDING.store(true, SeqCst);
    let result = (|| {
        let (tx, rx) = std::sync::mpsc::channel();
        let remove = app.clone();
        let cancelled = stop.clone();
        app.run_on_main_thread(move || {
            if cancelled.stopped() {
                return;
            }
            let geometry = remove.get_webview_window("main").and_then(|window| {
                let geometry = window.inner_size().ok().map(|size| {
                    restart_supervisor::logical_geometry(
                        (size.width, size.height),
                        window
                            .outer_position()
                            .ok()
                            .map(|position| (position.x, position.y)),
                        window.scale_factor().unwrap_or(1.0),
                        window.is_maximized().unwrap_or(false),
                    )
                });
                let _ = window.destroy();
                geometry
            });
            let _ = tx.send(geometry);
        })
        .map_err(|_| ())?;
        let geometry = reply(rx, stop)?;
        let deadline = Instant::now() + Duration::from_secs(5);
        while app.get_webview_window("main").is_some() {
            if Instant::now() >= deadline || stop.wait(Duration::from_millis(25)) {
                return Err(());
            }
        }
        let (tx, rx) = std::sync::mpsc::channel();
        let build = app.clone();
        let cancelled = stop.clone();
        app.run_on_main_thread(move || {
            if cancelled.stopped() {
                return;
            }
            let _ = tx.send(build_hub_window(&build, descriptor.as_ref(), geometry).is_ok());
        })
        .map_err(|_| ())?;
        if reply(rx, stop)? {
            Ok(())
        } else {
            Err(())
        }
    })();
    REBUILDING.store(false, SeqCst);
    result
}

pub fn setup(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let input = Input {
        path: std::env::var_os("TASKPAW_CONTROL_CREDENTIAL_FILE")
            .map(std::path::PathBuf::from)
            .or_else(|| default_credential_path("hub").ok()),
        expected_base: std::env::var("TASKPAW_UI_BASE").ok(),
    };
    let descriptor = input.read();
    build_hub_window(app, descriptor.as_ref(), None)?;
    let handle = app.clone();
    let reader = Reader::start(
        input,
        descriptor,
        move |next, stop| match rebuild_hub_window(&handle, next, stop) {
            Ok(()) => true,
            Err(()) => {
                if !stop.stopped() {
                    eprintln!("taskpaw: Hub window could not reconnect; reopen the dashboard");
                    handle.exit(1);
                }
                false
            }
        },
    )?;
    app.manage(HubReader(Mutex::new(Some(reader))));
    Ok(())
}

/// Returns true only for this connect-only client. Never reaches Agent cleanup.
pub fn stop_reader(app: &tauri::AppHandle) -> bool {
    let Some(state) = app.try_state::<HubReader>() else {
        return false;
    };
    crate::EXITING.store(true, SeqCst);
    let reader = state.0.lock().unwrap_or_else(|e| e.into_inner()).take();
    drop(reader); // wakes and joins only our connection reader
    true
}

#[cfg(test)]
#[path = "hub_connection_tests.rs"]
mod tests;
