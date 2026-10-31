// Number and time formatting of the 数据分析 page (EPIC §8.2), ported from the approved preview
// (design-system/taskpaw-v3/preview/media-analytics-v1.html). Times are the controller's local time:
// the page runs on the controller, the API sends UTC `Z` (§7.1).

export const MIN = 60_000;
export const HOUR = 3_600_000;
export const DAY = 86_400_000;

export const pad = (n: number) => String(n).padStart(2, "0");
export const fMD = (t: number) => { const d = new Date(t); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
export const fYMD = (t: number) => `${new Date(t).getFullYear()}-${fMD(t)}`;
export const fHM = (t: number) => { const d = new Date(t); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
export const fHMS = (t: number) => `${fHM(t)}:${pad(new Date(t).getSeconds())}`;
export const fMDHM = (t: number) => `${fMD(t)} ${fHM(t)}`;
export const fWk = (t: number) => `周${"日一二三四五六"[new Date(t).getDay()]}`;
export const fYM = (t: number) => { const d = new Date(t); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`; };

const finite = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);
export const n1 = (v: number | null | undefined) => (finite(v) ? (Math.round(v * 10) / 10).toFixed(1) : "—");
export const fInt = (v: number | null | undefined) => (finite(v) ? Math.round(v).toLocaleString("zh-CN") : "—");
/** Hours: 「45 分」 under an hour, else 「3.2 小时」. */
export const fH = (h: number | null | undefined) => (!finite(h) ? "—" : h < 1 ? `${Math.round(h * 60)} 分` : `${n1(h)} 小时`);
/** Minutes: 「40 秒」 / 「4.5 分」 / 「12 分」. */
export const fMin = (m: number | null | undefined) =>
  (!finite(m) ? "—" : m < 1 ? `${Math.round(m * 60)} 秒` : m < 10 ? `${n1(m)} 分` : `${Math.round(m)} 分`);
/** Hours shown in the unit that suits them: 「40 秒」 / 「12 分」 / 「3.2 小时」. */
export const fAutoH = (h: number | null | undefined) =>
  (!finite(h) ? "—" : h < 1 / 60 ? `${Math.round(h * 3600)} 秒` : h < 1 ? fMin(h * 60) : fH(h));
export const fSpan = (ms: number) => {
  const m = Math.round(ms / MIN);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60), r = m % 60;
  if (h < 48) return `${h} 小时${r ? ` ${r} 分` : ""}`;
  return `${n1(ms / DAY)} 天`;
};
export const fKwh = (v: number | null | undefined) =>
  (!finite(v) ? "—" : v >= 100 ? Math.round(v).toLocaleString("zh-CN") : v >= 10 ? n1(v) : v.toFixed(2));
export const fPct = (v: number | null | undefined) => (!finite(v) ? "—" : `${v < 10 ? n1(v) : Math.round(v)}%`);
/** Axis tick: 1200 → 「1.2K」. */
export const tickFmt = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)}K` : String(+v.toFixed(2)));
/** Bytes in decimal GB (the drive's own unit): 「590」. */
export const fGB = (b: number | null | undefined) => (finite(b) ? fInt(b / 1e9) : "—");
/** Bytes in decimal TB: 「1 TB」 / 「1.5 TB」. */
export const fTB = (b: number) => `${+(b / 1e12).toFixed(1)} TB`;
