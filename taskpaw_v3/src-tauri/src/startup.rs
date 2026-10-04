//! Fixed, nonsecret startup failures and explicitly confirmed desktop recovery.
use serde::Deserialize;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Code {
    MigrationRequired,
    InitializationRequired,
    StateRecoveryRequired,
    StateLeaseUnavailable,
    ConfigInvalid,
    ConfigUnwritable,
    PortInUse,
    BindAddressUnavailable,
    StartupFailed,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FailureFrame {
    taskpaw_startup_error: u8,
    role: String,
    code: Code,
}

/// Some means this line claims the startup-error protocol, including invalid frames.
pub fn parse_failure(line: &str) -> Option<Result<Code, ()>> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    value.get("taskpaw_startup_error")?;
    // Parse the original string again: reject duplicate fields, not only extra keys.
    Some(
        serde_json::from_str::<FailureFrame>(line)
            .map_err(|_| ())
            .and_then(|f| {
                if f.taskpaw_startup_error == 1 && f.role == "agent" {
                    Ok(f.code)
                } else {
                    Err(())
                }
            }),
    )
}

impl Code {
    pub fn message(self) -> &'static str {
        match self {
            Self::MigrationRequired => "旧版事件计数器需要一次明确确认的升级。不会自动重置事件 ID。",
            Self::InitializationRequired => "事件状态尚未初始化。只有确认旧配对已停用后，才能建立新身份。",
            Self::StateRecoveryRequired => "事件状态损坏、不一致或身份不匹配。请退出应用，按事件计数器恢复指南核对完整证据；不要删除或重置状态文件。",
            Self::StateLeaseUnavailable => "事件状态正在使用或无法取得排他锁。请先退出其他 TaskPaw Agent，再重试。",
            Self::ConfigInvalid => "Agent 配置无效。请核对现有配置；不会用默认配置覆盖它。",
            Self::ConfigUnwritable => "Agent 配置目录无法写入。请检查本机目录权限和可用空间。",
            Self::PortInUse => "Agent 端口被占用。请检查冲突服务；不会连接到不明后端。",
            Self::BindAddressUnavailable => "Agent 配置的绑定地址不可用。请检查当前网络地址与配置。",
            Self::StartupFailed => "Agent 启动失败。请查看本次后端日志，不要删除配置或事件状态。",
        }
    }
    fn action(self) -> Option<Action> {
        match self {
            Self::MigrationRequired => Some(Action::Migrate),
            Self::InitializationRequired => Some(Action::Initialize),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Migrate,
    Initialize,
}
impl Action {
    pub fn args(self) -> [&'static str; 3] {
        match self {
            Self::Migrate => [
                "agent-desktop-state",
                "migrate",
                "--confirm-intact-legacy-counter",
            ],
            Self::Initialize => ["agent-desktop-state", "initialize", "--confirm-new-pairing"],
        }
    }
    pub fn confirmation(self) -> &'static str {
        match self {
            Self::Migrate => "检测到旧版事件计数器。只有该记录完整、未被手工替换或回滚时才能继续。\n确认后将备份原记录，保留当前 Agent 身份和计数器，不从 1 开始。现有 Hub 事件通道可能还需按恢复指南采纳新事件流。\n你确认旧记录完整且未回滚，并同意升级吗？",
            Self::Initialize => "尚无可靠事件状态。继续将生成新 Agent 身份、新事件流，并从 ID 1 开始；不得把它当成旧设备的连续历史。\n只有已停用全部旧 Hub 配对、准备以新注册连接时才能继续。存在故障备份或任何现有状态时将拒绝初始化。\n你确认旧配对已停用，并同意建立新身份吗？",
        }
    }
}

pub fn recovery_eligible(
    supported_desktop: bool,
    bundled: bool,
    role: &str,
    args: &[String],
) -> bool {
    supported_desktop && bundled && role == "agent" && args == ["agent"]
}

#[derive(Debug, PartialEq, Eq)]
pub enum RecoveryError {
    Unavailable,
    Cancelled,
    HelperFailed,
}

/// At most one confirmation/helper/restart attempt per shell launch.
pub fn recover_once(
    code: Code,
    eligible: bool,
    attempted: &mut bool,
    confirm: impl FnOnce(Action) -> bool,
    helper: impl FnOnce(Action) -> bool,
) -> Result<(), RecoveryError> {
    let action = code
        .action()
        .filter(|_| eligible && !*attempted)
        .ok_or(RecoveryError::Unavailable)?;
    *attempted = true;
    if !confirm(action) {
        return Err(RecoveryError::Cancelled);
    }
    if !helper(action) {
        return Err(RecoveryError::HelperFailed);
    }
    Ok(())
}

