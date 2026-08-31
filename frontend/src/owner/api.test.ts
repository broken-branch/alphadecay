import { afterEach, describe, expect, it, vi } from "vitest";
import {
  OwnerRequestError,
  clearProviderSettings,
  createOwnerSession,
  getProviderSettings,
  readCsrfCookie,
  replaceProviderSettings,
} from "./api";

const configured = {
  schema_version: "v1",
  configured: true,
  provider: "GEMINI",
  endpoint: "https://generativelanguage.googleapis.com",
  model: "gemini-2.5-flash",
  generation: 2,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.restoreAllMocks());

describe("owner API", () => {
  it("reads only the expected CSRF cookie", () => {
    expect(readCsrfCookie("other=x; __Host-alphadecay_csrf=csrf%20token; tracking=no")).toBe(
      "csrf token",
    );
    expect(readCsrfCookie("other=x")).toBeNull();
  });

  it("creates a session without putting the access code in the URL or headers", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({
        schema_version: "v1",
        authenticated: true,
        expires_at: "2026-08-29T22:15:00Z",
      }),
    );
    const settingsValue = "owner-access-code";

    await createOwnerSession(settingsValue, fetcher as typeof fetch);

    expect(fetcher).toHaveBeenCalledOnce();
    const [url, request] = fetcher.mock.calls[0];
    expect(url).toBe("/api/session");
    expect(JSON.stringify(request?.headers)).not.toContain(settingsValue);
    expect(request?.body).toContain(settingsValue);
    expect(request?.credentials).toBe("same-origin");
    expect(request?.cache).toBe("no-store");
  });

  it("sends the CSRF cookie as a header and parses key-free provider status", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-alphadecay_csrf=csrf-token");
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(configured));

    const result = await getProviderSettings(fetcher as typeof fetch);

    expect(result).toEqual(configured);
    expect(result).not.toHaveProperty("api_key");
    const [, request] = fetcher.mock.calls[0];
    expect(request?.headers).toMatchObject({ "X-CSRF-Token": "csrf-token" });
  });

  it("uses write-only PUT and never includes the key in the returned status", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-alphadecay_csrf=csrf-token");
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(configured));
    const providerValue = "private-provider-key";

    const result = await replaceProviderSettings(
      {
        schema_version: "v1",
        provider: "GEMINI",
        model: "gemini-2.5-flash",
        api_key: providerValue,
        endpoint: null,
      },
      fetcher as typeof fetch,
    );

    const [url, request] = fetcher.mock.calls[0];
    expect(url).toBe("/api/owner/provider-settings");
    expect(request?.method).toBe("PUT");
    expect(request?.body).toContain(providerValue);
    expect(JSON.stringify(result)).not.toContain(providerValue);
  });

  it("uses DELETE without a request body and returns an empty status", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-alphadecay_csrf=csrf-token");
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ schema_version: "v1", configured: false }),
    );

    const result = await clearProviderSettings(fetcher as typeof fetch);

    expect(result.configured).toBe(false);
    expect(fetcher.mock.calls[0][1]?.method).toBe("DELETE");
    expect(fetcher.mock.calls[0][1]?.body).toBeUndefined();
  });

  it("returns one generic error shape for response and contract failures", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-alphadecay_csrf=csrf-token");
    await expect(getProviderSettings(vi.fn(async () => jsonResponse({}, 503)) as typeof fetch)).rejects.toEqual(
      expect.objectContaining({ name: "OwnerRequestError", message: "OWNER_REQUEST_FAILED" }),
    );
    await expect(getProviderSettings(vi.fn(async () => jsonResponse({ api_key: "bad" })) as typeof fetch)).rejects.toBeInstanceOf(
      OwnerRequestError,
    );
  });
});
