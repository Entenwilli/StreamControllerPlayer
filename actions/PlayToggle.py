import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from src.backend.PluginManager.ActionBase import ActionBase

from ..common import (
    bootstrap_play_sync,
    find_track,
    get_status,
    list_library_tracks,
    play_request,
    resolve_audio_path,
    send_request,
    spotify_open,
    spotify_status,
    stop_request,
    shorten,
)

TARGET_KEY = "target"
DEFAULT_TARGET = "player-cli"
TARGETS = ["player-cli", "spotify"]

PATH_KEY = "path"
REPLACE_KEY = "replace"
LOOP_KEY = "loop"
FADE_KEY = "fade_ms"
DEFAULT_FADE_MS = 0

PLAYING_BG = [0, 0, 0, 0]  # transparent
PAUSED_BG = [0, 0, 0, 0]  # transparent
IDLE_BG = [0, 0, 0, 0]  # transparent
ERROR_BG = [0, 0, 0, 0]  # transparent
UNCONFIGURED_BG = [0, 0, 0, 0]  # transparent

# Backgrounds are transparent, so the state text itself carries the color
# instead - green while actually playing, amber while paused, gray at rest.
PLAYING_TEXT = [60, 200, 120, 255]
PAUSED_TEXT = [230, 170, 40, 255]
IDLE_TEXT = [150, 150, 150, 255]


