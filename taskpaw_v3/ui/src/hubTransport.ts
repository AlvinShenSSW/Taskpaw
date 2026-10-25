// Hub-only limits. The Agent client keeps its existing request semantics.
// Buffer at most one bounded body before callers parse JSON or create an image.
const DEADLINE_MS = 20_000;
const MAX_BYTES = 8 * 1024 * 1024;

export async function hubRequest(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Error("Hub request timed out");
  let interruption: Error = timeout;
  const sourceSignal = init.signal;
  const cancelBody = () => {
    // Cancellation can race a closed/errored stream. Its rejection is not a new
    // request result; aborting fetch already closes the underlying connection.
    if (reader) void reader.cancel().catch(() => {});
  };
  let onCancel: () => void;
  const deadline = new Promise<never>((_resolve, reject) => {
    const stop = (error: Error) => { interruption = error; reject(error); controller.abort(); cancelBody(); };
    onCancel = () => stop(new DOMException("Hub request cancelled", "AbortError"));
    timer = setTimeout(() => stop(timeout), DEADLINE_MS);
    sourceSignal?.addEventListener("abort", onCancel, { once: true });
    if (sourceSignal?.aborted) onCancel();
  });
  const read = async () => {
    if (controller.signal.aborted) throw interruption;
    const response = await fetch(url, { ...init, redirect: "error", signal: controller.signal });
    // Fetch normally rejects redirects itself. Also reject an unexpected
    // redirected response before giving any body to the application.
    if (controller.signal.aborted || response.redirected || (response.status >= 300 && response.status < 400)) {
      void response.body?.cancel().catch(() => {});
      throw controller.signal.aborted ? interruption : new Error("Hub redirects are not allowed");
    }
    if (response.status === 401) {
      void response.body?.cancel().catch(() => {});
      return new Response(null, { status: 401 });
    }
    const length = response.headers.get("Content-Length");
    if (length && /^\d+$/.test(length) && Number(length) > MAX_BYTES) {
      void response.body?.cancel().catch(() => {});
      throw new Error("Hub response is too large");
    }
    if (!response.body) return response;
    reader = response.body.getReader();
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (controller.signal.aborted) throw interruption;
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) {
          controller.abort(); cancelBody();
          throw new Error("Hub response is too large");
        }
        chunks.push(new Uint8Array(value));
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return new Response(bytes, { status: response.status, statusText: response.statusText, headers: response.headers });
    } finally { reader.releaseLock(); reader = undefined; }
  };
  try { return await Promise.race([read(), deadline]); }
  finally { clearTimeout(timer); sourceSignal?.removeEventListener("abort", onCancel!); }
}
