export function hubFilmKind(typeId: unknown, metrics: Record<string, unknown> = {}): "jasna" | "avsubs" | null {
  if (typeId === "avsubs") return "avsubs";
  if (typeId === "jasna" && (Array.isArray(metrics.steps) || Array.isArray(metrics.films)
    || [metrics.subs_total, metrics.queue_restored].some(v => typeof v === "number" && Number.isFinite(v)))) return "jasna";
  return null;
}

// Decimal strings avoid Number rounding for both core and prerelease numbers.
function decimalCompare(a: string, b: string): -1 | 0 | 1 {
  if (a.length !== b.length) return a.length > b.length ? 1 : -1;
  return a === b ? 0 : a > b ? 1 : -1;
}

function semver(value: unknown): { core: string[]; pre?: string[] } | null {
  if (typeof value !== "string") return null;
  const match = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.exec(value);
  // JS $ also matches before a trailing newline; the grammar accepts no whitespace.
  if (!match || match[0] !== value) return null;
  const pre = match[4]?.split(".");
  if (pre?.some(id => /^[0-9]+$/.test(id) && id.length > 1 && id.startsWith("0"))) return null;
  return { core: match.slice(1, 4), pre };
}

export function compareSemver(left: unknown, right: unknown): -1 | 0 | 1 | null {
  const a = semver(left), b = semver(right);
  if (!a || !b) return null;
  for (let i = 0; i < 3; i++) {
    const order = decimalCompare(a.core[i], b.core[i]);
    if (order) return order;
  }
  if (!a.pre || !b.pre) return a.pre ? -1 : b.pre ? 1 : 0;
  for (let i = 0; i < Math.max(a.pre.length, b.pre.length); i++) {
    const x = a.pre[i], y = b.pre[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (x === y) continue;
    const xn = /^[0-9]+$/.test(x), yn = /^[0-9]+$/.test(y);
    if (xn && yn) return decimalCompare(x, y);
    if (xn !== yn) return xn ? -1 : 1;
    return x > y ? 1 : -1;
  }
  return 0;
}
