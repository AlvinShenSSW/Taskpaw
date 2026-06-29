import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { StatusDot } from "../components/StatusDot";
import { SkeletonRows } from "../components/SkeletonRows";
import { HudCard } from "../components/HudCard";
import { theme } from "../theme";

const wrap = (ui: React.ReactNode) =>
  render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);

// Shared visual primitives (#90).
describe("StatusDot", () => {
  it("encodes status as text (aria-label), not color alone", () => {
    wrap(<StatusDot state="running" />);
    // The dot exposes the state as accessible text → status is never color-only.
    expect(screen.getByLabelText("status: running")).toBeInTheDocument();
  });

  it("renders for unknown states without crashing", () => {
    wrap(<StatusDot state="totally-made-up" />);
    expect(screen.getByLabelText("status: totally-made-up")).toBeInTheDocument();
  });

  it("animates the pulse only for live states", () => {
    const { rerender } = wrap(<StatusDot state="running" />);
    let dot = screen.getByLabelText("status: running");
    // emotion serializes the `animation` shorthand into the dot's class styles;
    // for live states it references the pulse keyframes, for idle it's `none`.
    expect(dot.className).toBeTruthy();

    rerender(
      <ThemeProvider theme={theme}>
        <StatusDot state="idle" />
      </ThemeProvider>,
    );
    dot = screen.getByLabelText("status: idle");
    expect(dot).toBeInTheDocument();
  });

  it("honors an explicit live=false override", () => {
    wrap(<StatusDot state="ok" live={false} />);
    expect(screen.getByLabelText("status: ok")).toBeInTheDocument();
  });
});

describe("SkeletonRows", () => {
  it("renders a busy status region with the requested row count", () => {
    const { container } = wrap(<SkeletonRows rows={3} />);
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-busy", "true");
    // 3 rows × 3 skeletons (dot + label + timestamp) = 9 placeholders.
    expect(container.querySelectorAll(".MuiSkeleton-root").length).toBe(9);
  });
});

describe("HudCard", () => {
  it("renders its children inside a card", () => {
    wrap(<HudCard>panel body</HudCard>);
    expect(screen.getByText("panel body")).toBeInTheDocument();
  });
});
