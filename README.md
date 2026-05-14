# Steam Deck Input Disabler

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that disables the Steam Deck's built-in gamepad when a supported external controller connects, and re-enables it when all of them disconnect.

Useful for couch co-op and emulators, where the Deck's internal pad otherwise gets registered as player 1 (or steals input order) when you're really trying to play with a Bluetooth controller.

## Features

- **Supported controllers list.** Ships with PlayStation 4/5, Xbox 360/One/Series, Nintendo Switch Pro / Wii / Joy-Con, 8BitDo, Stadia, and a few more out of the box.
- **Add your own.** Any controller can be added by VID:PID — no need to wait for an update.
- **Currently Detected panel.** Shows every joystick-class input device currently visible to the Deck, with their `name`, `product`, `uniq`, and `vid:pid`. One click to copy the info as a ready-to-paste snippet for a GitHub issue. One click to add an unknown controller to your list.
- **Dynamic HID discovery.** Finds the Deck's internal controller by VID:PID (`28de:1205`), not by hardcoded USB bus path — works regardless of Deck revision.

## Usage

Flip the toggle on. Connect a controller from the supported list (or one you've added). The Deck's internal pad gets unbound. Disconnect every external controller, and it comes back.

If your controller doesn't trigger anything, open **Currently Detected**, find your pad, and click **Add to my list**. If you'd like it added to the built-in list in a future release, copy its info and [open an issue](https://github.com/BurritoSpray/steamdeck-input-disabler/issues).

## How it works

The plugin watches `udev` for `input` events via `pyudev`. When a device with `ID_INPUT_JOYSTICK=1` connects and its VID:PID is in your allowlist, the plugin writes the Steam Deck's USB HID interface paths to `/sys/bus/usb/drivers/usbhid/unbind`. On disconnect (when no allowed controllers remain), it writes them back to `/sys/bus/usb/drivers/usbhid/bind`. No `/etc/udev/rules.d/` files are installed — everything runs inside the plugin process.

## Credits

The original approach (unbinding the Deck's USB HID interfaces from `usbhid`) and the early supported-controller research come from [scawp's Steam-Deck.Auto-Disable-Steam-Controller](https://github.com/scawp/Steam-Deck.Auto-Disable-Steam-Controller). Earlier versions of this plugin shipped that script directly; if you find this useful, please star scawp's repo too.
