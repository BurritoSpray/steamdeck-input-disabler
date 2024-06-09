# Docked Steamdeck Input Disabler
A [decky-loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that lets you disable the steamdeck input when a Bluetooth controller is connected. This is useful when playing couch-coop games because sometimes the controller orders get mixed up and the game tries to use the steamdeck instead of the Bluetooth controller. Same with emulators; it's so annoying when you have to reconfigure your controllers when playing docked, because your Bluetooth controller is detected as player 2.

## Usage
It's really simple, you only have one toggle button. When activated, the steamdeck controls will stop being detected as soon as a Bluetooth controller is connected and will be reconnected a few seconds after disconnecting all Bluetooth controllers.

## Credits
Thanks to [scawp](https://github.com/scawp) and his project [Steam-Deck.Auto-Disable-Steam-Controller](https://github.com/scawp/Steam-Deck.Auto-Disable-Steam-Controller). This plugin is using his script under the hood to make the magic happen. If you like the plugin, make sure to star his project on GitHub to support him!
