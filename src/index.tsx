import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  TextField,
  Toggle,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FaArrowLeft,
  FaCopy,
  FaGamepad,
  FaListUl,
  FaPlus,
  FaSyncAlt,
  FaTrash,
} from "react-icons/fa";

type Controller = { vid_pid: string; name: string; active: boolean };

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
  is_active: boolean;
  is_internal: boolean;
};

type State = {
  enabled: boolean;
  kbm_enabled: boolean;
  disabled_deck: boolean;
  connected: ConnectedDevice[];
  present_vid_pids: string[];
  base_controllers: Controller[];
  user_controllers: Controller[];
  pyudev_available: boolean;
  pyudev_error: string | null;
  ready?: boolean;
};

type Result = { ok: boolean; error?: string };

const getState = callable<[], State>("get_state");
const setEnabled = callable<[enabled: boolean], Result>("set_enabled");
const setKbmEnabled = callable<[enabled: boolean], Result>("set_kbm_enabled");
const addController = callable<[vid_pid: string, name: string], Result>("add_controller");
const removeController = callable<[vid_pid: string], Result>("remove_controller");
const setControllerActive = callable<[vid_pid: string, active: boolean], Result>(
  "set_controller_active",
);
const getCurrentDevices = callable<[], CurrentDevice[]>("get_current_devices");

const VID_PID_PATTERN = /^[0-9a-f]{4}:[0-9a-f]{4}$/i;

// Steam unmounts the QAM panel content while a modal is on screen and remounts
// it from scratch when the modal closes. Keep the current view at module scope
// so the fresh Content instance can restore it instead of always landing back
// on the main panel.
type ViewName = "main" | "list";
let persistedView: ViewName = "main";

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
      onEscKeypress={() => closeModal?.()}
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

type FilterMode = "all" | "base" | "user" | "active" | "inactive" | "connected";

const FILTERS: { id: FilterMode; label: string }[] = [
  { id: "all", label: "All" },
  { id: "connected", label: "Connected" },
  { id: "user", label: "Mine" },
  { id: "base", label: "Built-in" },
  { id: "active", label: "Active" },
  { id: "inactive", label: "Inactive" },
];

function ConfirmRemoveModal({
  closeModal,
  name,
  vid_pid,
  onConfirm,
}: {
  closeModal?: () => void;
  name: string;
  vid_pid: string;
  onConfirm: () => Promise<void>;
}) {
  return (
    <ConfirmModal
      strTitle="Remove controller?"
      strDescription={
        <div>
          <div>{name}</div>
          <div style={{ opacity: 0.7, fontSize: "0.9em" }}>{vid_pid}</div>
          <div style={{ marginTop: "8px" }}>
            This will remove the entry from your list. You can add it back later.
          </div>
        </div>
      }
      strOKButtonText="Remove"
      strCancelButtonText="Cancel"
      bDestructiveWarning
      onOK={async () => {
        await onConfirm();
        closeModal?.();
      }}
      onCancel={() => closeModal?.()}
      onEscKeypress={() => closeModal?.()}
    />
  );
}

