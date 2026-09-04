// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const styles = readFileSync("frontend/src/styles.css", "utf8");

describe("workspace navigation", () => {
  it("keeps the setup underline aligned with its text after the divider inset", () => {
    expect(styles).toMatch(
      /\.workspace-nav button\.workspace-nav__secondary\s*\{[^}]*--workspace-nav-secondary-inset:\s*28px;[^}]*padding-left:\s*var\(--workspace-nav-secondary-inset\);/s,
    );
    expect(styles).toMatch(
      /\.workspace-nav button\.workspace-nav__secondary\[aria-current="page"\]::after\s*\{[^}]*left:\s*var\(--workspace-nav-secondary-inset\);/s,
    );
  });

  it("keeps phone navigation and utility controls at a 44px touch target", () => {
    expect(styles).toMatch(
      /\.workspace-nav button\s*\{[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;/s,
    );
    expect(styles).toMatch(
      /\.brand,\s*\.theme-toggle,\s*\.keyboard-trigger--icon,\s*\.view-help summary\s*\{[^}]*min-height:\s*44px;/s,
    );
    expect(styles).toMatch(/\.header-environment\s*\{[^}]*min-height:\s*44px;/s);
    expect(styles).toMatch(
      /\.footer-links button,\s*\.footer-links a\s*\{[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;/s,
    );
    expect(styles).toMatch(/\.primary-button\s*\{[^}]*min-height:\s*44px;/s);
  });

  it("wraps Replay tabs on phones and keeps the paper environment trigger visible", () => {
    expect(styles).toMatch(
      /\.tab-list--wrap-on-phone\s*\{[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\);[^}]*overflow-x:\s*visible;/s,
    );
    expect(styles).toMatch(
      /\.header-environment\s*\{[^}]*min-height:\s*44px;[^}]*display:\s*inline-flex;/s,
    );
    expect(styles).toMatch(
      /\.site-footer a:focus-visible,\s*\.site-footer button:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\);/s,
    );
  });
});
