//! Credentials are validated and read from the same OS file object, never reopened.
use serde::Deserialize;
use std::path::{Path, PathBuf};

const MAX_DESCRIPTOR: u64 = 16 * 1024;
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Descriptor {
    pub version: u8,
    pub role: String,
    pub base_url: String,
    pub boot_id: String,
    pub control_token: String,
}
#[derive(Clone, Deserialize)]
pub struct Ready {
    pub role: String,
    pub base_url: String,
    pub boot_id: String,
    pub control_credential_file: PathBuf,
}
#[derive(Debug)]
pub struct CredentialError;
impl std::fmt::Display for CredentialError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Local control credentials are invalid or unavailable")
    }
}
impl std::error::Error for CredentialError {}
pub fn canonical_base(raw: &str) -> bool {
    let Ok(url) = url::Url::parse(raw) else {
        return false;
    };
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return false;
    }
    let host = match url.host() {
        Some(url::Host::Ipv4(ip)) if ip == std::net::Ipv4Addr::LOCALHOST => "127.0.0.1",
        Some(url::Host::Ipv6(ip)) if ip == std::net::Ipv6Addr::LOCALHOST => "[::1]",
        _ => return false,
    };
    let Some(port) = url.port_or_known_default().filter(|p| *p > 0) else {
        return false;
    };
    raw == format!("{}://{}:{}", url.scheme(), host, port)
}
fn valid_boot(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn valid_role(role: &str) -> bool {
    matches!(role, "agent" | "hub")
}
impl Ready {
    pub fn valid_for(&self, role: &str) -> bool {
        self.role == role
            && valid_role(role)
            && canonical_base(&self.base_url)
            && valid_boot(&self.boot_id)
            && self.control_credential_file.is_absolute()
    }
}
impl Descriptor {
    pub fn matches_ready(&self, ready: &Ready) -> bool {
        self.role == ready.role && self.base_url == ready.base_url && self.boot_id == ready.boot_id
    }
}
pub fn parse_descriptor(bytes: &[u8]) -> Result<Descriptor, CredentialError> {
    if bytes.len() as u64 > MAX_DESCRIPTOR {
        return Err(CredentialError);
    }
    let value: Descriptor = serde_json::from_slice(bytes).map_err(|_| CredentialError)?;
    if value.version != 1
        || !valid_role(&value.role)
        || !canonical_base(&value.base_url)
        || !valid_boot(&value.boot_id)
        || value.control_token.is_empty()
        || value.control_token.len() > 1024
        || !value
            .control_token
            .bytes()
            .all(|b| (0x21..=0x7e).contains(&b))
    {
        return Err(CredentialError);
    }
    Ok(value)
}
pub fn read_descriptor(path: &Path) -> Result<Descriptor, CredentialError> {
    platform::read(path)
}

#[cfg(target_os = "macos")]
mod darwin_acl {
    use super::CredentialError;
    use std::ffi::c_void;
    type Acl = *mut c_void;
    extern "C" {
        fn acl_get_fd_np(fd: libc::c_int, acl_type: libc::c_int) -> Acl;
        fn acl_valid(acl: Acl) -> libc::c_int;
        fn acl_free(object: *mut c_void) -> libc::c_int;
        fn acl_get_entry(acl: Acl, index: libc::c_int, entry: *mut Acl) -> libc::c_int;
        fn acl_get_tag_type(entry: Acl, tag: *mut libc::c_int) -> libc::c_int;
        fn acl_get_permset_mask_np(entry: Acl, permissions: *mut u64) -> libc::c_int;
        fn acl_get_flagset_np(object: *mut c_void, flagset: *mut Acl) -> libc::c_int;
        fn acl_get_flag_np(flagset: Acl, flag: u32) -> libc::c_int;
    }
    struct Lease(Acl);
    impl Drop for Lease {
        fn drop(&mut self) {
            unsafe {
                acl_free(self.0);
            }
        }
    }
    pub(super) fn policy(
        tag: i32,
        permissions: u64,
        flags: u32,
        directory: bool,
        final_parent: bool,
    ) -> Result<(), CredentialError> {
        const KNOWN_PERMISSIONS: u64 = 0x103ffe;
        const KNOWN_FLAGS: u32 = 0x1f0;
        const FILE_UNSAFE: u64 = 0x353e;
        if !matches!(tag, 1 | 2)
            || permissions & !KNOWN_PERMISSIONS != 0
            || flags & !KNOWN_FLAGS != 0
        {
            return Err(CredentialError);
        }
        if tag == 2 {
            return Ok(());
        } // DENY never grants access (HOME deny-delete).
        let effective_unsafe = if !directory {
            FILE_UNSAFE
        } else if final_parent {
            0x3574
        } else {
            0x3550
        };
        if flags & 0x100 == 0 && permissions & effective_unsafe != 0 {
            return Err(CredentialError);
        }
        // Inherit-only grants can expose a future temporary descriptor even
        // when they do not grant access to the current directory itself.
        if directory && flags & 0x60 != 0 && permissions & FILE_UNSAFE != 0 {
            return Err(CredentialError);
        }
        Ok(())
    }
    pub(super) fn validate(
        fd: i32,
        directory: bool,
        final_parent: bool,
    ) -> Result<(), CredentialError> {
        unsafe {
            let acl = acl_get_fd_np(fd, 0x100); // ACL_TYPE_EXTENDED, same open fd.
            if acl.is_null() {
                // Darwin reports ENOENT for a file with no extended ACL.
                return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                    Ok(())
                } else {
                    Err(CredentialError)
                };
            }
            let _lease = Lease(acl);
            if acl_valid(acl) != 0 {
                return Err(CredentialError);
            }
            for index in 0..=128 {
                let mut entry = std::ptr::null_mut();
                let result = acl_get_entry(acl, index, &mut entry);
                // Darwin returns zero on success and -1/EINVAL at indexed
                // exhaustion (unlike the Linux POSIX ACL iteration convention).
                if result == -1
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::EINVAL)
                {
                    return Ok(());
                }
                if result != 0 || entry.is_null() || index == 128 {
                    return Err(CredentialError);
                }
                let mut tag = 0;
                let mut permissions = 0u64;
                let mut flagset = std::ptr::null_mut();
                if acl_get_tag_type(entry, &mut tag) != 0
                    || acl_get_permset_mask_np(entry, &mut permissions) != 0
                    || acl_get_flagset_np(entry, &mut flagset) != 0
                    || flagset.is_null()
                {
                    return Err(CredentialError);
                }
                let mut flags = 0u32;
                // Read all bits through the public API; acl_flagset_t is opaque.
                for bit in 0..32 {
                    let mask = 1u32 << bit;
                    match acl_get_flag_np(flagset, mask) {
                        0 => (),
                        1 => flags |= mask,
                        _ => return Err(CredentialError),
                    }
                }
                policy(tag, permissions, flags, directory, final_parent)?;
            }
            Err(CredentialError)
        }
    }
}

