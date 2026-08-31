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
});