class PlayToggle(ActionBase):
    """Plays a configured file on press; pressing it again while that file
    is playing stops just that track. Other tracks are left alone unless
    "replace" is enabled. "fade" ramps the track's volume over that many
    milliseconds instead of changing it instantly - on this key's own
    start/stop toggle, and on any later pause/resume elsewhere (e.g.
    Pause/Resume All). 0 disables fading.

    Switch the Target setting to Spotify to instead open a configured
    Spotify URI/share link on press (via `player-cli spotify open`, a
    `playerctl` wrapper) - "replace"/"loop" don't apply there, and every
    press just (re)opens the same URI rather than toggling.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._busy = False
        self._last_error = None
        self._library_tracks = []

    def on_ready(self):
        self.update_display()

    def on_tick(self):
        self.update_display()

    def on_key_down(self):
        if self._busy:
            return
        settings = self.get_settings() or {}
        path = settings.get(PATH_KEY, "").strip()
        if not path:
            self.show_error(3)
            return

        if self._configured_target() == "spotify":
            self._open_spotify(path)
            return

        replace = bool(settings.get(REPLACE_KEY, False))
        looping = bool(settings.get(LOOP_KEY, False))
        fade_ms = int(settings.get(FADE_KEY, DEFAULT_FADE_MS))
        track = find_track(get_status(), path)
        self._busy = True

        def finish(error):
            self._busy = False
            self._last_error = error
            get_status(force=True)
            self.update_display()

        def task():
            if track is not None:
                kind, payload = send_request(stop_request([str(track["id"])]))
                finish(payload if kind == "Error" else None)
                return

            # The daemon resolves paths against its own working directory,
            # so expand `~` and relative paths here first - otherwise a
            # perfectly validly-typed path can silently fail to play.
            resolved, error = resolve_audio_path(path)
            if error:
                finish(error)
                return

            # Try the daemon's socket first; only shell out to start a new
            # one if nothing answered.
            kind, payload = send_request(play_request([resolved], replace, looping, fade_ms))
            if kind == "Error":
                finish(payload)
                return
            if kind == "Played":
                failed = payload.get("failed") or []
                finish(failed[0][1] if failed else None)
                return

            success, error = bootstrap_play_sync(self.plugin_base, [resolved], replace, looping, fade_ms)
            finish(None if success else error)

        threading.Thread(target=task, daemon=True).start()

    def _open_spotify(self, uri):
        self._busy = True

        def finish(error):
            self._busy = False
            self._last_error = error
            spotify_status(self.plugin_base, force=True)
            self.update_display()

        def task():
            success, error = spotify_open(self.plugin_base, uri)
            finish(None if success else error)

        threading.Thread(target=task, daemon=True).start()

    def _configured_target(self) -> str:
        settings = self.get_settings() or {}
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        return target if target in TARGETS else DEFAULT_TARGET

    def update_display(self):
        settings = self.get_settings()
        if settings is None:
            return
        path = settings.get(PATH_KEY, "").strip()

        if not path:
            self.set_background_color(UNCONFIGURED_BG)
            self.set_center_label(self.plugin_base.lm.get("actions.play-toggle.unconfigured"), font_size=12)
            self.set_bottom_label(None)
            return

        if self._configured_target() == "spotify":
            self._update_spotify_display()
            return

        name, _ext = os.path.splitext(os.path.basename(path))
        self.set_center_label(shorten(name.title()), font_size=14)

        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_bottom_label(shorten(self._last_error, 20), font_size=10)
            return

        # No daemon reachable is just the resting state (nothing playing
        # yet), not an error - player-cli only starts on the first `play`.
        track = find_track(get_status(), path)
        if track is None:
            self.set_background_color(IDLE_BG)
            state = self.plugin_base.lm.get("actions.play-toggle.state-stopped")
            state_color = IDLE_TEXT
        elif track.get("paused", False):
            self.set_background_color(PAUSED_BG)
            state = self.plugin_base.lm.get("actions.play-toggle.state-paused")
            state_color = PAUSED_TEXT
        else:
            self.set_background_color(PLAYING_BG)
            state = self.plugin_base.lm.get("actions.play-toggle.state-playing")
            state_color = PLAYING_TEXT

        self.set_bottom_label(state, color=state_color, font_size=10)

    def _update_spotify_display(self):
        self.set_center_label(None)

        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_bottom_label(shorten(self._last_error, 20), font_size=10)
            return

        # Spotify not running is just the resting state, not an error.
        status = spotify_status(self.plugin_base)
        if status is None:
            self.set_background_color(IDLE_BG)
            self.set_bottom_label(
                self.plugin_base.lm.get("actions.play-toggle.state-stopped"), color=IDLE_TEXT, font_size=10
            )
            return

        self.set_background_color(PLAYING_BG)
        color = PAUSED_TEXT if status.get("status") == "Paused" else PLAYING_TEXT
        self.set_bottom_label(shorten(status.get("title") or "", 18), color=color, font_size=10)

    def get_config_rows(self):
        self.target_model = Gtk.StringList()
        for key in TARGETS:
            self.target_model.append(self.plugin_base.lm.get(f"actions.play-toggle.target.{key}"))
        self.target_row = Adw.ComboRow(
            model=self.target_model,
            title=self.plugin_base.lm.get("actions.play-toggle.target.label"),
        )

        self.library_model = Gtk.StringList()
        self.library_selector = Adw.ComboRow(
            model=self.library_model,
            title=self.plugin_base.lm.get("actions.play-toggle.library.label"),
            subtitle=self.plugin_base.lm.get("actions.play-toggle.library.subtitle"),
        )
        self.library_selector.set_enable_search(True)

        self.path_row = Adw.EntryRow(title=self.plugin_base.lm.get("actions.play-toggle.path.label"))
        self.replace_row = Adw.SwitchRow(
            title=self.plugin_base.lm.get("actions.play-toggle.replace.label"),
            subtitle=self.plugin_base.lm.get("actions.play-toggle.replace.subtitle"),
        )
        self.loop_row = Adw.SwitchRow(
            title=self.plugin_base.lm.get("actions.play-toggle.loop.label"),
            subtitle=self.plugin_base.lm.get("actions.play-toggle.loop.subtitle"),
        )
        self.fade_row = Adw.SpinRow.new_with_range(min=0, max=10000, step=100)
        self.fade_row.set_title(self.plugin_base.lm.get("actions.play-toggle.fade.label"))
        self.fade_row.set_subtitle(self.plugin_base.lm.get("actions.play-toggle.fade.subtitle"))

        self._refresh_library_selector()
        self.load_config_defaults()

        self.target_row.connect("notify::selected", self.on_target_changed)
        self.library_selector.connect("notify::selected", self.on_library_selected)
        self.path_row.connect("notify::text", self.on_path_changed)
        self.replace_row.connect("notify::active", self.on_replace_changed)
        self.loop_row.connect("notify::active", self.on_loop_changed)
        self.fade_row.connect("notify::value", self.on_fade_changed)

        self._apply_target_visibility()

        return [
            self.target_row,
            self.library_selector,
            self.path_row,
            self.replace_row,
            self.loop_row,
            self.fade_row,
        ]

    def _apply_target_visibility(self):
        is_spotify = self._configured_target() == "spotify"
        self.library_selector.set_visible(not is_spotify)
        self.replace_row.set_visible(not is_spotify)
        self.loop_row.set_visible(not is_spotify)
        self.fade_row.set_visible(not is_spotify)
        self.path_row.set_title(
            self.plugin_base.lm.get("actions.play-toggle.uri.label" if is_spotify else "actions.play-toggle.path.label")
        )

    def _refresh_library_selector(self):
        """Repopulates the dropdown from the library directories on disk.
        Called once per settings-panel open - add files to the library and
        reopen the panel to see them."""
        self._library_tracks = list_library_tracks()

        for _ in range(self.library_model.get_n_items()):
            self.library_model.remove(0)

        if self._library_tracks:
            self.library_model.append(self.plugin_base.lm.get("actions.play-toggle.library.placeholder"))
            for label, _path in self._library_tracks:
                self.library_model.append(label)
        else:
            self.library_model.append(self.plugin_base.lm.get("actions.play-toggle.library.empty"))
            self.library_selector.set_sensitive(False)

    def load_config_defaults(self):
        settings = self.get_settings()
        if settings is None:
            return
        target = settings.get(TARGET_KEY, DEFAULT_TARGET)
        try:
            self.target_row.set_selected(TARGETS.index(target))
        except ValueError:
            self.target_row.set_selected(0)
        self.path_row.set_text(settings.get(PATH_KEY, ""))
        self.replace_row.set_active(bool(settings.get(REPLACE_KEY, False)))
        self.loop_row.set_active(bool(settings.get(LOOP_KEY, False)))
        self.fade_row.set_value(int(settings.get(FADE_KEY, DEFAULT_FADE_MS)))
        self._select_matching_library_entry()

    def _select_matching_library_entry(self):
        """Highlights the dropdown entry matching the current path, if any,
        so reopening the settings panel shows what's already configured."""
        if not self._library_tracks:
            return
        current = self.path_row.get_text().strip()
        if current:
            resolved, _ = resolve_audio_path(current)
            if resolved is not None:
                for i, (_, path) in enumerate(self._library_tracks):
                    if path == resolved:
                        self.library_selector.set_selected(i + 1)
                        return
        self.library_selector.set_selected(0)

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

    def on_library_selected(self, combo, *args):
        index = combo.get_selected()
        track_index = index - 1
        if track_index < 0 or track_index >= len(self._library_tracks):
            return
        _, path = self._library_tracks[track_index]
        self.path_row.set_text(path)

    def on_path_changed(self, entry, *args):
        settings = self.get_settings()
        settings[PATH_KEY] = entry.get_text().strip()
        self.set_settings(settings)
        self._last_error = None
        self.update_display()

    def on_replace_changed(self, switch, *args):
        settings = self.get_settings()
        settings[REPLACE_KEY] = switch.get_active()
        self.set_settings(settings)

    def on_loop_changed(self, switch, *args):
        settings = self.get_settings()
        settings[LOOP_KEY] = switch.get_active()
        self.set_settings(settings)

    def on_fade_changed(self, spin, *args):
        settings = self.get_settings()
        settings[FADE_KEY] = int(spin.get_value())
        self.set_settings(settings)
