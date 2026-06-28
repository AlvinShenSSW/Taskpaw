fn main() {
    // Expose the build's target triple to the crate so the runtime can also probe
    // the target-suffixed sidecar name (taskpaw-backend-<triple>) — robust to how
    // Tauri places the externalBin file (#40 Codex).
    if let Ok(triple) = std::env::var("TARGET") {
        println!("cargo:rustc-env=TASKPAW_TARGET_TRIPLE={triple}");
    }
    // Recompile when the baked role changes, so sequential agent→hub builds in the
    // same target dir don't keep a stale option_env!("TASKPAW_BUILD_ROLE") (Codex).
    println!("cargo:rerun-if-env-changed=TASKPAW_BUILD_ROLE");
    tauri_build::build()
}
