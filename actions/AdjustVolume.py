import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionBase import ActionBase

from ..common import get_status, send_request, volume_request, spotify_adjust_volume, spotify_status, shorten

STEP_KEY = "step"
DEFAULT_STEP = 10

TARGET_KEY = "target"
DEFAULT_TARGET = "player-cli"
TARGETS = ["player-cli", "spotify"]

NORMAL_BG = [0, 0, 0, 0]  # transparent
IDLE_BG = [0, 0, 0, 0]  # transparent
ERROR_BG = [0, 0, 0, 0]  # transparent


class AdjustVolume(ActionBase):
    """Nudges a player's volume by a configured step. Target selects which
    player: player-cli's own (global) volume by default, or Spotify's own
    volume (via `player-cli spotify volume`, a `playerctl` wrapper) -
    separate volumes, same action.

    As a key: each press applies the configured step as-is (pair a +10 and
    a -10 instance for a volume up/down button pair). As a dial: turning it
    always raises/lowers volume in the intuitive direction (CW = up, CCW =
    down) by the step's magnitude, regardless of the step's configured
    sign; pressing the dial applies the signed step, same as a key.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._busy = False
        self._last_error = None

    def on_ready(self):
        self.update_display()

    def on_tick(self):
        self.update_display()

    def event_callback(self, event, data=None):
        if event == Input.Dial.Events.TURN_CW:
            self._adjust(abs(self._configured_step()))
            return
        if event == Input.Dial.Events.TURN_CCW:
            self._adjust(-abs(self._configured_step()))
            return
        # Key Down/Up and Dial Down/Up (press/release) still go through the
        # base class, which calls on_key_down()/on_key_up() as before.
        super().event_callback(event, data)

    def on_key_down(self):
        self._adjust(self._configured_step())

    def _configured_step(self) -> int:
        settings = self.get_settings() or {}
        return int(settings.get(STEP_KEY, DEFAULT_STEP))

    def _configured_target(self) -> str:
        settings = self.get_settings() or {}
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        return target if target in TARGETS else DEFAULT_TARGET

    def _adjust(self, step):
        if self._busy:
            return
        self._busy = True

        if self._configured_target() == "spotify":
            self._adjust_spotify(step)
        else:
            self._adjust_player_cli(step)

    def _adjust_spotify(self, step):
        def task():
            # Unlike player-cli's own (absolute-only) volume control,
            # playerctl supports relative changes directly, so there's no
            # need to read the current value first.
            success, error = spotify_adjust_volume(self.plugin_base, step)
            self._busy = False
            self._last_error = None if success else error
            spotify_status(self.plugin_base, force=True)
            self.update_display()

        threading.Thread(target=task, daemon=True).start()

    def _adjust_player_cli(self, step):
        def task():
            status = get_status(force=True)
            if status is None:
                # Nothing running - there's no volume to adjust.
                self._busy = False
                self._last_error = None
                self.update_display()
                return

            new_level = max(0, min(100, round(status.get("volume", 100) + step)))
            kind, payload = send_request(volume_request(new_level))
            self._busy = False
            self._last_error = payload if kind == "Error" else None
            get_status(force=True)
            self.update_display()

        threading.Thread(target=task, daemon=True).start()

    def update_display(self):
        settings = self.get_settings() or {}
        step = int(settings.get(STEP_KEY, DEFAULT_STEP))
        self.set_top_label(f"{'+' if step >= 0 else ''}{step}%", font_size=12)

        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_bottom_label(shorten(self._last_error, 20), font_size=10)
            return

        # No player reachable is just the resting state (nothing playing
        # yet), not an error.
        target = self._configured_target()
        status = spotify_status(self.plugin_base) if target == "spotify" else get_status()
        if status is None:
            self.set_background_color(IDLE_BG)
            self.set_center_label("—", font_size=22)
            self.set_bottom_label(None)
            return

        self.set_background_color(NORMAL_BG)
        volume = status.get("volume")
        self.set_center_label(f"{round(volume)}%" if volume is not None else "—", font_size=22)
        self.set_bottom_label(None)

    def get_config_rows(self):
        self.target_model = Gtk.StringList()
        for key in TARGETS:
            self.target_model.append(self.plugin_base.lm.get(f"actions.adjust-volume.target.{key}"))
        self.target_row = Adw.ComboRow(
            model=self.target_model,
            title=self.plugin_base.lm.get("actions.adjust-volume.target.label"),
        )

        self.step_row = Adw.SpinRow.new_with_range(min=-100, max=100, step=5)
        self.step_row.set_title(self.plugin_base.lm.get("actions.adjust-volume.step.label"))
        self.step_row.set_subtitle(self.plugin_base.lm.get("actions.adjust-volume.step.subtitle"))

        settings = self.get_settings() or {}
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        try:
            self.target_row.set_selected(TARGETS.index(target))
        except ValueError:
            self.target_row.set_selected(0)
        self.step_row.set_value(settings.get(STEP_KEY, DEFAULT_STEP))

        self.target_row.connect("notify::selected", self.on_target_changed)
        self.step_row.connect("notify::value", self.on_step_changed)

        return [self.target_row, self.step_row]

    def on_target_changed(self, combo_row, *args):
        index = combo_row.get_selected()
        if index < 0 or index >= len(TARGETS):
            return
        settings = self.get_settings()
        settings[TARGET_KEY] = TARGETS[index]
        self.set_settings(settings)
        self._last_error = None
        self.update_display()

    def on_step_changed(self, spin, *args):
        settings = self.get_settings()
        settings[STEP_KEY] = int(spin.get_value())
        self.set_settings(settings)
        self.update_display()
