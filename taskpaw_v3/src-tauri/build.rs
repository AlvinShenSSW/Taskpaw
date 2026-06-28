fn main() {
    // Expose the build's target triple to the crate so the runtime can also probe
    // the target-suffixed sidecar name (taskpaw-backend-<triple>) — robust to how
    // Tauri places the externalBin file (#40 Codex).
    if let Ok(triple) = std::env::var("TARGET") {
        println!("cargo:rustc-env=TASKPAW_TARGET_TRIPLE={triple}");
    }
    tauri_build::build()
}
