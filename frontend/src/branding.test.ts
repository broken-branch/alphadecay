import { describe, expect, it } from "vitest";
// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import indexHtml from "../../index.html?raw";
import darkFavicon from "../../public/favicon-dark.svg?raw";
import lightFavicon from "../../public/favicon-light.svg?raw";
import darkLockup from "./assets/branding/alphadecay-lockup.svg?raw";
import lightLockup from "./assets/branding/alphadecay-lockup-light.svg?raw";
import darkMark from "./assets/branding/alphadecay-mark.svg?raw";
import lightMark from "./assets/branding/alphadecay-mark-light.svg?raw";
import replayShellSource from "./replay/ReplayShell.tsx?raw";

const styles = readFileSync("frontend/src/styles.css", "utf8");

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const linear = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrastRatio(foreground: string, background: string): number {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort(
    (left, right) => right - left,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

describe("canonical alphadecay branding", () => {
  it("uses the canonical wordmark spelling in the page title", () => {
    expect(indexHtml).toContain("<title>alphadecay</title>");
  });

  it("keeps the canonical lockup geometry and mobile mark in both app themes", () => {
    for (const asset of [darkLockup, lightLockup, darkMark, lightMark, darkFavicon, lightFavicon]) {
      expect(asset).toContain("M626 0H571Q518 0 491 21.5");
      expect(asset).not.toContain("M104 72C47 72 18 108 18 154");
      expect(asset).not.toContain("<rect");
      expect(asset).not.toContain("<mask");
      expect(asset).not.toContain("opacity=");
      expect(asset).not.toContain("Gradient");
    }
    expect(darkLockup).toContain('transform="translate(0 190) scale(.18 -.18)"');
    expect(lightLockup).toContain('transform="translate(0 190) scale(.18 -.18)"');
    expect(darkLockup).toContain('transform="translate(120 190) scale(.18 -.18)"');
    expect(lightLockup).toContain('transform="translate(120 190) scale(.18 -.18)"');
    expect(darkLockup).toContain("#A991FF");
    expect(lightLockup).toContain("#684FC6");
    expect(darkMark).toContain("#A991FF");
    expect(lightMark).toContain("#684FC6");
    expect(darkMark).not.toContain("lphadecay");
    expect(lightMark).not.toContain("lphadecay");
  });

  it("selects transparent favicons by browser color scheme", () => {
    expect(indexHtml).toContain('href="/favicon-dark.svg"');
    expect(indexHtml).toContain('href="/favicon-light.svg"');
    expect(indexHtml).toContain('media="(prefers-color-scheme: dark)"');
    expect(indexHtml).toContain('media="(prefers-color-scheme: light)"');
    expect(indexHtml).not.toContain('href="/favicon.svg"');
    expect(darkFavicon).toContain('fill="#A991FF"');
    expect(lightFavicon).toContain('fill="#5B3FD6"');
    expect(darkFavicon).not.toContain("background");
    expect(lightFavicon).not.toContain("background");
  });

  it("starts Dark without client persistence", () => {
    expect(indexHtml).toContain('<html lang="en" data-theme="dark">');
    expect(indexHtml).not.toContain("<script>");
    for (const storageApi of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "serviceWorker",
      "document.cookie",
      "CacheStorage",
    ]) {
      expect(indexHtml).not.toContain(storageApi);
      expect(replayShellSource).not.toContain(storageApi);
    }
  });

  it("defines both palettes and non-color control boundaries", () => {
    expect(styles).toContain(':root[data-theme="dark"]');
    expect(styles).toContain(':root[data-theme="light"]');
    expect(styles).toContain("--control-border: #767676");
    expect(styles).toContain("--control-border: #77737b");
    for (const token of [
      "--canvas: #f3f2ef",
      "--sheet: #fbfaf7",
      "--quiet: #e6e3dd",
      "--primary: #242326",
      "--secondary: #4f4d52",
      "--muted: #646168",
      "--accent: #684fc6",
      "--accent-fill: #e7e0ff",
      "--positive: #067a67",
      "--adverse: #a92630",
    ]) {
      expect(styles).toContain(token);
    }
    expect(styles).toMatch(/\.scenario-select select\s*\{[^}]*border: 1px solid var\(--control-border\)/s);
    expect(styles).toMatch(/\.theme-toggle\s*\{[^}]*border: 0/s);
    expect(styles).toMatch(/\.theme-toggle\s*\{[^}]*opacity: 0\.75/s);
    expect(styles).toMatch(/\.keyboard-trigger--icon\s*\{[^}]*border: 0/s);
    expect(styles).toMatch(/\.keyboard-trigger--icon\s*\{[^}]*opacity: 0\.75/s);
    expect(styles).toMatch(/\.theme-toggle:focus-visible[^}]*outline: 2px solid var\(--accent\)/s);
    expect(styles).toMatch(/\.brand__art--light\s*\{[^}]*display: none/s);
    expect(styles).toMatch(/\.drift-list > div\s*\{[^}]*align-items: center/s);
    expect(styles).toMatch(/\.drift-list dd:last-child\s*\{[^}]*white-space: nowrap/s);
    expect(styles).toMatch(/\.view-help p\s*\{[^}]*background: var\(--sheet\)/s);
  });

  it("keeps semantic trading colors readable on both review surfaces", () => {
    for (const [foreground, backgrounds] of [
      ["#f23645", ["#0d0d0d", "#171717"]],
      ["#089981", ["#0d0d0d", "#171717"]],
      ["#a92630", ["#f3f2ef", "#fbfaf7"]],
      ["#067a67", ["#f3f2ef", "#fbfaf7"]],
    ] as const) {
      for (const background of backgrounds) {
        expect(contrastRatio(foreground, background)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});
