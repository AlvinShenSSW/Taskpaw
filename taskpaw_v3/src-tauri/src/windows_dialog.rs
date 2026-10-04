//! Native startup dialogs work before Tauri has created a webview/event loop.

#[derive(Clone, Copy)]
pub enum Kind {
    Error,
    Confirm,
}

// Win32 styles kept together with the result policy so both are testable on
// other build hosts. Confirmation defaults to No; API failure is never consent.
fn style(kind: Kind) -> u32 {
    const SET_FOREGROUND: u32 = 0x0001_0000;
    SET_FOREGROUND
        | match kind {
            Kind::Error => 0x10,                 // MB_OK | MB_ICONERROR
            Kind::Confirm => 0x4 | 0x30 | 0x100, // MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2
        }
}

fn confirmed(kind: Kind, result: i32) -> bool {
    matches!(kind, Kind::Confirm) && result == 6 // IDYES only
}

fn wide(value: &str) -> Vec<u16> {
    value.replace('\0', " ").encode_utf16().chain([0]).collect()
}

#[cfg(windows)]
pub fn show(message: &str, kind: Kind) -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW;
    let message = wide(message);
    let title = wide("TaskPaw — 启动");
    let result = unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            message.as_ptr(),
            title.as_ptr(),
            style(kind),
        )
    };
    confirmed(kind, result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovery_defaults_to_no_and_only_yes_confirms() {
        assert_eq!(style(Kind::Confirm) & 0xf, 4);
        assert_eq!(style(Kind::Confirm) & 0x300, 0x100);
        for result in [0, 1, 2, 7, -1] {
            assert!(!confirmed(Kind::Confirm, result));
        }
        assert!(confirmed(Kind::Confirm, 6));
        assert!(!confirmed(Kind::Error, 6));
        assert_eq!(style(Kind::Error), 0x10010);
    }

    #[test]
    fn messages_preserve_unicode_and_log_paths_without_nul_truncation() {
        let text = "启动失败\0\nC:\\用户\\TaskPaw\\taskpaw-backend-agent.log";
        let encoded = wide(text);
        assert_eq!(encoded.last(), Some(&0));
        assert!(!encoded[..encoded.len() - 1].contains(&0));
        assert_eq!(
            String::from_utf16(&encoded[..encoded.len() - 1]).unwrap(),
            text.replace('\0', " ")
        );
    }
}
