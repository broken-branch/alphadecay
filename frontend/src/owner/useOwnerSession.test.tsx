import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OwnerRequestError, type OwnerSettingsClient } from "./api";
import { useOwnerSession } from "./useOwnerSession";

const emptySettings = { schema_version: "v1" as const, configured: false };

function client(overrides: Partial<OwnerSettingsClient> = {}): OwnerSettingsClient {
  return {
    createSession: vi.fn(async () => undefined),
    read: vi.fn(async () => emptySettings),
    replace: vi.fn(async () => emptySettings),
    clear: vi.fn(async () => emptySettings),
    signOut: vi.fn(async () => undefined),
    ...overrides,
  };
}

afterEach(cleanup);

describe("useOwnerSession", () => {
  it("verifies the server session before sharing authenticated state", async () => {
    const api = client();
    const readCsrf = () => "verified-csrf";
    const { result } = renderHook(() => useOwnerSession(true, api, readCsrf));

    await waitFor(() => expect(result.current.status).toBe("signedIn"));

    expect(api.read).toHaveBeenCalledOnce();
    expect(result.current.session).toEqual({ authenticated: true, csrfToken: "verified-csrf" });
    expect(result.current.settings).toEqual(emptySettings);
  });

  it("does not claim authentication when the readable CSRF cookie is absent", () => {
    const api = client();
    const readCsrf = () => null;
    const { result } = renderHook(() => useOwnerSession(true, api, readCsrf));

    expect(result.current.status).toBe("signedOut");
    expect(result.current.session).toEqual({ authenticated: false, csrfToken: null });
    expect(api.read).not.toHaveBeenCalled();
  });

  it("shares a new session in memory and clears it after sign-out", async () => {
    let csrfToken: string | null = null;
    const api = client({
      createSession: vi.fn(async () => {
        csrfToken = "new-csrf";
      }),
    });
    const readCsrf = () => csrfToken;
    const { result } = renderHook(() => useOwnerSession(true, api, readCsrf));

    await act(async () => result.current.signIn("private-owner-code"));
    expect(result.current.session).toEqual({ authenticated: true, csrfToken: "new-csrf" });

    await act(async () => result.current.signOut());
    expect(result.current.session).toEqual({ authenticated: false, csrfToken: null });
    expect(result.current.settings).toBeNull();
  });

  it("drops shared authentication when the server rejects the session", async () => {
    const api = client({
      read: vi.fn(async () => {
        throw new OwnerRequestError(403);
      }),
    });
    const readCsrf = () => "expired-csrf";
    const { result } = renderHook(() => useOwnerSession(true, api, readCsrf));

    await waitFor(() => {
      expect(api.read).toHaveBeenCalledOnce();
      expect(result.current.status).toBe("signedOut");
    });
    expect(result.current.session.authenticated).toBe(false);
  });
});
