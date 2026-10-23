// The NATIVE Tauri file/directory picker (#71), shared by the monitor form's PathWidget and
// 设置 · 媒体工作流. The locked shell injects __TAURI_INTERNALS__ on the webview; its absence means a
// browser (Vite dev / web build) with no native dialog — a web <input type=file> can't expose a real
// filesystem path anyway, so callers hide their button there and keep the plain text input.
export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

// The absolute path picked, or null when the dialog is unavailable or cancelled.
export async function pickPath(directory: boolean, title: string): Promise<string | null> {
  try {
    // Imported lazily so the web build never needs the Tauri plugin at load.
    const { open } = await import("@tauri-apps/plugin-dialog");
    const picked = await open({ directory, multiple: false, title });
    return typeof picked === "string" ? picked : null;
  } catch {
    return null;
  }
}
