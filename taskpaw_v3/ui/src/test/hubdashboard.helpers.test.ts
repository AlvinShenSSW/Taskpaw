import { describe, expect, it } from "vitest";
import { compareSemver, hubFilmKind } from "../views/hubDashboard.helpers";

describe("#210 semver ordering", () => {
  it.each([
    ["3.9.7", "3.9.6", 1], ["3.10.0", "3.9.99", 1], ["3.9.6", "3.9.7", -1],
    ["3.9.7", "3.9.7", 0], ["3.9.7+a", "3.9.7+b", 0],
    ["9007199254740993.0.0", "9007199254740992.0.0", 1],
    ["1.0.0-9007199254740993", "1.0.0-9007199254740992", 1],
    ["1.0.0-1", "1.0.0-a", -1], ["1.0.0-A", "1.0.0-a", -1],
  ])("compares %s and %s", (a, b, expected) => expect(compareSemver(a, b)).toBe(expected));
  it.each([undefined, null, 7, {}, "", "v3.9.7", " 3.9.7", "3.9.7\n", "3.9", "03.9.7", "3.09.7", "3.9.07", "3.9.7-", "3.9.7-a..b", "3.9.7-01", "3.9.7+", "3.9.7+é", "3.9.7+foo..bar"])("rejects malformed %s on either side", value => {
    expect(compareSemver(value, "3.9.7")).toBeNull(); expect(compareSemver("3.9.7", value)).toBeNull();
  });
  it("orders the complete prerelease sequence", () => {
    const versions = ["alpha", "alpha.1", "alpha.beta", "beta", "beta.2", "beta.11", "rc.1"].map(x => `1.0.0-${x}`).concat("1.0.0");
    versions.forEach((a, i) => versions.forEach((b, j) => expect(compareSemver(a, b)).toBe(Math.sign(i - j))));
  });
});

describe("#210 typed film eligibility", () => {
  it.each([
    ["avsubs", {}, "avsubs"], ["avsubs", { queue_pre_done: 3 }, "avsubs"],
    ["jasna", { steps: [] }, "jasna"], ["jasna", { films: [] }, "jasna"],
    ["jasna", { subs_total: 0 }, "jasna"], ["jasna", { queue_restored: 0 }, "jasna"],
    ["jasna", { queue_total: 3 }, null], ["jasna", { subs_total: NaN }, null],
    ["jasna", { queue_restored: Infinity }, null], ["jasna", { steps: "bad" }, null],
    [undefined, { steps: [] }, null], ["other", { films: [] }, null], [42, {}, null],
  ])("selects %s %#", (type, metrics, expected) => expect(hubFilmKind(type, metrics)).toBe(expected));
});
