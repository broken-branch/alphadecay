import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  OwnerRequestError,
  ownerSettingsClient,
  readCsrfCookie,
  type OwnerSettingsClient,
} from "./api";
import type { ProviderSettingsResponse } from "./contracts";

export type OwnerSessionStatus = "checking" | "signedOut" | "signedIn" | "unavailable";

export type OwnerSessionSnapshot = {
  authenticated: boolean;
  csrfToken: string | null;
};

export type OwnerSessionController = {
  status: OwnerSessionStatus;
  session: OwnerSessionSnapshot;
  settings: ProviderSettingsResponse | null;
  signIn: (accessCode: string) => Promise<void>;
  signOut: () => Promise<void>;
  updateSettings: (settings: ProviderSettingsResponse) => void;
  invalidate: () => void;
};

type SessionState = {
  status: OwnerSessionStatus;
  session: OwnerSessionSnapshot;
  settings: ProviderSettingsResponse | null;
};

const signedOutState: SessionState = {
  status: "signedOut",
  session: { authenticated: false, csrfToken: null },
  settings: null,
};

function failureState(error: unknown): SessionState {
  return error instanceof OwnerRequestError && (error.status === 401 || error.status === 403)
    ? signedOutState
    : { ...signedOutState, status: "unavailable" };
}

export function useOwnerSession(
  enabled: boolean,
  client: OwnerSettingsClient = ownerSettingsClient,
  csrfReader: () => string | null = readCsrfCookie,
): OwnerSessionController {
  const [state, setState] = useState<SessionState>(signedOutState);
  const requestVersion = useRef(0);

  const invalidate = useCallback(() => {
    requestVersion.current += 1;
    setState(signedOutState);
  }, []);

  useEffect(() => {
    const version = ++requestVersion.current;
    if (!enabled) {
      setState(signedOutState);
      return;
    }
    const csrfToken = csrfReader();
    if (!csrfToken) {
      setState(signedOutState);
      return;
    }
    setState((current) => ({ ...current, status: "checking" }));
    client.read().then(
      (settings) => {
        if (requestVersion.current !== version) return;
        const currentToken = csrfReader();
        if (!currentToken) {
          setState(signedOutState);
          return;
        }
        setState({
          status: "signedIn",
          session: { authenticated: true, csrfToken: currentToken },
          settings,
        });
      },
      (error: unknown) => {
        if (requestVersion.current === version) setState(failureState(error));
      },
    );
  }, [client, csrfReader, enabled]);

  const signIn = useCallback(async (accessCode: string) => {
    const version = ++requestVersion.current;
    setState((current) => ({ ...current, status: "checking" }));
    try {
      await client.createSession(accessCode);
      const csrfToken = csrfReader();
      if (!csrfToken) throw new OwnerRequestError(403);
      const settings = await client.read();
      if (requestVersion.current !== version) return;
      setState({
        status: "signedIn",
        session: { authenticated: true, csrfToken },
        settings,
      });
    } catch (error) {
      if (requestVersion.current === version) setState(signedOutState);
      throw error;
    }
  }, [client, csrfReader]);

  const signOut = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      await client.signOut();
      if (requestVersion.current === version) setState(signedOutState);
    } catch (error) {
      if (requestVersion.current === version) setState(failureState(error));
      throw error;
    }
  }, [client]);

  const updateSettings = useCallback((settings: ProviderSettingsResponse) => {
    setState((current) => current.status === "signedIn" ? { ...current, settings } : current);
  }, []);

  return useMemo(() => ({
    status: state.status,
    session: state.session,
    settings: state.settings,
    signIn,
    signOut,
    updateSettings,
    invalidate,
  }), [invalidate, signIn, signOut, state, updateSettings]);
}
