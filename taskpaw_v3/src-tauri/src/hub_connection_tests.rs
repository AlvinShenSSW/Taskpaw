use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;

#[test]
fn packaged_mac_hub_cannot_become_an_owned_agent() {
    for runtime in [
        None,
        Some(""),
        Some("agent"),
        Some("invalid"),
        Some(" HUB "),
    ] {
        assert_eq!(effective_role(true, Some("hub"), runtime), "hub");
        assert!(connect_only(
            true,
            &effective_role(true, Some("hub"), runtime)
        ));
    }
    assert!(!connect_only(false, "hub"));
    assert!(!connect_only(true, "agent"));
}

#[test]
fn other_shell_roles_keep_existing_override_semantics() {
    assert_eq!(effective_role(true, Some("agent"), Some(" HUB ")), "hub");
    assert_eq!(effective_role(false, Some("hub"), Some("agent")), "agent");
    assert_eq!(effective_role(false, Some("agent"), Some("typo")), "agent");
    assert_eq!(effective_role(true, None, None), "agent");
}

#[cfg(unix)]
struct Fixture(std::path::PathBuf);
#[cfg(unix)]
impl Fixture {
    fn new() -> Self {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!(
                "taskpaw-hub-connection-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        Self(dir)
    }
    fn path(&self) -> std::path::PathBuf {
        self.0.join("hub.control.json")
    }
    fn raw(&self, bytes: &[u8]) {
        use std::os::unix::fs::PermissionsExt;
        let temp = self.0.join("new.control.json");
        std::fs::write(&temp, bytes).unwrap();
        std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::rename(temp, self.path()).unwrap();
    }
    fn descriptor(&self, role: &str, base: &str, boot: &str, token: &str) {
        self.raw(serde_json::json!({"version":1,"role":role,"base_url":base,"boot_id":boot,"control_token":token}).to_string().as_bytes());
    }
}
#[cfg(unix)]
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
const BOOT: &str = "0123456789abcdef0123456789abcdef";
const BASE: &str = "http://127.0.0.1:5691";

#[cfg(unix)]
#[test]
fn protected_hub_reader_rejects_missing_invalid_wrong_role_and_unsafe_files() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let fixture = Fixture::new();
    let input = Input {
        path: Some(fixture.path()),
        expected_base: None,
    };
    assert!(input.read().is_none());
    fixture.raw(b"invalid-secret-marker");
    assert!(input.read().is_none());
    fixture.descriptor("agent", BASE, BOOT, "fake-token");
    assert!(input.read().is_none());
    fixture.descriptor("hub", "http://10.0.0.1:5691", BOOT, "fake-token");
    assert!(input.read().is_none());
    fixture.descriptor("hub", BASE, BOOT, "fake-token");
    assert!(input.read().is_some());
    assert!(Input {
        path: Some(fixture.path()),
        expected_base: Some("http://127.0.0.1:8888".into())
    }
    .read()
    .is_none());
    std::fs::set_permissions(fixture.path(), std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(input.read().is_none());
    fixture.descriptor("hub", BASE, BOOT, "fake-token");
    let link = fixture.0.join("link.control.json");
    symlink(fixture.path(), &link).unwrap();
    assert!(Input {
        path: Some(link),
        expected_base: None
    }
    .read()
    .is_none());
    fixture.raw(&vec![b' '; 16 * 1024 + 1]);
    assert!(input.read().is_none());
}

#[cfg(unix)]
#[test]
fn changes_rebind_once_and_identical_failures_do_not_rebuild() {
    let fixture = Fixture::new();
    let input = Input {
        path: Some(fixture.path()),
        expected_base: None,
    };
    let (tx, rx) = mpsc::channel();
    let mut reader = Reader::start_with(
        None,
        Duration::from_millis(20),
        move || input.read(),
        move |value, _| {
            tx.send(value.map(|d| (d.boot_id, d.control_token, d.base_url)))
                .unwrap();
            true
        },
    )
    .unwrap();
    assert!(rx.recv_timeout(Duration::from_millis(100)).is_err());
    fixture.descriptor("hub", BASE, BOOT, "fake-token-1");
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(3)).unwrap().unwrap().1,
        "fake-token-1"
    );
    assert!(rx.recv_timeout(Duration::from_millis(100)).is_err());
    fixture.descriptor("hub", BASE, BOOT, "fake-token-2");
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(3)).unwrap().unwrap().1,
        "fake-token-2"
    );
    let new_boot = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    fixture.descriptor("hub", BASE, new_boot, "fake-token-2");
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(3)).unwrap().unwrap().0,
        new_boot
    );
    fixture.descriptor("hub", "http://127.0.0.1:7777", new_boot, "fake-token-2");
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(3)).unwrap().unwrap().2,
        "http://127.0.0.1:7777"
    );
    fixture.raw(b"bad credentials first");
    assert!(rx.recv_timeout(Duration::from_secs(3)).unwrap().is_none());
    fixture.raw(b"different bad credentials");
    assert!(rx.recv_timeout(Duration::from_millis(120)).is_err());
    fixture.descriptor("hub", BASE, BOOT, "fake-token-3");
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(3)).unwrap().unwrap().1,
        "fake-token-3"
    );
    reader.stop();
    assert!(rx.recv_timeout(Duration::from_secs(1)).is_err());
}

