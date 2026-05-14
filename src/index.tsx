import {
  ButtonItem,
  ConfirmModal,
  Field,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
  toaster,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { FaCopy, FaGamepad, FaPlus, FaSyncAlt, FaTrash } from "react-icons/fa";

type Controller = { vid_pid: string; name: string };

type ConnectedDevice = {
  kernel: string;
  vid_pid: string;
  name: string;
};

type CurrentDevice = {
  kernel: string;
  name: string;
  uniq: string;
  product: string;
  vid_pid: string;
  is_supported: boolean;
  is_internal: boolean;
};

type State = {
  enabled: boolean;
  disabled_deck: boolean;
  connected: ConnectedDevice[];
  base_controllers: Controller[];
  user_controllers: Controller[];
  pyudev_available: boolean;
  pyudev_error: string | null;
};

type Result = { ok: boolean; error?: string };

const getState = callable<[], State>("get_state");
const setEnabled = callable<[enabled: boolean], Result>("set_enabled");
const addController = callable<[vid_pid: string, name: string], Result>("add_controller");
const removeController = callable<[vid_pid: string], Result>("remove_controller");
const getCurrentDevices = callable<[], CurrentDevice[]>("get_current_devices");

const VID_PID_PATTERN = /^[0-9a-f]{4}:[0-9a-f]{4}$/i;

function formatSnippet(d: CurrentDevice): string {
  return [
    "```",
    `name:    ${d.name}`,
    `product: ${d.product}`,
    `uniq:    ${d.uniq}`,
    `vid:pid: ${d.vid_pid}`,
    "```",
  ].join("\n");
}

function AddControllerModal({
  closeModal,
  onAdded,
  defaultVidPid = "",
  defaultName = "",
}: {
  closeModal?: () => void;
  onAdded: () => void;
  defaultVidPid?: string;
  defaultName?: string;
}) {
  const [vidPid, setVidPid] = useState(defaultVidPid);
  const [name, setName] = useState(defaultName);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    const cleaned = vidPid.trim().toLowerCase();
    if (!VID_PID_PATTERN.test(cleaned)) {
      setError("VID:PID must look like 054c:0ce6.");
      return;
    }
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setBusy(true);
    const res = await addController(cleaned, name.trim());
    setBusy(false);
    if (!res.ok) {
      setError(res.error || "Failed to add.");
      return;
    }
    onAdded();
    closeModal?.();
  };

  return (
    <ConfirmModal
      strTitle="Add Controller"
      strOKButtonText={busy ? "Adding..." : "Add"}
      strCancelButtonText="Cancel"
      onOK={submit}
      onCancel={() => closeModal?.()}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <TextField
          label="Name"
          description="e.g. DualSense, Xbox Series Controller"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextField
          label="VID:PID"
          description="Four hex digits, colon, four hex digits. Use the Currently Detected panel to find it."
          value={vidPid}
          onChange={(e) => setVidPid(e.target.value)}
        />
        {error && (
          <div style={{ color: "#ff6b6b", fontSize: "0.9em" }}>{error}</div>
        )}
      </div>
    </ConfirmModal>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [devices, setDevices] = useState<CurrentDevice[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const refreshState = useCallback(async () => {
    try {
      const s = await getState();
      setState(s);
    } catch (e) {
      toaster.toast({ title: "Plugin error", body: String(e) });
    }
  }, []);

  const refreshDevices = useCallback(async () => {
    setRefreshing(true);
    try {
      const d = await getCurrentDevices();
      setDevices(d);
    } catch (e) {
      toaster.toast({ title: "Plugin error", body: String(e) });
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refreshState();
  }, [refreshState]);

  const onToggle = async (next: boolean) => {
    const res = await setEnabled(next);
    if (!res.ok) {
      toaster.toast({ title: "Couldn't change state", body: res.error || "Unknown error" });
    }
    await refreshState();
  };

  const onRemove = async (vid_pid: string) => {
    const res = await removeController(vid_pid);
    if (!res.ok) {
      toaster.toast({ title: "Remove failed", body: res.error || "Unknown error" });
    }
    await refreshState();
  };

  const openAddModal = (defaultVidPid = "", defaultName = "") => {
    showModal(
      <AddControllerModal
        defaultVidPid={defaultVidPid}
        defaultName={defaultName}
        onAdded={refreshState}
      />,
    );
  };

  const copyDeviceInfo = async (d: CurrentDevice) => {
    const snippet = formatSnippet(d);
    try {
      await navigator.clipboard.writeText(snippet);
      toaster.toast({ title: "Copied", body: d.vid_pid });
    } catch {
      toaster.toast({ title: "Couldn't copy", body: snippet });
    }
  };

  if (!state) {
    return (
      <PanelSection title="Loading">
        <PanelSectionRow>
          <Field label="Reading state..." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <>
      <PanelSection title="Options">
        <PanelSectionRow>
          <ToggleField
            checked={state.enabled}
            disabled={!state.pyudev_available}
            label="Enable plugin"
            description={
              state.pyudev_available
                ? state.disabled_deck
                  ? "Deck pad is currently disabled."
                  : "Idle. Connect a supported controller to disable the Deck pad."
                : `Unavailable: ${state.pyudev_error || "pyudev not installed"}`
            }
            onChange={onToggle}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Supported Controllers">
        {state.user_controllers.length === 0 && state.base_controllers.length === 0 && (
          <PanelSectionRow>
            <Field label="No controllers in the list." />
          </PanelSectionRow>
        )}
        {state.user_controllers.map((c) => (
          <PanelSectionRow key={`user-${c.vid_pid}`}>
            <Field
              label={c.name}
              description={c.vid_pid}
              icon={<FaTrash />}
              onClick={() => onRemove(c.vid_pid)}
              bottomSeparator="standard"
            />
          </PanelSectionRow>
        ))}
        {state.base_controllers.map((c) => (
          <PanelSectionRow key={`base-${c.vid_pid}`}>
            <Field label={c.name} description={c.vid_pid} bottomSeparator="standard" />
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem layout="below" icon={<FaPlus />} onClick={() => openAddModal()}>
            Add controller
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Currently Detected">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            icon={<FaSyncAlt />}
            onClick={refreshDevices}
            disabled={refreshing}
          >
            {refreshing ? "Scanning..." : "Refresh"}
          </ButtonItem>
        </PanelSectionRow>
        {devices === null && (
          <PanelSectionRow>
            <Field description="Press Refresh to scan connected input devices." />
          </PanelSectionRow>
        )}
        {devices !== null && devices.length === 0 && (
          <PanelSectionRow>
            <Field description="No joystick-class input devices found." />
          </PanelSectionRow>
        )}
        {devices?.map((d) => {
          const badge = d.is_internal
            ? "INTERNAL"
            : d.is_supported
              ? "SUPPORTED"
              : "UNKNOWN";
          const badgeColor = d.is_internal
            ? "#888"
            : d.is_supported
              ? "#5ec269"
              : "#e0a93b";
          return (
            <PanelSectionRow key={`dev-${d.kernel}`}>
              <Field
                label={d.name || d.kernel}
                description={
                  <span>
                    <span style={{ color: badgeColor, fontWeight: 600 }}>{badge}</span>
                    {" — "}
                    {d.vid_pid}
                  </span>
                }
                bottomSeparator="standard"
              />
              <div style={{ display: "flex", gap: "4px", marginTop: "4px" }}>
                <ButtonItem
                  layout="below"
                  icon={<FaCopy />}
                  onClick={() => copyDeviceInfo(d)}
                >
                  Copy info
                </ButtonItem>
                {!d.is_supported && !d.is_internal && (
                  <ButtonItem
                    layout="below"
                    icon={<FaPlus />}
                    onClick={() => openAddModal(d.vid_pid, d.name)}
                  >
                    Add to my list
                  </ButtonItem>
                )}
              </div>
            </PanelSectionRow>
          );
        })}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "Steam Deck Input Disabler",
    titleView: <div className={staticClasses.Title}>Steam Deck Input Disabler</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {},
  };
});
