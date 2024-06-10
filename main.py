import os
import subprocess
import pathlib

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code one directory up
# or add the `decky-loader/plugin` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky_plugin

# Path to the script
SCRIPT_PATH = "'" + os.path.join(pathlib.Path(__file__).parent.resolve(),"auto-disable-steam-controller/disable_steam_input.sh'")

# Path to the udev rules
RULES_PATH = "/etc/udev/rules.d/99-disable-steam-input.rules"

# File used to store currently connected bluetooth controllers
CONTROLLER_FILE = "/tmp/scawp/SDADSC/controller_id.txt"

# Udev rules
RULES = [
    f'KERNEL=="input*", SUBSYSTEM=="input", ENV{{ID_INPUT_JOYSTICK}}=="1", ACTION=="add", RUN+="{SCRIPT_PATH} disable %k %E{{NAME}} %E{{UNIQ}} %E{{PRODUCT}}"\n',
    f'KERNEL=="input*", SUBSYSTEM=="input", ENV{{ID_INPUT_JOYSTICK}}=="1", ACTION=="remove", RUN+="{SCRIPT_PATH} enable %k %E{{NAME}} %E{{UNIQ}} %E{{PRODUCT}}"\n'
]

# Steamdeck controller identifier
REENABLE_STEAMDECK_COMMANDS = [
    '3-3:1.0',
    '3-3:1.1',
    '3-3:1.2'
]

class Plugin:
    async def set_script(self, enabled: bool):
        try:
            exists = os.path.exists(RULES_PATH)
            
            if not enabled and exists:
                # Remove the udev rules
                try:
                    os.remove(RULES_PATH)
                except OSError:
                    pass
                
            elif enabled:
                # Add the rules in udev rules folder
                with open(RULES_PATH, 'w') as f:
                    f.writelines(RULES)
                    
            # Reload udev after changing the option
            subprocess.run(['udevadm', 'control', '--reload'])
            
            if not enabled:
                # Remove the old 
                try:
                    os.remove(CONTROLLER_FILE)
                except OSError:
                    pass
                
                # Force reenable the steamdeck when turning off the option
                for device in REENABLE_STEAMDECK_COMMANDS:
                    try:                        
                        with open('/sys/bus/usb/drivers/usbhid/bind', 'w') as f:
                            f.write(device)
                    except OSError:
                        pass
            else:
                # Restart bluetooth to redetect controllers and run udev rules
                subprocess.run(['bluetoothctl', 'power', 'off'])
                subprocess.run(['bluetoothctl', 'power', 'on'])
                
        except Exception as e:
            return str(e)
        return "OK"
            

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        decky_plugin.logger.info("Hello World!")

    # Function called first during the unload process, utilize this to handle your plugin being removed
    async def _unload(self):
        decky_plugin.logger.info("Goodbye World!")
        pass

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        decky_plugin.logger.info("Migrating")
        # Here's a migration example for logs:
        # - `~/.config/decky-template/template.log` will be migrated to `decky_plugin.DECKY_PLUGIN_LOG_DIR/template.log`
        decky_plugin.migrate_logs(os.path.join(decky_plugin.DECKY_USER_HOME,
                                               ".config", "decky-template", "template.log"))
        # Here's a migration example for settings:
        # - `~/homebrew/settings/template.json` is migrated to `decky_plugin.DECKY_PLUGIN_SETTINGS_DIR/template.json`
        # - `~/.config/decky-template/` all files and directories under this root are migrated to `decky_plugin.DECKY_PLUGIN_SETTINGS_DIR/`
        decky_plugin.migrate_settings(
            os.path.join(decky_plugin.DECKY_HOME, "settings", "template.json"),
            os.path.join(decky_plugin.DECKY_USER_HOME, ".config", "decky-template"))
        # Here's a migration example for runtime data:
        # - `~/homebrew/template/` all files and directories under this root are migrated to `decky_plugin.DECKY_PLUGIN_RUNTIME_DIR/`
        # - `~/.local/share/decky-template/` all files and directories under this root are migrated to `decky_plugin.DECKY_PLUGIN_RUNTIME_DIR/`
        decky_plugin.migrate_runtime(
            os.path.join(decky_plugin.DECKY_HOME, "template"),
            os.path.join(decky_plugin.DECKY_USER_HOME, ".local", "share", "decky-template"))
