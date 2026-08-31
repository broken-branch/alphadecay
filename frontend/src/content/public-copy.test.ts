import { describe, expect, it } from "vitest";
import { copy } from "./copy";

function collectStrings(value: unknown): string[] {
  if (typeof value === "string") {
    return [value];
  }
  if (Array.isArray(value)) {
    return value.flatMap(collectStrings);
  }
  if (value && typeof value === "object") {
    return Object.values(value).flatMap(collectStrings);
  }
  return [];
}

describe("public copy catalog", () => {
  it("contains no empty public strings", () => {
    expect(collectStrings(copy).every((value) => value.trim().length > 0)).toBe(true);
  });

  it("states the Replay execution boundary directly", () => {
    expect(copy.provenance.banner).toContain("NO ORDER SENT");
    expect(copy.provenance.banner).toBe("REPLAY · SAMPLE DATA · NO ORDER SENT");
    expect(copy.provenance.development).toBe("DEVELOPMENT / PAPER");
    expect(copy.provenance.submission).toBe("SUBMISSION / PAPER");
    expect(copy.performance.sourceLabel).toBe("SUBMISSION / PAPER");
    expect(copy.certificate.disabled).toContain("Replay");
    expect(copy.certificate.limitations).toContain("not market fills");
    expect(copy.exposure.noBrokerOutcomeDetail).toContain("No order was sent");
  });
});
