import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copy } from "../content/copy";
import { OwnerRequestError, type OwnerSettingsClient } from "./api";
import { InformationDialog, OwnerSettings } from "./OwnerSettings";

const emptyStatus = { schema_version: "v1" as const, configured: false };
const configuredStatus = {
  schema_version: "v1" as const,
  configured: true,
  provider: "GEMINI" as const,
  endpoint: "https://generativelanguage.googleapis.com",
  model: "gemini-2.5-flash",
  generation: 4,
};

function client(overrides: Partial<OwnerSettingsClient> = {}): OwnerSettingsClient {
  return {
    createSession: vi.fn(async () => undefined),
    read: vi.fn(async () => emptyStatus),
    replace: vi.fn(async () => configuredStatus),
    clear: vi.fn(async () => emptyStatus),
    signOut: vi.fn(async () => undefined),
    ...overrides,
  };
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(cleanup);

describe("owner settings", () => {
  it("shows only shortcuts and makes no owner request in Replay-only mode", async () => {
    const api = client();
    render(
      <OwnerSettings
        open
        onClose={vi.fn()}
        triggerRef={createRef()}
        ownerControlsEnabled={false}
        client={api}
      />,
    );

    expect(screen.getByText(copy.keyboardGuide.help)).toBeVisible();
    expect(screen.queryByLabelText(copy.ownerSettings.settingsCode)).not.toBeInTheDocument();
    expect(api.read).not.toHaveBeenCalled();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it("accepts the access code only in the signed-out view and clears it after sign-in", async () => {
    const user = userEvent.setup();
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const api = client({
      read: vi
        .fn()
        .mockRejectedValueOnce(new OwnerRequestError(403))
        .mockResolvedValueOnce(emptyStatus),
    });
    const triggerRef = createRef<HTMLButtonElement>();
    const { container } = render(
      <>
        <button ref={triggerRef}>trigger</button>
        <OwnerSettings open onClose={vi.fn()} triggerRef={triggerRef} client={api} />
      </>,
    );
    const settingsValue = "private-owner-code";
    const input = await screen.findByLabelText(copy.ownerSettings.settingsCode);
    expect(input).toHaveFocus();

    await user.type(input, settingsValue);
    await user.click(screen.getByRole("button", { name: copy.ownerSettings.signIn }));

    await screen.findByText(copy.ownerSettings.currentTitle);
    expect(api.createSession).toHaveBeenCalledWith(settingsValue);
    expect(screen.queryByLabelText(copy.ownerSettings.settingsCode)).not.toBeInTheDocument();
    expect(container.textContent).not.toContain(settingsValue);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(storageWrite).not.toHaveBeenCalled();
  });

  it("saves a Gemini key once, clears the field, and shows only key-free status", async () => {
    const user = userEvent.setup();
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const providerValue = "private-gemini-key";
    const api = client();
    render(
      <OwnerSettings open onClose={vi.fn()} triggerRef={createRef()} client={api} />,
    );
    const model = await screen.findByLabelText(copy.ownerSettings.model);
    await user.type(model, "gemini-2.5-flash");
    const keyInput = document.getElementById("owner-provider-key") as HTMLInputElement;
    expect(screen.getByText(copy.ownerSettings.providerFieldLabel).closest("label")).toHaveAttribute(
      "for",
      "owner-provider-key",
    );
    await user.type(keyInput, providerValue);
    await user.click(screen.getByRole("button", { name: copy.ownerSettings.save }));

    await screen.findByText(copy.ownerSettings.saved);
    expect(api.replace).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "GEMINI",
        api_key: providerValue,
        endpoint: null,
      }),
    );
    expect(keyInput).toHaveValue("");
    expect(screen.getByRole("dialog")).not.toHaveTextContent(providerValue);
    expect(screen.getByText("gemini-2.5-flash")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(storageWrite).not.toHaveBeenCalled();
  });

  it("shows the custom endpoint only for OpenAI-compatible providers", async () => {
    const user = userEvent.setup();
    render(<OwnerSettings open onClose={vi.fn()} triggerRef={createRef()} client={client()} />);
    const provider = await screen.findByLabelText(copy.ownerSettings.provider);
    expect(screen.queryByLabelText(copy.ownerSettings.endpoint)).not.toBeInTheDocument();

    await user.selectOptions(provider, "OPENAI_COMPATIBLE");
    const endpoint = document.getElementById("owner-provider-endpoint");
    expect(endpoint).toBeRequired();
    expect(screen.getByText(copy.ownerSettings.endpoint).closest("label")).toHaveAttribute(
      "for",
      "owner-provider-endpoint",
    );
    expect(screen.getByText(copy.ownerSettings.endpointHelp)).toBeInTheDocument();
  });

  it("clears a rejected access code and shows no server detail", async () => {
    const user = userEvent.setup();
    const api = client({
      read: vi.fn(async () => {
        throw new OwnerRequestError(403);
      }),
      createSession: vi.fn(async () => {
        throw new Error("private server detail");
      }),
    });
    render(<OwnerSettings open onClose={vi.fn()} triggerRef={createRef()} client={api} />);
    const input = await screen.findByLabelText(copy.ownerSettings.settingsCode);
    await user.type(input, "rejected-owner-code");
    await user.click(screen.getByRole("button", { name: copy.ownerSettings.signIn }));

    expect(await screen.findByRole("alert")).toHaveTextContent(copy.ownerSettings.signInFailed);
    expect(screen.getByRole("alert")).not.toHaveTextContent("private server detail");
    expect(input).toHaveValue("");
  });

  it("traps focus and restores the settings trigger when closed", async () => {
    const user = userEvent.setup();
    const triggerRef = createRef<HTMLButtonElement>();
    const api = client();
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button ref={triggerRef}>trigger</button>
          <OwnerSettings open={open} onClose={() => setOpen(false)} triggerRef={triggerRef} client={api} />
        </>
      );
    }
    render(<Harness />);
    const dialog = await screen.findByRole("dialog", { name: copy.ownerSettings.title });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: copy.ownerSettings.title })).not.toBeInTheDocument();
    await waitFor(() => expect(triggerRef.current).toHaveFocus());
  });

  it("presents the current privacy and product limits as readable dialogs", async () => {
    const triggerRef = createRef<HTMLButtonElement>();
    const { rerender } = render(
      <InformationDialog kind="privacy" onClose={vi.fn()} triggerRef={triggerRef} />,
    );
    const privacy = screen.getByRole("dialog", { name: copy.legal.privacyTitle });
    expect(privacy).toHaveTextContent(copy.legal.privacyOwnerBody);
    expect(privacy).toHaveTextContent(copy.legal.privacyKeyBody);
    expect(privacy).toHaveTextContent(copy.legal.privacyFlowBody);

    rerender(
      <InformationDialog kind="important" onClose={vi.fn()} triggerRef={triggerRef} />,
    );
    const important = screen.getByRole("dialog", { name: copy.legal.importantTitle });
    expect(within(important).getByText(copy.legal.importantPaperBody)).toBeInTheDocument();
    expect(important).toHaveTextContent(copy.legal.importantAdviceBody);
  });
});
