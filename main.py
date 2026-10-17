import asyncio
import glob
import json
import os
import re
import shutil
import subprocess

import decky

try:
    import pyudev
    _PYUDEV_AVAILABLE = True
    _PYUDEV_IMPORT_ERROR = None
except Exception as e:
    pyudev = None
    _PYUDEV_AVAILABLE = False
    _PYUDEV_IMPORT_ERROR = f"{type(e).__name__}: {e}"

PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR

BASE_CONTROLLERS_PATH = os.path.join(PLUGIN_DIR, "base_controllers.json")
USER_CONTROLLERS_PATH = os.path.join(SETTINGS_DIR, "user_controllers.json")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
# Per-VID:PID active overrides for built-in entries. User entries store
# their active flag inline; the shipped base_controllers.json is read-only,
# so we persist overrides separately to survive upgrades.
OVERRIDES_PATH = os.path.join(SETTINGS_DIR, "controller_overrides.json")

DECK_VID = "28de"
DECK_PID = "1205"

VID_PID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")

# The 2026 Steam Controller never shows up as an evdev joystick while Steam
# runs: Steam reads it through hidraw, and hid-steam only registers its input
# device while no hidraw client is open. So these are watched as hidraw nodes.
# IDs from the kernel's hid-ids.h.
STEAM_2026_WIRED = "28de:1302"
STEAM_2026_BLE = "28de:1303"
STEAM_2026_PUCK = "28de:1304"
STEAM_2026_IDS = (STEAM_2026_WIRED, STEAM_2026_BLE, STEAM_2026_PUCK)

# The puck is plugged in whether or not a controller is on, so each of its
# four slot interfaces is read to learn whether a controller is connected.
# Report layout from hid-steam.c (Ibex).
REPORT_ID_INPUT = 0x42
REPORT_ID_BATTERY = 0x43
REPORT_ID_INPUT2 = 0x45
REPORT_ID_WIRELESS_EVENT = 0x79
WIRELESS_EVENT_DISCONNECT = 1
WIRELESS_EVENT_CONNECT = 2
# Report sizes include the report ID byte.
SLOT_PRESENCE_REPORTS = {REPORT_ID_INPUT: 54, REPORT_ID_INPUT2: 46, REPORT_ID_BATTERY: 15}
# The puck's stripped-down pogo pin interface; its one collection has this usage.
PUCK_POGO_USAGE = 0xFF000002


def _slot_event(report):
    """True if this puck slot report means a controller is connected, False if
    it means disconnected, None if it says neither."""
    if not report:
        return None
    rid = report[0]
    if rid == REPORT_ID_WIRELESS_EVENT and len(report) == 2:
        if report[1] == WIRELESS_EVENT_CONNECT:
            return True
        if report[1] == WIRELESS_EVENT_DISCONNECT:
            return False
        return None
    if SLOT_PRESENCE_REPORTS.get(rid) == len(report):
        return True
    return None


