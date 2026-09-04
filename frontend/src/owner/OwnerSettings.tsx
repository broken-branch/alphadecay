import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent, RefObject } from "react";
import { copy } from "../content/copy";
import { ownerSettingsClient, type OwnerSettingsClient } from "./api";
import type { OwnerModelProvider, ProviderSettingsResponse } from "./contracts";
import type { OwnerSessionController } from "./useOwnerSession";

const shortcutRows = [
  [copy.keyboardGuide.previousScenarioKey, copy.keyboardGuide.previousScenario],
  [copy.keyboardGuide.nextScenarioKey, copy.keyboardGuide.nextScenario],
  [copy.keyboardGuide.previousTabKey, copy.keyboardGuide.previousTab],
  [copy.keyboardGuide.nextTabKey, copy.keyboardGuide.nextTab],
  [copy.keyboardGuide.tabKey, copy.keyboardGuide.moveFocus],
  [copy.keyboardGuide.questionKey, copy.keyboardGuide.toggleGuide],
  [copy.keyboardGuide.escapeKey, copy.keyboardGuide.close],
] as const;

function trapModalFocus(event: ReactKeyboardEvent<HTMLElement>, panel: HTMLElement | null) {
  if (event.key !== "Tab" || !panel) return;
  const controls = Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (controls.length === 0) return;
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function ProviderStatus({ settings }: { settings: ProviderSettingsResponse }) {
  if (!settings.configured) return <p className="muted">{copy.ownerSettings.notConfigured}</p>;
  const serviceName =
    settings.provider === "GEMINI"
      ? copy.ownerSettings.gemini
      : copy.ownerSettings.openAiCompatible;
  return (
    <dl className="owner-status">
      <div>
        <dt>{copy.ownerSettings.currentProvider}</dt>
        <dd>{serviceName}</dd>
      </div>
      <div>
        <dt>{copy.ownerSettings.currentModel}</dt>
        <dd>{settings.model}</dd>
      </div>
      <div>
        <dt>{copy.ownerSettings.currentEndpoint}</dt>
        <dd>{settings.endpoint}</dd>
      </div>
      <div>
        <dt>{copy.ownerSettings.generation}</dt>
        <dd>{settings.generation}</dd>
      </div>
    </dl>
  );
}

export function OwnerSettings({
  open,
  onClose,
  triggerRef,
  ownerSession,
  ownerControlsEnabled = true,
  client = ownerSettingsClient,
}: {
  open: boolean;
  onClose: () => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
  ownerSession: OwnerSessionController;
  ownerControlsEnabled?: boolean;
  client?: OwnerSettingsClient;
}) {
  const [settingsValue, setSettingsValue] = useState("");
  const [selectedService, setSelectedService] = useState<OwnerModelProvider>("GEMINI");
  const [model, setModel] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [providerValue, setProviderValue] = useState("");
  const [busy, setBusy] = useState<"login" | "save" | "remove" | "signout" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const accessRef = useRef<HTMLInputElement | null>(null);
  const modelRef = useRef<HTMLInputElement | null>(null);
  const wasOpenRef = useRef(false);

  const clearSensitiveFields = () => {
    setSettingsValue("");
    setProviderValue("");
  };
  const close = () => {
    clearSensitiveFields();
    setMessage(null);
    onClose();
  };

  useEffect(() => {
    if (!open) return;
    const target =
      ownerSession.status === "signedOut"
        ? accessRef.current
        : ownerSession.status === "signedIn"
          ? modelRef.current
          : panelRef.current;
    target?.focus();
  }, [open, ownerSession.status]);

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      triggerRef.current?.focus();
    }
  }, [open, triggerRef]);

  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  });

  if (!open) return null;

  const signIn = async (event: FormEvent) => {
    event.preventDefault();
    setBusy("login");
    setMessage(null);
    try {
      await ownerSession.signIn(settingsValue);
      setSettingsValue("");
    } catch {
      setMessage(copy.ownerSettings.signInFailed);
    } finally {
      setSettingsValue("");
      setBusy(null);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy("save");
    setMessage(null);
    try {
      const result = await client.replace({
        schema_version: "v1",
        provider: selectedService,
        model,
        api_key: providerValue,
        endpoint: selectedService === "GEMINI" ? null : endpoint,
      });
      ownerSession.updateSettings(result);
      setMessage(copy.ownerSettings.saved);
    } catch {
      setMessage(copy.ownerSettings.saveFailed);
    } finally {
      setProviderValue("");
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("remove");
    setMessage(null);
    try {
      const result = await client.clear();
      ownerSession.updateSettings(result);
      setMessage(copy.ownerSettings.removed);
    } catch {
      setMessage(copy.ownerSettings.removeFailed);
    } finally {
      clearSensitiveFields();
      setBusy(null);
    }
  };

  const signOut = async () => {
    setBusy("signout");
    setMessage(null);
    try {
      await ownerSession.signOut();
      clearSensitiveFields();
    } catch {
      setMessage(copy.ownerSettings.sessionFailed);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="owner-modal-backdrop" aria-hidden="true" onClick={close} />
      <section
        ref={panelRef}
        className="owner-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="owner-settings-title"
        tabIndex={-1}
        onKeyDown={(event) => trapModalFocus(event, panelRef.current)}
      >
        <header className="owner-modal__heading">
          <h2 id="owner-settings-title">{copy.ownerSettings.title}</h2>
          <button type="button" className="quiet-button" onClick={close}>
            {copy.ownerSettings.close}
          </button>
        </header>

        <section className="settings-shortcuts" aria-labelledby="settings-shortcuts-title">
          <div>
            <h3 id="settings-shortcuts-title">{copy.keyboardGuide.title}</h3>
            <p>{copy.keyboardGuide.help}</p>
          </div>
          <dl>
            {shortcutRows.map(([key, action]) => (
              <div key={key}>
                <dt>
                  <kbd>{key}</kbd>
                </dt>
                <dd>{action}</dd>
              </div>
            ))}
          </dl>
        </section>

        {ownerControlsEnabled && ownerSession.status === "checking" ? <p aria-live="polite">{copy.ownerSettings.checking}</p> : null}
        {ownerControlsEnabled && ownerSession.status === "unavailable" ? (
          <p className="form-message" role="alert">
            {copy.ownerSettings.sessionFailed}
          </p>
        ) : null}
        {ownerControlsEnabled && ownerSession.status === "signedOut" ? (
          <form className="owner-form owner-form--login" onSubmit={signIn}>
            <h3>{copy.ownerSettings.signInTitle}</h3>
            <p className="muted">{copy.ownerSettings.signInHelp}</p>
            <label>
              <span>{copy.ownerSettings.settingsCode}</span>
              <input
                ref={accessRef}
                type="password"
                autoFocus
                autoComplete="off"
                minLength={16}
                maxLength={256}
                required
                value={settingsValue}
                onChange={(event) => setSettingsValue(event.target.value)}
              />
            </label>
            <button type="submit" className="primary-button" disabled={busy !== null}>
              {busy === "login" ? copy.ownerSettings.signingIn : copy.ownerSettings.signIn}
            </button>
            {message ? <p className="form-message" role="alert">{message}</p> : null}
          </form>
        ) : null}
        {ownerControlsEnabled && ownerSession.status === "signedIn" && ownerSession.settings ? (
          <div className="owner-settings-body">
            <div className="owner-session-line">
              <div>
                <strong>{copy.ownerSettings.signedIn}</strong>
                <span>{copy.ownerSettings.sessionNote}</span>
              </div>
              <button
                type="button"
                className="quiet-button"
                onClick={signOut}
                disabled={busy !== null}
              >
                {copy.ownerSettings.signOut}
              </button>
            </div>
            <section aria-labelledby="current-provider-title">
              <h3 id="current-provider-title">{copy.ownerSettings.currentTitle}</h3>
              <ProviderStatus settings={ownerSession.settings} />
            </section>
            <form className="owner-form" onSubmit={save}>
              <div>
                <h3>{copy.ownerSettings.providerTitle}</h3>
                <p className="muted">{copy.ownerSettings.providerHelp}</p>
              </div>
              <label>
                <span>{copy.ownerSettings.provider}</span>
                <select
                  value={selectedService}
                  onChange={(event) => {
                    const next = event.target.value as OwnerModelProvider;
                    setSelectedService(next);
                    if (next === "GEMINI") setEndpoint("");
                  }}
                >
                  <option value="GEMINI">{copy.ownerSettings.gemini}</option>
                  <option value="OPENAI_COMPATIBLE">{copy.ownerSettings.openAiCompatible}</option>
                </select>
              </label>
              <label>
                <span>{copy.ownerSettings.model}</span>
                <input
                  ref={modelRef}
                  value={model}
                  maxLength={256}
                  placeholder={
                    selectedService === "GEMINI"
                      ? copy.ownerSettings.modelExampleGemini
                      : copy.ownerSettings.modelExampleCompatible
                  }
                  onChange={(event) => setModel(event.target.value)}
                  required
                />
              </label>
              {selectedService === "OPENAI_COMPATIBLE" ? (
                <div className="owner-field">
                  <label htmlFor="owner-provider-endpoint">
                    <span>{copy.ownerSettings.endpoint}</span>
                  </label>
                  <input
                    id="owner-provider-endpoint"
                    type="url"
                    aria-describedby="owner-provider-endpoint-help"
                    value={endpoint}
                    maxLength={2048}
                    onChange={(event) => setEndpoint(event.target.value)}
                    required
                  />
                  <small id="owner-provider-endpoint-help">
                    {copy.ownerSettings.endpointHelp}
                  </small>
                </div>
              ) : null}
              <div className="owner-field">
                <label htmlFor="owner-provider-key">
                  <span>{copy.ownerSettings.providerFieldLabel}</span>
                </label>
                <input
                  id="owner-provider-key"
                  type="password"
                  autoComplete="off"
                  aria-describedby="owner-provider-key-help"
                  maxLength={16_384}
                  value={providerValue}
                  onChange={(event) => setProviderValue(event.target.value)}
                  required
                />
                <small id="owner-provider-key-help">{copy.ownerSettings.providerFieldHelp}</small>
              </div>
              <div className="owner-form__actions">
                <button type="submit" className="primary-button" disabled={busy !== null}>
                  {busy === "save" ? copy.ownerSettings.saving : copy.ownerSettings.save}
                </button>
                {ownerSession.settings.configured ? (
                  <button
                    type="button"
                    className="quiet-button semantic-adverse"
                    onClick={remove}
                    disabled={busy !== null}
                  >
                    {busy === "remove" ? copy.ownerSettings.removing : copy.ownerSettings.remove}
                  </button>
                ) : null}
              </div>
              {message ? <p className="form-message" role="status">{message}</p> : null}
            </form>
          </div>
        ) : null}
      </section>
    </>
  );
}

type InformationKind = "privacy" | "important";

export function InformationDialog({
  kind,
  onClose,
  triggerRef,
}: {
  kind: InformationKind | null;
  onClose: () => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (!kind) {
      if (wasOpen.current) {
        wasOpen.current = false;
        triggerRef.current?.focus();
      }
      return;
    }
    wasOpen.current = true;
    panelRef.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [kind, onClose, triggerRef]);
  if (!kind) return null;
  const privacy = kind === "privacy";
  const sections = privacy
    ? [
        [copy.legal.privacyReplayTitle, copy.legal.privacyReplayBody],
        [copy.legal.privacyDraftTitle, copy.legal.privacyDraftBody],
        [copy.legal.privacyCurationTitle, copy.legal.privacyCurationBody],
        [copy.legal.privacyOwnerTitle, copy.legal.privacyOwnerBody],
        [copy.legal.privacyKeyTitle, copy.legal.privacyKeyBody],
        [copy.legal.privacyFlowTitle, copy.legal.privacyFlowBody],
      ]
    : [
        [copy.legal.importantPaperTitle, copy.legal.importantPaperBody],
        [copy.legal.importantAdviceTitle, copy.legal.importantAdviceBody],
        [copy.legal.importantModelTitle, copy.legal.importantModelBody],
      ];
  return (
    <>
      <div className="owner-modal-backdrop" aria-hidden="true" onClick={onClose} />
      <section
        ref={panelRef}
        className="owner-modal information-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="information-title"
        tabIndex={-1}
        onKeyDown={(event) => trapModalFocus(event, panelRef.current)}
      >
        <header className="owner-modal__heading">
          <h2 id="information-title">
            {privacy ? copy.legal.privacyTitle : copy.legal.importantTitle}
          </h2>
          <button type="button" className="quiet-button" onClick={onClose}>
            {copy.legal.close}
          </button>
        </header>
        <div className="information-sections">
          {sections.map(([title, body]) => (
            <section key={title}>
              <h3>{title}</h3>
              <p>{body}</p>
            </section>
          ))}
        </div>
      </section>
    </>
  );
}
