import { useEffect, useState } from "react";

// Local HTTP response age only; this says nothing about monitor sample age.
export const RESPONSE_FRESH_MS = 15_000;

export function useResponseOutdated(lastSuccessAt: number) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!lastSuccessAt) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = () => {
      clearTimeout(timer);
      const time = Date.now();
      setNow(time);
      const remaining = lastSuccessAt + RESPONSE_FRESH_MS - time;
      if (remaining > 0) timer = setTimeout(update, remaining);
    };
    update();
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [lastSuccessAt]);
  return lastSuccessAt > 0 && Math.max(now, Date.now()) - lastSuccessAt >= RESPONSE_FRESH_MS;
}

export function requestTime(time: number, language: string) {
  return new Date(time).toLocaleTimeString(language);
}

// A no-data manual retry clears Query's current error, but keeps its timestamp.
// Success advances dataUpdatedAt, so this presentation evidence clears on success.
export function requestFailed(failureCount: number, errorUpdatedAt: number, dataUpdatedAt: number, error: unknown) {
  return failureCount > 0 || error != null || errorUpdatedAt > dataUpdatedAt;
}