#[test]
fn poll_frequency_and_shutdown_are_bounded_and_drop_joins() {
    let reads = Arc::new(AtomicUsize::new(0));
    let counter = reads.clone();
    let mut reader = Reader::start_with(
        None,
        Duration::from_millis(50),
        move || {
            counter.fetch_add(1, Ordering::SeqCst);
            None
        },
        |_, _| panic!("identical unavailable state must not rebuild"),
    )
    .unwrap();
    std::thread::sleep(Duration::from_millis(180));
    reader.stop();
    let count = reads.load(Ordering::SeqCst);
    assert!(
        (1..=4).contains(&count),
        "reader busy-looped or never sampled: {count}"
    );
    std::thread::sleep(Duration::from_millis(80));
    assert_eq!(reads.load(Ordering::SeqCst), count);
    reader.stop(); // joined shutdown is idempotent
    let start = std::time::Instant::now();
    drop(Reader::start_with(None, Duration::from_secs(60), || None, |_, _| true).unwrap());
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[test]
fn quitting_cancels_an_inflight_window_callback_without_owning_its_service() {
    let (tx, rx) = mpsc::channel();
    let value = control_credentials::parse_descriptor(serde_json::json!({"version":1,"role":"hub","base_url":BASE,"boot_id":BOOT,"control_token":"fake-token"}).to_string().as_bytes()).unwrap();
    let mut reader = Reader::start_with(
        None,
        Duration::from_millis(10),
        move || Some(value.clone()),
        move |_, stop| {
            tx.send(()).unwrap();
            assert!(stop.wait(Duration::from_secs(60)));
            false
        },
    )
    .unwrap();
    rx.recv_timeout(Duration::from_secs(3)).unwrap();
    let start = std::time::Instant::now();
    reader.stop();
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[test]
fn reader_exit_leaves_an_independent_fixture_process_running() {
    struct FixtureChild(std::process::Child);
    impl Drop for FixtureChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    // This is an isolated fake service, not TaskPaw or a machine's real Hub.
    // Only this test's guard owns it; the reader never receives its PID/handle.
    let mut service = FixtureChild(
        std::process::Command::new("node")
            .args(["-e", "setInterval(()=>{},1000)"])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap(),
    );
    let reader = Reader::start_with(None, Duration::from_millis(20), || None, |_, _| true).unwrap();
    drop(reader);
    assert!(
        service.0.try_wait().unwrap().is_none(),
        "closing the client stopped the fixture service"
    );
}

#[test]
fn main_thread_reply_wait_is_cancelled_before_join() {
    let stop = Stop::default();
    let worker_stop = stop.clone();
    let (tx, rx) = mpsc::channel::<()>();
    let waiter = std::thread::spawn(move || reply(rx, &worker_stop));
    // Keep the sender alive: an unresponsive main thread must not block quit.
    let start = std::time::Instant::now();
    stop.stop();
    assert_eq!(waiter.join().unwrap(), Err(()));
    assert!(start.elapsed() < Duration::from_secs(1));
    drop(tx);
}

#[test]
fn failed_window_application_ends_reader_instead_of_rebuild_storm() {
    let count = Arc::new(AtomicUsize::new(0));
    let calls = count.clone();
    let value = control_credentials::parse_descriptor(serde_json::json!({"version":1,"role":"hub","base_url":BASE,"boot_id":BOOT,"control_token":"fake-token"}).to_string().as_bytes()).unwrap();
    let (tx, rx) = mpsc::channel();
    let mut reader = Reader::start_with(
        None,
        Duration::from_millis(10),
        move || Some(value.clone()),
        move |_, _| {
            calls.fetch_add(1, Ordering::SeqCst);
            tx.send(()).unwrap();
            false
        },
    )
    .unwrap();
    rx.recv_timeout(Duration::from_secs(3)).unwrap();
    reader.stop();
    assert_eq!(count.load(Ordering::SeqCst), 1);
}

#[test]
fn hub_initialization_rebinds_credentials_without_restart_capability() {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let descriptor = control_credentials::parse_descriptor(br#"{"version":1,"role":"hub","base_url":"http://127.0.0.1:5691","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-hub-first"}"#).unwrap();
    let mut replacement = descriptor.clone();
    replacement.boot_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into();
    replacement.control_token = "fake-hub-second".into();
    let harness = r#"
const vm=require('node:vm'), assert=require('node:assert/strict');
let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{
 const scripts=JSON.parse(input);
 for(const [i,script] of scripts.entries()) {
  for(const href of ['tauri://localhost/index.html','https://tauri.localhost/','https://foreign.example/','http://127.0.0.1:5691/','tauri://foreign/','file:///tmp/index.html']) {
   for(const top of [true,false]) {
    const window={};window.top=top?window:{};
    vm.runInNewContext(script,{window,location:new URL(href),URL});
    const trusted=top&&['tauri://localhost/index.html','https://tauri.localhost/'].includes(href);
    if(!trusted) { assert.equal(window.__TASKPAW__,undefined); continue; }
    assert.equal(window.__TASKPAW__.restart,false);
    assert.equal(window.__TASKPAW__.role,'hub');
    assert.equal(window.__TASKPAW__.controlToken,i?'fake-hub-second':'fake-hub-first');
    assert.equal(window.__TASKPAW__.bootId,i?'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa':'0123456789abcdef0123456789abcdef');
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
    let input = serde_json::json!([
        crate::init_script(&descriptor, false, false),
        crate::init_script(&replacement, false, false),
    ]);
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.to_string().as_bytes())
        .unwrap();
    assert!(
        child.wait_with_output().unwrap().status.success(),
        "Hub initialization guard or capability failed"
    );
}
