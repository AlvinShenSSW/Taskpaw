// #299: shared fixtures for the setup UI tests (network view, detection).
import type { Detection, NetworkView } from "../api";

export function network(over: Partial<NetworkView> = {}): NetworkView {
  return {
    lan_access: false, bind_host: "127.0.0.1", bind_port: 5680, control_port: 5681,
    addresses: [{ ip: "192.168.1.20", nic: "以太网", primary: true }, { ip: "10.0.0.5", nic: "WLAN", primary: false }],
    address_changed_from: null, token_set: false, api_token_hint: null,
    firewall: { platform: "windows", rule: "absent", profile: "private" }, ...over,
  };
}

export const DETECTED: Detection = {
  gpu: { name: "NVIDIA GeForce RTX 5060", vram_mib: 8151, driver: "581.57", power_limit_w: 145, worker_ok: false },
  tools: {
    ffprobe: { found: true, path: "C:\\ffmpeg\\bin\\ffprobe.exe", source: "path", version: "8.0.1" },
    jasna: { found: true, path: "C:\\Jasna\\jasna.exe", source: "default", version: "0.11.3" },
    whisperjav: { found: true, path: "C:\\WhisperJAV\\Scripts\\whisperjav.exe", source: "default", version: null },
    miko: { status: "complete", root: "D:\\Work\\engines\\MDCX304", python: "D:\\Work\\engines\\MDCX304\\.venv\\Scripts\\python.exe", profile: "C:\\Users\\u\\AppData\\Roaming\\com.mdcx.desktop\\config.json" },
  },
  suggested: { inbox: "D:\\TaskpawInbox", data_dir: "D:\\TaskpawWork", staging: "D:\\TaskpawStaging" },
};