#[cfg(unix)]
mod platform {
    use super::*;
    use std::ffi::CString;
    use std::fs::File;
    use std::io::Read;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
    fn open_at(dir: i32, name: &std::ffi::OsStr, flags: i32) -> Result<File, CredentialError> {
        let name = CString::new(name.as_bytes()).map_err(|_| CredentialError)?;
        let fd = unsafe {
            libc::openat(
                dir,
                name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(CredentialError);
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub fn read(path: &Path) -> Result<Descriptor, CredentialError> {
        read_with_hook(path, || ())
    }
    pub(super) fn read_with_hook(
        path: &Path,
        after_open: impl FnOnce(),
    ) -> Result<Descriptor, CredentialError> {
        use std::path::Component;
        if !path.is_absolute() {
            return Err(CredentialError);
        }
        let components: Vec<_> = path.components().collect();
        if components.len() < 2
            || components
                .iter()
                .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
        {
            return Err(CredentialError);
        }
        let uid = unsafe { libc::geteuid() };
        let mut directory = File::open("/").map_err(|_| CredentialError)?;
        #[cfg(target_os = "macos")]
        super::darwin_acl::validate(directory.as_raw_fd(), true, components.len() == 2)?;
        // Keep every ancestor handle alive while reading the descriptor.
        let mut leases = Vec::new();
        for (_index, component) in components[1..components.len() - 1].iter().enumerate() {
            let Component::Normal(name) = component else {
                return Err(CredentialError);
            };
            let next = open_at(
                directory.as_raw_fd(),
                name,
                libc::O_RDONLY | libc::O_DIRECTORY,
            )?;
            let meta = next.metadata().map_err(|_| CredentialError)?;
            if !meta.is_dir()
                || (meta.uid() != uid && meta.uid() != 0)
                || (meta.mode() & 0o022 != 0 && meta.mode() & 0o1000 == 0)
            {
                return Err(CredentialError);
            }
            #[cfg(target_os = "macos")]
            super::darwin_acl::validate(next.as_raw_fd(), true, _index + 3 == components.len())?;
            leases.push(directory);
            directory = next;
        }
        let parent = directory.metadata().map_err(|_| CredentialError)?;
        if parent.uid() != uid || parent.mode() & 0o022 != 0 {
            return Err(CredentialError);
        }
        let Component::Normal(name) = components.last().ok_or(CredentialError)? else {
            return Err(CredentialError);
        };
        let file = open_at(
            directory.as_raw_fd(),
            name,
            libc::O_RDONLY | libc::O_NONBLOCK,
        )?;
        let meta = file.metadata().map_err(|_| CredentialError)?;
        if !meta.is_file()
            || meta.uid() != uid
            || meta.nlink() != 1
            || meta.mode() & 0o077 != 0
            || meta.len() > MAX_DESCRIPTOR
        {
            return Err(CredentialError);
        }
        #[cfg(target_os = "macos")]
        super::darwin_acl::validate(file.as_raw_fd(), false, false)?;
        after_open();
        let mut bytes = Vec::new();
        file.take(MAX_DESCRIPTOR + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| CredentialError)?;
        parse_descriptor(&bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_canonical_descriptor_and_ready_mapping() {
        let json = r#"{"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-test-key"}"#;
        let descriptor = parse_descriptor(json.as_bytes()).unwrap();
        assert!(descriptor.matches_ready(&Ready {
            role: "agent".into(),
            base_url: "http://127.0.0.1:5681".into(),
            boot_id: "0123456789abcdef0123456789abcdef".into(),
            control_credential_file: "ignored".into()
        }));
        assert!(!descriptor.matches_ready(&Ready {
            role: "hub".into(),
            base_url: "http://127.0.0.1:5681".into(),
            boot_id: "0123456789abcdef0123456789abcdef".into(),
            control_credential_file: "ignored".into()
        }));
        for bad in [
            "http://localhost:5681",
            "http://127.1:5681",
            "http://u:p@127.0.0.1:5681",
            "http://127.0.0.1:5681/x",
            "http://127.0.0.1:5681?x",
            "http://evil:5681",
        ] {
            assert!(!canonical_base(bad));
        }
    }
    #[test]
    fn descriptor_schema_rejects_unknown_duplicate_invalid_or_oversized_values() {
        let valid = serde_json::json!({"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-schema-key"});
        for (field, value) in [
            ("version", serde_json::json!(true)),
            ("version", serde_json::json!(2)),
            ("role", serde_json::json!("evil")),
            ("boot_id", serde_json::json!("old-boot")),
            ("control_token", serde_json::json!("")),
            ("control_token", serde_json::json!("bad key")),
            ("control_token", serde_json::json!("非ASCII")),
            ("control_token", serde_json::json!("k".repeat(1025))),
            ("unexpected", serde_json::json!(1)),
        ] {
            let mut bad = valid.clone();
            bad[field] = value;
            assert!(parse_descriptor(bad.to_string().as_bytes()).is_err());
        }
        let duplicate = valid.to_string().replacen("{", "{\"role\":\"hub\",", 1);
        assert!(parse_descriptor(duplicate.as_bytes()).is_err());
        assert!(parse_descriptor(&vec![b' '; MAX_DESCRIPTOR as usize + 1]).is_err());
        assert!(canonical_base("http://127.0.0.1:80"));
        assert!(canonical_base("https://[::1]:443"));
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_acl_policy_is_bounded_and_fails_closed() {
        use super::darwin_acl::{policy, validate};
        assert!(policy(1, 0x2, 0, false, false).is_err());
        assert!(policy(1, 0x4, 0, true, false).is_ok());
        assert!(policy(1, 0x4, 0, true, true).is_err());
        assert!(policy(1, 0x40, 0, true, false).is_err());
        assert!(policy(1, 0x2, 0x120, true, false).is_err());
        assert!(policy(1, 0x2, 0x40, true, false).is_err());
        assert!(policy(2, 0x10, 0, true, true).is_ok());
        assert!(policy(0, 0, 0, true, true).is_err());
        assert!(policy(2, 1, 0, true, true).is_err());
        assert!(policy(2, 0x10, 1, true, true).is_err());
        assert!(validate(-1, false, false).is_err());
        use std::os::fd::AsRawFd;
        let home = std::fs::File::open(std::env::var_os("HOME").unwrap()).unwrap();
        assert!(
            validate(home.as_raw_fd(), true, false).is_ok(),
            "Read-only inspection of normal HOME ACL must remain compatible"
        );
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_acl_rejects_effective_and_inherited_grants_but_preserves_deny() {
        use std::os::unix::fs::PermissionsExt;
        struct Fixture(PathBuf);
        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = std::process::Command::new("/bin/chmod")
                    .args(["-RN"])
                    .arg(&self.0)
                    .status();
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        fn acl(path: &Path, value: &str) {
            assert!(
                std::process::Command::new("/bin/chmod")
                    .args(["+a", value])
                    .arg(path)
                    .status()
                    .unwrap()
                    .success(),
                "Native ACL fixture setup failed"
            );
        }
        fn clear(path: &Path) {
            assert!(
                std::process::Command::new("/bin/chmod")
                    .arg("-N")
                    .arg(path)
                    .status()
                    .unwrap()
                    .success(),
                "Native ACL fixture reset failed"
            );
        }
        let root = std::fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!(
                "taskpaw-darwin-acl-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let fixture = Fixture(root);
        let path = fixture.0.join("agent.control.json");
        std::fs::write(&path,br#"{"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-native-acl-key"}"#).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(read_descriptor(&path).is_ok());
        acl(&path, "everyone allow read,write");
        assert!(
            read_descriptor(&path).is_err(),
            "Mode 0600 must not hide broad file ACL permissions"
        );
        clear(&path);
        acl(&fixture.0, "everyone allow write,append");
        assert!(
            read_descriptor(&path).is_err(),
            "Mode 0700 must not hide parent create rights"
        );
        clear(&fixture.0);
        acl(
            &fixture.0,
            "everyone allow read,write,execute,file_inherit,directory_inherit",
        );
        assert!(
            read_descriptor(&path).is_err(),
            "Unsafe inheritance must fail before reading credentials"
        );
        clear(&fixture.0);
        acl(&fixture.0, "everyone allow read,file_inherit,only_inherit");
        assert!(
            read_descriptor(&path).is_err(),
            "Inherit-only read grants are still unsafe for future descriptors"
        );
        clear(&fixture.0);
        acl(&fixture.0, "everyone deny delete");
        assert!(
            read_descriptor(&path).is_ok(),
            "Normal macOS HOME deny-delete ACL must remain supported"
        );
        clear(&fixture.0);
        let nested = fixture.0.join("nested");
        std::fs::create_dir(&nested).unwrap();
        std::fs::set_permissions(&nested, std::fs::Permissions::from_mode(0o700)).unwrap();
        let nested_file = nested.join("agent.control.json");
        std::fs::copy(&path, &nested_file).unwrap();
        acl(&fixture.0, "everyone allow delete_child");
        assert!(
            read_descriptor(&nested_file).is_err(),
            "Unsafe ancestor ACL must reject even with a safe final parent"
        );
        clear(&fixture.0);
        acl(&fixture.0, "everyone allow read,execute");
        assert!(
            read_descriptor(&path).is_ok(),
            "Harmless directory list/search rights must remain supported"
        );
    }
    #[cfg(unix)]
    #[test]
    fn posix_reader_rejects_unsafe_objects_and_reads_the_validated_fd() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let temporary = std::fs::canonicalize(std::env::temp_dir()).unwrap();
        let dir = temporary.join(format!(
            "taskpaw-rust-reader-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.join("agent.control.json");
        let payload=br#"{"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"0123456789abcdef0123456789abcdef","control_token":"fake-fd-key"}"#;
        std::fs::write(&path, payload).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(read_descriptor(&path).is_ok());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_descriptor(&path).is_err());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let link = dir.join("link.control.json");
        symlink(&path, &link).unwrap();
        assert!(read_descriptor(&link).is_err());
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o777)).unwrap();
        assert!(read_descriptor(&path).is_err());
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let descriptor = platform::read_with_hook(&path, || {
            std::fs::remove_file(&path).unwrap();
            std::fs::write(&path, b"invalid replacement").unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)).unwrap();
        })
        .unwrap();
        assert_eq!(descriptor.control_token, "fake-fd-key");
        assert!(read_descriptor(&path).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(windows)]
mod platform {
    use super::*;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Foundation::{CloseHandle, LocalFree, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Security::Authorization::{GetSecurityInfo, SE_FILE_OBJECT};
    use windows_sys::Win32::Security::*;
    use windows_sys::Win32::Storage::FileSystem::*;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
    struct Handle(HANDLE);
    impl Drop for Handle {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    struct SecurityDescriptor(PSECURITY_DESCRIPTOR);
    impl Drop for SecurityDescriptor {
        fn drop(&mut self) {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
    struct Identities {
        user: Vec<usize>,
        system: Vec<u32>,
        admins: Vec<u32>,
    }
    impl Identities {
        fn user_sid(&self) -> PSID {
            unsafe { (*(self.user.as_ptr() as *const TOKEN_USER)).User.Sid }
        }
        fn trusted(&self, sid: PSID, file: bool) -> bool {
            unsafe {
                EqualSid(sid, self.user_sid()) != 0
                    || EqualSid(sid, self.system.as_ptr() as PSID) != 0
                    || (!file && EqualSid(sid, self.admins.as_ptr() as PSID) != 0)
            }
        }
    }
    fn identities() -> Result<Identities, CredentialError> {
        unsafe {
            let mut token = null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return Err(CredentialError);
            }
            let token = Handle(token);
            let mut length = 0;
            GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut length);
            if length == 0 {
                return Err(CredentialError);
            }
            let mut user = vec![0usize; (length as usize).div_ceil(std::mem::size_of::<usize>())];
            if GetTokenInformation(
                token.0,
                TokenUser,
                user.as_mut_ptr() as _,
                length,
                &mut length,
            ) == 0
            {
                return Err(CredentialError);
            }
            fn known(kind: WELL_KNOWN_SID_TYPE) -> Result<Vec<u32>, CredentialError> {
                let mut data = vec![0u32; 17];
                let mut size = (data.len() * 4) as u32;
                if unsafe {
                    CreateWellKnownSid(kind, null_mut(), data.as_mut_ptr() as _, &mut size)
                } == 0
                {
                    return Err(CredentialError);
                }
                Ok(data)
            }
            Ok(Identities {
                user,
                system: known(WinLocalSystemSid)?,
                admins: known(WinBuiltinAdministratorsSid)?,
            })
        }
    }
    fn open(path: &Path, directory: bool) -> Result<Handle, CredentialError> {
        let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
        if wide.contains(&0) {
            return Err(CredentialError);
        }
        wide.push(0);
        let access = READ_CONTROL
            | if directory {
                FILE_LIST_DIRECTORY
            } else {
                FILE_GENERIC_READ
            };
        let share =
            FILE_SHARE_READ | FILE_SHARE_WRITE | if directory { 0 } else { FILE_SHARE_DELETE };
        let flags = FILE_FLAG_OPEN_REPARSE_POINT
            | if directory {
                FILE_FLAG_BACKUP_SEMANTICS
            } else {
                0
            };
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                access,
                share,
                null(),
                OPEN_EXISTING,
                flags,
                null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            return Err(CredentialError);
        }
        Ok(Handle(handle))
    }
    fn validate(
        handle: &Handle,
        ids: &Identities,
        file: bool,
        final_parent: bool,
    ) -> Result<(), CredentialError> {
        unsafe {
            let mut info: FILE_ATTRIBUTE_TAG_INFO = std::mem::zeroed();
            if GetFileType(handle.0) != FILE_TYPE_DISK
                || GetFileInformationByHandleEx(
                    handle.0,
                    FileAttributeTagInfo,
                    &mut info as *mut _ as _,
                    std::mem::size_of_val(&info) as u32,
                ) == 0
                || info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
                || (info.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0) == file
            {
                return Err(CredentialError);
            }
            let mut owner = null_mut();
            let mut dacl = null_mut();
            let mut sd = null_mut();
            let result = GetSecurityInfo(
                handle.0,
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut dacl,
                null_mut(),
                &mut sd,
            );
            let _lease = SecurityDescriptor(sd);
            if result != 0
                || sd.is_null()
                || owner.is_null()
                || IsValidSid(owner) == 0
                || dacl.is_null()
                || IsValidAcl(dacl) == 0
            {
                return Err(CredentialError);
            }
            if file {
                if EqualSid(owner, ids.user_sid()) == 0 {
                    return Err(CredentialError);
                }
            } else if !ids.trusted(owner, false) {
                return Err(CredentialError);
            }
            let mut control = 0u16;
            let mut revision = 0;
            if GetSecurityDescriptorControl(sd, &mut control, &mut revision) == 0
                || (file && control & SE_DACL_PROTECTED == 0)
            {
                return Err(CredentialError);
            }
            for index in 0..(*dacl).AceCount {
                let mut ace = null_mut();
                if GetAce(dacl, index as u32, &mut ace) == 0 || ace.is_null() {
                    return Err(CredentialError);
                }
                let header = &*(ace as *const ACE_HEADER);
                // Only ordinary allow/deny ACE layouts are understood; unknown
                // object/callback ACEs fail closed rather than bypassing checks.
                if !matches!(header.AceType, 0 | 1)
                    || header.AceSize < std::mem::size_of::<ACCESS_ALLOWED_ACE>() as u16
                    || (file && header.AceFlags & 0x10 != 0)
                {
                    return Err(CredentialError);
                }
                if !file && header.AceFlags & 0x08 != 0 {
                    continue;
                }
                if header.AceType == 1 {
                    continue;
                }
                let allow = &*(ace as *const ACCESS_ALLOWED_ACE);
                let sid = &allow.SidStart as *const u32 as PSID;
                if IsValidSid(sid) == 0 {
                    return Err(CredentialError);
                }
                if ids.trusted(sid, file) {
                    continue;
                }
                let unsafe_mask = 0x10
                    | 0x40
                    | 0x100
                    | 0x10000
                    | 0x40000
                    | 0x80000
                    | 0x40000000
                    | 0x10000000
                    | if final_parent { 0x2 | 0x4 } else { 0 };
                if file || allow.Mask & unsafe_mask != 0 {
                    return Err(CredentialError);
                }
            }
        }
        Ok(())
    }
    pub fn read(path: &Path) -> Result<Descriptor, CredentialError> {
        use std::path::{Component, Prefix};
        let parts: Vec<_> = path.components().collect();
        if !path.is_absolute()
            || parts.len() < 3
            || !matches!(parts[0], Component::Prefix(p) if matches!(p.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
            || parts.iter().any(|p| {
                !matches!(
                    p,
                    Component::Prefix(_) | Component::RootDir | Component::Normal(_)
                )
            })
        {
            return Err(CredentialError);
        }
        let ids = identities()?;
        let mut current = PathBuf::new();
        let mut leases = Vec::new();
        for (index, part) in parts[..parts.len() - 1].iter().enumerate() {
            current.push(part.as_os_str());
            if index == 0 {
                continue;
            }
            let handle = open(&current, true)?;
            validate(&handle, &ids, false, index == parts.len() - 2)?;
            leases.push(handle);
        }
        let file = open(path, false)?;
        validate(&file, &ids, true, false)?;
        unsafe {
            let mut info: FILE_STANDARD_INFO = std::mem::zeroed();
            if GetFileInformationByHandleEx(
                file.0,
                FileStandardInfo,
                &mut info as *mut _ as _,
                std::mem::size_of_val(&info) as u32,
            ) == 0
                || info.EndOfFile < 0
                || info.EndOfFile as u64 > MAX_DESCRIPTOR
                || info.Directory != 0
                || info.NumberOfLinks != 1
            {
                return Err(CredentialError);
            }
            let mut bytes = vec![0u8; (MAX_DESCRIPTOR + 1) as usize];
            let mut total = 0usize;
            loop {
                let mut read = 0;
                if ReadFile(
                    file.0,
                    bytes[total..].as_mut_ptr(),
                    (bytes.len() - total) as u32,
                    &mut read,
                    null_mut(),
                ) == 0
                {
                    return Err(CredentialError);
                }
                if read == 0 {
                    break;
                }
                total += read as usize;
                if total as u64 > MAX_DESCRIPTOR {
                    return Err(CredentialError);
                }
            }
            parse_descriptor(&bytes[..total])
        }
    }
}
