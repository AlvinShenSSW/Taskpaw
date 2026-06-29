import { describe, expect, it } from "vitest";
import { theme, statusColors } from "../theme";

// Theme base smoke (#89): CJK font fallback, deepened background, starting state,
// and that label variants are reset for Chinese.
describe("theme", () => {
  it("includes a CJK fallback in the font stacks", () => {
    expect(theme.typography.fontFamily).toContain("Noto Sans SC");
    expect(String(theme.typography.body2.fontFamily)).toContain("Noto Sans SC");
    expect(theme.typography.fontFamily).toMatch(/Fira Sans/); // Latin/numerals first
  });

  it("deepens the background and layers the paper above it", () => {
    expect(theme.palette.background.default).toBe("#0B1120");
    expect(theme.palette.background.paper).not.toBe(theme.palette.background.default);
  });

  it("defines a starting status color for the pulse", () => {
    expect(statusColors.starting).toBe("#38BDF8");
    expect(statusColors.ok).toBeTruthy();
  });

  it("resets uppercase/tracking for Chinese label variants", () => {
    const css = theme.components?.MuiCssBaseline?.styleOverrides as Record<string, any>;
    // `|="zh"` so it matches the real document lang "zh-CN", not just bare "zh".
    const zhOverline = css?.['html[lang|="zh"] .MuiTypography-overline'];
    expect(zhOverline?.textTransform).toBe("none");
  });
});
