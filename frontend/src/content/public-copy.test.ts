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
    expect(copy.provenance.banner).toContain("NO ORDER");
    expect(copy.provenance.banner).toBe("REPLAY · SAMPLE DATA · NO ORDER SENT");
    expect(copy.provenance.development).toBe("DEVELOPMENT ACCOUNT");
    expect(copy.provenance.submission).toBe("COMPETITION ACCOUNT");
    expect(copy.performance.sourceLabel).toBe("COMPETITION ACCOUNT");
    expect(copy.certificate.disabled).toContain("Replay");
    expect(copy.certificate.limitations).toContain("not market fills");
    expect(copy.exposure.noBrokerOutcomeDetail).toContain("No order was sent");
  });

  it("uses the judge-first experiment and Replay labels", () => {
    expect(copy.productShell.experimentsTitle).toBe(
      "Test a market thesis with rules you can inspect.",
    );
    expect(copy.navigation).toEqual({
      label: "Replay experiment sections",
      overview: "Decision",
      comparison: "Thesis and rules",
      run: "Decision path",
      record: "Record details",
    });
    expect(copy.productShell.resultNode).toBe("Result pending publication");
    expect(copy.provenance.paperOnlyCompact).toBe("Paper / Replay only");
  });
});
