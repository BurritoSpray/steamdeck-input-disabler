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
        self._settings = _read_json(SETTINGS_PATH, {"enabled": False})
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
        self._observer = pyudev.MonitorObserver(monitor, callback=self._on_udev_event_threadsafe)
        self._observer.start()
        decky.logger.info("pyudev observer started")
        await self._enumerate_existing(context)

    async def _stop_observer(self):
        if self._observer is None:
            return
        try:
            self._observer.stop()
        except Exception as e:
            decky.logger.error(f"Failed to stop observer: {e}")
        self._observer = None
        decky.logger.info("pyudev observer stopped")

    def _on_udev_event_threadsafe(self, device):
        try:
            asyncio.run_coroutine_threadsafe(self._on_udev_event(device), self._loop)
        except Exception as e:
            decky.logger.error(f"Failed to schedule udev event: {e}")

    async def _on_udev_event(self, device):
        async with self._lock:
            self._handle_device_event(device.action, device)

    def _handle_device_event(self, action, device):
        if not self._settings.get("enabled"):
            return
        if device.get("ID_INPUT_JOYSTICK") != "1":
            return
        parsed = _parse_product(device.get("PRODUCT"))
        if parsed is None:
            return
        vid_pid = f"{parsed[0]}:{parsed[1]}"
        if vid_pid not in self._allowlist:
            return
        kernel = device.sys_name
        if action == "add":
            if kernel in self._connected:
                return
            self._connected[kernel] = {
                "kernel": kernel,
                "vid_pid": vid_pid,
                "name": (device.get("NAME") or "").strip('"'),
            }
            decky.logger.info(f"Allowed controller added: {kernel} ({vid_pid})")
            if not self._disabled:
                _unbind_deck()
                self._disabled = True
        elif action == "remove":
            if kernel not in self._connected:
                return
            self._connected.pop(kernel)
            decky.logger.info(f"Allowed controller removed: {kernel} ({vid_pid})")
            if not self._connected and self._disabled:
                _rebind_deck()
                self._disabled = False

    async def _enumerate_existing(self, context):
        try:
            for device in context.list_devices(subsystem="input", ID_INPUT_JOYSTICK="1"):
                self._handle_device_event("add", device)
        except Exception as e:
            decky.logger.error(f"Startup enumeration failed: {e}")

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
        except Exception as e:
            decky.logger.debug(f"enumerate_present failed: {e}")
        return sorted(present)

    async def get_state(self) -> dict:
        return {
            "enabled": self._settings.get("enabled", False),
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
        except Exception as e:
            decky.logger.error(f"get_current_devices failed: {e}")
        return out
