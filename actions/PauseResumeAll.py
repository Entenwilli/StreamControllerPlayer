import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from src.backend.PluginManager.ActionBase import ActionBase

from ..common import (
    get_status,
    pause_request,
    resume_request,
    send_request_async,
    shorten,
    spotify_next,
    spotify_pause,
    spotify_play,
    spotify_play_pause,
    spotify_previous,
    spotify_status,
    spotify_stop,
)

TARGET_KEY = "target"
DEFAULT_TARGET = "player-cli"
TARGETS = ["player-cli", "spotify"]

ACTION_KEY = "spotify_action"
DEFAULT_ACTION = "play_pause"
ACTIONS = {
    "play_pause": spotify_play_pause,
    "play": spotify_play,
    "pause": spotify_pause,
    "next": spotify_next,
    "previous": spotify_previous,
    "stop": spotify_stop,
}
ACTION_ORDER = ["play_pause", "play", "pause", "next", "previous", "stop"]
GLYPHS = {
    "play_pause": "⏯",
    "play": "▶",
    "pause": "⏸",
    "next": "⏭",
    "previous": "⏮",
    "stop": "⏹",
}

PLAYING_BG = [0, 0, 0, 0]  # transparent
PAUSED_BG = [0, 0, 0, 0]  # transparent
IDLE_BG = [0, 0, 0, 0]  # transparent
ERROR_BG = [0, 0, 0, 0]  # transparent

GLYPH_PAUSE = "⏸"
GLYPH_PLAY = "▶"


class PauseResumeAll(ActionBase):
    """Pauses everything if anything is playing, otherwise resumes
    everything - for player-cli tracks by default.

    Switch the Target setting to Spotify to instead send one playback
    command to Spotify over MPRIS (via `player-cli spotify <command>`, a
    `playerctl` wrapper), picked from the Command dropdown - Play/Pause,
    Play, Pause, Next Track, Previous Track, or Stop.
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

    def on_key_down(self):
        if self._busy:
            return

        if self._configured_target() == "spotify":
            self._run_spotify_action()
            return

        status = get_status()
        tracks = (status or {}).get("tracks", [])
        if not tracks:
            return

        any_playing = any(not t.get("paused", False) for t in tracks)
        request = pause_request() if any_playing else resume_request()

        def done(kind, payload):
            self._busy = False
            self._last_error = payload if kind == "Error" else None
            get_status(force=True)
            self.update_display()

        self._busy = True
        send_request_async(request, on_done=done)

    def _run_spotify_action(self):
        self._busy = True
        fn = ACTIONS.get(self._configured_action(), spotify_play_pause)

        def task():
            success, error = fn(self.plugin_base)
            self._busy = False
            self._last_error = None if success else error
            spotify_status(self.plugin_base, force=True)
            self.update_display()

        threading.Thread(target=task, daemon=True).start()

    def _configured_target(self) -> str:
        settings = self.get_settings() or {}
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        return target if target in TARGETS else DEFAULT_TARGET

    def _configured_action(self) -> str:
        settings = self.get_settings() or {}
        action = settings.get(ACTION_KEY, DEFAULT_ACTION)
        return action if action in ACTIONS else DEFAULT_ACTION

    def update_display(self):
        if self._configured_target() == "spotify":
            self._update_spotify_display()
            return

        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_center_label(shorten(self._last_error, 12), font_size=12)
            return

        # No daemon reachable is just the resting state (nothing playing
        # yet), not an error - player-cli only starts on the first `play`.
        tracks = (get_status() or {}).get("tracks", [])
        if not tracks:
            self.set_background_color(IDLE_BG)
            self.set_center_label(GLYPH_PAUSE, font_size=24)
            self.set_bottom_label(self.plugin_base.lm.get("actions.pause-resume-all.state-idle"), font_size=10)
            return

        any_playing = any(not t.get("paused", False) for t in tracks)
        if any_playing:
            self.set_background_color(PLAYING_BG)
            self.set_center_label(GLYPH_PAUSE, font_size=24)
            self.set_bottom_label(self.plugin_base.lm.get("actions.pause-resume-all.state-playing"), font_size=10)
        else:
            self.set_background_color(PAUSED_BG)
            self.set_center_label(GLYPH_PLAY, font_size=24)
            self.set_bottom_label(self.plugin_base.lm.get("actions.pause-resume-all.state-paused"), font_size=10)

    def _update_spotify_display(self):
        action = self._configured_action()
        self.set_top_label(GLYPHS.get(action, "⏯"), font_size=16)

        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_center_label("!", font_size=22)
            self.set_bottom_label(shorten(self._last_error, 20), font_size=10)
            return

        # Spotify not running is just the resting state, not an error.
        status = spotify_status(self.plugin_base)
        if status is None:
            self.set_background_color(IDLE_BG)
            self.set_center_label(self.plugin_base.lm.get("actions.pause-resume-all.spotify-idle"), font_size=12)
            self.set_bottom_label(None)
            return

        self.set_background_color(PLAYING_BG)
        self.set_center_label(shorten(status.get("title") or "", 14), font_size=12)
        self.set_bottom_label(shorten(status.get("artist") or "", 18), font_size=10)

    def get_config_rows(self):
        self.target_model = Gtk.StringList()
        for key in TARGETS:
            self.target_model.append(self.plugin_base.lm.get(f"actions.pause-resume-all.target.{key}"))
        self.target_row = Adw.ComboRow(
            model=self.target_model,
            title=self.plugin_base.lm.get("actions.pause-resume-all.target.label"),
        )

        self.action_model = Gtk.StringList()
        for key in ACTION_ORDER:
            self.action_model.append(self.plugin_base.lm.get(f"actions.pause-resume-all.action.{key}"))
        self.action_row = Adw.ComboRow(
            model=self.action_model,
            title=self.plugin_base.lm.get("actions.pause-resume-all.action.label"),
            subtitle=self.plugin_base.lm.get("actions.pause-resume-all.action.subtitle"),
        )

        settings = self.get_settings() or {}
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        try:
            self.target_row.set_selected(TARGETS.index(target))
        except ValueError:
            self.target_row.set_selected(0)

        action = settings.get(ACTION_KEY, DEFAULT_ACTION)
        try:
            self.action_row.set_selected(ACTION_ORDER.index(action))
        except ValueError:
            self.action_row.set_selected(0)

        self.target_row.connect("notify::selected", self.on_target_changed)
        self.action_row.connect("notify::selected", self.on_action_changed)

        self._apply_target_visibility()
        return [self.target_row, self.action_row]

    def _apply_target_visibility(self):
        self.action_row.set_visible(self._configured_target() == "spotify")

    def on_target_changed(self, combo_row, *args):
        index = combo_row.get_selected()
        if index < 0 or index >= len(TARGETS):
            return
        settings = self.get_settings()
        settings[TARGET_KEY] = TARGETS[index]
        self.set_settings(settings)
        self._last_error = None
        self._apply_target_visibility()
        self.update_display()

    def on_action_changed(self, combo_row, *args):
        index = combo_row.get_selected()
        if index < 0 or index >= len(ACTION_ORDER):
            return
        settings = self.get_settings()
        settings[ACTION_KEY] = ACTION_ORDER[index]
        self.set_settings(settings)
        self.update_display()
