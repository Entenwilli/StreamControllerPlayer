import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

from .actions.PlayToggle import PlayToggle
from .actions.StopAll import StopAll
from .actions.PauseResumeAll import PauseResumeAll
from .actions.AdjustVolume import AdjustVolume

# Most actions here are discrete triggers (play/stop/pause) - buttons are
# enough, no dial/touchscreen gestures needed.
KEY_ONLY_SUPPORT = {
    Input.Key: ActionInputSupport.SUPPORTED,
    Input.Dial: ActionInputSupport.UNSUPPORTED,
    Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
}

# Adjust Volume additionally supports dials: turning nudges the volume,
# pressing the dial applies the configured step same as a key press.
KEY_AND_DIAL_SUPPORT = {
    Input.Key: ActionInputSupport.SUPPORTED,
    Input.Dial: ActionInputSupport.SUPPORTED,
    Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
}


class PlayerCliPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.lm = self.locale_manager
        self.lm.set_to_os_default()

        self.has_plugin_settings = True

        self.play_toggle_holder = ActionHolder(
            plugin_base=self,
            action_base=PlayToggle,
            action_id_suffix="PlayToggle",
            action_name=self.lm.get("actions.play-toggle.name"),
            action_support=KEY_ONLY_SUPPORT,
        )
        self.add_action_holder(self.play_toggle_holder)

        self.stop_all_holder = ActionHolder(
            plugin_base=self,
            action_base=StopAll,
            action_id_suffix="StopAll",
            action_name=self.lm.get("actions.stop-all.name"),
            action_support=KEY_ONLY_SUPPORT,
        )
        self.add_action_holder(self.stop_all_holder)

        self.pause_resume_all_holder = ActionHolder(
            plugin_base=self,
            action_base=PauseResumeAll,
            action_id_suffix="PauseResumeAll",
            action_name=self.lm.get("actions.pause-resume-all.name"),
            action_support=KEY_ONLY_SUPPORT,
        )
        self.add_action_holder(self.pause_resume_all_holder)

        self.adjust_volume_holder = ActionHolder(
            plugin_base=self,
            action_base=AdjustVolume,
            action_id_suffix="AdjustVolume",
            action_name=self.lm.get("actions.adjust-volume.name"),
            action_support=KEY_AND_DIAL_SUPPORT,
        )
        self.add_action_holder(self.adjust_volume_holder)

        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/fschwickerath/player-cli",
            plugin_version="1.0.0",
            app_version="1.5.0",
        )

    def get_selector_icon(self) -> Gtk.Widget:
        return Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")

    def get_settings_area(self):
        group = Adw.PreferencesGroup(title=self.lm.get("plugin.settings.title"))

        self.binary_path_row = Adw.EntryRow(title=self.lm.get("plugin.settings.binary-path.label"))

        settings = self.get_settings()
        self.binary_path_row.set_text(settings.get("binary_path", ""))
        self.binary_path_row.connect("notify::text", self.on_binary_path_changed)

        group.add(self.binary_path_row)
        return group

    def on_binary_path_changed(self, entry, *args):
        settings = self.get_settings()
        settings["binary_path"] = entry.get_text().strip()
        self.set_settings(settings)
