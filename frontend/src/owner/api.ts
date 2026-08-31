import {
  ProviderSettingsResponseSchema,
  ProviderSettingsUpdateRequestSchema,
  SessionCreateRequestSchema,
  SessionResponseSchema,
  type ProviderSettingsResponse,
  type ProviderSettingsUpdateRequest,
} from "./contracts";

const csrfCookieName = "__Host-alphadecay_csrf";

export class OwnerRequestError extends Error {
  constructor(readonly status: number) {
    super("OWNER_REQUEST_FAILED");
    this.name = "OwnerRequestError";
  }
}

export function readCsrfCookie(cookieHeader: string = document.cookie): string | null {
  for (const entry of cookieHeader.split(";")) {
    const [rawName, ...rawValue] = entry.trim().split("=");
    if (rawName === csrfCookieName) return decodeURIComponent(rawValue.join("="));
  }
  return null;
}

async function parseResponse<T>(response: Response, parse: (value: unknown) => T): Promise<T> {
  if (!response.ok) throw new OwnerRequestError(response.status);
  try {
    return parse(await response.json());
  } catch {
    throw new OwnerRequestError(503);
  }
}

function ownerHeaders(csrf: string): HeadersInit {
  return {
    Accept: "application/json",
    "Cache-Control": "no-store",
    Origin: window.location.origin,
    "X-CSRF-Token": csrf,
  };
}

export async function createOwnerSession(
  settingsValue: string,
  fetcher: typeof fetch = fetch,
): Promise<void> {
  const payload = SessionCreateRequestSchema.parse({ access_code: settingsValue });
  const response = await fetcher("/api/session", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Origin: window.location.origin },
    body: JSON.stringify(payload),
  });
  const result = await parseResponse(response, (value) => SessionResponseSchema.parse(value));
  if (!result.authenticated) throw new OwnerRequestError(401);
}

export async function getProviderSettings(
  fetcher: typeof fetch = fetch,
): Promise<ProviderSettingsResponse> {
  const csrf = readCsrfCookie();
  if (!csrf) throw new OwnerRequestError(403);
  const response = await fetcher("/api/owner/provider-settings", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: ownerHeaders(csrf),
  });
  return parseResponse(response, (value) => ProviderSettingsResponseSchema.parse(value));
}

export async function replaceProviderSettings(
  input: ProviderSettingsUpdateRequest,
  fetcher: typeof fetch = fetch,
): Promise<ProviderSettingsResponse> {
  const csrf = readCsrfCookie();
  if (!csrf) throw new OwnerRequestError(403);
  const payload = ProviderSettingsUpdateRequestSchema.parse(input);
  const response = await fetcher("/api/owner/provider-settings", {
    method: "PUT",
    credentials: "same-origin",
    cache: "no-store",
    headers: { ...ownerHeaders(csrf), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return parseResponse(response, (value) => ProviderSettingsResponseSchema.parse(value));
}

export async function clearProviderSettings(
  fetcher: typeof fetch = fetch,
): Promise<ProviderSettingsResponse> {
  const csrf = readCsrfCookie();
  if (!csrf) throw new OwnerRequestError(403);
  const response = await fetcher("/api/owner/provider-settings", {
    method: "DELETE",
    credentials: "same-origin",
    cache: "no-store",
    headers: ownerHeaders(csrf),
  });
  return parseResponse(response, (value) => ProviderSettingsResponseSchema.parse(value));
}

export async function deleteOwnerSession(fetcher: typeof fetch = fetch): Promise<void> {
  const csrf = readCsrfCookie();
  if (!csrf) throw new OwnerRequestError(403);
  const response = await fetcher("/api/session", {
    method: "DELETE",
    credentials: "same-origin",
    cache: "no-store",
    headers: ownerHeaders(csrf),
  });
  const result = await parseResponse(response, (value) => SessionResponseSchema.parse(value));
  if (result.authenticated) throw new OwnerRequestError(503);
}

export const ownerSettingsClient = {
  createSession: createOwnerSession,
  read: getProviderSettings,
  replace: replaceProviderSettings,
  clear: clearProviderSettings,
  signOut: deleteOwnerSession,
};
export type OwnerSettingsClient = typeof ownerSettingsClient;