function ControllerListView({
  state,
  onBack,
  onAdd,
  onToggleActive,
  onRequestRemove,
  onRefresh,
}: {
  state: State;
  onBack: () => void;
  onAdd: () => void;
  onToggleActive: (vid_pid: string, next: boolean) => Promise<void>;
  onRequestRemove: (vid_pid: string, name: string) => void;
  onRefresh: () => Promise<unknown>;
}) {
  const [query, setQuery] = useState("");
  // Default to the 'Connected' filter if at least one known controller is
  // currently plugged in, otherwise fall back to 'All'. Only evaluated once
  // on mount so plugging or unplugging later won't yank the user's filter
  // out from under them.
  const [filter, setFilter] = useState<FilterMode>(() => {
    const present = new Set((state.present_vid_pids || []).map((v) => v.toLowerCase()));
    if (present.size === 0) return "all";
    const known = [
      ...state.user_controllers,
      ...state.base_controllers,
    ].some((c) => present.has(c.vid_pid.toLowerCase()));
    return known ? "connected" : "all";
  });

  // Poll while the list is open so the 'connected' tag follows plug/unplug
  // events without the user having to back out. The interval only exists
  // while the page is visible; hiding the QAM tears it down and showing it
  // again recreates it, so we never poll in the background.
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id !== null) return;
      id = setInterval(onRefresh, 2000);
    };
    const stop = () => {
      if (id === null) return;
      clearInterval(id);
      id = null;
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") start();
      else stop();
    };
    document.addEventListener("visibilitychange", onVisibility);
    if (document.visibilityState === "visible") start();
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [onRefresh]);

  const presentSet = useMemo(
    () => new Set((state.present_vid_pids || []).map((v) => v.toLowerCase())),
    [state.present_vid_pids],
  );

  type Row = Controller & { source: "user" | "base"; connected: boolean };
  const rows = useMemo<Row[]>(() => {
    const all: Row[] = [
      ...state.user_controllers.map((c) => ({
        ...c,
        source: "user" as const,
        connected: presentSet.has(c.vid_pid.toLowerCase()),
      })),
      ...state.base_controllers.map((c) => ({
        ...c,
        source: "base" as const,
        connected: presentSet.has(c.vid_pid.toLowerCase()),
      })),
    ];
    const q = query.trim().toLowerCase();
    return all.filter((c) => {
      if (filter === "base" && c.source !== "base") return false;
      if (filter === "user" && c.source !== "user") return false;
      if (filter === "active" && !c.active) return false;
      if (filter === "inactive" && c.active) return false;
      if (filter === "connected" && !c.connected) return false;
      if (!q) return true;
      return c.name.toLowerCase().includes(q) || c.vid_pid.toLowerCase().includes(q);
    });
  }, [state.user_controllers, state.base_controllers, presentSet, query, filter]);

  const total = state.user_controllers.length + state.base_controllers.length;

  return (
    <Focusable onCancel={onBack}>
      <PanelSection>
        <PanelSectionRow>
          <DialogButton
            style={{ display: "flex", alignItems: "center", gap: "8px", justifyContent: "center" }}
            onClick={onBack}
          >
            <FaArrowLeft />
            <span>Back</span>
          </DialogButton>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Search"
            description={`${rows.length} of ${total} shown`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Focusable
            style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}
            flow-children="horizontal"
          >
            {FILTERS.map((f) => (
              <DialogButton
                key={f.id}
                style={{
                  minWidth: 0,
                  padding: "4px 10px",
                  fontSize: "0.85em",
                  opacity: filter === f.id ? 1 : 0.6,
                }}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </DialogButton>
            ))}
          </Focusable>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Supported Controllers">
        {rows.length === 0 && (
          <PanelSectionRow>
            <Field label={query || filter !== "all" ? "No matches." : "No controllers in the list."} />
          </PanelSectionRow>
        )}
        {rows.map((c) => (
          <PanelSectionRow key={`${c.source}-${c.vid_pid}`}>
            <Field
              label={c.name}
              description={
                <span>
                  {c.vid_pid}
                  {" \u2014 "}
                  <span style={{ opacity: 0.7 }}>
                    {c.source === "user" ? "yours" : "built-in"}
                  </span>
                  {c.connected && (
                    <>
                      {" \u2014 "}
                      <span style={{ color: "#5ec269", fontWeight: 600 }}>
                        connected
                      </span>
                    </>
                  )}
                </span>
              }
              bottomSeparator="standard"
              padding="compact"
              childrenContainerWidth="max"
            >
              <Focusable
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "flex-end",
                  gap: "12px",
                }}
                flow-children="horizontal"
              >
                <div style={{ flexShrink: 0 }}>
                  <Toggle
                    value={c.active}
                    onChange={(next) => onToggleActive(c.vid_pid, next)}
                  />
                </div>
                {c.source === "user" && (
                  <DialogButton
                    style={{
                      minWidth: 0,
                      width: "36px",
                      padding: "4px 8px",
                      flexShrink: 0,
                    }}
                    onClick={() => onRequestRemove(c.vid_pid, c.name)}
                  >
                    <FaTrash />
                  </DialogButton>
                )}
              </Focusable>
            </Field>
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem layout="below" icon={<FaPlus />} onClick={onAdd}>
            Add controller
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </Focusable>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [devices, setDevices] = useState<CurrentDevice[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [view, setViewState] = useState<ViewName>(() => persistedView);
  const setView = useCallback((v: ViewName) => {
    persistedView = v;
    setViewState(v);
  }, []);

  const refreshState = useCallback(async () => {
    try {
      const s = await getState();
      setState(s);
      return s;
    } catch (e) {
      // Don't toast on the initial poll: the backend's _main may not have
      // finished yet, in which case the RPC layer rejects. We just retry.
      console.warn("[steamdeck-input-disabler] get_state failed:", e);
      return null;
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
    // Drop the toggle key persisted by versions <= 1.0.2; backend state is now authoritative.
    try { localStorage.removeItem("script_state"); } catch {}

    let cancelled = false;
    let attempt = 0;
    const poll = async () => {
      while (!cancelled) {
        const s = await refreshState();
        if (cancelled) return;
        if (s && s.ready !== false) return;
        // Backend isn't ready yet (or call failed). Back off and try again.
        attempt += 1;
        const delay = Math.min(2000, 150 * attempt);
        await new Promise((r) => setTimeout(r, delay));
      }
    };
    poll();
    return () => {
      cancelled = true;
    };
  }, [refreshState]);

  // Light polling while the main panel is visible so the description and the
  // disabled_deck status follow plug/unplug events without a manual refresh.
  // Same visibility-gated pattern as the controller list.
  useEffect(() => {
    if (view !== "main") return;
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id !== null) return;
      id = setInterval(refreshState, 2000);
    };
    const stop = () => {
      if (id === null) return;
      clearInterval(id);
      id = null;
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") start();
      else stop();
    };
    document.addEventListener("visibilitychange", onVisibility);
    if (document.visibilityState === "visible") start();
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [view, refreshState]);

  // Toast on disabled_deck transitions. We track the previous value across
  // renders via a ref and skip the initial sighting so opening the panel
  // while the Deck pad is already disabled doesn't fire a stale toast.
  const prevDisabledRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (!state) return;
    const next = state.disabled_deck;
    const prev = prevDisabledRef.current;
    prevDisabledRef.current = next;
    if (prev === null) return; // first observation, no transition
    if (prev === next) return;
    // toaster.toast can throw on certain loader versions if the toaster API
    // isn't fully wired up. Don't take down the panel for a notification.
    try {
      if (next) {
        toaster.toast({
          title: "Deck pad disabled",
          body: "External device connected.",
        });
      } else {
        toaster.toast({
          title: "Deck pad enabled",
          body: "External device disconnected.",
        });
      }
    } catch (e) {
      console.warn("[steamdeck-input-disabler] toaster.toast failed:", e);
    }
  }, [state]);

  const onToggle = async (next: boolean) => {
    const res = await setEnabled(next);
    if (!res.ok) {
      toaster.toast({ title: "Couldn't change state", body: res.error || "Unknown error" });
    }
    await refreshState();
  };

  const onKbmToggle = async (next: boolean) => {
    const res = await setKbmEnabled(next);
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

  const onToggleActive = async (vid_pid: string, next: boolean) => {
    const res = await setControllerActive(vid_pid, next);
    if (!res.ok) {
      toaster.toast({ title: "Couldn't change controller", body: res.error || "Unknown error" });
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

  if (view === "list") {
    return (
      <ControllerListView
        state={state}
        onBack={() => setView("main")}
        onAdd={() => openAddModal()}
        onToggleActive={onToggleActive}
        onRequestRemove={(vid_pid, name) =>
          showModal(
            <ConfirmRemoveModal
              vid_pid={vid_pid}
              name={name}
              onConfirm={() => onRemove(vid_pid)}
            />,
          )
        }
        onRefresh={refreshState}
      />
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
        <PanelSectionRow>
          <ToggleField
            checked={state.kbm_enabled}
            disabled={!state.pyudev_available || !state.enabled}
            label="Enable keyboard and mouse support"
            description={
              !state.enabled
                ? "Turn the plugin on first."
                : "Also disable the Deck pad when an external keyboard or mouse is connected."
            }
            onChange={onKbmToggle}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Supported Controllers">
        <PanelSectionRow>
          <DialogButton
            style={{ display: "flex", alignItems: "center", gap: "8px", justifyContent: "center" }}
            onClick={() => setView("list")}
          >
            <FaListUl />
            <span>
              {`Manage controllers (${
                state.user_controllers.length + state.base_controllers.length
              })`}
            </span>
          </DialogButton>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Currently Detected">
        <PanelSectionRow>
          <DialogButton
            style={{ display: "flex", alignItems: "center", gap: "8px", justifyContent: "center" }}
            onClick={refreshDevices}
            disabled={refreshing}
          >
            <FaSyncAlt />
            <span>{refreshing ? "Scanning..." : "Refresh"}</span>
          </DialogButton>
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
          let badge: string;
          let badgeColor: string;
          if (d.is_internal) {
            badge = "INTERNAL";
            badgeColor = "#888";
          } else if (d.is_supported && d.is_active) {
            badge = "SUPPORTED";
            badgeColor = "#5ec269";
          } else if (d.is_supported) {
            // Known to the plugin but the entry is toggled off in the list.
            badge = "SUPPORTED (disabled)";
            badgeColor = "#888";
          } else {
            badge = "UNKNOWN";
            badgeColor = "#e0a93b";
          }
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
              <div
                style={{
                  display: "flex",
                  gap: "6px",
                  marginTop: "4px",
                  width: "100%",
                }}
              >
                <DialogButton
                  style={{
                    flex: "1 1 0",
                    minWidth: 0,
                    padding: "4px 8px",
                    fontSize: "0.85em",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                  }}
                  onClick={() => copyDeviceInfo(d)}
                >
                  <FaCopy />
                  <span>Copy info</span>
                </DialogButton>
                {!d.is_supported && !d.is_internal && (
                  <DialogButton
                    style={{
                      flex: "1 1 0",
                      minWidth: 0,
                      padding: "4px 8px",
                      fontSize: "0.85em",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: "6px",
                    }}
                    onClick={() => openAddModal(d.vid_pid, d.name)}
                  >
                    <FaPlus />
                    <span>Add to my list</span>
                  </DialogButton>
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
