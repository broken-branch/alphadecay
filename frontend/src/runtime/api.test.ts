import { afterEach, describe, expect, it, vi } from "vitest";
import { loadRuntimeStatus } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("runtime API adapter", () => {
  it("loads a Replay-only health response without browser credentials", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: "v1",
          status: "ok",
          build: "a".repeat(40),
          runtime_mode: "REPLAY_ONLY",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(loadRuntimeStatus()).resolves.toMatchObject({ runtime_mode: "REPLAY_ONLY" });
    expect(fetchMock).toHaveBeenCalledWith("/api/health", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "omit",
    });
  });

  it("rejects an invalid or unavailable health response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(loadRuntimeStatus()).rejects.toThrow();

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(null, { status: 503 }));
    await expect(loadRuntimeStatus()).rejects.toThrow("RUNTIME_STATUS_UNAVAILABLE");
  });
});
