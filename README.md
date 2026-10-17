# Steam Deck Input Disabler

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that disables the Steam Deck's built-in gamepad when an external controller — or optionally an external keyboard or mouse — is connected, and re-enables it once the last one disconnects.

Useful for couch co-op and emulators where the Deck's internal pad otherwise gets registered as player 1 or steals input order, and for desktop-style sessions where you'd rather not have a phantom gamepad on the bus while using a real keyboard and mouse.

## Features

- **Supported controllers list.** Ships with PlayStation 4/5, Xbox 360/One/Series, Nintendo Switch Pro / Wii / Joy-Con, 8BitDo, Stadia, the 2026 Steam Controller (wired, Bluetooth or through its wireless puck), and a few more out of the box.
- **Add your own.** Any controller can be added by VID:PID — no need to wait for an update.
- **Manage controllers sub-view.** A dedicated, searchable list with filters (All / Connected / Mine / Built-in / Active / Inactive), so picking the right Xbox model out of ten near-identical entries is a five-second job.
- **Per-controller active toggle.** Disable any single entry without removing it. Useful if you only want a specific controller to take over from the Deck pad.
- **Live connected tags.** Entries currently plugged in are tagged in green; the list defaults to the *Connected* filter when something known is attached.
- **Optional keyboard and mouse support.** Flip a second toggle and any external keyboard or mouse (USB, Bluetooth, or 2.4 GHz dongle) also disables the Deck pad while connected. Off by default.
- **Unidentified Controllers / Currently Detected panel.** One click to copy a ready-to-paste snippet for a GitHub issue, one click to add an unknown controller to your list.
- **Transition notifications.** A toast appears whenever the Deck pad is disabled or re-enabled so you know the plugin acted.
- **Clean unload.** Updating, disabling or uninstalling the plugin always restores the Deck pad. A fresh process also rebinds defensively on startup in case a previous instance was killed mid-flight.
- **Dynamic HID discovery.** Finds the Deck's internal controller by VID:PID (`28de:1205`), not by hardcoded USB bus path — works regardless of Deck revision.

## Usage

Flip **Enable plugin** on. Connect a controller from the supported list (or one you've added). The Deck's internal pad gets unbound. Disconnect every external controller, and it comes back.

To also have the Deck pad disable when an external keyboard or mouse is connected, flip **Enable keyboard and mouse support**. The Deck's own touchpads, gyro and built-in buttons are unaffected by this option; only devices on USB peripherals other than Valve's hub, or anywhere in the Bluetooth subsystem, trigger it.

If your controller doesn't trigger anything, open **Currently Detected**, find your pad, and click **Add to my list**. If you'd like it added to the built-in list in a future release, copy its info and [open an issue](https://github.com/BurritoSpray/steamdeck-input-disabler/issues).

## How it works

The plugin watches `udev` for `input` events via `pyudev`. When a device whose VID:PID is in your active allowlist connects (or, with the KB/M option, any external keyboard or mouse), the plugin writes the Steam Deck's USB HID interface paths to `/sys/bus/usb/drivers/usbhid/unbind`. On disconnect, once nothing tracked remains, it writes them back to `/sys/bus/usb/drivers/usbhid/bind`. No `/etc/udev/rules.d/` files are installed — everything runs inside the plugin process.

The 2026 Steam Controller is handled differently, because it never appears as an `input` joystick while Steam is running: Steam reads it through `hidraw`, and the kernel's `hid-steam` driver only creates its input device while nothing has the `hidraw` node open. The plugin watches `hidraw` nodes instead. A wired (`28de:1302`) or Bluetooth (`28de:1303`) controller counts as connected while its node exists. The wireless puck (`28de:1304`) is plugged in whether or not a controller is on, so the plugin reads each of its four controller slots alongside Steam (every `hidraw` reader gets every report) and follows the connect and disconnect events and input reports. The puck's pogo-pin interface is skipped.

## Credits

The original approach (unbinding the Deck's USB HID interfaces from `usbhid`) and the early supported-controller research come from [scawp's Steam-Deck.Auto-Disable-Steam-Controller](https://github.com/scawp/Steam-Deck.Auto-Disable-Steam-Controller). Earlier versions of this plugin shipped that script directly; if you find this useful, please star scawp's repo too.