def _first_collection_usage(rdesc):
    """Usage page and usage (page << 16 | usage) of the first collection in a
    HID report descriptor, or None."""
    page = 0
    usage = None
    i = 0
    while i < len(rdesc):
        prefix = rdesc[i]
        if prefix == 0xFE:  # long item: size byte, tag byte, data
            if i + 1 >= len(rdesc):
                return None
            i += 3 + rdesc[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(rdesc[i + 1:i + 1 + size], "little")
        tag = prefix & 0xFC
        if tag == 0x04:  # Usage Page
            page = value
        elif tag == 0x08:  # Usage
            usage = value if size == 4 else (page << 16) | value
        elif tag == 0xA0:  # Collection
            return usage
        i += 1 + size
    return None


def _parse_hid_id(hid_id):
    """'0003:000028DE:00001304' -> '28de:1304', else None."""
    parts = (hid_id or "").split(":")
    if len(parts) != 3:
        return None
    try:
        return f"{int(parts[1], 16):04x}:{int(parts[2], 16):04x}"
    except ValueError:
        return None


def _steam_2026_info(device):
    """Describe a hidraw node of a 2026 Steam Controller, or None.

    Works whether hid-steam or hid-generic is bound: hid-steam's client device
    (the hidraw Steam opens) copies the real interface's IDs and descriptor.
    """
    try:
        hid = device.find_parent("hid")
    except Exception:
        hid = None
    if hid is None:
        return None
    vid_pid = _parse_hid_id(hid.get("HID_ID"))
    if vid_pid not in STEAM_2026_IDS:
        return None
    if vid_pid == STEAM_2026_PUCK:
        try:
            with open(os.path.join(hid.sys_path, "report_descriptor"), "rb") as f:
                rdesc = f.read()
        except OSError as e:
            decky.logger.debug(f"report_descriptor for {device.sys_name}: {e}")
            return None
        if _first_collection_usage(rdesc) == PUCK_POGO_USAGE:
            return None
    return {
        "vid_pid": vid_pid,
        "name": hid.get("HID_NAME") or "Steam Controller (2026)",
        "uniq": hid.get("HID_UNIQ") or "",
        "product": hid.get("HID_ID") or "",
        "devnode": device.device_node,
    }


def _read_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        decky.logger.error(f"Malformed JSON at {path}: {e}")
        return default
    except OSError as e:
        decky.logger.error(f"Failed to read {path}: {e}")
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _parse_product(product):
    if not product:
        return None
    parts = product.split("/")
    if len(parts) < 3:
        return None
    try:
        vid = f"{int(parts[1], 16):04x}"
        pid = f"{int(parts[2], 16):04x}"
        return vid, pid
    except (ValueError, IndexError):
        return None


def _find_deck_hid_interfaces():
    interfaces = []
    for dev_path in glob.glob("/sys/bus/usb/devices/*"):
        vid_path = os.path.join(dev_path, "idVendor")
        pid_path = os.path.join(dev_path, "idProduct")
        if not (os.path.isfile(vid_path) and os.path.isfile(pid_path)):
            continue
        try:
            with open(vid_path) as f:
                vid = f.read().strip().lower()
            with open(pid_path) as f:
                pid = f.read().strip().lower()
        except OSError:
            continue
        if vid != DECK_VID or pid != DECK_PID:
            continue
        dev_name = os.path.basename(dev_path)
        for iface in glob.glob(os.path.join(dev_path, f"{dev_name}:*")):
            interfaces.append(os.path.basename(iface))
    return interfaces


def _unbind_deck():
    interfaces = _find_deck_hid_interfaces()
    if not interfaces:
        decky.logger.warning("No Steam Deck HID interfaces found to unbind")
        return
    for iface in interfaces:
        try:
            with open("/sys/bus/usb/drivers/usbhid/unbind", "w") as f:
                f.write(iface)
        except OSError as e:
            decky.logger.debug(f"unbind {iface}: {e}")


def _rebind_deck():
    interfaces = _find_deck_hid_interfaces()
    if not interfaces:
        decky.logger.warning("No Steam Deck HID interfaces found to rebind")
        return
    for iface in interfaces:
        try:
            with open("/sys/bus/usb/drivers/usbhid/bind", "w") as f:
                f.write(iface)
        except OSError as e:
            decky.logger.debug(f"bind {iface}: {e}")


class Plugin:
    def __init__(self):
        # Initialize every attribute synchronously so any RPC call (e.g. get_state)
        # that arrives before _main has been scheduled by the event loop still
        # sees a fully-formed Plugin instance instead of raising AttributeError.
        self._loop = None
        self._lock = asyncio.Lock()
        self._observer = None
        self._connected = {}
        # 2026 Steam Controller hidraw nodes, keyed by kernel name (hidrawN):
        # the _steam_2026_info dict plus "connected" and "fd". fd is set only
        # for puck slots, which are read to follow the connection state.
        self._hidraw = {}
        self._disabled = False
        self._import_error = _PYUDEV_IMPORT_ERROR
        self._base_controllers = []
        self._user_controllers = []
        self._settings = {"enabled": False}
        self._overrides = {}
        self._allowlist = set()
        self._ready = False

    async def _main(self):
        self._loop = asyncio.get_running_loop()

        self._base_controllers = _read_json(BASE_CONTROLLERS_PATH, [])
        self._user_controllers = _read_json(USER_CONTROLLERS_PATH, [])
        self._settings = _read_json(SETTINGS_PATH, {"enabled": False, "kbm_enabled": False})
        # Make sure new settings fields exist for users upgrading from older versions.
        self._settings.setdefault("kbm_enabled", False)
        self._overrides = _read_json(OVERRIDES_PATH, {})
        self._rebuild_allowlist()

        # Defensive rebind on startup. A previous plugin process that crashed or was
        # killed mid-flight (e.g. during an update) can leave the Deck's HID interfaces
        # unbound from the kernel driver. A fresh process has no tracked controllers,
        # so it's safe to assume any unbound state we observe now is stale. The
        # observer's enumerate step right below will re-disable the Deck pad if a
        # supported controller is actually present, so the steady-state behavior is
        # unchanged.
        try:
            _rebind_deck()
        except Exception as e:
            decky.logger.debug(f"Startup rebind: {e}")
        self._disabled = False

        self._ready = True

        decky.logger.info(
            f"steamdeck-input-disabler loaded "
            f"(base={len(self._base_controllers)}, user={len(self._user_controllers)}, "
            f"enabled={self._settings.get('enabled', False)}, pyudev={_PYUDEV_AVAILABLE})"
        )

        if self._settings.get("enabled") and _PYUDEV_AVAILABLE:
            await self._start_observer()

    async def _unload(self):
        # Always rebind on the way out. The previous behavior of gating this on
        # self._disabled was a stale-state landmine: if the process gets killed or
        # the in-memory flag desyncs from the kernel, the Deck pad stays unbound
        # until reboot, taking Steam Input down with it.
        decky.logger.info("Unloading steamdeck-input-disabler")
        try:
            await self._stop_observer()
        except Exception as e:
            decky.logger.error(f"Observer stop during unload: {e}")
        try:
            _rebind_deck()
        except Exception as e:
            decky.logger.error(f"Rebind during unload: {e}")
        self._disabled = False
        self._connected.clear()

    async def _uninstall(self):
        # Decky calls this when the plugin is being removed. Same cleanup as _unload
        # so we don't leave the system with the Deck pad permanently unbound.
        decky.logger.info("Uninstalling steamdeck-input-disabler")
        try:
            await self._stop_observer()
        except Exception as e:
            decky.logger.error(f"Observer stop during uninstall: {e}")
        try:
            _rebind_deck()
        except Exception as e:
            decky.logger.error(f"Rebind during uninstall: {e}")

    async def _migration(self):
        # Versions <= 1.0.2 installed a udev rule pointing at a bundled bash script
        # and left a runtime dir under /tmp/scawp. Clean both up so the new pyudev-based
        # path is the only thing watching for controller events.
        legacy_rules = "/etc/udev/rules.d/99-disable-steam-input.rules"
        if os.path.exists(legacy_rules):
            try:
                os.remove(legacy_rules)
                decky.logger.info(f"Removed legacy udev rules at {legacy_rules}")
                subprocess.run(["udevadm", "control", "--reload"], check=False)
            except OSError as e:
                decky.logger.error(f"Could not remove {legacy_rules}: {e}")

        legacy_tmp = "/tmp/scawp"
        if os.path.isdir(legacy_tmp):
            try:
                shutil.rmtree(legacy_tmp, ignore_errors=True)
                decky.logger.info(f"Removed legacy runtime dir {legacy_tmp}")
            except Exception as e:
                decky.logger.error(f"Could not remove {legacy_tmp}: {e}")

        # If the old version had unbound the Deck pad and never got a chance to rebind it,
        # restore it now. If _main re-disables it for a connected controller, that's fine.
        try:
            _rebind_deck()
        except Exception as e:
            decky.logger.debug(f"Migration rebind: {e}")

    def _is_active(self, vid_pid, entry, is_user):
        # User entries store their active flag inline; built-in entries pick
        # it up from the overrides map. Anything missing defaults to active.
        if is_user:
            return bool(entry.get("active", True))
        return bool(self._overrides.get(vid_pid, True))

    def _decorate(self, entry, is_user):
        vp = (entry.get("vid_pid") or "").lower()
        out = dict(entry)
        out["vid_pid"] = vp
        out["active"] = self._is_active(vp, entry, is_user)
        return out

    def _rebuild_allowlist(self):
        ids = set()
        for entry in self._base_controllers:
            vp = (entry.get("vid_pid") or "").lower()
            if VID_PID_RE.match(vp) and self._is_active(vp, entry, False):
                ids.add(vp)
        for entry in self._user_controllers:
            vp = (entry.get("vid_pid") or "").lower()
            if VID_PID_RE.match(vp) and self._is_active(vp, entry, True):
                ids.add(vp)
        self._allowlist = ids

    async def _start_observer(self):
        if not _PYUDEV_AVAILABLE or self._observer is not None:
            return
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="input")
        monitor.filter_by(subsystem="hidraw")
        self._observer = pyudev.MonitorObserver(monitor, callback=self._on_udev_event_threadsafe)
        self._observer.start()
        decky.logger.info("pyudev observer started")
        await self._enumerate_existing(context)

    def _is_external_input(self, device):
        """True only if we can prove this input device is on an external peripheral.

        Walks the parent chain looking for either a non-Valve USB vendor or a
        bluetooth-subsystem ancestor. Defaults to internal when neither is
        found, so unrecognised internal buses (i2c, ACPI, virtual) never
        trigger an unbind by accident. Wrapped in a defensive try/except so
        a weird device can never crash the enumerate sweep.
        """
        try:
            chain = []
            node = device
            external_via_usb = None  # None=undecided, True/False once known
            seen_bluetooth = False
            for _ in range(32):
                if node is None:
                    break
                subsys = None
                try:
                    subsys = node.subsystem
                except Exception:
                    pass
                sys_name = getattr(node, "sys_name", "?")
                vendor = None
                try:
                    attrs = getattr(node, "attributes", None)
                    if attrs is not None:
                        v = attrs.get("idVendor")
                        if v is not None:
                            vendor = v.decode() if isinstance(v, bytes) else str(v)
                            vendor = vendor.strip().lower()
                except Exception:
                    pass
                chain.append(f"{subsys}/{sys_name}{('@'+vendor) if vendor else ''}")
                if subsys == "bluetooth":
                    seen_bluetooth = True
                if vendor and external_via_usb is None:
                    external_via_usb = (vendor != DECK_VID)
                try:
                    node = node.parent
                except Exception:
                    node = None
            decky.logger.debug(
                f"kbm classify {getattr(device, 'sys_name', '?')}: "
                f"chain={' -> '.join(chain)} ext_via_usb={external_via_usb} bt={seen_bluetooth}"
            )
            if external_via_usb is True:
                return True
            if external_via_usb is False:
                return False
            return seen_bluetooth
        except Exception as e:
            decky.logger.error(
                f"_is_external_input crashed on {getattr(device, 'sys_name', '?')}: {e}"
            )
            return False

    async def _stop_observer(self):
        if self._observer is None:
            return
        try:
            self._observer.stop()
        except Exception as e:
            decky.logger.error(f"Failed to stop observer: {e}")
        self._observer = None
        for kernel in list(self._hidraw):
            self._close_hidraw(kernel)
        decky.logger.info("pyudev observer stopped")

    def _on_udev_event_threadsafe(self, device):
        try:
            asyncio.run_coroutine_threadsafe(self._on_udev_event(device), self._loop)
        except Exception as e:
            decky.logger.error(f"Failed to schedule udev event: {e}")

    async def _on_udev_event(self, device):
        async with self._lock:
            self._handle_device_event(device.action, device)

    def _classify_device(self, device):
        """Return ('controller'|'kbm', vid_pid) if this event is relevant, else None.

        Controllers must be in the allowlist; KB/M must be external. Returns
        None if neither condition holds or the device isn't of a tracked kind.

        A single USB mouse creates ~7 input subdevices in udev (event*, mouse*,
        plus one inputN per HID interface). We only act on the inputN nodes
        themselves and skip their event*/mouse* children so a single physical
        device produces a single tracked entry per HID interface.
        """
        # Filter out event* / mouse* / js* subdevices. They share the same
        # ID_INPUT_* flags as their parent inputN, so handling both would
        # double or triple count the same hardware.
        sys_name = getattr(device, "sys_name", "") or ""
        if not sys_name.startswith("input"):
            return None

        is_joystick = device.get("ID_INPUT_JOYSTICK") == "1"
        is_keyboard = device.get("ID_INPUT_KEYBOARD") == "1"
        is_mouse = device.get("ID_INPUT_MOUSE") == "1"
        parsed = _parse_product(device.get("PRODUCT"))
        vid_pid = f"{parsed[0]}:{parsed[1]}" if parsed else None

        # Tracked through hidraw instead. hid-steam's evdev for these comes and
        # goes with Steam opening and closing the device.
        if vid_pid in STEAM_2026_IDS:
            return None

        if is_joystick and vid_pid and vid_pid in self._allowlist:
            return "controller", vid_pid

        if (is_keyboard or is_mouse) and self._settings.get("kbm_enabled"):
            # Joystick-class devices often also flag keyboard/mouse capability;
            # don't double-count those.
            if is_joystick:
                return None
            # Skip the Deck's internal hub and Steam's emulated input devices.
            if vid_pid and vid_pid == f"{DECK_VID}:{DECK_PID}":
                return None
            if self._is_external_input(device):
                return "kbm", vid_pid or "unknown"

        return None

    def _track(self, kernel, vid_pid, name, kind):
        if kernel in self._connected:
            return
        self._connected[kernel] = {
            "kernel": kernel,
            "vid_pid": vid_pid,
            "name": name,
            "kind": kind,
        }
        decky.logger.info(f"{kind} added: {kernel} ({vid_pid})")
        if not self._disabled:
            _unbind_deck()
            self._disabled = True

    def _untrack(self, kernel):
        if kernel not in self._connected:
            return
        entry = self._connected.pop(kernel)
        decky.logger.info(
            f"{entry.get('kind', 'device')} removed: {kernel} ({entry.get('vid_pid')})"
        )
        if not self._connected and self._disabled:
            _rebind_deck()
            self._disabled = False

    def _handle_device_event(self, action, device):
        if not self._settings.get("enabled"):
            return
        if getattr(device, "subsystem", None) == "hidraw":
            self._handle_hidraw_event(action, device)
            return
        classified = self._classify_device(device)
        if classified is None:
            return
        kind, vid_pid = classified
        kernel = device.sys_name
        if action == "add":
            self._track(kernel, vid_pid, (device.get("NAME") or "").strip('"'), kind)
        elif action == "remove":
            self._untrack(kernel)

    def _handle_hidraw_event(self, action, device):
        kernel = device.sys_name
        if action == "remove":
            self._close_hidraw(kernel)
            return
        if action != "add":
            return
        if kernel in self._hidraw:
            # A re-enumeration, e.g. after the entry was switched back on.
            self._sync_hidraw(kernel)
            return
        info = _steam_2026_info(device)
        if info is None:
            return
        entry = dict(info, connected=True, fd=None)
        if info["vid_pid"] == STEAM_2026_PUCK:
            # A puck slot: connected only once the slot's reports say so.
            entry["connected"] = False
            try:
                fd = os.open(info["devnode"], os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                decky.logger.error(f"Couldn't open {info['devnode']}: {e}")
                return
            entry["fd"] = fd
            self._loop.add_reader(fd, self._read_slot, kernel)
        self._hidraw[kernel] = entry
        decky.logger.info(f"Steam Controller (2026) node: {kernel} ({info['vid_pid']})")
        self._sync_hidraw(kernel)

    def _sync_hidraw(self, kernel):
        """Track or untrack a 2026 Steam Controller node to match its state."""
        entry = self._hidraw.get(kernel)
        wanted = (
            entry is not None
            and entry["connected"]
            and entry["vid_pid"] in self._allowlist
        )
        if wanted:
            self._track(kernel, entry["vid_pid"], entry["name"], "controller")
        else:
            self._untrack(kernel)

    def _read_slot(self, kernel):
        # Runs on the event loop thread (add_reader), like the udev handlers.
        entry = self._hidraw.get(kernel)
        if entry is None or entry["fd"] is None:
            return
        state = entry["connected"]
        while True:
            try:
                report = os.read(entry["fd"], 64)
            except BlockingIOError:
                break
            except OSError as e:
                # ENODEV once the puck goes; the udev remove cleans up too.
                decky.logger.info(f"{kernel} read ended: {e}")
                self._close_hidraw(kernel)
                return
            if not report:
                break
            event = _slot_event(report)
            if event is not None:
                state = event
        if state != entry["connected"]:
            entry["connected"] = state
            decky.logger.info(
                f"Steam Controller (2026) {'connected' if state else 'disconnected'} on {kernel}"
            )
            self._sync_hidraw(kernel)

    def _close_hidraw(self, kernel):
        entry = self._hidraw.pop(kernel, None)
        if entry is None:
            return
        fd = entry.get("fd")
        if fd is not None:
            try:
                self._loop.remove_reader(fd)
            except Exception as e:
                decky.logger.debug(f"remove_reader {kernel}: {e}")
            try:
                os.close(fd)
            except OSError:
                pass
        self._untrack(kernel)

    async def _enumerate_existing(self, context):
        # Sweep all input devices, not just joysticks, so KB/M get picked up too.
        # Per-device try/except so one weird device can't abort the rest of the
        # sweep and leave us with an inconsistent _connected set.
        # hidraw is swept for the 2026 Steam Controller.
        for subsystem in ("input", "hidraw"):
            try:
                devices = list(context.list_devices(subsystem=subsystem))
            except Exception as e:
                decky.logger.error(f"Failed to list {subsystem} devices: {e}")
                continue
            for device in devices:
                try:
                    self._handle_device_event("add", device)
                except Exception as e:
                    decky.logger.error(
                        f"Enumeration: device {getattr(device, 'sys_name', '?')} "
                        f"raised {type(e).__name__}: {e}"
                    )

    def _steam_2026_connected(self, context):
        """(kernel, info) for each connected 2026 Steam Controller.

        Uses the tracked nodes while the plugin is on. With it off, nothing
        reads the puck's slots, so only wired and Bluetooth controllers show.
        """
        if self._hidraw:
            return [(k, e) for k, e in self._hidraw.items() if e["connected"]]
        out = []
        for device in context.list_devices(subsystem="hidraw"):
            info = _steam_2026_info(device)
            if info is not None and info["vid_pid"] != STEAM_2026_PUCK:
                out.append((device.sys_name, info))
        return out

    def _enumerate_present_vid_pids(self):
        """Cheap snapshot of currently-plugged joystick-class devices' VID:PIDs.

        Independent of plugin enabled-state and the observer, so the UI can show
        connection tags even before the user turns the plugin on.
        """
        if not _PYUDEV_AVAILABLE:
            return []
        present = set()
        try:
            context = pyudev.Context()
            for device in context.list_devices(subsystem="input", ID_INPUT_JOYSTICK="1"):
                parsed = _parse_product(device.get("PRODUCT"))
                if parsed is None:
                    continue
                present.add(f"{parsed[0]}:{parsed[1]}")
            for _, info in self._steam_2026_connected(context):
                present.add(info["vid_pid"])
        except Exception as e:
            decky.logger.debug(f"enumerate_present failed: {e}")
        return sorted(present)

    async def get_state(self) -> dict:
        return {
            "enabled": self._settings.get("enabled", False),
            "kbm_enabled": self._settings.get("kbm_enabled", False),
            "disabled_deck": self._disabled,
            "connected": list(self._connected.values()),
            "present_vid_pids": self._enumerate_present_vid_pids(),
            "base_controllers": [self._decorate(e, False) for e in self._base_controllers],
            "user_controllers": [self._decorate(e, True) for e in self._user_controllers],
            "pyudev_available": _PYUDEV_AVAILABLE,
            "pyudev_error": self._import_error,
            "ready": self._ready,
        }

    async def set_enabled(self, enabled: bool) -> dict:
        async with self._lock:
            if enabled and not _PYUDEV_AVAILABLE:
                return {"ok": False, "error": f"pyudev unavailable: {self._import_error}"}
            was = self._settings.get("enabled", False)
            self._settings["enabled"] = bool(enabled)
            try:
                _write_json(SETTINGS_PATH, self._settings)
            except OSError as e:
                self._settings["enabled"] = was
                return {"ok": False, "error": f"Failed to write settings: {e}"}
            if enabled and not was:
                await self._start_observer()
            elif not enabled and was:
                await self._stop_observer()
                if self._disabled:
                    _rebind_deck()
                    self._disabled = False
                self._connected.clear()
        return {"ok": True}

    async def set_kbm_enabled(self, enabled: bool) -> dict:
        enabled = bool(enabled)
        async with self._lock:
            was = self._settings.get("kbm_enabled", False)
            if was == enabled:
                return {"ok": True}
            self._settings["kbm_enabled"] = enabled
            try:
                _write_json(SETTINGS_PATH, self._settings)
            except OSError as e:
                self._settings["kbm_enabled"] = was
                return {"ok": False, "error": f"Failed to write settings: {e}"}

            # Only act on currently-connected state if the plugin is active.
            if self._settings.get("enabled") and _PYUDEV_AVAILABLE:
                if not enabled:
                    # Drop tracked KB/M entries and possibly rebind.
                    stale = [k for k, v in self._connected.items() if v.get("kind") == "kbm"]
                    for k in stale:
                        self._connected.pop(k, None)
                    if not self._connected and self._disabled:
                        _rebind_deck()
                        self._disabled = False
                else:
                    # Re-enumerate so a plugged-in KB/M takes effect immediately.
                    try:
                        context = pyudev.Context()
                        await self._enumerate_existing(context)
                    except Exception as e:
                        decky.logger.error(f"Re-enumerate after kbm enable failed: {e}")
        return {"ok": True}

    async def add_controller(self, vid_pid: str, name: str) -> dict:
        vid_pid = (vid_pid or "").strip().lower()
        name = (name or "").strip()
        if not VID_PID_RE.match(vid_pid):
            return {"ok": False, "error": "VID:PID must be in format xxxx:xxxx (hex digits)."}
        if not name:
            return {"ok": False, "error": "Name is required."}
        async with self._lock:
            for entry in self._base_controllers:
                if (entry.get("vid_pid") or "").lower() == vid_pid:
                    return {"ok": False, "error": "Already in the built-in list."}
            for entry in self._user_controllers:
                if (entry.get("vid_pid") or "").lower() == vid_pid:
                    return {"ok": False, "error": "Already in your list."}
            self._user_controllers.append({"vid_pid": vid_pid, "name": name, "active": True})
            try:
                _write_json(USER_CONTROLLERS_PATH, self._user_controllers)
            except OSError as e:
                self._user_controllers.pop()
                return {"ok": False, "error": f"Failed to save: {e}"}
            self._rebuild_allowlist()
        return {"ok": True}

    async def set_controller_active(self, vid_pid: str, active: bool) -> dict:
        vid_pid = (vid_pid or "").strip().lower()
        if not VID_PID_RE.match(vid_pid):
            return {"ok": False, "error": "Invalid VID:PID."}
        active = bool(active)
        async with self._lock:
            # User entry takes precedence if both exist (shouldn't happen, but safe).
            user_idx = next(
                (i for i, e in enumerate(self._user_controllers)
                 if (e.get("vid_pid") or "").lower() == vid_pid),
                None,
            )
            is_base = any(
                (e.get("vid_pid") or "").lower() == vid_pid
                for e in self._base_controllers
            )
            if user_idx is None and not is_base:
                return {"ok": False, "error": "Controller is not in any list."}

            if user_idx is not None:
                prev = self._user_controllers[user_idx].get("active", True)
                self._user_controllers[user_idx]["active"] = active
                try:
                    _write_json(USER_CONTROLLERS_PATH, self._user_controllers)
                except OSError as e:
                    self._user_controllers[user_idx]["active"] = prev
                    return {"ok": False, "error": f"Failed to save: {e}"}
            else:
                prev = self._overrides.get(vid_pid)
                # Only persist an override when it diverges from the default (True).
                if active:
                    self._overrides.pop(vid_pid, None)
                else:
                    self._overrides[vid_pid] = False
                try:
                    _write_json(OVERRIDES_PATH, self._overrides)
                except OSError as e:
                    if prev is None:
                        self._overrides.pop(vid_pid, None)
                    else:
                        self._overrides[vid_pid] = prev
                    return {"ok": False, "error": f"Failed to save: {e}"}

            self._rebuild_allowlist()

            if not active:
                # Drop it from the tracked set and possibly rebind the Deck pad.
                stale = [k for k, v in self._connected.items() if v.get("vid_pid") == vid_pid]
                for k in stale:
                    self._connected.pop(k, None)
                if not self._connected and self._disabled:
                    _rebind_deck()
                    self._disabled = False
            elif self._settings.get("enabled") and _PYUDEV_AVAILABLE:
                # Re-enumerate so a currently-plugged-in controller that was just
                # activated triggers the unbind without needing a replug.
                try:
                    context = pyudev.Context()
                    await self._enumerate_existing(context)
                except Exception as e:
                    decky.logger.error(f"Re-enumerate after activate failed: {e}")
        return {"ok": True}

    async def remove_controller(self, vid_pid: str) -> dict:
        vid_pid = (vid_pid or "").strip().lower()
        async with self._lock:
            for i, entry in enumerate(self._user_controllers):
                if (entry.get("vid_pid") or "").lower() == vid_pid:
                    removed = self._user_controllers.pop(i)
                    try:
                        _write_json(USER_CONTROLLERS_PATH, self._user_controllers)
                    except OSError as e:
                        self._user_controllers.insert(i, removed)
                        return {"ok": False, "error": f"Failed to save: {e}"}
                    self._rebuild_allowlist()
                    return {"ok": True}
        return {"ok": False, "error": "Not in your list. Built-in entries can't be removed."}

    def _known_vid_pids(self):
        known = set()
        for e in self._base_controllers:
            vp = (e.get("vid_pid") or "").lower()
            if VID_PID_RE.match(vp):
                known.add(vp)
        for e in self._user_controllers:
            vp = (e.get("vid_pid") or "").lower()
            if VID_PID_RE.match(vp):
                known.add(vp)
        return known

    async def get_current_devices(self) -> list:
        out = []
        if not _PYUDEV_AVAILABLE:
            return out
        try:
            context = pyudev.Context()
            known = self._known_vid_pids()
            seen = set()
            for device in context.list_devices(subsystem="input", ID_INPUT_JOYSTICK="1"):
                parsed = _parse_product(device.get("PRODUCT"))
                if parsed is None:
                    continue
                vid_pid = f"{parsed[0]}:{parsed[1]}"
                if vid_pid in STEAM_2026_IDS:
                    continue  # listed from hidraw below
                name = (device.get("NAME") or "").strip('"')
                key = (vid_pid, name)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "kernel": device.sys_name,
                    "name": name,
                    "uniq": (device.get("UNIQ") or "").strip('"'),
                    "product": device.get("PRODUCT") or "",
                    "vid_pid": vid_pid,
                    # is_supported = known to the plugin (built-in or user entry).
                    # is_active = will actually disable the Deck pad (in allowlist).
                    "is_supported": vid_pid in known,
                    "is_active": vid_pid in self._allowlist,
                    "is_internal": vid_pid == f"{DECK_VID}:{DECK_PID}",
                })
            for kernel, info in self._steam_2026_connected(context):
                out.append({
                    "kernel": kernel,
                    "name": info["name"],
                    "uniq": info["uniq"],
                    "product": info["product"],
                    "vid_pid": info["vid_pid"],
                    "is_supported": info["vid_pid"] in known,
                    "is_active": info["vid_pid"] in self._allowlist,
                    "is_internal": False,
                })
        except Exception as e:
            decky.logger.error(f"get_current_devices failed: {e}")
        return out
