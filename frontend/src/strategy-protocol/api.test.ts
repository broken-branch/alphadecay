import { describe, expect, it, vi } from "vitest";
import {
  createStrategyCuration,
  StrategyCurationRequestError,
} from "./api";
import { curatedProtocolFixture } from "./test-fixture";

const strategyCurationFixture = curatedProtocolFixture();
const strategyCurationRequestFixture = {
  brief: strategyCurationFixture.intake,
  protocol_fields: strategyCurationFixture.protocol_fields,
};

describe("strategy curation client", () => {
  it("posts one exact owner request without browser persistence", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(strategyCurationFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await createStrategyCuration(
      strategyCurationRequestFixture,
      "csrf-token",
      fetcher,
    );

    expect(result).toEqual(strategyCurationFixture);
    expect(fetcher).toHaveBeenCalledOnce();
    const [path, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/owner/strategy-curations");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    });
    expect(init.headers).toMatchObject({
      "Cache-Control": "no-store",
      "X-CSRF-Token": "csrf-token",
    });
    expect(JSON.parse(String(init.body))).toEqual(strategyCurationRequestFixture);
  });

  it("rejects missing csrf, invalid input, server errors, and invalid responses", async () => {
    await expect(
      createStrategyCuration(strategyCurationRequestFixture, " ", vi.fn()),
    ).rejects.toMatchObject({ status: 403 });

    await expect(
      createStrategyCuration(
        { ...strategyCurationRequestFixture, protocol_fields: { ...strategyCurationRequestFixture.protocol_fields, entry_rule: " " } },
        "csrf-token",
        vi.fn(),
      ),
    ).rejects.toMatchObject({ status: 422 });

    await expect(
      createStrategyCuration(
        strategyCurationRequestFixture,
        "csrf-token",
        vi.fn().mockResolvedValue(new Response(null, { status: 503 })),
      ),
    ).rejects.toMatchObject({ status: 503 });

    await expect(
      createStrategyCuration(
        strategyCurationRequestFixture,
        "csrf-token",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({ ...strategyCurationFixture, execution_eligible: true })),
        ),
      ),
    ).rejects.toBeInstanceOf(StrategyCurationRequestError);
  });
});
