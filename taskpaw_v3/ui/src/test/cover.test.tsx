// U1b (operator feedback 2026-10-21): the shared cover shows a landscape jacket's front cover (its
// right part) and opens a lightbox with the full, uncropped image.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../api";
import { Cover } from "../components/media/parts";
import { MEDIA_CSS } from "../components/media/mediaStyles";
import { theme } from "../theme";
import { setLang } from "../i18n";

const POSTER = "data:image/jpeg;base64,cG9zdGVy";
const JACKET = "data:image/jpeg;base64,amFja2V0";

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

// jsdom loads no images: give the <img> its natural size, then fire its load event.
function loadAs(img: HTMLElement, width: number, height: number) {
  Object.defineProperty(img, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(img, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(img);
}

function mockImages() {
  return vi.spyOn(api, "image").mockImplementation(async (path: string) =>
    (path === api.coverPath("f1") ? JACKET : POSTER));
}

beforeEach(() => { setLang("zh-CN"); });
afterEach(() => { vi.restoreAllMocks(); });

describe("Cover orientation", () => {
  it("a portrait poster fills the box centred, as before", async () => {
    mockImages();
    wrap(<Cover filmId="f1" hasPoster source="lookup-1" size="card" label="晨雾车站" />);
    const img = await screen.findByRole("img", { name: "晨雾车站 封面" });
    loadAs(img, 421, 600);
    expect(img).not.toHaveClass("landscape");
    expect(img.closest(".cover")).toHaveClass("card");
  });

  it("a landscape jacket shows its right part, the front cover", async () => {
    mockImages();
    wrap(<Cover filmId="f1" hasPoster source="input-1" size="review" label="ABF-393" />);
    const img = await screen.findByRole("img", { name: "ABF-393 封面" });
    loadAs(img, 2184, 1477);
    await waitFor(() => expect(img).toHaveClass("landscape"));
    expect(img.closest(".cover")).toHaveClass("review");
    expect(MEDIA_CSS).toContain(".mw .cover img.landscape{object-position:right center}");
  });

  it("without a poster the box says 「无封面」 and opens nothing", () => {
    mockImages();
    wrap(<Cover filmId="f1" hasPoster={false} size="large" label="空片" />);
    expect(screen.getByRole("img", { name: "空片：无封面" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看大图" })).toBeNull();
  });
});

describe("Cover lightbox", () => {
  it("opens the full jacket uncropped and returns focus to the cover on Esc", async () => {
    const image = mockImages();
    wrap(<Cover filmId="f1" hasPoster source="final-1" hasCover coverSource="final-2" size="large" label="ABF-393" />);
    const button = await screen.findByRole("button", { name: "查看大图" });
    expect(button.tagName).toBe("BUTTON");  // native Enter / Space activation
    expect(button).toHaveAttribute("type", "button");
    await screen.findByRole("img", { name: "ABF-393 封面" });
    expect(image).not.toHaveBeenCalledWith(api.coverPath("f1"));  // only once asked for
    button.focus();
    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog", { name: "ABF-393 大图" });
    expect(image).toHaveBeenCalledWith(api.coverPath("f1"));
    const full = await screen.findByRole("img", { name: "ABF-393 大图" });
    await waitFor(() => expect(full).toHaveAttribute("src", JACKET));
    expect(full).toHaveStyle({ objectFit: "contain", maxWidth: "92vw", maxHeight: "88vh" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "ABF-393 大图" })).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("without a jacket it shows the poster; 「关闭大图」 closes it", async () => {
    const image = mockImages();
    wrap(<Cover filmId="f1" hasPoster source="lookup-1" hasCover={false} size="review" label="晨雾车站" />);
    fireEvent.click(await screen.findByRole("button", { name: "查看大图" }));
    const full = await screen.findByRole("img", { name: "晨雾车站 大图" });
    expect(full).toHaveAttribute("src", POSTER);
    expect(image).not.toHaveBeenCalledWith(api.coverPath("f1"));
    fireEvent.click(screen.getByRole("button", { name: "关闭大图" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: "查看大图" })).toHaveFocus());
  });

  it("a backdrop click closes it", async () => {
    mockImages();
    wrap(<Cover filmId="f1" hasPoster source="lookup-1" size="card" label="晨雾车站" />);
    fireEvent.click(await screen.findByRole("button", { name: "查看大图" }));
    await screen.findByRole("dialog", { name: "晨雾车站 大图" });
    const backdrop = document.querySelector(".MuiBackdrop-root") as HTMLElement;
    fireEvent.mouseDown(backdrop);
    fireEvent.click(backdrop);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("a failed jacket fetch falls back to the poster", async () => {
    vi.spyOn(api, "image").mockImplementation(async (path: string) => {
      if (path === api.coverPath("f1")) throw new Error("404");
      return POSTER;
    });
    wrap(<Cover filmId="f1" hasPoster source="input-1" hasCover coverSource="input-2" size="card" label="ABF-393" />);
    fireEvent.click(await screen.findByRole("button", { name: "查看大图" }));
    const full = await screen.findByRole("img", { name: "ABF-393 大图" });
    await waitFor(() => expect(full).toHaveAttribute("src", POSTER));
  });

  it("the cover button has a visible focus ring and a zoom cursor", () => {
    expect(MEDIA_CSS).toMatch(/\.mw \.cover-btn:focus-visible\{[^}]*outline:2px solid var\(--blue\)/);
    expect(MEDIA_CSS).toMatch(/\.mw \.cover-btn\{[^}]*cursor:zoom-in/);
  });
});
