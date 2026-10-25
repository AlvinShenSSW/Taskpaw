// Browser receipt evidence lives in the day query cache, so remounts and range changes cannot
// refresh an old sensor sample. No cross-machine wall clock subtraction is used.
import type { MachineTemperatures, PowerView } from "../../api";

export const TEMPERATURE_STALE_S = 30;
export interface TimedPowerView extends PowerView { temperatureReceivedMs?: number }
export const validAge = (age: unknown): age is number => typeof age === "number" && Number.isFinite(age) && age >= 0;

function stamp(
  value: MachineTemperatures | null | undefined, previous: MachineTemperatures | null | undefined,
  requestS: number, previousCacheS: number,
): MachineTemperatures | null | undefined {
  if (!value) return value;
  let age = validAge(value.age_s) ? value.age_s + requestS : null;
  // Repeated responses from the same sample must not lower its known age. A genuinely new
  // sampled_at may be younger; the timestamp is used for identity only, never as a clock.
  if (age !== null && typeof value.sampled_at === "number" && Number.isFinite(value.sampled_at) && value.sampled_at >= 0
    && value.sampled_at === previous?.sampled_at && validAge(previous.age_s)) {
    age = Math.max(age, previous.age_s + previousCacheS);
  }
  return { ...value, age_s: age, stale: value.stale !== false || age === null || age >= TEMPERATURE_STALE_S };
}

export function stampPowerTemperatures(
  value: PowerView, startedMs: number, receivedMs: number, previous?: TimedPowerView,
): TimedPowerView {
  const requestS = Math.max(0, receivedMs - startedMs) / 1000;
  const previousCacheS = previous?.temperatureReceivedMs === undefined ? 0
    : Math.max(0, receivedMs - previous.temperatureReceivedMs) / 1000;
  return {
    ...value, temperatureReceivedMs: receivedMs,
    temperatures: stamp(value.temperatures, previous?.temperatures, requestS, previousCacheS),
    peers: value.peers?.map((p) => ({ ...p, temperatures: stamp(p.temperatures,
      previous?.peers?.find((old) => old.name === p.name && old.address === p.address && old.machine === p.machine)?.temperatures,
      requestS, previousCacheS) })),
  };
}

export function temperatureAge(value: MachineTemperatures, cacheS: number | null): number | null {
  return validAge(value.age_s) && validAge(cacheS) ? value.age_s + cacheS : null;
}