struct OwnedChild(Option<Child>);
impl OwnedChild {
    fn cleanup(&mut self) -> bool {
        let Some(mut child) = self.0.take() else {
            return true;
        };
        #[cfg(target_os = "macos")]
        {
            // waitid(WNOWAIT) keeps this leader PID allocated until here. Kill the
            // assigned private group BEFORE reaping, even when the leader exited.
            super::signal_group(&child, libc::SIGKILL);
            child.wait().is_ok()
        }
        #[cfg(not(target_os = "macos"))]
        {
            super::terminate_child(&mut child);
            matches!(child.try_wait(), Ok(Some(_)))
        }
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn observed_exit(child: &mut Child) -> Result<Option<bool>, std::io::Error> {
    #[cfg(target_os = "macos")]
    {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                child.id(),
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok((info.si_pid != 0).then_some(info.si_code == libc::CLD_EXITED && info.si_status == 0))
    }
    #[cfg(not(target_os = "macos"))]
    {
        child.try_wait().map(|s| s.map(|s| s.success()))
    }
}

/// No pipes: fixed dialogs return confirmation through status; offline stdout is ignored.
/// Caller selects stderr policy. Every path closes the owned group before leader reap.
pub fn run_owned(command: &mut Command, timeout: Duration) -> bool {
    command.stdin(Stdio::null()).stdout(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    let end = Instant::now() + timeout;
    let Ok(child) = command.spawn() else {
        return false;
    };
    // Keep this job alive until after the owned helper has been reaped; closing
    // it also removes PyInstaller descendants on timeout/failure.
    #[cfg(windows)]
    let _job = match super::jobobj::assign(&child) {
        Some(job) => job,
        None => {
            let mut child = child;
            let _ = child.kill();
            let _ = child.wait();
            return false;
        }
    };
    let mut child = OwnedChild(Some(child));
    let success = loop {
        match observed_exit(child.0.as_mut().unwrap()) {
            Ok(Some(success)) => break success,
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(20)),
            _ => break false,
        }
    };
    let clean = child.cleanup();
    success && clean
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_startup_frames_reject_untrusted_actions_and_fields() {
        for code in [
            "migration_required",
            "initialization_required",
            "state_recovery_required",
            "state_lease_unavailable",
            "config_invalid",
            "config_unwritable",
            "port_in_use",
            "bind_address_unavailable",
            "startup_failed",
        ] {
            let line = format!(r#"{{"taskpaw_startup_error":1,"role":"agent","code":"{code}"}}"#);
            assert!(parse_failure(&line).unwrap().is_ok());
        }
        for line in [
            r#"{"taskpaw_startup_error":true,"role":"agent","code":"migration_required"}"#,
            r#"{"taskpaw_startup_error":2,"role":"agent","code":"migration_required"}"#,
            r#"{"taskpaw_startup_error":1,"role":"hub","code":"migration_required"}"#,
            r#"{"taskpaw_startup_error":1,"role":"agent","code":"reset"}"#,
            r#"{"taskpaw_startup_error":1,"role":"agent","code":"migration_required","path":"fake-secret"}"#,
            r#"{"taskpaw_startup_error":1,"role":"agent","role":"agent","code":"migration_required"}"#,
            r#"{"taskpaw_startup_error":1,"role":"agent"}"#,
        ] {
            assert_eq!(parse_failure(line), Some(Err(())));
        }
        assert!(parse_failure("not-json fake-secret").is_none());
    }
    #[test]
    fn recovery_only_for_owned_supported_desktop_agent_with_fixed_arguments() {
        let args = vec!["agent".to_string()];
        assert!(recovery_eligible(true, true, "agent", &args));
        assert!(!recovery_eligible(false, true, "agent", &args));
        assert!(!recovery_eligible(true, false, "agent", &args));
        assert!(!recovery_eligible(true, true, "hub", &args));
        assert!(!recovery_eligible(true, true, "agent", &[]));
        assert_eq!(
            Action::Migrate.args(),
            [
                "agent-desktop-state",
                "migrate",
                "--confirm-intact-legacy-counter"
            ]
        );
        assert_eq!(
            Action::Initialize.args(),
            ["agent-desktop-state", "initialize", "--confirm-new-pairing"]
        );
    }
    #[test]
    fn cancel_fault_and_repeat_never_run_an_unconfirmed_helper() {
        let mut attempted = false;
        assert_eq!(
            recover_once(
                Code::MigrationRequired,
                true,
                &mut attempted,
                |_| false,
                |_| panic!("cancel must not write")
            ),
            Err(RecoveryError::Cancelled)
        );
        assert_eq!(
            recover_once(
                Code::MigrationRequired,
                true,
                &mut attempted,
                |_| panic!("no repeat"),
                |_| panic!("no repeat")
            ),
            Err(RecoveryError::Unavailable)
        );
        let mut attempted = false;
        assert_eq!(
            recover_once(
                Code::StateRecoveryRequired,
                true,
                &mut attempted,
                |_| panic!("no reset"),
                |_| panic!("no reset")
            ),
            Err(RecoveryError::Unavailable)
        );
        assert!(!attempted);
        assert_eq!(
            recover_once(
                Code::InitializationRequired,
                true,
                &mut attempted,
                |_| true,
                |_| false
            ),
            Err(RecoveryError::HelperFailed)
        );
        assert!(attempted);
    }
    #[test]
    fn successful_recovery_allows_only_one_restart() {
        let mut attempted = false;
        let mut helpers = 0;
        assert_eq!(
            recover_once(
                Code::MigrationRequired,
                true,
                &mut attempted,
                |_| true,
                |a| {
                    assert_eq!(a, Action::Migrate);
                    helpers += 1;
                    true
                }
            ),
            Ok(())
        );
        assert_eq!(helpers, 1);
        assert_eq!(
            recover_once(
                Code::InitializationRequired,
                true,
                &mut attempted,
                |_| panic!("no second dialog"),
                |_| panic!("no second helper")
            ),
            Err(RecoveryError::Unavailable)
        );
    }
    #[test]
    fn owned_command_timeout_and_nonzero_are_not_confirmation() {
        let start = Instant::now();
        assert!(!run_owned(
            Command::new("node").args(["-e", "setTimeout(()=>{},10000)"]),
            Duration::from_millis(10)
        ));
        assert!(start.elapsed() < Duration::from_secs(7));
        assert!(!run_owned(
            Command::new("node").args(["-e", "process.exit(1)"]),
            Duration::from_secs(2)
        ));
        assert!(run_owned(
            Command::new("node").args(["-e", "process.exit(0)"]),
            Duration::from_secs(2)
        ));
    }
    #[cfg(windows)]
    #[test]
    fn windows_owned_helper_job_removes_children_after_success_or_timeout() {
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0, WAIT_TIMEOUT};
        use windows_sys::Win32::System::Threading::{
            OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
        };
        let root = std::env::temp_dir().join(format!(
            "taskpaw-windows-helper-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        for timeout in [false, true] {
            let path = root.join(if timeout { "timeout.pid" } else { "exit.pid" });
            let child_script = "require('fs').writeFileSync(process.argv[1],String(process.pid));setInterval(()=>{},1000)";
            let parent_script = "const {spawn}=require('child_process');const fs=require('fs');spawn(process.execPath,['-e',process.argv[1],process.argv[2]],{stdio:'ignore'});const t=setInterval(()=>{if(fs.existsSync(process.argv[2])){clearInterval(t);if(process.argv[3]==='exit')process.exit(0)}},5);setInterval(()=>{},1000)";
            let result = run_owned(
                Command::new("node")
                    .args(["-e", parent_script, child_script])
                    .arg(&path)
                    .arg(if timeout { "timeout" } else { "exit" }),
                Duration::from_secs(3),
            );
            assert_eq!(result, !timeout);
            let pid: u32 = std::fs::read_to_string(&path).unwrap().parse().unwrap();
            let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
            if handle.is_null() {
                assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(87));
            } else {
                let status = unsafe { WaitForSingleObject(handle, 2000) };
                unsafe {
                    CloseHandle(handle);
                }
                assert_ne!(
                    status, WAIT_TIMEOUT,
                    "helper's descendant survived Job closure"
                );
                assert_eq!(status, WAIT_OBJECT_0);
            }
        }
        std::fs::remove_dir_all(root).unwrap();
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn owned_group_children_are_gone_after_leader_exit_or_timeout() {
        let root = std::env::temp_dir().join(format!(
            "taskpaw-owned-startup-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        for timeout in [false, true] {
            let path = root.join(if timeout { "timeout.pid" } else { "exit.pid" });
            let child_script = "process.on('SIGTERM',()=>{});require('fs').writeFileSync(process.argv[1],String(process.pid));setInterval(()=>{},1000)";
            let parent_script = "const {spawn}=require('child_process');const fs=require('fs');spawn(process.execPath,['-e',process.argv[1],process.argv[2]],{stdio:'ignore'});const t=setInterval(()=>{if(fs.existsSync(process.argv[2])){clearInterval(t);if(process.argv[3]==='exit')process.exit(0)}},5);setInterval(()=>{},1000)";
            let mut command = Command::new("node");
            command
                .args(["-e", parent_script, child_script])
                .arg(&path)
                .arg(if timeout { "timeout" } else { "exit" });
            let success = run_owned(
                &mut command,
                if timeout {
                    Duration::from_secs(1)
                } else {
                    Duration::from_secs(3)
                },
            );
            assert_eq!(success, !timeout);
            let pid: libc::pid_t = std::fs::read_to_string(&path).unwrap().parse().unwrap();
            let end = Instant::now() + Duration::from_secs(2);
            while unsafe { libc::kill(pid, 0) } == 0 && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(10));
            }
            assert_eq!(
                unsafe { libc::kill(pid, 0) },
                -1,
                "owned group descendant must not survive its leader"
            );
            assert_eq!(
                std::io::Error::last_os_error().raw_os_error(),
                Some(libc::ESRCH)
            );
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
