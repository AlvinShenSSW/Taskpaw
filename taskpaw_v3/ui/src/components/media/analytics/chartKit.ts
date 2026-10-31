// Shared plumbing of the 数据分析 charts (EPIC 2026-10-31 §8.2, §8.5): the tooltip context and its
// handlers, the measured chart width, roving tabindex and SVG geometry. Kept apart from charts.tsx so
// that file exports components only; C4/C5 primitives import these too.
import { createContext, useContext, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent, type PointerEvent, type RefObject } from "react";

// ---- the shared tooltip ----
export interface TipRow { color?: string | null; value: string; label?: string }
export interface TipContent { title?: string; rows: TipRow[]; note?: string | null }
export interface ChartTip { show: (content: TipContent, x: number, y: number, above?: boolean) => void; move: (x: number, y: number) => void; hide: () => void }

const NO_TIP: ChartTip = { show: () => {}, move: () => {}, hide: () => {} };
export const TipContext = createContext<ChartTip>(NO_TIP);
export const useChartTip = () => useContext(TipContext);

/** Pointer and focus handlers that show `content` in the shared tooltip. */
export function tipHandlers(tip: ChartTip, content: () => TipContent) {
  return {
    "data-tip": "",
    onPointerEnter: (e: PointerEvent<Element>) => tip.show(content(), e.clientX, e.clientY),
    onPointerMove: (e: PointerEvent<Element>) => tip.move(e.clientX, e.clientY),
    onPointerLeave: () => tip.hide(),
    onFocus: (e: FocusEvent<Element>) => { const r = e.currentTarget.getBoundingClientRect(); tip.show(content(), r.left + r.width / 2, r.top, true); },
    onBlur: () => tip.hide(),
  };
}

// ---- layout and keyboard ----
/** The width of a chart's box: measured on mount, then followed with a 120 ms debounce. */
export function useChartWidth<T extends HTMLElement = HTMLDivElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(Math.floor(el.clientWidth));
    if (typeof ResizeObserver === "undefined") return;
    let timer: number | undefined;
    const ro = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setWidth((w) => (Math.abs(w - next) > 2 ? next : w)), 120);
    });
    ro.observe(el);
    return () => { ro.disconnect(); window.clearTimeout(timer); };
  }, []);
  return [ref, width];
}

const ROVE_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]);
/** Roving tabindex over `count` items: one in the tab order, arrows / Home / End move the focus. */
export function useRoving(count: number) {
  const [active, setActive] = useState(0);
  const refs = useRef<Array<SVGGElement | null>>([]);
  const current = Math.min(active, Math.max(0, count - 1));
  return (i: number) => ({
    ref: (el: SVGGElement | null) => { refs.current[i] = el; },
    tabIndex: i === current ? 0 : -1,
    "data-rove": "",
    onKeyDown: (e: KeyboardEvent<SVGGElement>) => {
      if (!ROVE_KEYS.has(e.key)) return;
      e.preventDefault();
      const step = e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1;
      const next = e.key === "Home" ? 0 : e.key === "End" ? count - 1 : Math.max(0, Math.min(count - 1, i + step));
      setActive(next);
      refs.current[next]?.focus();
    },
    onFocusCapture: () => setActive(i),
  });
}

// ---- geometry helpers ----
/** 0 … a round top value in about `count` steps (1 / 2 / 2.5 / 5 × 10ⁿ). */
export function niceTicks(max: number, count: number): number[] {
  if (!(max > 1e-6)) return [0, 1];
  const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((x) => x * mag).find((x) => x >= raw - 1e-12)!;
  const top = Math.ceil(max / step - 1e-9) * step, out: number[] = [];
  for (let v = 0; v <= top + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
/** A column with rounded top corners. */
export const colPath = (x: number, y: number, w: number, h: number) => {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y + r}V${y + h}Z`;
};
/** A polyline; a null point lifts the pen. */
export const linePath = (pts: Array<[number, number] | null>) => {
  let d = "", pen = false;
  for (const p of pts) {
    if (!p) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    pen = true;
  }
  return d;
};
/** A horizontal bar with rounded right corners. */
export const barPath = (x: number, y: number, w: number, h: number) => {
  const r = Math.min(4, h / 2, w);
  return `M${x},${y}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y + r}V${y + h - r}A${r},${r} 0 0 1 ${x + w - r},${y + h}H${x}Z`;
};
/** Time-axis labels at least `minGap` px apart, left to right (preview `timeTicks`). */
export function thinTicks<T extends { px: number }>(ticks: T[], minGap = 58): T[] {
  const out: T[] = [];
  let last = -Infinity;
  for (const t of ticks) if (t.px - last >= minGap) { out.push(t); last = t.px; }
  return out;
}
